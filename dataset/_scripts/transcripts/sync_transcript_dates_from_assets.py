"""
Copy `primary_timeline_date` from catalog video/audio rows onto matching transcripts.

For each `assets/transcripts/{asset_id}.transcript.json`, if a
`{asset_id}.video.json` or `{asset_id}.audio.json` exists, set the transcript's
`primary_timeline_date` to the asset's value (including null). Idempotent; atomic writes.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TRANSCRIPTS = ROOT / "assets/transcripts"
VIDEO = ROOT / "assets/video"
AUDIO = ROOT / "assets/audio"
SUFFIX = ".transcript.json"


def atomic_write_json(path: Path, obj: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    changed = 0
    skipped_no_asset = 0
    for tp in TRANSCRIPTS.glob(f"*{SUFFIX}"):
        aid = tp.name[: -len(SUFFIX)]
        vp = VIDEO / f"{aid}.video.json"
        apath = AUDIO / f"{aid}.audio.json"
        if vp.is_file():
            src = vp
        elif apath.is_file():
            src = apath
        else:
            skipped_no_asset += 1
            continue

        t = json.loads(tp.read_text(encoding="utf-8"))
        a = json.loads(src.read_text(encoding="utf-8"))
        a_date = a.get("primary_timeline_date")
        if t.get("primary_timeline_date") == a_date:
            continue
        t["primary_timeline_date"] = a_date
        changed += 1
        if not args.dry_run:
            atomic_write_json(tp, t)

    print(f"transcripts_updated={changed}")
    print(f"transcripts_no_catalog_av={skipped_no_asset}")


if __name__ == "__main__":
    main()
