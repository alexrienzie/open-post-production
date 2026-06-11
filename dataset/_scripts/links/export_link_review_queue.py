#!/usr/bin/env python3
"""
Summarize human-manifest link components and catalog symmetry hints for **manual review**.

Writes `_runs/link_review_queue_<timestamp>.json` (no transcript mutation).

Includes:
- Components with >1 catalog asset (suggest reviewing linkage / propagation).
- Audio catalog rows with machine transcript but no primary video link.

Usage:
  python _scripts/links/export_link_review_queue.py
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RUNS_DIR = ROOT / "_runs"
TRANSCRIPTS_DIR = ROOT / "assets" / "catalog" / "transcripts"
AUDIO_DIR = ROOT / "assets" / "catalog" / "audio"

sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "_scripts"))
from _lib.linked_assets import audio_primary_video_id  # noqa: E402
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))  # shared modules live at _scripts root
from human_link_components import discover_components, load_manifest_asset_ids  # noqa: E402


def main() -> int:
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    manifest = load_manifest_asset_ids()
    comps = discover_components(manifest)

    multi = []
    for c in comps:
        if len(c) <= 1:
            continue
        seeds = sorted(c & manifest)
        multi.append(
            {
                "component_size": len(c),
                "manifest_seeds_in_component": seeds,
                "non_seed_asset_ids": sorted(c - set(seeds)),
            }
        )
    multi.sort(key=lambda x: -x["component_size"])

    audio_no_video_link: list[dict[str, str]] = []
    for ap in sorted(AUDIO_DIR.glob("*.audio.json")):
        try:
            a = json.loads(ap.read_text(encoding="utf-8"))
        except Exception:
            continue
        aid = a.get("asset_id")
        if not aid:
            continue
        if audio_primary_video_id(a):
            continue
        tp = TRANSCRIPTS_DIR / f"{aid}.transcript.json"
        if not tp.exists():
            continue
        audio_no_video_link.append({"audio_asset_id": aid})

    run_id = datetime.now(timezone.utc).strftime("link_review_queue_%Y%m%dT%H%M%SZ")
    out = {
        "run_id": run_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "manifest_distinct_assets": len(manifest),
        "link_components": len(comps),
        "multi_asset_components": len(multi),
        "multi_asset_component_examples": multi[:80],
        "audio_with_transcript_but_no_linked_video_count": len(audio_no_video_link),
        "audio_with_transcript_but_no_linked_video_sample": audio_no_video_link[:50],
    }
    path = RUNS_DIR / f"{run_id}.json"
    path.write_text(json.dumps(out, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({"run_id": run_id, "out": str(path.relative_to(ROOT))}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
