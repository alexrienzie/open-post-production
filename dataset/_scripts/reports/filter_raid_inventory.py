"""
Derive filtered RAID inventories from assets/catalog_prep/raid_full.csv.

Reads the flat listing (one row per path), adds path-derived columns, excludes
OS/editorial noise for media views, and writes:

- raid_media_inventory.csv   — video/audio/still files under useful roots
- raid_premiere_projects.csv — .prproj / .prin
- raid_premiere_cache.csv     — Premiere peaks/cache (.pek, .cfa, .PRV paths)
- raid_inventory_stats.txt    — counts and notes

Idempotent. Uses atomic writes (tmp + os.replace) for outputs.

Usage:
  python _scripts/reports/filter_raid_inventory.py
  python _scripts/reports/filter_raid_inventory.py --dry-run
"""
from __future__ import annotations

import argparse
import csv
import io
import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_INPUT = ROOT / "assets/catalog_prep/raid_full.csv"
DEFAULT_OUT_DIR = ROOT / "assets/catalog_prep"

# Path substrings that should not appear in media-focused inventory (case-insensitive).
NOISE_SUBSTRINGS = (
    "\\.spotlight-v100\\",
    "\\.documentrevisions-v100\\",
    "\\.fseventsd\\",
    "\\$recycle.bin\\",
    "\\system volume information\\",
    "\\.temporaryitems\\",
    "\\.trashes\\",
)

MEDIA_VIDEO = {".mp4", ".mov", ".mxf", ".mts", ".r3d"}
MEDIA_AUDIO = {".wav", ".mp3", ".m4a"}
MEDIA_STILL = {".jpg", ".jpeg", ".png", ".heic", ".arw", ".dng", ".tiff", ".webp"}

PREMIERE_PROJECT_EXT = {".prproj", ".prin"}
PREMIERE_CACHE_EXT = {".pek", ".cfa"}


def _norm_path_for_scan(p: str) -> str:
    """Backslash path, lowercased, wrapped with \\ for substring checks."""
    return "\\" + p.replace("/", "\\").lower() + "\\"


def is_noise_path(full_path: str) -> bool:
    n = _norm_path_for_scan(full_path)
    return any(s in n for s in NOISE_SUBSTRINGS)


def is_premiere_cache_path(full_path: str, ext: str) -> bool:
    """True for .pek/.cfa that live under Premiere cache trees (not arbitrary media)."""
    e = (ext or "").lower()
    if e not in PREMIERE_CACHE_EXT:
        return False
    n = full_path.replace("/", "\\").lower()
    if ".prv\\" in n or n.rsplit("\\", 1)[-1].endswith(".prv"):
        return True
    if "adobe premiere" in n:
        return True
    return False


def is_proxy_path(full_path: str) -> bool:
    parts = full_path.replace("/", "\\").lower().split("\\")
    return any(p in ("proxy", "proxies") for p in parts)


def parse_project(full_path: str) -> tuple[str, str]:
    """
    Returns (project_relpath, first_segment).

    project_relpath is relative to D:\\Project\\ with no leading slash.
    If not under Project, returns ("", "").
    """
    fp = full_path.replace("/", "\\")
    m = re.match(r"^([a-z]:)\\Project(?:\\(.*))?$", fp, re.IGNORECASE)
    if not m:
        return "", ""
    rest = (m.group(2) or "").rstrip("\\")
    if not rest:
        return "", ""
    first = rest.split("\\", 1)[0]
    return rest, first


def root_bucket(full_path: str) -> str:
    """First path segment after drive letter (e.g. D:\\X\\y -> X)."""
    fp = full_path.replace("/", "\\")
    m = re.match(r"^[a-z]:\\([^\\]+)", fp, re.IGNORECASE)
    return m.group(1) if m else ""


def top_bucket(full_path: str, gp_relpath: str) -> str:
    if gp_relpath:
        return gp_relpath.split("\\", 1)[0]
    return root_bucket(full_path)


def media_class(ext: str) -> str:
    e = (ext or "").lower()
    if e in MEDIA_VIDEO:
        return "video"
    if e in MEDIA_AUDIO:
        return "audio"
    if e in MEDIA_STILL:
        return "still"
    return ""


def atomic_write_text(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    os.replace(tmp, path)


def enrich_row(full_path: str, row: dict) -> dict:
    ext = (row.get("ext") or "").strip()
    gp_rel, _ = parse_project(full_path)
    tb = top_bucket(full_path, gp_rel)
    base = Path(full_path).name
    stem = Path(full_path).stem
    out = {
        **row,
        "project_relpath": gp_rel,
        "top_bucket": tb,
        "is_proxy_path": "1" if is_proxy_path(full_path) else "0",
        "is_premiere_cache": "1" if is_premiere_cache_path(full_path, ext) else "0",
        "basename": base,
        "stem": stem,
    }
    return out


def read_raid_csv(path: Path) -> list[dict]:
    rows: list[dict] = []
    with path.open(encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for r in reader:
            rows.append(dict(r))
    return rows


def write_raid_csv(path: Path, fieldnames: list[str], rows: list[dict]) -> None:
    buf = io.StringIO()
    w = csv.DictWriter(
        buf,
        fieldnames=fieldnames,
        extrasaction="ignore",
        quoting=csv.QUOTE_MINIMAL,
    )
    w.writeheader()
    for r in rows:
        w.writerow({k: r.get(k, "") for k in fieldnames})
    atomic_write_text(path, buf.getvalue())


def main() -> None:
    ap = argparse.ArgumentParser(description="Derive RAID inventory CSVs from raid_full.csv")
    ap.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    ap.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    inp = args.input.resolve()
    out_dir = args.out_dir.resolve()
    if not inp.is_file():
        raise SystemExit(f"Missing input: {inp}")

    raw = read_raid_csv(inp)
    extra_fields = [
        "project_relpath",
        "top_bucket",
        "is_proxy_path",
        "is_premiere_cache",
        "basename",
        "stem",
        "media_class",
    ]
    base_fields = list(raw[0].keys()) if raw else [
        "full_path",
        "type",
        "size_bytes",
        "mtime_utc",
        "depth",
        "ext",
    ]
    out_fields = base_fields + [f for f in extra_fields if f not in base_fields]

    media_rows: list[dict] = []
    premiere_proj: list[dict] = []
    premiere_cache: list[dict] = []
    stats = {
        "input_rows": len(raw),
        "noise_skipped_media": 0,
    }

    for row in raw:
        full_path = (row.get("full_path") or "").strip().strip('"')
        typ = (row.get("type") or "").strip().upper()
        ext = (row.get("ext") or "").strip().lower()

        enriched = enrich_row(full_path, row)
        mc = media_class(ext)
        enriched["media_class"] = mc

        if typ == "F":
            el = ext.lower()
            if el in PREMIERE_PROJECT_EXT:
                premiere_proj.append(enriched)
            elif is_premiere_cache_path(full_path, ext):
                premiere_cache.append(enriched)

            if mc and not is_noise_path(full_path):
                media_rows.append(enriched)
            elif mc and is_noise_path(full_path):
                stats["noise_skipped_media"] += 1

    stats["premiere_projects"] = len(premiere_proj)
    stats["premiere_cache"] = len(premiere_cache)
    stats["media_video"] = sum(1 for r in media_rows if r.get("media_class") == "video")
    stats["media_audio"] = sum(1 for r in media_rows if r.get("media_class") == "audio")
    stats["media_still"] = sum(1 for r in media_rows if r.get("media_class") == "still")

    stats_lines = [
        "=== RAID derived inventory ===",
        f"Source: {inp.name}",
        f"Input rows: {stats['input_rows']}",
        "",
        "raid_media_inventory.csv",
        f"  total media files: {len(media_rows)}",
        f"  video: {stats['media_video']}",
        f"  audio: {stats['media_audio']}",
        f"  still: {stats['media_still']}",
        f"  skipped (noise path): {stats['noise_skipped_media']}",
        "",
        "raid_premiere_projects.csv",
        f"  count: {stats['premiere_projects']}",
        "",
        "raid_premiere_cache.csv",
        f"  count: {stats['premiere_cache']}",
        "",
        "Columns added to all derived CSVs:",
        "  project_relpath, top_bucket, is_proxy_path, is_premiere_cache,",
        "  basename, stem, media_class (media inventory only)",
    ]
    stats_text = "\n".join(stats_lines) + "\n"

    if args.dry_run:
        print(stats_text)
        return

    out_dir.mkdir(parents=True, exist_ok=True)
    media_path = out_dir / "raid_media_inventory.csv"
    proj_path = out_dir / "raid_premiere_projects.csv"
    cache_path = out_dir / "raid_premiere_cache.csv"
    stats_path = out_dir / "raid_inventory_stats.txt"

    write_raid_csv(media_path, out_fields, media_rows)
    write_raid_csv(proj_path, out_fields, premiere_proj)
    write_raid_csv(cache_path, out_fields, premiere_cache)
    atomic_write_text(stats_path, stats_text)
    print(stats_text)


if __name__ == "__main__":
    main()
