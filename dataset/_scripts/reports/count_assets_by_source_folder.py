"""
Count catalog assets grouped by RAID source folder.

Groups by:
- full_source_dir: parent directory of asset["source_path"]
- rel_source_dir: same, but with leading "D:\\Project\\" stripped if present

Reads asset JSONs directly from:
  assets/video/*.video.json
  assets/audio/*.audio.json
  assets/stills/*.still.json

Writes results to:
  _runs/source_folder_counts_<utc_ts>/{counts.csv, counts.json}
"""

from __future__ import annotations

import csv
import datetime as dt
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Optional


ROOT = Path(__file__).resolve().parents[2]
VIDEO_DIR = ROOT / "assets/video"
AUDIO_DIR = ROOT / "assets/audio"
STILLS_DIR = ROOT / "assets/stills"
RUNS_DIR = ROOT / "_runs"

GRAND_PREFIX = r"D:\Project"


def _utc_ts_slug() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d_%H%M%S")


def _norm_windows_path(p: str) -> str:
    # Normalize for grouping: slashes, casing, trailing separators.
    s = (p or "").strip().replace("/", "\\").rstrip("\\")
    return s.lower()


def _strip_grand_prefix(norm_path: str) -> str:
    prefix = _norm_windows_path(GRAND_PREFIX) + "\\"
    if norm_path.startswith(prefix):
        return norm_path[len(prefix) :].lstrip("\\")
    return norm_path


def _parent_dir(norm_full_path: str) -> str:
    # Assumes file path; fallback to itself if no separator.
    if "\\" not in norm_full_path:
        return norm_full_path
    return norm_full_path.rsplit("\\", 1)[0]

def _top_level_folder(rel_dir: str) -> str:
    s = (rel_dir or "").strip().strip("\\")
    if not s:
        return ""
    return s.split("\\", 1)[0]


def _iter_asset_jsons() -> list[tuple[str, Path]]:
    out: list[tuple[str, Path]] = []
    out.extend(("video", p) for p in VIDEO_DIR.glob("*.video.json"))
    out.extend(("audio", p) for p in AUDIO_DIR.glob("*.audio.json"))
    out.extend(("still", p) for p in STILLS_DIR.glob("*.still.json"))
    return out


def _date_from_indexed_at(v: object) -> Optional[str]:
    # indexed_at examples: "2026-05-02T03:10:24Z" or "...+00:00"
    if not isinstance(v, str):
        return None
    s = v.strip()
    if len(s) < 10:
        return None
    d = s[:10]
    if d[4:5] != "-" or d[7:8] != "-":
        return None
    return d


def main() -> int:
    rows = _iter_asset_jsons()
    by_full_dir: Counter[str] = Counter()
    by_rel_dir: Counter[str] = Counter()
    by_full_dir_and_type: dict[str, Counter[str]] = defaultdict(Counter)
    by_rel_dir_and_type: dict[str, Counter[str]] = defaultdict(Counter)
    by_top_level_and_type: dict[str, Counter[str]] = defaultdict(Counter)
    by_index_date_and_type: dict[str, Counter[str]] = defaultdict(Counter)
    size_bytes_by_index_date_and_type: dict[str, Counter[str]] = defaultdict(Counter)
    runtime_sec_by_index_date_and_type: dict[str, Counter[str]] = defaultdict(Counter)

    total_assets = 0
    missing_source_path = 0
    unreadable = 0
    missing_indexed_at = 0

    for media_type, path in rows:
        try:
            rec = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            unreadable += 1
            continue

        sp = rec.get("source_path") or ""
        if not isinstance(sp, str) or not sp.strip():
            missing_source_path += 1
            continue

        norm_full = _norm_windows_path(sp)
        full_dir = _parent_dir(norm_full)
        rel_dir = _strip_grand_prefix(full_dir)
        top = _top_level_folder(rel_dir)

        by_full_dir[full_dir] += 1
        by_rel_dir[rel_dir] += 1
        by_full_dir_and_type[full_dir][media_type] += 1
        by_rel_dir_and_type[rel_dir][media_type] += 1
        if top:
            by_top_level_and_type[top][media_type] += 1

        idx_date = _date_from_indexed_at(rec.get("indexed_at"))
        if idx_date:
            by_index_date_and_type[idx_date][media_type] += 1

            sz = rec.get("filesize_bytes") or 0
            try:
                sz_int = int(sz)
            except Exception:
                sz_int = 0
            size_bytes_by_index_date_and_type[idx_date][media_type] += sz_int

            dur = ((rec.get("ffprobe") or {}) if isinstance(rec.get("ffprobe"), dict) else {}).get("duration_sec") or 0
            try:
                dur_f = float(dur)
            except Exception:
                dur_f = 0.0
            # Store milliseconds as int to avoid float drift while summing.
            runtime_sec_by_index_date_and_type[idx_date][media_type] += int(round(dur_f * 1000.0))
        else:
            missing_indexed_at += 1

        total_assets += 1

    out_dir = RUNS_DIR / f"source_folder_counts_{_utc_ts_slug()}"
    out_dir.mkdir(parents=True, exist_ok=True)

    csv_path = out_dir / "counts.csv"
    json_path = out_dir / "counts.json"
    top_level_csv_path = out_dir / "top_level_folders_by_type.csv"
    top_level_json_path = out_dir / "top_level_folders_by_type.json"
    index_date_csv_path = out_dir / "indexed_date_counts.csv"
    index_date_json_path = out_dir / "indexed_date_counts.json"
    index_date_sums_csv_path = out_dir / "indexed_date_sums.csv"
    index_date_sums_json_path = out_dir / "indexed_date_sums.json"

    def row_for(key: str, counter: Counter[str]) -> dict:
        types = counter
        return {
            "source_dir": key,
            "total": int(types.get("video", 0) + types.get("audio", 0) + types.get("still", 0)),
            "video": int(types.get("video", 0)),
            "audio": int(types.get("audio", 0)),
            "still": int(types.get("still", 0)),
        }

    rel_rows = [row_for(k, by_rel_dir_and_type[k]) for k, _ in by_rel_dir.most_common()]
    full_rows = [row_for(k, by_full_dir_and_type[k]) for k, _ in by_full_dir.most_common()]
    top_rows = sorted(
        [row_for(k, by_top_level_and_type[k]) for k in by_top_level_and_type.keys()],
        key=lambda r: (r["total"], r["source_dir"]),
        reverse=True,
    )

    index_date_rows = sorted(
        [row_for(k, by_index_date_and_type[k]) for k in by_index_date_and_type.keys()],
        key=lambda r: (r["source_dir"],),
    )

    def sums_row_for(date_key: str) -> dict:
        sz = size_bytes_by_index_date_and_type.get(date_key) or Counter()
        rt_ms = runtime_sec_by_index_date_and_type.get(date_key) or Counter()
        # Totals across types
        total_bytes = int(sz.get("video", 0) + sz.get("audio", 0) + sz.get("still", 0))
        total_rt_ms = int(rt_ms.get("video", 0) + rt_ms.get("audio", 0) + rt_ms.get("still", 0))
        return {
            "indexed_date": date_key,
            "total_size_bytes": total_bytes,
            "total_size_gb": total_bytes / (1024**3),
            "video_size_bytes": int(sz.get("video", 0)),
            "audio_size_bytes": int(sz.get("audio", 0)),
            "still_size_bytes": int(sz.get("still", 0)),
            "total_runtime_sec": total_rt_ms / 1000.0,
            "total_runtime_hours": (total_rt_ms / 1000.0) / 3600.0,
            "video_runtime_sec": int(rt_ms.get("video", 0)) / 1000.0,
            "audio_runtime_sec": int(rt_ms.get("audio", 0)) / 1000.0,
            "still_runtime_sec": int(rt_ms.get("still", 0)) / 1000.0,
        }

    index_date_sums_rows = sorted(
        [sums_row_for(k) for k in by_index_date_and_type.keys()],
        key=lambda r: r["indexed_date"],
    )

    with csv_path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(
            f,
            fieldnames=["grouping", "source_dir", "total", "video", "audio", "still"],
        )
        w.writeheader()
        for r in rel_rows:
            w.writerow({"grouping": "rel_under_project", **r})
        for r in full_rows:
            w.writerow({"grouping": "full_path", **r})

    with top_level_csv_path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["top_level_folder", "total", "video", "audio", "still"])
        w.writeheader()
        for r in top_rows:
            w.writerow(
                {
                    "top_level_folder": r["source_dir"],
                    "total": r["total"],
                    "video": r["video"],
                    "audio": r["audio"],
                    "still": r["still"],
                }
            )

    with index_date_csv_path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["indexed_date", "total", "video", "audio", "still"])
        w.writeheader()
        for r in index_date_rows:
            w.writerow(
                {
                    "indexed_date": r["source_dir"],
                    "total": r["total"],
                    "video": r["video"],
                    "audio": r["audio"],
                    "still": r["still"],
                }
            )

    with index_date_sums_csv_path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(
            f,
            fieldnames=[
                "indexed_date",
                "total_size_bytes",
                "total_size_gb",
                "video_size_bytes",
                "audio_size_bytes",
                "still_size_bytes",
                "total_runtime_sec",
                "total_runtime_hours",
                "video_runtime_sec",
                "audio_runtime_sec",
                "still_runtime_sec",
            ],
        )
        w.writeheader()
        for r in index_date_sums_rows:
            w.writerow(r)

    payload = {
        "generated_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "inputs": {
            "video_dir": str(VIDEO_DIR.relative_to(ROOT)),
            "audio_dir": str(AUDIO_DIR.relative_to(ROOT)),
            "stills_dir": str(STILLS_DIR.relative_to(ROOT)),
        },
        "notes": {
            "grouping_key": "parent directory of source_path, normalized to lowercase windows path",
            "grand_prefix": GRAND_PREFIX,
        },
        "totals": {
            "assets_counted": total_assets,
            "missing_source_path": missing_source_path,
            "unreadable_json": unreadable,
            "unique_full_dirs": len(by_full_dir),
            "unique_rel_dirs": len(by_rel_dir),
            "unique_top_level_folders": len(by_top_level_and_type),
            "missing_indexed_at": missing_indexed_at,
            "unique_index_dates": len(by_index_date_and_type),
        },
        "top_rel_dirs": rel_rows[:100],
        "top_full_dirs": full_rows[:100],
    }
    json_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    top_payload = {
        "generated_at_utc": payload["generated_at_utc"],
        "notes": {
            "grouping_key": "first path component under D:\\Project (from parent directory of source_path)",
            "grand_prefix": GRAND_PREFIX,
        },
        "totals": {
            "assets_counted": total_assets,
            "unique_top_level_folders": len(by_top_level_and_type),
        },
        "rows": top_rows,
    }
    top_level_json_path.write_text(json.dumps(top_payload, indent=2, ensure_ascii=False), encoding="utf-8")

    index_date_payload = {
        "generated_at_utc": payload["generated_at_utc"],
        "notes": {
            "grouping_key": "date portion (YYYY-MM-DD) of asset indexed_at",
        },
        "totals": {
            "assets_counted": total_assets,
            "missing_indexed_at": missing_indexed_at,
            "unique_index_dates": len(by_index_date_and_type),
        },
        "rows": index_date_rows,
    }
    index_date_json_path.write_text(json.dumps(index_date_payload, indent=2, ensure_ascii=False), encoding="utf-8")

    index_date_sums_payload = {
        "generated_at_utc": payload["generated_at_utc"],
        "notes": {
            "size_bytes": "sum(filesize_bytes) across assets for that date",
            "runtime_sec": "sum(ffprobe.duration_sec) across assets for that date; stills contribute 0",
            "runtime_precision": "stored as integer milliseconds during aggregation",
        },
        "totals": {
            "assets_counted": total_assets,
            "missing_indexed_at": missing_indexed_at,
            "unique_index_dates": len(by_index_date_and_type),
        },
        "rows": index_date_sums_rows,
    }
    index_date_sums_json_path.write_text(
        json.dumps(index_date_sums_payload, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    # Console summary (high signal)
    print(f"[counts] assets counted: {total_assets}")
    print(f"[counts] missing source_path: {missing_source_path}; unreadable json: {unreadable}")
    print(f"[counts] unique source dirs (rel): {len(by_rel_dir)}; (full): {len(by_full_dir)}")
    print(f"[counts] wrote: {csv_path.relative_to(ROOT)}")
    print(f"[counts] wrote: {json_path.relative_to(ROOT)}")
    print(f"[counts] wrote: {top_level_csv_path.relative_to(ROOT)}")
    print(f"[counts] wrote: {top_level_json_path.relative_to(ROOT)}")
    print(f"[counts] wrote: {index_date_csv_path.relative_to(ROOT)}")
    print(f"[counts] wrote: {index_date_json_path.relative_to(ROOT)}")
    print(f"[counts] wrote: {index_date_sums_csv_path.relative_to(ROOT)}")
    print(f"[counts] wrote: {index_date_sums_json_path.relative_to(ROOT)}")
    print("")
    print("Top 25 source folders (relative to D:\\Project):")
    for r in rel_rows[:25]:
        print(f"{r['total']:5d}  v={r['video']:4d} a={r['audio']:3d} s={r['still']:4d}  {r['source_dir']}")

    print("")
    print("Top 25 top-level folders (relative to D:\\Project) by asset type:")
    for r in top_rows[:25]:
        print(f"{r['total']:5d}  v={r['video']:4d} a={r['audio']:3d} s={r['still']:4d}  {r['source_dir']}")

    print("")
    print("Indexed date counts (all types):")
    for r in index_date_rows:
        print(f"{r['source_dir']}  total={r['total']:5d}  v={r['video']:5d} a={r['audio']:4d} s={r['still']:5d}")

    print("")
    print("Indexed date size/runtime sums:")
    for r in index_date_sums_rows:
        print(
            f"{r['indexed_date']}  size_gb={r['total_size_gb']:.2f}  "
            f"runtime_hr={r['total_runtime_hours']:.2f}  "
            f"(v={r['video_runtime_sec'] / 3600.0:.2f}h a={r['audio_runtime_sec'] / 3600.0:.2f}h)"
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

