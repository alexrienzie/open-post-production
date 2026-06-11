"""
Catalog rows with no usable primary_timeline_date, grouped by source_path parent folder.

Treats as empty: missing, blank, or any date not passing
``calendar_day_is_usable_for_primary`` (e.g. ``0000-00-00``).

Writes _review_drafts/empty_primary_timeline_by_folder.tsv

Usage:
  python _scripts/reports/report_empty_primary_timeline_by_folder.py
"""
from __future__ import annotations

import csv
import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "_review_drafts" / "empty_primary_timeline_by_folder.tsv"

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from _lib.timeline_date import calendar_day_is_usable_for_primary  # noqa: E402

SPECS: list[tuple[str, Path, str]] = [
    ("video", ROOT / "assets/video", "*.video.json"),
    ("audio", ROOT / "assets/audio", "*.audio.json"),
    ("still", ROOT / "assets/stills", "*.still.json"),
]


def is_empty_primary(ptd: object) -> bool:
    if ptd is None:
        return True
    if not isinstance(ptd, str):
        return True
    s = ptd.strip()
    if not s:
        return True
    day = s[:10]
    if len(s) >= 10 and s[4] == ":" and s[7] == ":":
        day = f"{s[0:4]}-{s[5:7]}-{s[8:10]}"
    return not calendar_day_is_usable_for_primary(day)


def folder_key(source_path: str) -> str:
    if not source_path:
        return "(no source_path)"
    return str(Path(source_path.replace("/", "\\")).parent)


def main() -> int:
    by_folder: dict[str, list[dict[str, str]]] = defaultdict(list)
    total = 0

    for record_kind, d, pat in SPECS:
        if not d.is_dir():
            continue
        for p in d.glob(pat):
            try:
                rec = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                continue
            ptd = rec.get("primary_timeline_date")
            if not is_empty_primary(ptd):
                continue
            sp = rec.get("source_path") or ""
            aid = rec.get("asset_id") or ""
            fk = folder_key(sp)
            disp = ptd if isinstance(ptd, str) else ""
            row = {
                "folder": fk,
                "primary_timeline_date": disp,
                "record_kind": record_kind,
                "date_source": str(rec.get("date_source") or ""),
                "asset_id": aid,
                "source_path": sp,
            }
            by_folder[fk].append(row)
            total += 1

    OUT.parent.mkdir(parents=True, exist_ok=True)
    fields = ["folder", "primary_timeline_date", "record_kind", "date_source", "asset_id", "source_path"]
    all_rows: list[dict[str, str]] = []
    for fk in sorted(by_folder.keys(), key=lambda x: x.lower()):
        all_rows.extend(sorted(by_folder[fk], key=lambda r: r["source_path"].lower()))

    with OUT.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, delimiter="\t")
        w.writeheader()
        w.writerows(all_rows)

    print(f"assets_with_empty_or_placeholder_primary_date: {total}")
    print(f"wrote: {OUT.relative_to(ROOT)}")
    print()
    print("folder\tcount")
    for fk in sorted(by_folder.keys(), key=lambda x: x.lower()):
        print(f"{fk}\t{len(by_folder[fk])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
