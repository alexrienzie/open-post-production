"""
Propose (and optionally apply) audio → video links using **machine** transcripts only.

Scores each audio against same-day video candidates (optionally same `shoot_label`)
using token Jaccard + `SequenceMatcher` + longest-substring overlap + a small
duration term — same weights used historically in this repo (calibrated against
published candidate JSONL for a known pair).

Writes JSONL for review and can optionally set `linked_assets` on audio + reverse
edges on video. Supports `--cluster-eps` to add reverse links on additional
same-score-cluster videos while keeping a single primary on the audio.

Usage:
  python _scripts/links/propose_audio_video_links_by_transcript.py
  python _scripts/links/propose_audio_video_links_by_transcript.py --min-score 0.4 --top-k 8
  python _scripts/links/propose_audio_video_links_by_transcript.py --apply --apply-min-score 0.72 --apply-margin 0.12
  python _scripts/links/propose_audio_video_links_by_transcript.py --apply --apply-min-score 0.72 --apply-margin 0.12 --cluster-eps 0.02 --top-k 15
  python _scripts/links/propose_audio_video_links_by_transcript.py --sync-reverse-audio-links
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[2]
VIDEO_DIR = ROOT / "assets/video"
AUDIO_DIR = ROOT / "assets/audio"
TRANSCRIPT_DIR = ROOT / "assets/transcripts"
DEFAULT_OUT_DIR = ROOT / "_review_drafts"
DEFAULT_OUT = DEFAULT_OUT_DIR / "audio_video_transcript_link_candidates.jsonl"
DEFAULT_APPLY_LOG = DEFAULT_OUT_DIR / "audio_video_link_apply_log.jsonl"

sys.path.insert(0, str(ROOT))
from _lib.linked_assets import (  # noqa: E402
    ESTABLISHED_PROPOSE_AV,
    ESTABLISHED_SYNC_REVERSE,
    audio_primary_video_id,
    merge_video_reverse_audio,
    set_audio_primary_video,
)

# --- scoring weights (sum = 1.0 with duration term) ---

_JACC_W = 0.15
_SEQ_W = 0.35
_SUB_W = 0.45
_DUR_W = 0.05

_STOP = frozenset(
    {
        "the",
        "a",
        "an",
        "and",
        "or",
        "but",
        "in",
        "on",
        "at",
        "to",
        "for",
        "of",
        "as",
        "is",
        "was",
        "are",
        "were",
        "be",
        "been",
        "being",
        "it",
        "this",
        "that",
        "these",
        "those",
        "i",
        "you",
        "he",
        "she",
        "we",
        "they",
        "me",
        "him",
        "her",
        "them",
        "my",
        "your",
        "our",
        "their",
        "do",
        "did",
        "does",
        "doing",
        "have",
        "has",
        "had",
        "having",
        "get",
        "got",
        "go",
        "going",
        "went",
        "come",
        "came",
        "can",
        "could",
        "would",
        "should",
        "just",
        "like",
        "so",
        "very",
        "really",
        "then",
        "there",
        "here",
        "oh",
        "yeah",
        "um",
        "uh",
        "okay",
        "ok",
    }
)


def atomic_write_json(path: Path, data: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def append_jsonl(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def load_transcript_text(asset_id: str) -> str:
    p = TRANSCRIPT_DIR / f"{asset_id}.transcript.json"
    if not p.is_file():
        return ""
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return ""
    return str(d.get("full_text") or "").strip()


def norm_token_list(text: str) -> list[str]:
    text = re.sub(r"[^a-z0-9\s]+", " ", (text or "").lower())
    return [t for t in text.split() if len(t) >= 2 and t not in _STOP]


def normalize_text(text: str) -> str:
    return " ".join(norm_token_list(text))


def token_set(norm_text: str) -> frozenset[str]:
    if not norm_text:
        return frozenset()
    return frozenset(norm_text.split())


def duration_similarity(da: float | None, db: float | None) -> float:
    if da is None or db is None:
        return 0.0
    if da <= 0 or db <= 0:
        return 0.0
    return max(0.0, 1.0 - abs(float(da) - float(db)) / max(float(da), float(db)))


def _substring_boost(na: str, nb: str) -> float:
    if not na or not nb:
        return 0.0
    if na in nb or nb in na:
        return 1.0
    sm = SequenceMatcher(None, na, nb)
    m = sm.find_longest_match(0, len(na), 0, len(nb))
    return m.size / max(8, min(len(na), len(nb)))


@dataclass(frozen=True)
class AssetRow:
    asset_id: str
    media: str
    primary_date: str | None
    shoot_label: str | None
    duration_sec: float | None
    linked_video_asset_id: str | None
    transcript_text: str
    norm_text: str
    tok_set: frozenset[str]
    tok_count: int


def transcript_score_row(a: AssetRow, b: AssetRow, *, use_duration: bool = True) -> float:
    if a.tok_count < 1 or b.tok_count < 1:
        return 0.0
    sa, sb = a.tok_set, b.tok_set
    if not sa or not sb:
        return 0.0
    jac = len(sa & sb) / len(sa | sb)
    seq = SequenceMatcher(None, a.norm_text, b.norm_text).ratio()
    sub = _substring_boost(a.norm_text, b.norm_text)
    base = _JACC_W * jac + _SEQ_W * seq + _SUB_W * sub
    if use_duration:
        return base + _DUR_W * duration_similarity(a.duration_sec, b.duration_sec)
    return base


def _ffprobe_duration_sec(rec: dict) -> float | None:
    ff = rec.get("ffprobe")
    if not isinstance(ff, dict):
        return None
    d = ff.get("duration_sec")
    if isinstance(d, (int, float)) and d > 0:
        return float(d)
    return None


def load_asset_row(media: str, rec: dict, *, transcript_text: str | None = None) -> AssetRow | None:
    aid = rec.get("asset_id")
    if not isinstance(aid, str) or len(aid) != 64:
        return None
    if transcript_text is None:
        txt = load_transcript_text(aid)
    else:
        txt = transcript_text
    pm = rec.get("path_metadata") if isinstance(rec.get("path_metadata"), dict) else {}
    primary_date = rec.get("primary_timeline_date")
    if not isinstance(primary_date, str) or not primary_date.strip():
        sd = pm.get("shoot_date")
        primary_date = sd if isinstance(sd, str) else None
    sl = pm.get("shoot_label")
    shoot_label = sl if isinstance(sl, str) else None
    lv: str | None = None
    if media == "audio":
        lv = audio_primary_video_id(rec)
    nt = normalize_text(txt)
    ts = token_set(nt)
    toks = nt.split()
    return AssetRow(
        asset_id=aid,
        media=media,
        primary_date=primary_date,
        shoot_label=shoot_label,
        duration_sec=_ffprobe_duration_sec(rec),
        linked_video_asset_id=lv,
        transcript_text=txt,
        norm_text=nt,
        tok_set=ts,
        tok_count=len(toks),
    )


def load_catalog_rows() -> tuple[list[AssetRow], list[AssetRow]]:
    audio_rows: list[AssetRow] = []
    video_rows: list[AssetRow] = []
    for p in sorted(AUDIO_DIR.glob("*.audio.json")):
        try:
            rec = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        aid = rec.get("asset_id")
        if not isinstance(aid, str):
            continue
        row = load_asset_row("audio", rec)
        if row:
            audio_rows.append(row)
    for p in sorted(VIDEO_DIR.glob("*.video.json")):
        try:
            rec = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        vid = rec.get("asset_id")
        if not isinstance(vid, str):
            continue
        row = load_asset_row("video", rec)
        if row:
            video_rows.append(row)
    return audio_rows, video_rows


def index_videos_by_date(video_rows: Iterable[AssetRow]) -> dict[str, list[AssetRow]]:
    by_date: dict[str, list[AssetRow]] = defaultdict(list)
    for v in video_rows:
        d = v.primary_date or ""
        if d:
            by_date[d].append(v)
    return dict(by_date)


def scored_videos_for_audio(
    audio: AssetRow,
    by_date: dict[str, list[AssetRow]],
    *,
    require_shoot_label: bool,
    min_score: float,
    use_duration: bool,
) -> list[tuple[float, AssetRow]]:
    date = audio.primary_date or ""
    if not date:
        return []
    cands = by_date.get(date, [])
    out: list[tuple[float, AssetRow]] = []
    for v in cands:
        if require_shoot_label and (audio.shoot_label or "") != (v.shoot_label or ""):
            continue
        s = transcript_score_row(audio, v, use_duration=use_duration)
        if s >= min_score:
            out.append((s, v))
    out.sort(key=lambda x: -x[0])
    return out


def merge_video_linked_audio(
    video_asset_id: str,
    audio_asset_id: str,
    *,
    established_by: str = ESTABLISHED_PROPOSE_AV,
) -> bool:
    """Ensure `audio_video_reverse` on the video JSON. Returns True if file changed."""
    vpath = VIDEO_DIR / f"{video_asset_id}.video.json"
    if not vpath.is_file():
        return False
    try:
        raw = json.loads(vpath.read_text(encoding="utf-8"))
    except Exception:
        return False
    if not merge_video_reverse_audio(raw, audio_asset_id, established_by=established_by):
        return False
    atomic_write_json(vpath, raw)
    return True


def sync_reverse_links_from_audio_catalog() -> tuple[int, int]:
    """For every audio with a primary video, ensure the video lists reverse audio."""
    videos_updated = 0
    pair_additions = 0
    for ap in sorted(AUDIO_DIR.glob("*.audio.json")):
        try:
            a = json.loads(ap.read_text(encoding="utf-8"))
        except Exception:
            continue
        aid = a.get("asset_id")
        if not isinstance(aid, str) or len(aid) != 64:
            continue
        vid = audio_primary_video_id(a)
        if not vid:
            continue
        if merge_video_linked_audio(vid, aid, established_by=ESTABLISHED_SYNC_REVERSE):
            videos_updated += 1
            pair_additions += 1
    return videos_updated, pair_additions


def _apply_one_audio(
    audio_row: AssetRow,
    scored: list[tuple[float, AssetRow]],
    *,
    apply_min_score: float,
    apply_margin: float,
    cluster_eps: float,
    apply_log: Path,
    no_apply_log: bool,
) -> bool:
    if not scored:
        return False
    best_s, best_v = scored[0]
    second_s = scored[1][0] if len(scored) > 1 else 0.0
    margin_ok = (best_s - second_s) >= apply_margin if len(scored) > 1 else True
    if best_s < apply_min_score or not margin_ok:
        return False

    apath = AUDIO_DIR / f"{audio_row.asset_id}.audio.json"
    if not apath.is_file():
        return False
    try:
        araw = json.loads(apath.read_text(encoding="utf-8"))
    except Exception:
        return False
    cur = audio_primary_video_id(araw)
    if cur and cur != best_v.asset_id:
        return False

    now = datetime.now(timezone.utc).isoformat()
    cluster_videos = [best_v]
    if cluster_eps > 0:
        floor = best_s - cluster_eps
        for s, v in scored:
            if v.asset_id == best_v.asset_id:
                continue
            if s < apply_min_score:
                continue
            if s < floor - 1e-9:
                break
            cluster_videos.append(v)

    wrote_audio = False
    if cur != best_v.asset_id:
        set_audio_primary_video(
            araw,
            best_v.asset_id,
            established_by=ESTABLISHED_PROPOSE_AV,
            confidence=round(best_s, 4),
        )
        atomic_write_json(apath, araw)
        wrote_audio = True

    rev_writes = 0
    for v in cluster_videos:
        if merge_video_linked_audio(v.asset_id, audio_row.asset_id):
            rev_writes += 1

    if not no_apply_log and (wrote_audio or rev_writes):
        append_jsonl(
            apply_log,
            {
                "logged_at": now,
                "event": "apply",
                "audio_asset_id": audio_row.asset_id,
                "video_asset_id": best_v.asset_id,
                "score": round(best_s, 4),
                "second_best_score": round(second_s, 4),
                "apply_min_score": apply_min_score,
                "apply_margin": apply_margin,
                "candidate_count": len(scored),
                "cluster_videos": [v.asset_id for v in cluster_videos],
                "cluster_eps": cluster_eps,
                "link_source": "propose_audio_video_links_by_transcript",
            },
        )
    return wrote_audio or rev_writes > 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Propose / apply audio→video links from machine transcripts.")
    ap.add_argument("--min-score", type=float, default=0.0, help="Minimum score to include in candidate lists.")
    ap.add_argument("--min-tokens", type=int, default=8, help="Skip assets with fewer transcript tokens.")
    ap.add_argument("--top-k", type=int, default=12, help="Max video candidates per audio in output.")
    ap.add_argument(
        "--require-shoot-label",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Fence candidates to the same path_metadata.shoot_label (default: on).",
    )
    ap.add_argument("--use-duration", action=argparse.BooleanOptionalAction, default=True, help="Blend duration into score (default: on).")
    ap.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUT,
        help="JSONL output path (one row per audio).",
    )
    ap.add_argument("--apply", action="store_true", help="Write catalog links for confident matches.")
    ap.add_argument("--apply-min-score", type=float, default=0.72)
    ap.add_argument("--apply-margin", type=float, default=0.12)
    ap.add_argument(
        "--cluster-eps",
        type=float,
        default=0.0,
        help="Also write reverse video edges for candidates within this score of the best (default: 0 = off).",
    )
    ap.add_argument("--apply-log", type=Path, default=DEFAULT_APPLY_LOG)
    ap.add_argument("--no-apply-log", action="store_true")
    ap.add_argument(
        "--only-unlinked-audio",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Only emit / apply for audio without a primary video (default: on).",
    )
    ap.add_argument(
        "--sync-reverse-audio-links",
        action="store_true",
        help="Scan audio primaries and ensure videos have matching reverse edges; then exit.",
    )
    args = ap.parse_args()

    if args.sync_reverse_audio_links:
        vu, pa = sync_reverse_links_from_audio_catalog()
        print(json.dumps({"videos_updated": vu, "pair_additions": pa}, indent=2))
        return 0

    audio_rows, video_rows = load_catalog_rows()
    by_date = index_videos_by_date(video_rows)

    lines: list[str] = []
    applied = 0

    for a in audio_rows:
        if args.only_unlinked_audio and a.linked_video_asset_id:
            continue
        if a.tok_count < args.min_tokens or not a.norm_text:
            continue

        scored = scored_videos_for_audio(
            a,
            by_date,
            require_shoot_label=args.require_shoot_label,
            min_score=args.min_score,
            use_duration=args.use_duration,
        )[: max(1, args.top_k)]

        cands_out: list[dict[str, Any]] = []
        for s, v in scored:
            cands_out.append(
                {
                    "video_asset_id": v.asset_id,
                    "score": round(s, 4),
                    "video_duration_sec": v.duration_sec,
                    "video_transcript_preview": (load_transcript_text(v.asset_id) or "")[:220],
                }
            )

        rec = {
            "audio_asset_id": a.asset_id,
            "primary_timeline_date": a.primary_date,
            "shoot_label": a.shoot_label,
            "audio_duration_sec": a.duration_sec,
            "audio_transcript_preview": (a.transcript_text or "")[:220],
            "candidates": cands_out,
        }
        lines.append(json.dumps(rec, ensure_ascii=False))

        if args.apply and scored:
            if _apply_one_audio(
                a,
                scored,
                apply_min_score=args.apply_min_score,
                apply_margin=args.apply_margin,
                cluster_eps=args.cluster_eps,
                apply_log=args.apply_log,
                no_apply_log=args.no_apply_log,
            ):
                applied += 1

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")

    try:
        odisp = str(args.output.relative_to(ROOT))
    except Exception:
        odisp = str(args.output)
    print(f"Wrote {odisp} ({len(lines)} audio rows)")
    if args.apply:
        print(f"apply: updated {applied} audio (plus cluster reverse edges as applicable)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
