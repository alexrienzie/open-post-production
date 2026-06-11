"""
Insert `date_source` immediately after `primary_timeline_date` on catalog media JSON.

Values: source_path | camera_metadata | filesystem_metadata (see _lib/timeline_date.py).

Idempotent. Does not change primary_timeline_date.

Usage:
  python _scripts/catalog/backfill_asset_date_source.py --dry-run
  python _scripts/catalog/backfill_asset_date_source.py
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from _lib.timeline_date import infer_date_source_from_asset_record  # noqa: E402

VIDEO_DIR = ROOT / "assets/video"
AUDIO_DIR = ROOT / "assets/audio"
STILLS_DIR = ROOT / "assets/stills"


def atomic_write_json(path: Path, data: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def merge_date_source(rec: dict) -> dict:
    ds = infer_date_source_from_asset_record(rec)
    out: dict = {}
    for k, v in rec.items():
        if k == "date_source":
            continue
        out[k] = v
        if k == "primary_timeline_date":
            out["date_source"] = ds
    if "primary_timeline_date" not in rec:
        out["date_source"] = ds
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    counts = {"examined": 0, "written": 0, "unchanged": 0, "errors": 0}
    for d, pat in (
        (VIDEO_DIR, "*.video.json"),
        (AUDIO_DIR, "*.audio.json"),
        (STILLS_DIR, "*.still.json"),
    ):
        for p in sorted(d.glob(pat)):
            counts["examined"] += 1
            try:
                rec = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                counts["errors"] += 1
                continue
            new = merge_date_source(rec)
            if json.dumps(rec, indent=2, ensure_ascii=False) == json.dumps(
                new, indent=2, ensure_ascii=False
            ):
                counts["unchanged"] += 1
                continue
            counts["written"] += 1
            if not args.dry_run:
                atomic_write_json(p, new)

    print(json.dumps(counts, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
