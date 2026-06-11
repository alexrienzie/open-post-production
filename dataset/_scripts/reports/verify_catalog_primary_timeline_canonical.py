"""
Compare stored primary_timeline_date + date_source to the canonical derivation from
stored path_metadata / ffprobe / exif + source_path via timeline_date_fields_for_new_asset.

Does not re-read files from disk — only detects drift between JSON fields and current
library logic (or stale embedded blobs vs what a full refetch would produce).

Usage:
  python _scripts/reports/verify_catalog_primary_timeline_canonical.py
  python _scripts/reports/verify_catalog_primary_timeline_canonical.py --limit 500
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from _lib.timeline_date import (  # noqa: E402
    calendar_day_is_usable_for_primary,
    timeline_date_fields_for_new_asset,
)

VIDEO_DIR = ROOT / "assets/video"
AUDIO_DIR = ROOT / "assets/audio"
STILL_DIR = ROOT / "assets/stills"


def _iter_media_catalog():
    for d in (VIDEO_DIR, AUDIO_DIR, STILL_DIR):
        if not d.is_dir():
            continue
        for p in sorted(d.glob("*.json")):
            yield p


def _stored_primary_day(ptd: object) -> str | None:
    if ptd is None:
        return None
    if not isinstance(ptd, str):
        return None
    s = ptd.strip()
    if not s:
        return None
    if len(s) >= 10 and s[4] == ":" and s[7] == ":":
        day = f"{s[0:4]}-{s[5:7]}-{s[8:10]}"
    else:
        day = s[:10]
    if not calendar_day_is_usable_for_primary(day):
        return None
    return day


def _stored_ds(ds: object, *, has_primary: bool) -> str | None:
    if not has_primary:
        return None
    if ds is None:
        return None
    if isinstance(ds, str) and not ds.strip():
        return None
    return str(ds).strip() if isinstance(ds, str) else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="Max catalog files to scan (0 = all).")
    args = ap.parse_args()

    examined = 0
    mismatch_primary = 0
    mismatch_ds = 0
    sample_pm: list[str] = []
    sample_ds: list[str] = []

    for p in _iter_media_catalog():
        if args.limit and examined >= args.limit:
            break
        examined += 1
        try:
            rec = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue

        kind = rec.get("record_kind")
        if kind not in ("video", "audio", "still"):
            continue

        sp = rec.get("source_path")
        sp_s = sp if isinstance(sp, str) else None
        pm = rec.get("path_metadata") if isinstance(rec.get("path_metadata"), dict) else {}
        ff = rec.get("ffprobe") if isinstance(rec.get("ffprobe"), dict) else None
        ex = rec.get("exif") if isinstance(rec.get("exif"), dict) else None

        if kind == "still":
            exp_p, exp_ds = timeline_date_fields_for_new_asset("still", pm, None, ex, sp_s)
        elif kind == "video":
            exp_p, exp_ds = timeline_date_fields_for_new_asset("video", pm, ff, None, sp_s)
        else:
            exp_p, exp_ds = timeline_date_fields_for_new_asset("audio", pm, ff, None, sp_s)

        if exp_p is None:
            exp_ds = None

        st_p = _stored_primary_day(rec.get("primary_timeline_date"))
        st_has = st_p is not None
        st_ds = _stored_ds(rec.get("date_source"), has_primary=st_has)

        exp_ds_eff = exp_ds if exp_p else None

        if st_p != exp_p:
            mismatch_primary += 1
            if len(sample_pm) < 15:
                sample_pm.append(f"{p.name}\tstored={st_p!r}\texpected={exp_p!r}\t{sp_s or ''}")

        if st_ds != exp_ds_eff:
            mismatch_ds += 1
            if len(sample_ds) < 15:
                sample_ds.append(
                    f"{p.name}\tprimary={st_p!r}\tstored_ds={st_ds!r}\texpected_ds={exp_ds_eff!r}"
                )

    print(f"examined={examined}")
    print(f"mismatch_primary={mismatch_primary}")
    print(f"mismatch_date_source={mismatch_ds}  (given stored primary vs canonical pairing)")
    if sample_pm:
        print("\n# sample primary mismatches (path)")
        for line in sample_pm:
            print(line)
    if sample_ds:
        print("\n# sample date_source mismatches")
        for line in sample_ds:
            print(line)

    return 0 if mismatch_primary == 0 and mismatch_ds == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
