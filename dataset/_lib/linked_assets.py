"""
Unified catalog linkage: `linked_assets` on video/audio/still JSON records.

Shape (every bucket is an array of edge objects):

  "linked_assets": {
    "video": [ { "target_asset_id", "link_kind", ... }, ... ],
    "audio": [ ... ],
    "stills": [ ... ]
  }

`link_kind` values:
  - audio_video_transcript: audio → primary video (transcript / propose_audio match)
  - audio_video_reverse: video → audio (mirror: that audio lists this video as primary)
  - same_kind_video: video ↔ video co-recording
  - same_kind_audio: audio ↔ audio co-recording
  - still_to_video: still → source / parent video
"""
from __future__ import annotations

from typing import Any

LK_AUDIO_VIDEO_TRANSCRIPT = "audio_video_transcript"
LK_AUDIO_VIDEO_REVERSE = "audio_video_reverse"
LK_SAME_KIND_VIDEO = "same_kind_video"
LK_SAME_KIND_AUDIO = "same_kind_audio"
LK_STILL_TO_VIDEO = "still_to_video"

BUCKETS = ("video", "audio", "stills")

ESTABLISHED_PROPOSE_AV = "propose_audio_video_links_by_transcript"
ESTABLISHED_SAME_KIND = "propose_same_kind_links_by_transcript"
ESTABLISHED_SYNC_REVERSE = "sync_reverse_links_from_audio_catalog"
ESTABLISHED_MIGRATE = "migrate_catalog_linked_assets"

# Expected `schema_version` after `migrate_catalog_linked_assets.py`.
SCHEMA_WITH_LINKED_ASSETS = {"video": 7, "audio": 5, "still": 4}


def empty_linked_assets() -> dict[str, list]:
    return {"video": [], "audio": [], "stills": []}


def ensure_linked_assets(raw: dict) -> dict[str, list]:
    la = raw.get("linked_assets")
    if not isinstance(la, dict):
        la = empty_linked_assets()
        raw["linked_assets"] = la
    for k in BUCKETS:
        if k not in la or not isinstance(la[k], list):
            la[k] = []
    return la


def _is_sha64(s: Any) -> bool:
    return isinstance(s, str) and len(s) == 64


def parse_edge(obj: Any) -> tuple[str | None, str | None]:
    if not isinstance(obj, dict):
        return None, None
    tid = obj.get("target_asset_id")
    if not _is_sha64(tid):
        return None, None
    lk = obj.get("link_kind")
    if not isinstance(lk, str) or not lk:
        return None, None
    return tid, lk


def _sort_edges(edges: list[dict]) -> None:
    edges.sort(key=lambda e: (e.get("target_asset_id") or "", e.get("link_kind") or ""))


def neighbor_target_ids(raw: dict) -> set[str]:
    """Neighbor asset ids from `linked_assets` plus legacy flat fields (if still present)."""
    out: set[str] = set()
    la = raw.get("linked_assets")
    if isinstance(la, dict):
        for b in BUCKETS:
            for item in la.get(b) or []:
                tid, _ = parse_edge(item)
                if tid:
                    out.add(tid)
    lv = raw.get("linked_video_asset_id")
    if _is_sha64(lv):
        out.add(lv)
    for k in ("linked_audio_asset_ids", "linked_video_asset_ids"):
        for x in raw.get(k) or []:
            if _is_sha64(x):
                out.add(x)
    return out


def add_edge(
    raw: dict,
    bucket: str,
    target_id: str,
    link_kind: str,
    *,
    established_by: str | None = None,
    confidence: float | None = None,
    symmetric: bool | None = None,
) -> bool:
    """Add one edge if (bucket, target, link_kind) is new. Returns True if `raw` changed."""
    if bucket not in BUCKETS or not _is_sha64(target_id) or not link_kind:
        return False
    la = ensure_linked_assets(raw)
    arr = la[bucket]
    for ex in arr:
        if not isinstance(ex, dict):
            continue
        if ex.get("target_asset_id") == target_id and ex.get("link_kind") == link_kind:
            return False
    edge: dict[str, Any] = {"target_asset_id": target_id, "link_kind": link_kind}
    if established_by:
        edge["established_by"] = established_by
    if confidence is not None:
        edge["confidence"] = confidence
    if symmetric is not None:
        edge["symmetric"] = symmetric
    arr.append(edge)
    _sort_edges(arr)
    return True


def strip_legacy_link_keys(raw: dict) -> None:
    raw.pop("linked_video_asset_id", None)
    raw.pop("linked_audio_asset_ids", None)
    raw.pop("linked_video_asset_ids", None)


def set_audio_primary_video(
    raw: dict,
    video_id: str,
    *,
    established_by: str,
    confidence: float | None = None,
) -> bool:
    """Set exactly one `audio_video_transcript` edge on an audio record. Clears legacy key."""
    if not _is_sha64(video_id):
        return False
    la = ensure_linked_assets(raw)
    vedges = la["video"]
    new_edges: list[dict] = []
    for ex in vedges:
        if isinstance(ex, dict) and ex.get("link_kind") == LK_AUDIO_VIDEO_TRANSCRIPT:
            continue
        new_edges.append(ex)
    edge: dict[str, Any] = {
        "target_asset_id": video_id,
        "link_kind": LK_AUDIO_VIDEO_TRANSCRIPT,
        "established_by": established_by,
    }
    if confidence is not None:
        edge["confidence"] = confidence
    new_edges.append(edge)
    _sort_edges(new_edges)
    la["video"] = new_edges
    strip_legacy_link_keys(raw)
    return True


def audio_primary_video_id(raw: dict) -> str | None:
    la = raw.get("linked_assets")
    if isinstance(la, dict):
        for item in la.get("video") or []:
            tid, lk = parse_edge(item)
            if tid and lk == LK_AUDIO_VIDEO_TRANSCRIPT:
                return tid
    lv = raw.get("linked_video_asset_id")
    if _is_sha64(lv):
        return lv
    return None


def merge_video_reverse_audio(
    raw: dict,
    audio_asset_id: str,
    *,
    established_by: str | None = None,
) -> bool:
    """On a video record, ensure an `audio_video_reverse` edge to `audio_asset_id`."""
    return add_edge(
        raw,
        "audio",
        audio_asset_id,
        LK_AUDIO_VIDEO_REVERSE,
        established_by=established_by or ESTABLISHED_PROPOSE_AV,
    )


def reverse_audio_asset_ids(video_raw: dict) -> list[str]:
    """Audio ids declared as reverse links on a video record (new + legacy)."""
    out: list[str] = []
    la = video_raw.get("linked_assets")
    if isinstance(la, dict):
        for item in la.get("audio") or []:
            tid, lk = parse_edge(item)
            if tid and lk == LK_AUDIO_VIDEO_REVERSE:
                out.append(tid)
    for x in video_raw.get("linked_audio_asset_ids") or []:
        if _is_sha64(x) and x not in out:
            out.append(x)
    out.sort()
    return out


def video_has_reverse_audio_edge(video_raw: dict, audio_asset_id: str) -> bool:
    la = video_raw.get("linked_assets")
    if isinstance(la, dict):
        for item in la.get("audio") or []:
            tid, lk = parse_edge(item)
            if tid == audio_asset_id and lk == LK_AUDIO_VIDEO_REVERSE:
                return True
    for x in video_raw.get("linked_audio_asset_ids") or []:
        if x == audio_asset_id:
            return True
    return False
