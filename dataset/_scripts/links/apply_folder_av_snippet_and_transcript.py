"""
1) Apply `linked_assets` for **every** row in `_audit/audio_video_folder_snippet_matches.csv`
   where `already_linked_on_video` is `no`: add `audio_video_transcript` on the audio and
   `audio_video_reverse` on the video for that pair (same pair is idempotent). Multiple
   videos may link to the same audio when the CSV lists several clearing pairs.

2) Scan the **same folder scope** as `scan_audio_video_folder_snippet_matches.py`
   (audio under each video's shoot parent tree) and score every video↔audio pair
   with **machine transcript** similarity (`propose_audio_video_links_by_transcript.py`).

3) Optionally `--apply-transcript` to set `audio_video_transcript` on the audio and
   `audio_video_reverse` on the winning video(s) when the best in-folder video clears
   thresholds (skips audio that already has a different primary).

Usage:
  python _scripts/links/apply_folder_av_snippet_and_transcript.py
  python _scripts/links/apply_folder_av_snippet_and_transcript.py --skip-snippet-csv
  python _scripts/links/apply_folder_av_snippet_and_transcript.py --apply-transcript --tx-min-score 0.48 --tx-margin 0.06
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "_scripts"))
sys.path.insert(0, str(ROOT))

from propose_audio_video_links_by_transcript import (  # noqa: E402
    append_jsonl,
    atomic_write_json,
    load_asset_row,
    merge_video_linked_audio,
    transcript_score_row,
)
from _lib.linked_assets import (  # noqa: E402
    LK_AUDIO_VIDEO_TRANSCRIPT,
    add_edge,
    audio_primary_video_id,
    set_audio_primary_video,
    strip_legacy_link_keys,
)

VIDEO_DIR = ROOT / "assets/video"
AUDIO_DIR = ROOT / "assets/audio"
SNIPPET_CSV = ROOT / "_audit/audio_video_folder_snippet_matches.csv"
TX_OUT = ROOT / "_audit/audio_video_folder_transcript_pairs.csv"
APPLY_LOG = ROOT / "_review_drafts/folder_transcript_av_apply_log.jsonl"

EST_SNIPPET = "folder_filename_snippet_match"
EST_FOLDER_TX = "same_folder_transcript_match"


def path_cf(p: str) -> str:
    return str(Path(p)).replace("/", "\\").casefold()


def audio_under_video_dir(audio_cf: str, video_dir_cf: str) -> bool:
    if not video_dir_cf:
        return False
    if audio_cf == video_dir_cf:
        return True
    return audio_cf.startswith(video_dir_cf + "\\")


def apply_snippet_csv(csv_path: Path, *, dry_run: bool) -> dict[str, int | str]:
    if not csv_path.is_file():
        return {"error": f"missing {csv_path}"}
    rows: list[dict[str, str]] = []
    with csv_path.open(encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            if (r.get("already_linked_on_video") or "").strip().lower() != "no":
                continue
            rows.append(r)

    applied = 0
    skipped = 0
    audio_json_writes = 0
    video_json_writes = 0

    for r in rows:
        vid = (r.get("video_asset_id") or "").strip()
        aid = (r.get("audio_asset_id") or "").strip()
        if len(aid) != 64 or len(vid) != 64:
            skipped += 1
            continue
        ap = AUDIO_DIR / f"{aid}.audio.json"
        vp = VIDEO_DIR / f"{vid}.video.json"
        if not ap.is_file() or not vp.is_file():
            skipped += 1
            continue
        if dry_run:
            applied += 1
            continue

        ar = json.loads(ap.read_text(encoding="utf-8"))
        audio_changed = add_edge(
            ar,
            "video",
            vid,
            LK_AUDIO_VIDEO_TRANSCRIPT,
            established_by=EST_SNIPPET,
        )
        if audio_changed:
            strip_legacy_link_keys(ar)
            atomic_write_json(ap, ar)
            audio_json_writes += 1

        if merge_video_linked_audio(vid, aid, established_by=EST_SNIPPET):
            video_json_writes += 1

        applied += 1

    by_audio: dict[str, int] = defaultdict(int)
    for r in rows:
        a = (r.get("audio_asset_id") or "").strip()
        if len(a) == 64:
            by_audio[a] += 1

    return {
        "snippet_csv_rows_already_not_linked": len(rows),
        "pairs_applied_or_dry": applied,
        "skipped_invalid_or_missing_files": skipped,
        "distinct_audios_touched_in_csv": len(by_audio),
        "audio_catalog_writes": audio_json_writes,
        "video_catalog_writes": video_json_writes,
    }


def iter_folder_av_pairs() -> list[tuple[str, str, str, str]]:
    """(video_id, audio_id, video_dir_cf, reason) for each audio under video's parent dir tree."""
    videos: list[tuple[str, str, str]] = []
    for p in VIDEO_DIR.glob("*.video.json"):
        try:
            r = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        aid = r.get("asset_id")
        sp = r.get("source_path")
        if not isinstance(aid, str) or len(aid) != 64 or not isinstance(sp, str) or not sp:
            continue
        vdir = path_cf(str(Path(sp).parent))
        videos.append((aid, sp, vdir))

    audios: list[tuple[str, str]] = []
    for p in AUDIO_DIR.glob("*.audio.json"):
        try:
            r = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        aid = r.get("asset_id")
        sp = r.get("source_path")
        if not isinstance(aid, str) or len(aid) != 64 or not isinstance(sp, str):
            continue
        audios.append((aid, path_cf(sp)))

    pairs: list[tuple[str, str, str, str]] = []
    for vid, _vsp, vdir in videos:
        for aaid, ap_cf in audios:
            if audio_under_video_dir(ap_cf, vdir):
                pairs.append((vid, aaid, vdir, "same_video_parent_tree"))
    return pairs


def main() -> int:
    ap = argparse.ArgumentParser(description="Apply folder snippet CSV + transcript scan/apply for co-located A/V.")
    ap.add_argument("--snippet-csv", type=Path, default=SNIPPET_CSV)
    ap.add_argument("--skip-snippet-csv", action="store_true")
    ap.add_argument("--dry-run-snippet", action="store_true")
    ap.add_argument("--transcript-out", type=Path, default=TX_OUT)
    ap.add_argument("--min-tokens", type=int, default=8)
    ap.add_argument("--use-duration", action=argparse.BooleanOptionalAction, default=True)
    ap.add_argument("--apply-transcript", action="store_true")
    ap.add_argument("--tx-min-score", type=float, default=0.48)
    ap.add_argument("--tx-margin", type=float, default=0.06)
    ap.add_argument("--apply-log", type=Path, default=APPLY_LOG)
    ap.add_argument("--no-apply-log", action="store_true")
    args = ap.parse_args()

    if not args.skip_snippet_csv:
        res = apply_snippet_csv(args.snippet_csv, dry_run=args.dry_run_snippet)
        print(json.dumps(res, indent=2))

    vrec: dict[str, dict] = {}
    for p in VIDEO_DIR.glob("*.video.json"):
        try:
            r = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        xid = r.get("asset_id")
        if isinstance(xid, str) and len(xid) == 64:
            vrec[xid] = r
    arec: dict[str, dict] = {}
    for p in AUDIO_DIR.glob("*.audio.json"):
        try:
            r = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        xid = r.get("asset_id")
        if isinstance(xid, str) and len(xid) == 64:
            arec[xid] = r

    vrow_cache: dict[str, object | None] = {}
    arow_cache: dict[str, object | None] = {}

    def vrow(vid: str):
        if vid not in vrow_cache:
            r = vrec.get(vid)
            vrow_cache[vid] = load_asset_row("video", r) if r else None
        return vrow_cache[vid]

    def arow(aid: str):
        if aid not in arow_cache:
            r = arec.get(aid)
            arow_cache[aid] = load_asset_row("audio", r) if r else None
        return arow_cache[aid]

    pairs = iter_folder_av_pairs()
    csv_rows: list[dict[str, object]] = []
    by_audio: dict[str, list[tuple[float, str, str]]] = defaultdict(list)

    for vid, aaid, vdir, why in pairs:
        va = vrow(vid)
        aa = arow(aaid)
        if va is None or aa is None:
            continue
        if va.tok_count < args.min_tokens or aa.tok_count < args.min_tokens:
            continue
        if not va.norm_text or not aa.norm_text:
            continue
        if not (va.tok_set & aa.tok_set):
            continue
        sc = transcript_score_row(va, aa, use_duration=args.use_duration)
        cur = audio_primary_video_id(arec[aaid])
        csv_rows.append(
            {
                "video_asset_id": vid,
                "audio_asset_id": aaid,
                "scope": why,
                "video_shoot_parent_dir": vdir,
                "transcript_score": round(sc, 4),
                "video_token_count": va.tok_count,
                "audio_token_count": aa.tok_count,
                "audio_primary_video_asset_id": cur or "",
            }
        )
        by_audio[aaid].append((sc, vid, vdir))

    args.transcript_out.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "video_asset_id",
        "audio_asset_id",
        "scope",
        "video_shoot_parent_dir",
        "transcript_score",
        "video_token_count",
        "audio_token_count",
        "audio_primary_video_asset_id",
    ]
    with args.transcript_out.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in sorted(csv_rows, key=lambda x: (-float(x["transcript_score"]), x["video_asset_id"], x["audio_asset_id"])):
            w.writerow(r)

    try:
        tdisp = str(args.transcript_out.relative_to(ROOT))
    except Exception:
        tdisp = str(args.transcript_out)
    print(f"Wrote {tdisp} ({len(csv_rows)} scored in-folder pairs)", file=sys.stderr)

    tx_applied = 0
    if args.apply_transcript:
        now = datetime.now(timezone.utc).isoformat()
        for aaid, scored in by_audio.items():
            scored.sort(key=lambda x: -x[0])
            if not scored:
                continue
            best_s, best_vid, _vdir = scored[0]
            second_s = scored[1][0] if len(scored) > 1 else 0.0
            margin_ok = (best_s - second_s) >= args.tx_margin if len(scored) > 1 else True
            if best_s < args.tx_min_score or not margin_ok:
                continue

            ap = AUDIO_DIR / f"{aaid}.audio.json"
            if not ap.is_file():
                continue
            araw = json.loads(ap.read_text(encoding="utf-8"))
            cur = audio_primary_video_id(araw)
            if cur and cur != best_vid:
                continue

            wrote = False
            if cur != best_vid:
                set_audio_primary_video(
                    araw,
                    best_vid,
                    established_by=EST_FOLDER_TX,
                    confidence=round(best_s, 4),
                )
                atomic_write_json(ap, araw)
                wrote = True
            if merge_video_linked_audio(best_vid, aaid, established_by=EST_FOLDER_TX):
                wrote = True

            if wrote and not args.no_apply_log:
                append_jsonl(
                    args.apply_log,
                    {
                        "logged_at": now,
                        "event": "apply_folder_transcript",
                        "audio_asset_id": aaid,
                        "video_asset_id": best_vid,
                        "score": round(best_s, 4),
                        "second_best_score": round(second_s, 4),
                        "tx_min_score": args.tx_min_score,
                        "tx_margin": args.tx_margin,
                        "in_folder_candidate_count": len(scored),
                        "link_source": EST_FOLDER_TX,
                    },
                )
            if wrote:
                tx_applied += 1

        print(json.dumps({"transcript_apply": tx_applied}, indent=2))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
