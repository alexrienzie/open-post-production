#!/usr/bin/env python3
"""Extract a draft sidecar for one beat from an xmeml export.

Applies the b_06 rules codified in editor/story/sidecars/sidecars_README.md
"Building a sidecar":

  Rule 1: scope by timeline_start_frames in [beat_start, beat_end)
  Rule 2: drop Premiere orphans (start=-1 AND empty <link>)
  Rule 3: resolve -1 sentinels via <link> traversal to a V1 clipitem
  Rule 4: detect audio-spine clipitems (A1 with no V1 coverage in window)
  Rule 5: clip_id taxonomy — c#### V1, o#### V2+, a#### A-tracks
  Rule 6: emit boundary_anchors block

Rules 7-9 (scene boundaries, annotation enrichment, taxonomy values) are
editorial — the extractor leaves null placeholders for the human to fill.

Inputs:
  --xml            xmeml export
  --beat-id        e.g. b_07
  --label          beat label (e.g. "Catalyst")  [optional]
  --timeline-start beat start, in frames
  --timeline-end   beat end, in frames           (exclusive)
  --frame-rate     default 24000/1001 (23.976...)
  --asset-map      _index/asset_map.json for post-Stage-B pathurls
  --prior-sidecar  optional: a prior beat's sidecar to inherit
                   annotations + continue c#### / o#### / a#### numbering from
  --c-start, --o-start, --a-start
                   optional: explicit starting indices (override prior-sidecar
                   inference). Use when the prior sidecar isn't the immediate
                   predecessor.
  --out            output sidecar path

Outputs a sidecar JSON with:
  - timeline_range_frames / _seconds
  - empty scenes[] (human fills in)
  - annotations[] populated with content_key + clip_id + name + null
    enrichment fields + transcript_ref (where asset_id is known)
  - graphics_overlays[] empty
  - boundary_anchors describing what got dropped per rule 2

Usage:
  python make_beat_sidecar.py \
    --xml "../../xml exports/project_act II_premiere export_20260519 7pm_proxy_FULL_REMAP.xml" \
    --beat-id b_08 \
    --label "Catalyst" \
    --timeline-start 15341 \
    --timeline-end   24500 \
    --asset-map "E:/open-post-stack/derivative media/_index/asset_map.json" \
    --prior-sidecar "../sidecars/actII_b_07.sidecar.json" \
    --out "../sidecars/actII_b_08.sidecar.json"
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
import urllib.parse
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Optional

DEFAULT_FRAME_RATE = 24000 / 1001  # 23.976023976...

SHA_RE = re.compile(r"([0-9a-f]{64})\.", re.IGNORECASE)


# ----------------------------- XML helpers -----------------------------


def _as_int(el: Optional[ET.Element]) -> Optional[int]:
    if el is None or not el.text:
        return None
    try:
        return int(el.text)
    except ValueError:
        return None


def _asset_id_from_pathurl(pathurl: str, asset_map_inv: dict) -> Optional[str]:
    """Old layout: SHA in filename. New layout: lookup by relative path under
    derivative media/."""
    decoded = urllib.parse.unquote(pathurl)
    m = SHA_RE.search(decoded)
    if m:
        return m.group(1).lower()
    marker = "/derivative media/"
    idx = decoded.find(marker)
    if idx == -1:
        return None
    rel = decoded[idx + len(marker):].replace("/", "\\")
    return asset_map_inv.get(rel) or asset_map_inv.get(rel.lower())


def _load_asset_map_inv(path: Optional[Path]) -> dict:
    inv: dict = {}
    if not path or not path.exists():
        return inv
    with open(path, "r", encoding="utf-8") as f:
        am = json.load(f)
    for aid, kinds in am.get("entries", {}).items():
        for _, info in kinds.items():
            rel = info.get("relative_path")
            if rel:
                inv[rel] = aid
    return inv


# ----------------------------- Clipitem extraction -----------------------------


def _extract_all_clipitems(xml_path: Path, asset_map_inv: dict) -> list[dict]:
    """Return one dict per clipitem in document order, with all fields needed
    for rules 1-5. Includes orphans + -1 sentinels (filtered downstream)."""
    tree = ET.parse(str(xml_path))
    root = tree.getroot()
    seq = root.find("sequence")
    if seq is None:
        raise SystemExit("no <sequence> in XML root")

    # Pass 1: <file id="..."> → asset_id + pathurl + name
    file_meta: dict[str, dict] = {}
    for f in seq.iter("file"):
        fid = f.attrib.get("id")
        if not fid:
            continue
        pathurl_el = f.find("pathurl")
        if pathurl_el is None or not pathurl_el.text:
            continue
        aid = _asset_id_from_pathurl(pathurl_el.text, asset_map_inv)
        name_el = f.find("name")
        file_meta[fid] = {
            "asset_id": aid,
            "pathurl": pathurl_el.text,
            "name": name_el.text if name_el is not None else None,
        }

    # Pass 1b: <sequence id="..."> definitions with bodies (for multicam refs).
    # Only the first occurrence of a multicam source sequence has the body;
    # subsequent occurrences are stubs (`<sequence id="sequence-28"/>` or
    # `<sequence id="sequence-28"></sequence>`). To resolve a stub-ref we look
    # the id up in this map.
    sequence_defs: dict[str, "etree._Element"] = {}
    for s in seq.iter("sequence"):
        sid = s.attrib.get("id")
        if sid and len(s) > 0 and sid not in sequence_defs:
            sequence_defs[sid] = s

    # Pass 2: walk all clipitems with track context. Two clipitem shapes:
    #   1. Regular file-ref: <clipitem><file id="..."/></clipitem> — file_meta
    #      lookup gives asset_id directly.
    #   2. Multicam ref: <clipitem><sequence id="...">...</sequence>
    #      <sourcetrack><mediatype>audio</mediatype><trackindex>N</trackindex>
    #      </sourcetrack></clipitem> — the clipitem has NO <file>; the nested
    #      <sequence> is the multicam SOURCE sequence definition, with the
    #      camera angles on its internal V/A tracks. We resolve by walking
    #      into that nested sequence, finding the track of the matching
    #      mediatype at trackindex (xmeml trackindex is 0-based), and pulling
    #      the asset_id of the first internal clipitem's <file> ref. The
    #      OUTER clipitem's <in>/<out> are source frames in the multicam
    #      sequence's timeline; we keep them as-is for now (the inner-clip
    #      timing varies per angle and would need a more sophisticated map
    #      to be precise; transcript inlining via segments_overlap will still
    #      find the right region since multicam audio normally starts at
    #      multicam-time-0 == source-file-time-0 for the master audio).
    def _resolve_multicam_v_angles(ci_el) -> list[dict]:
        """Return one dict per V-angle in the multicam source sequence:
        {asset_id, file_id, pathurl, name}. Used to surface the linked video
        angles (visible when the editor double-clicks the audio multicam in
        Premiere) as metadata on the outer audio annotation."""
        nested = ci_el.find("sequence")
        if nested is None:
            return []
        if len(nested) == 0:
            sid = nested.attrib.get("id")
            nested = sequence_defs.get(sid) if sid else None
            if nested is None:
                return []
        v_media = nested.find("media/video") if nested.find("media") is not None else None
        if v_media is None:
            v_media_root = nested.find("media")
            v_media = v_media_root.find("video") if v_media_root is not None else None
        if v_media is None:
            return []
        angles: list[dict] = []
        seen_fids: set[str] = set()
        for v_track in v_media.findall("track"):
            for inner_ci in v_track.findall("clipitem"):
                inner_file = inner_ci.find("file")
                if inner_file is None:
                    continue
                inner_fid = inner_file.attrib.get("id")
                if not inner_fid or inner_fid in seen_fids:
                    continue
                seen_fids.add(inner_fid)
                fm = file_meta.get(inner_fid)
                if fm and fm.get("asset_id"):
                    angles.append({
                        "asset_id": fm.get("asset_id"),
                        "file_id": inner_fid,
                        "pathurl": fm.get("pathurl"),
                        "name": fm.get("name"),
                    })
        return angles


    def _resolve_multicam_file_meta(ci_el) -> tuple[str | None, dict]:
        """For a clipitem that has no <file>, look into nested <sequence>.
        Returns (file_id_or_None, fmeta_dict). file_id is synthetic since the
        outer clipitem doesn't reference a file_id directly — we use the
        underlying file's id from file_meta so downstream lookups work.

        Handles BOTH first-occurrence (sequence body inline) and stub-ref
        (`<sequence id="..."/>` empty) clipitems — for stubs we look up the
        first-occurrence definition by id in `sequence_defs`."""
        nested = ci_el.find("sequence")
        if nested is None:
            return None, {}
        # If this is a stub-ref (no body), look up the canonical definition
        # somewhere else in the document.
        if len(nested) == 0:
            sid = nested.attrib.get("id")
            nested = sequence_defs.get(sid) if sid else None
            if nested is None:
                return None, {}
        st = ci_el.find("sourcetrack")
        st_media = st.findtext("mediatype") if st is not None else None
        st_idx_raw = st.findtext("trackindex") if st is not None else None
        try:
            st_idx = int(st_idx_raw) if st_idx_raw is not None else 0
        except ValueError:
            st_idx = 0
        if st_media not in ("video", "audio"):
            return None, {}
        media_el = nested.find("media")
        if media_el is None:
            return None, {}
        media_subtree = media_el.find(st_media)
        if media_subtree is None:
            return None, {}
        tracks = media_subtree.findall("track")
        if not tracks:
            return None, {}
        # xmeml trackindex is 0-based; clamp to available
        track = tracks[min(st_idx, len(tracks) - 1)]
        for inner_ci in track.findall("clipitem"):
            inner_file = inner_ci.find("file")
            if inner_file is None:
                continue
            inner_fid = inner_file.attrib.get("id")
            if inner_fid and inner_fid in file_meta:
                return inner_fid, file_meta[inner_fid]
        return None, {}

    clipitems: list[dict] = []
    for media in seq.findall("media"):
        for media_type in ("video", "audio"):
            md = media.find(media_type)
            if md is None:
                continue
            track_prefix = "V" if media_type == "video" else "A"
            for track_idx, track_el in enumerate(md.findall("track"), start=1):
                track_label = f"{track_prefix}{track_idx}"
                for ci in track_el.findall("clipitem"):
                    file_el = ci.find("file")
                    file_id = file_el.attrib.get("id") if file_el is not None else None
                    fmeta = file_meta.get(file_id, {})
                    is_multicam = False
                    mc_v_angles: list[dict] = []
                    # Multicam fallback: no direct <file>, has <sequence> child
                    if file_id is None and ci.find("sequence") is not None:
                        file_id, fmeta = _resolve_multicam_file_meta(ci)
                        is_multicam = file_id is not None
                        if is_multicam:
                            mc_v_angles = _resolve_multicam_v_angles(ci)
                    name_el = ci.find("name")

                    # <link> children: a link element has a <linkclipref>
                    link_refs = []
                    for link in ci.findall("link"):
                        lref = link.find("linkclipref")
                        if lref is not None and lref.text:
                            link_refs.append(lref.text)

                    clipitems.append({
                        "clipitem_id": ci.attrib.get("id"),
                        "name": name_el.text if name_el is not None else None,
                        "file_id": file_id,
                        "asset_id": fmeta.get("asset_id"),
                        "pathurl": fmeta.get("pathurl"),
                        "track": track_label,
                        "source_in_frames": _as_int(ci.find("in")),
                        "source_out_frames": _as_int(ci.find("out")),
                        "timeline_start_frames": _as_int(ci.find("start")),
                        "timeline_end_frames": _as_int(ci.find("end")),
                        "link_refs": link_refs,
                        "is_multicam_ref": is_multicam,
                        "multicam_v_angles": mc_v_angles,
                    })
    return clipitems


# ----------------------------- Rules 2 + 3 -----------------------------


def _resolve_sentinels_and_orphans(
    clipitems: list[dict],
) -> tuple[list[dict], list[dict]]:
    """Apply rule 2 (orphan filter) and rule 3 (-1 sentinel via <link>).

    Returns (kept, orphans). For kept clipitems, timeline_start_frames and
    timeline_end_frames are populated with the resolved values (sentinels
    replaced by linked V1's start/end + this clip's duration)."""
    by_id = {ci["clipitem_id"]: ci for ci in clipitems if ci["clipitem_id"]}
    kept: list[dict] = []
    orphans: list[dict] = []

    for ci in clipitems:
        if ci["timeline_start_frames"] != -1:
            kept.append(ci)
            continue
        # start == -1: either orphan (rule 2) or linked-to-V1 (rule 3)
        v1_anchor = None
        for lref in ci["link_refs"]:
            target = by_id.get(lref)
            if target is None:
                continue
            if target.get("track") == "V1" and (target.get("timeline_start_frames") or -1) >= 0:
                v1_anchor = target
                break
        if v1_anchor is None:
            orphans.append(ci)
            continue
        # Inherit the V1's start; preserve duration from this clip's own
        # source range so end is computable.
        duration = None
        if (ci["source_in_frames"] is not None and
                ci["source_out_frames"] is not None):
            duration = ci["source_out_frames"] - ci["source_in_frames"]
        ci = dict(ci)  # don't mutate the input list
        ci["timeline_start_frames"] = v1_anchor["timeline_start_frames"]
        ci["timeline_end_frames"] = (
            v1_anchor["timeline_start_frames"] + duration
            if duration is not None
            else v1_anchor["timeline_end_frames"]
        )
        ci["resolved_from_sentinel"] = v1_anchor["clipitem_id"]
        kept.append(ci)

    return kept, orphans


# ----------------------------- Rule 1 (scope) -----------------------------


def _scope_to_beat(clipitems: list[dict], start: int, end: int) -> list[dict]:
    return [
        ci for ci in clipitems
        if ci["timeline_start_frames"] is not None
        and start <= ci["timeline_start_frames"] < end
    ]


# ----------------------------- Rule 4 (audio-spine detection) -----------------------------


def _detect_audio_spines(in_beat: list[dict]) -> set[str]:
    """Return the set of clipitem_ids that are audio-spine clips: an A-track
    clip whose [start, end) has *no* V1 clipitem coverage anywhere within
    the beat's clipitems."""
    v1_windows = [
        (ci["timeline_start_frames"], ci["timeline_end_frames"])
        for ci in in_beat
        if ci.get("track") == "V1"
        and ci["timeline_start_frames"] is not None
        and ci["timeline_end_frames"] is not None
    ]
    spine_ids: set[str] = set()
    for ci in in_beat:
        if not ci.get("track", "").startswith("A"):
            continue
        s, e = ci["timeline_start_frames"], ci["timeline_end_frames"]
        if s is None or e is None:
            continue
        # Spine iff zero V1 overlap. (Overlap = max(starts) < min(ends).)
        overlaps_any = any(max(s, vs) < min(e, ve) for vs, ve in v1_windows)
        if not overlaps_any:
            spine_ids.add(ci["clipitem_id"])
    return spine_ids


# ----------------------------- Rule 5 (clip_id assignment) -----------------------------


def _next_index_from_prior(prior: Optional[dict], prefix: str) -> int:
    """Read prior sidecar's annotations[] and return the highest <prefix>####
    index + 1. Returns 0 if prior is None or has no clips of that prefix."""
    if not prior:
        return 0
    pat = re.compile(rf"^{prefix}(\d+)$")
    highest = -1
    for ann in prior.get("annotations", []):
        cid = ann.get("clip_id", "") or ""
        m = pat.match(cid)
        if m:
            highest = max(highest, int(m.group(1)))
    return highest + 1


def _assign_clip_ids(
    in_beat: list[dict],
    spine_ids: set[str],
    c_start: int,
    o_start: int,
    a_start: int,
    prior_ann_by_key: dict | None = None,
) -> dict[str, str]:
    """Return clipitem_id → clip_id. If a clipitem's content key matches a
    prior annotation that has a clip_id, REUSE that clip_id (stable IDs
    across re-extractions). Otherwise assign the next free index in the
    appropriate prefix range.

    Sort order: V1 then V2+ then A, each by (timeline_start, in_frames,
    track, clipitem_id)."""
    # By design: graphics (V2+ video overlays) are skipped from
    # the sidecar/HTML views entirely. Only V1 spine + audio tracks are emitted.
    by_track: dict[str, list[dict]] = {"V1": [], "V_OVERLAY": [], "A": []}
    for ci in in_beat:
        if ci.get("track") == "V1":
            by_track["V1"].append(ci)
        elif (ci.get("track") or "").startswith("V"):
            continue  # skip V2/V3+ graphics
        elif (ci.get("track") or "").startswith("A"):
            by_track["A"].append(ci)

    def sort_key(ci):
        return (
            ci["timeline_start_frames"] or 0,
            ci["source_in_frames"] or 0,
            ci["track"] or "",
            ci["clipitem_id"] or "",
        )

    prior_ann_by_key = prior_ann_by_key or {}

    def reuse_or_assign(ci, prefix: str, idx: int) -> tuple[str, int]:
        ktuple = (
            ci.get("asset_id"),
            ci["source_in_frames"], ci["source_out_frames"],
            ci["timeline_start_frames"], ci["track"],
        )
        prior = prior_ann_by_key.get(ktuple)
        prior_cid = (prior or {}).get("clip_id") or ""
        if prior_cid.startswith(prefix) and prior_cid[1:].isdigit():
            return prior_cid, idx  # reuse, don't advance fresh counter
        return f"{prefix}{idx:04d}", idx + 1

    # First pass: assign reused clip_ids and note which fresh indices are
    # already taken (so fresh assignment skips them).
    out: dict[str, str] = {}

    def collect_taken(prefix: str) -> set[int]:
        taken = set()
        for ann in prior_ann_by_key.values():
            cid = (ann or {}).get("clip_id") or ""
            if cid.startswith(prefix) and cid[len(prefix):].isdigit():
                taken.add(int(cid[len(prefix):]))
        return taken

    def assign_track(items, prefix, idx, taken):
        for ci in sorted(items, key=sort_key):
            cid, idx = reuse_or_assign(ci, prefix, idx)
            # If reused, idx didn't advance; if fresh-assigned, skip over taken
            while idx in taken:
                idx += 1
            out[ci["clipitem_id"]] = cid
        return idx

    assign_track(by_track["V1"], "c", c_start, collect_taken("c"))
    assign_track(by_track["V_OVERLAY"], "o", o_start, collect_taken("o"))
    assign_track(by_track["A"], "a", a_start, collect_taken("a"))
    return out


# ----------------------------- Annotation build + prior-sidecar inheritance -----------------------------


def _make_annotation(ci: dict, clip_id: str, is_audio_spine: bool) -> dict:
    aid = ci.get("asset_id")
    out = {
        "key": {
            "asset_id": aid,
            "source_in_frames": ci["source_in_frames"],
            "source_out_frames": ci["source_out_frames"],
            "timeline_start_frames": ci["timeline_start_frames"],
            "track": ci["track"],
        },
        "clip_id": clip_id,
        "name": ci.get("name"),
        "scene": None,
        "rationale": None,
        "lower_third": None,
        "location_title": None,
        "date_tracker": None,
        "speakers": [],
        "chunk_subject": None,
        "chunk_action": None,
        "audio_spine": True if is_audio_spine else False,
        "transcript_ref": (
            f"dataset/assets/transcripts/{aid}.transcript.json"
            if aid else None
        ),
        "_resolved_from_sentinel": ci.get("resolved_from_sentinel"),
    }
    # Multicam metadata: surface the source-sequence linkage so downstream
    # renderers can show what video angles are available (visible when the
    # editor double-clicks the audio multicam clip in Premiere).
    if ci.get("is_multicam_ref"):
        out["is_multicam_ref"] = True
        v_angles = ci.get("multicam_v_angles") or []
        if v_angles:
            out["multicam_v_angles"] = v_angles
    return out


def _inherit_from_prior(annotation: dict, prior_ann_by_key: dict) -> dict:
    """If the prior sidecar has an annotation with the same content key,
    carry forward the editorial fields (rationale, lower_third, location_title,
    date_tracker, scene if applicable to this beat — usually not, so kept null).
    Speakers always re-derived; gemini fields refreshed elsewhere."""
    k = annotation["key"]
    ktuple = (
        k["asset_id"], k["source_in_frames"], k["source_out_frames"],
        k["timeline_start_frames"], k["track"],
    )
    prior = prior_ann_by_key.get(ktuple)
    if not prior:
        return annotation
    for field in ("rationale", "lower_third", "location_title", "date_tracker",
                  "audio_spine", "is_audio_spine", "_force_ride"):
        if prior.get(field) is not None:
            annotation[field] = prior[field]
    return annotation


def _index_prior_annotations(prior: Optional[dict]) -> dict:
    if not prior:
        return {}
    out = {}
    for ann in prior.get("annotations", []):
        k = ann.get("key") or {}
        ktuple = (
            k.get("asset_id"), k.get("source_in_frames"), k.get("source_out_frames"),
            k.get("timeline_start_frames"), k.get("track"),
        )
        out[ktuple] = ann
    return out


# ----------------------------- Scene assignment by timeline range -----------------------------


def _scene_for_timeline(tls_frames: Optional[int], scenes: list[dict]) -> Optional[str]:
    """Return the scene id whose timeline_range_frames contains tls_frames.

    Falls back to None on ambiguity (multiple scenes match — should not
    happen if scene ranges are disjoint) or no match. Transcript-text
    disambiguation for null cases is an LLM-judgment editorial step, not
    encoded here."""
    if tls_frames is None:
        return None
    matches = []
    for sc in scenes:
        rng = sc.get("timeline_range_frames") or [None, None]
        s, e = rng[0], rng[1]
        if s is None or e is None:
            continue
        if s <= tls_frames < e:
            matches.append(sc.get("id"))
    if len(matches) == 1:
        return matches[0]
    return None  # 0 or ambiguous — leave for human/LLM review


# ----------------------------- Boundary anchors -----------------------------


def _build_boundary_anchors(
    beat_id: str, in_beat: list[dict], clip_ids: dict[str, str],
    orphans_dropped_in_window: list[dict],
) -> dict:
    v1_clips = sorted(
        (ci for ci in in_beat if ci.get("track") == "V1"
         and ci["timeline_end_frames"] is not None),
        key=lambda ci: ci["timeline_end_frames"],
    )
    last_v1 = v1_clips[-1] if v1_clips else None

    orphan_summary = (
        f"{len(orphans_dropped_in_window)} clipitems with start=-1 AND empty <link> "
        "in the XML were dropped — Premiere export vestiges. Identity: "
        + ", ".join(
            f"{(o.get('name') or '?')} ({o.get('clipitem_id', '?')})"
            for o in orphans_dropped_in_window[:10]
        )
        + ("..." if len(orphans_dropped_in_window) > 10 else "")
    ) if orphans_dropped_in_window else "0 orphans dropped in this beat's window."

    anchors = {
        "description": (
            f"{beat_id} boundary anchors. Generated by make_beat_sidecar.py "
            "from xmeml. Adjust last_clip / next_beat_starts_at by hand if you "
            "drop more clips during editorial review."
        ),
        "orphan_clipitems_dropped": orphan_summary,
    }
    if last_v1:
        anchors[f"last_clip_in_{beat_id}"] = {
            "asset": f"{last_v1.get('name') or '?'} ({clip_ids.get(last_v1['clipitem_id'], '?')})",
            "timeline_end_frames": last_v1["timeline_end_frames"],
        }
    return anchors


# ----------------------------- Main -----------------------------


def build_sidecar(
    xml_path: Path,
    beat_id: str,
    label: Optional[str],
    timeline_start: int,
    timeline_end: int,
    frame_rate: float,
    asset_map: Optional[Path],
    prior_sidecar: Optional[Path],
    c_start_override: Optional[int],
    o_start_override: Optional[int],
    a_start_override: Optional[int],
) -> dict:
    asset_map_inv = _load_asset_map_inv(asset_map)
    all_cis = _extract_all_clipitems(xml_path, asset_map_inv)
    resolved, all_orphans = _resolve_sentinels_and_orphans(all_cis)
    in_beat = _scope_to_beat(resolved, timeline_start, timeline_end)
    # Orphans that *would* have fallen in this beat's window if they had a
    # valid timeline_start. We can't truly know — the convention is to attribute
    # an orphan to the beat whose c#### range it's closest to. We just record
    # all orphans here; the human can re-attribute if needed.
    spine_ids = _detect_audio_spines(in_beat)

    prior = None
    if prior_sidecar and prior_sidecar.exists():
        with open(prior_sidecar, "r", encoding="utf-8") as f:
            prior = json.load(f)

    c_start = c_start_override if c_start_override is not None else _next_index_from_prior(prior, "c")
    o_start = o_start_override if o_start_override is not None else _next_index_from_prior(prior, "o")
    a_start = a_start_override if a_start_override is not None else _next_index_from_prior(prior, "a")

    prior_ann_idx = _index_prior_annotations(prior)
    clip_id_map = _assign_clip_ids(
        in_beat, spine_ids, c_start, o_start, a_start,
        prior_ann_by_key=prior_ann_idx,
    )

    # Inherit scenes[] from prior IFF the prior was a sidecar for THIS beat
    # (re-extraction case). For previous-beat priors, scenes don't apply.
    inherited_scenes: list[dict] = []
    if prior and prior.get("beat_id") == beat_id:
        inherited_scenes = prior.get("scenes", []) or []

    # Build annotations in clip_id order (c#### then o#### then a####)
    annotations = []
    for ci in sorted(
        in_beat,
        key=lambda c: (
            clip_id_map.get(c["clipitem_id"], "zzzz"),
        ),
    ):
        cid = clip_id_map.get(ci["clipitem_id"])
        if not cid:
            continue
        ann = _make_annotation(
            ci, cid, is_audio_spine=(ci["clipitem_id"] in spine_ids),
        )
        ann = _inherit_from_prior(ann, prior_ann_idx)
        # Auto-assign scene by timeline_start_frames falling in a scene's
        # timeline_range_frames. If the prior annotation already named a scene,
        # _inherit_from_prior doesn't carry it (intentional — scene field is
        # only inherited if the same beat re-extraction has scenes defined).
        if inherited_scenes:
            ann["scene"] = _scene_for_timeline(
                ann["key"]["timeline_start_frames"], inherited_scenes,
            )
        annotations.append(ann)

    boundary_anchors = _build_boundary_anchors(
        beat_id, in_beat, clip_id_map, all_orphans,
    )

    xml_sha = hashlib.sha256(open(xml_path, "rb").read()).hexdigest()

    return {
        "schema_version": 1,
        "beat_id": beat_id,
        "label": label or "(beat name TBD)",
        "xml_source": str(xml_path),
        "xml_sha256": xml_sha,
        "frame_rate": frame_rate,
        "timeline_range_frames": [timeline_start, timeline_end],
        "timeline_range_seconds": [
            round(timeline_start / frame_rate, 3),
            round(timeline_end / frame_rate, 3),
        ],
        "generated_by": "make_beat_sidecar.py",
        "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "prior_sidecar": str(prior_sidecar) if prior_sidecar else None,
        "scenes": inherited_scenes,
        "annotations": annotations,
        "graphics_overlays": [],
        "boundary_anchors": boundary_anchors,
        "_counts": {
            "n_annotations": len(annotations),
            "n_audio_spine": sum(1 for a in annotations if a.get("audio_spine")),
            "n_unresolved_asset_id": sum(1 for a in annotations if a["key"]["asset_id"] is None),
            "n_inherited_from_prior": sum(
                1 for a in annotations
                if (a.get("rationale") is not None
                    or a.get("lower_third") is not None
                    or a.get("location_title") is not None
                    or a.get("date_tracker") is not None)
            ),
            "n_orphans_in_xml": len(all_orphans),
        },
    }


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--xml", required=True)
    ap.add_argument("--beat-id", required=True)
    ap.add_argument("--label", default=None)
    ap.add_argument("--timeline-start", type=int, required=True)
    ap.add_argument("--timeline-end", type=int, required=True)
    ap.add_argument("--frame-rate", type=float, default=DEFAULT_FRAME_RATE)
    ap.add_argument("--asset-map", default=None)
    ap.add_argument("--prior-sidecar", default=None)
    ap.add_argument("--c-start", type=int, default=None)
    ap.add_argument("--o-start", type=int, default=None)
    ap.add_argument("--a-start", type=int, default=None)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    sc = build_sidecar(
        xml_path=Path(args.xml),
        beat_id=args.beat_id,
        label=args.label,
        timeline_start=args.timeline_start,
        timeline_end=args.timeline_end,
        frame_rate=args.frame_rate,
        asset_map=Path(args.asset_map) if args.asset_map else None,
        prior_sidecar=Path(args.prior_sidecar) if args.prior_sidecar else None,
        c_start_override=args.c_start,
        o_start_override=args.o_start,
        a_start_override=args.a_start,
    )

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(sc, f, indent=2, ensure_ascii=False)

    c = sc["_counts"]
    print(f"beat:           {args.beat_id} [{args.timeline_start}, {args.timeline_end}) frames")
    print(f"annotations:    {c['n_annotations']} "
          f"(c=v1 spine, o=v2+ overlays, a=audio)")
    print(f"audio_spine:    {c['n_audio_spine']}")
    print(f"unresolved aid: {c['n_unresolved_asset_id']}")
    print(f"inherited:      {c['n_inherited_from_prior']} fields carried from prior sidecar")
    print(f"xml orphans:    {c['n_orphans_in_xml']} (start=-1 AND empty <link>)")
    print(f"output:         {out_path}")
    return 0 if c["n_unresolved_asset_id"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
