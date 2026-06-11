#!/usr/bin/env python3
"""
Audit (and optionally fix) audio↔video catalog link symmetry.

Checks:
- Audio with a primary video link (`linked_assets` / legacy) ⇒ that video has an
  `audio_video_reverse` edge (or legacy list) for that audio.
- Video listing an audio as reverse ⇒ that audio's primary video points back (warning if mismatch).

Fix mode (`--fix-reverse-audio`) runs `sync_reverse_links_from_audio_catalog` (idempotent).

Usage:
  python _scripts/links/audit_catalog_link_symmetry.py
  python _scripts/links/audit_catalog_link_symmetry.py --fix-reverse-audio
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
VIDEO_DIR = ROOT / "assets" / "catalog" / "video"
AUDIO_DIR = ROOT / "assets" / "catalog" / "audio"
AUDIT_DIR = ROOT / "_audit"

sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "_scripts"))
from _lib.linked_assets import audio_primary_video_id, reverse_audio_asset_ids  # noqa: E402
from propose_audio_video_links_by_transcript import sync_reverse_links_from_audio_catalog  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fix-reverse-audio", action="store_true", help="Merge missing reverse links on video JSON.")
    args = ap.parse_args()

    run_id = datetime.now(timezone.utc).strftime("link_symmetry_audit_%Y%m%dT%H%M%SZ")
    log_path = AUDIT_DIR / f"{run_id}.jsonl"

    issues: list[dict] = []
    for apath in sorted(AUDIO_DIR.glob("*.audio.json")):
        try:
            a = json.loads(apath.read_text(encoding="utf-8"))
        except Exception:
            continue
        aid = a.get("asset_id")
        vid = audio_primary_video_id(a)
        if not aid or not isinstance(vid, str) or not vid.strip():
            continue
        vp = VIDEO_DIR / f"{vid}.video.json"
        if not vp.exists():
            issues.append({"kind": "audio_points_missing_video", "audio_asset_id": aid, "linked_video_asset_id": vid})
            continue
        try:
            v = json.loads(vp.read_text(encoding="utf-8"))
        except Exception:
            issues.append({"kind": "video_read_error", "audio_asset_id": aid, "video_asset_id": vid})
            continue
        la = set(reverse_audio_asset_ids(v))
        if aid not in la:
            issues.append({"kind": "video_missing_reverse_audio", "audio_asset_id": aid, "video_asset_id": vid})

    for vpath in sorted(VIDEO_DIR.glob("*.video.json")):
        try:
            v = json.loads(vpath.read_text(encoding="utf-8"))
        except Exception:
            continue
        vid = v.get("asset_id")
        for aid in reverse_audio_asset_ids(v):
            if not isinstance(aid, str) or not aid:
                continue
            apath = AUDIO_DIR / f"{aid}.audio.json"
            if not apath.exists():
                issues.append({"kind": "video_lists_missing_audio", "video_asset_id": vid, "audio_asset_id": aid})
                continue
            try:
                a = json.loads(apath.read_text(encoding="utf-8"))
            except Exception:
                continue
            back = audio_primary_video_id(a)
            if back != vid:
                issues.append(
                    {
                        "kind": "audio_backlink_mismatch",
                        "video_asset_id": vid,
                        "audio_asset_id": aid,
                        "audio_primary_video_asset_id": back,
                    }
                )

    videos_updated = pair_additions = 0
    if args.fix_reverse_audio:
        videos_updated, pair_additions = sync_reverse_links_from_audio_catalog()

    with log_path.open("w", encoding="utf-8") as f:
        for rec in issues:
            f.write(json.dumps({"run_id": run_id, **rec}, ensure_ascii=False) + "\n")
        f.write(
            json.dumps(
                {
                    "run_id": run_id,
                    "kind": "summary",
                    "issues": len(issues),
                    "fix_reverse_audio": bool(args.fix_reverse_audio),
                    "videos_updated": videos_updated,
                    "pair_additions": pair_additions,
                },
                ensure_ascii=False,
            )
            + "\n"
        )

    print(
        json.dumps(
            {
                "run_id": run_id,
                "issues": len(issues),
                "fix_reverse_audio": bool(args.fix_reverse_audio),
                "videos_updated": videos_updated,
                "pair_additions": pair_additions,
                "log": str(log_path.relative_to(ROOT)),
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
