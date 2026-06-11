#!/usr/bin/env python3
"""
Backfill camera timecode (TC) and (optionally) location metadata for existing
video catalog records under:
  assets/video/{asset_id}.video.json

Design goals:
- Additive-only: does not change schema_version.
- Deterministic + resumable: supports --offset/--limit and stable ordering.
- Avoids touching records unless a value changes (atomic writes).

Timecode extraction strategy (best-effort):
- Run ffprobe with -show_format -show_streams (json).
- Prefer any stream tag named "timecode" (case-insensitive), prioritizing data
  streams (QuickTime timecode tracks often appear as codec_tag tmcd / other data).
- Fallback to format tags.

Location strategy (optional; best-effort):
- Re-run ffprobe tags and look for QuickTime ISO6709 location tags such as:
  "com.apple.quicktime.location.ISO6709" or "location"
- Optionally run exiftool (when installed) to get numeric GPSLatitude/GPSLongitude.

Usage:
  python _scripts/catalog/backfill_video_timecode_and_location.py --dry-run --limit 50
  python _scripts/catalog/backfill_video_timecode_and_location.py --write
  python _scripts/catalog/backfill_video_timecode_and_location.py --write --with-location
  python _scripts/catalog/backfill_video_timecode_and_location.py --write --with-location --prefer-exiftool
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
VIDEO_DIR = ROOT / "assets/video"


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def atomic_write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, indent=2, ensure_ascii=False)
    tmp = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    tmp.write_text(payload, encoding="utf-8")
    # Windows can occasionally throw transient PermissionError if AV/indexer grabs the file.
    # Retry a few times with backoff.
    last_err: Exception | None = None
    for i in range(6):
        try:
            os.replace(tmp, path)
            return
        except (FileNotFoundError, PermissionError) as e:
            last_err = e
            try:
                tmp.write_text(payload, encoding="utf-8")
            except Exception:
                pass
            time.sleep(0.15 * (2**i))
    if last_err:
        raise last_err


def find_ffprobe() -> str:
    p = shutil.which("ffprobe")
    if not p:
        raise RuntimeError("ffprobe not found on PATH. Install via `winget install Gyan.FFmpeg`.")
    return p


def find_exiftool() -> str:
    p = shutil.which("exiftool")
    if not p:
        raise RuntimeError("exiftool not found on PATH. Install via `winget install OliverBetz.ExifTool`.")
    return p


def _ffprobe_json(path: str, ffprobe_bin: str) -> dict:
    cmd = [ffprobe_bin, "-v", "error", "-show_format", "-show_streams", "-of", "json", "-i", path]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=90, check=False)
    if out.returncode != 0:
        return {"_error": f"ffprobe_rc_{out.returncode}", "_stderr": out.stderr[:500]}
    try:
        return json.loads(out.stdout)
    except json.JSONDecodeError:
        return {"_error": "ffprobe_json_parse"}


def _iter_video_records(*, offset: int, limit: int) -> list[Path]:
    all_p = sorted((p for p in VIDEO_DIR.glob("*.video.json") if p.is_file()), key=lambda p: p.name)
    if offset:
        all_p = all_p[offset:]
    if limit:
        all_p = all_p[:limit]
    return all_p


def _tag_lookup(tags: dict) -> dict[str, Any]:
    """Return a normalized dict mapping lowercase keys -> original value."""
    out: dict[str, Any] = {}
    if not isinstance(tags, dict):
        return out
    for k, v in tags.items():
        if isinstance(k, str):
            out[k.lower()] = v
    return out


def _extract_timecode_from_ffprobe(data: dict) -> dict | None:
    """
    Return a dict like:
      { "timecode": "HH:MM:SS:FF", "source": "...", "stream_index": 2 }
    or None if not found.
    """
    if not isinstance(data, dict) or data.get("_error"):
        return None

    fmt = (data.get("format") or {}) if isinstance(data.get("format"), dict) else {}
    streams = data.get("streams") or []

    # Collect candidates: (priority, tc, source, stream_index)
    candidates: list[tuple[int, str, str, int | None]] = []

    def add_candidate(tc: Any, *, pri: int, source: str, stream_index: int | None) -> None:
        if not isinstance(tc, str):
            return
        s = tc.strip()
        if not s:
            return
        candidates.append((pri, s, source, stream_index))

    # Stream-level timecode tags are most useful; prefer data streams first.
    for s in streams if isinstance(streams, list) else []:
        if not isinstance(s, dict):
            continue
        tags = _tag_lookup(s.get("tags") or {})
        tc = tags.get("timecode")
        codec_type = s.get("codec_type")
        codec_tag = (s.get("codec_tag_string") or "").lower() if isinstance(s.get("codec_tag_string"), str) else ""
        codec_name = (s.get("codec_name") or "").lower() if isinstance(s.get("codec_name"), str) else ""
        idx = s.get("index")
        stream_index = idx if isinstance(idx, int) else None

        is_data = codec_type == "data"
        is_timecode_track = codec_tag == "tmcd" or codec_name == "tmcd"

        # Priority: explicit timecode track > other data stream > video stream > others
        if tc:
            if is_timecode_track:
                add_candidate(tc, pri=0, source=f"stream_tag_timecode(codec_tag={codec_tag or 'n/a'})", stream_index=stream_index)
            elif is_data:
                add_candidate(tc, pri=1, source=f"data_stream_tag_timecode(codec_tag={codec_tag or 'n/a'})", stream_index=stream_index)
            elif codec_type == "video":
                add_candidate(tc, pri=2, source="video_stream_tag_timecode", stream_index=stream_index)
            else:
                add_candidate(tc, pri=3, source=f"stream_tag_timecode(codec_type={codec_type or 'unknown'})", stream_index=stream_index)

    # Format tags fallback.
    fmt_tags = _tag_lookup(fmt.get("tags") or {})
    if "timecode" in fmt_tags:
        add_candidate(fmt_tags.get("timecode"), pri=10, source="format_tag_timecode", stream_index=None)

    if not candidates:
        return None
    candidates.sort(key=lambda t: (t[0], t[1]))
    pri, tc, src, si = candidates[0]
    return {"timecode": tc, "source": src, "stream_index": si, "extracted_at": now_iso()}


ISO6709_RE = re.compile(r"[+-]\d+(?:\.\d+)?")


def _parse_iso6709(s: str) -> tuple[float | None, float | None, float | None]:
    chunks = ISO6709_RE.findall((s or "").strip())
    if len(chunks) < 2:
        return None, None, None
    try:
        lat = float(chunks[0])
        lon = float(chunks[1])
        alt = float(chunks[2]) if len(chunks) >= 3 else None
        return lat, lon, alt
    except Exception:
        return None, None, None


def _extract_location_from_tags(merged_tags: dict[str, Any]) -> dict | None:
    if not merged_tags:
        return None
    # Common keys across iOS / DJI / GoPro / QuickTime.
    keys = [
        "com.apple.quicktime.location.iso6709",
        "com.apple.quicktime.location.ISO6709".lower(),
        "location",
    ]
    raw_key = None
    raw_val = None
    for k in keys:
        v = merged_tags.get(k)
        if isinstance(v, str) and v.strip():
            raw_key = k
            raw_val = v.strip()
            break
    if not raw_val:
        return None
    lat, lon, alt = _parse_iso6709(raw_val)
    return {
        "raw": {raw_key: raw_val},
        "lat": lat,
        "lon": lon,
        "alt_m": alt,
        "source": "ffprobe_tag",
        "extracted_at": now_iso(),
    }


def _exiftool_gps(path: str, exiftool_bin: str) -> dict | None:
    # Use numeric (-n) outputs.
    cmd = [
        exiftool_bin,
        "-json",
        "-fast",
        "-n",
        "-GPSLatitude",
        "-GPSLongitude",
        "-GPSAltitude",
        path,
    ]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=90, check=False)
    if out.returncode != 0:
        return None
    try:
        lst = json.loads(out.stdout)
        d = lst[0] if lst else {}
    except Exception:
        return None
    lat = d.get("GPSLatitude")
    lon = d.get("GPSLongitude")
    alt = d.get("GPSAltitude")
    if lat is None or lon is None:
        return None
    try:
        return {
            "raw": {"GPSLatitude": lat, "GPSLongitude": lon, "GPSAltitude": alt},
            "lat": float(lat),
            "lon": float(lon),
            "alt_m": float(alt) if alt is not None else None,
            "source": "exif",
            "extracted_at": now_iso(),
        }
    except Exception:
        return {
            "raw": {"GPSLatitude": lat, "GPSLongitude": lon, "GPSAltitude": alt},
            "lat": None,
            "lon": None,
            "alt_m": None,
            "source": "exif",
            "extracted_at": now_iso(),
        }


def _pick_richer_location(old: dict | None, new: dict | None) -> dict | None:
    def score(loc: dict | None) -> int:
        if not loc or not isinstance(loc, dict):
            return 0
        if isinstance(loc.get("lat"), (int, float)) and isinstance(loc.get("lon"), (int, float)):
            return 2
        if loc.get("raw"):
            return 1
        return 0

    if not new:
        return old
    if not old:
        return new
    return new if score(new) > score(old) else old


def _json_sig(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, default=str) if obj is not None else ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--write", action="store_true", help="Actually write changes (otherwise read-only scan).")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--offset", type=int, default=0)
    ap.add_argument("--with-location", action="store_true", help="Also backfill/update `location` when missing.")
    ap.add_argument(
        "--prefer-exiftool",
        action="store_true",
        help="When --with-location is set, use exiftool GPS if present (slower).",
    )
    ap.add_argument(
        "--no-exiftool",
        action="store_true",
        help="Force-disable exiftool even if installed (use ffprobe-only for location).",
    )
    ap.add_argument(
        "--heartbeat-every",
        type=int,
        default=25,
        help="Print progress every N files (default: 25).",
    )
    ap.add_argument(
        "--per-file-timeout-sec",
        type=int,
        default=120,
        help="Abort a single file if processing exceeds this many seconds (default: 120).",
    )
    args = ap.parse_args()

    ffprobe_bin = find_ffprobe()
    exiftool_bin = None
    if args.with_location and args.prefer_exiftool and not args.no_exiftool:
        try:
            exiftool_bin = find_exiftool()
        except Exception:
            exiftool_bin = None

    n_seen = n_changed = n_missing_source = n_ffprobe = n_exiftool = n_errors = 0
    t0 = time.time()

    for p in _iter_video_records(offset=args.offset, limit=args.limit):
        n_seen += 1
        if args.heartbeat_every and (n_seen == 1 or (n_seen % args.heartbeat_every == 0)):
            elapsed = time.time() - t0
            rate = (n_seen / elapsed) if elapsed > 0 else 0.0
            print(f"[tc-backfill] {n_seen} examined, {n_changed} changed ({rate:.2f}/s) last={p.name}")

        try:
            rec = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            n_errors += 1
            continue

        # Fast skip: don't rescan unless we have missing target fields.
        ff_existing = rec.get("ffprobe") if isinstance(rec.get("ffprobe"), dict) else {}
        has_tc = isinstance(ff_existing.get("timecode"), str) and bool(ff_existing.get("timecode").strip())
        has_loc = isinstance(rec.get("location"), dict) and bool(rec.get("location"))
        if has_tc and (not args.with_location or has_loc):
            continue

        sp = rec.get("source_path")
        if not isinstance(sp, str) or not sp:
            continue
        src = Path(sp)
        if not src.is_file():
            n_missing_source += 1
            continue

        before = _json_sig((rec.get("ffprobe") or {}).get("timecode")) + "|" + _json_sig(rec.get("location"))

        try:
            t_file0 = time.time()
            n_ffprobe += 1
            data = _ffprobe_json(str(src), ffprobe_bin)
            if data.get("_error"):
                continue

            # Merge tags (format + all streams) for location parsing.
            merged_tags: dict[str, Any] = {}
            fmt = (data.get("format") or {}) if isinstance(data.get("format"), dict) else {}
            fmt_tags = fmt.get("tags") if isinstance(fmt.get("tags"), dict) else {}
            merged_tags.update(_tag_lookup(fmt_tags))
            streams = data.get("streams") or []
            for s in streams if isinstance(streams, list) else []:
                if not isinstance(s, dict):
                    continue
                merged_tags.update(_tag_lookup(s.get("tags") or {}))

            tc_info = _extract_timecode_from_ffprobe(data)
            if tc_info:
                ff = rec.get("ffprobe")
                if not isinstance(ff, dict):
                    ff = {}
                    rec["ffprobe"] = ff
                ff["timecode"] = tc_info["timecode"]
                ff["timecode_source"] = tc_info["source"]
                ff["timecode_stream_index"] = tc_info["stream_index"]
                ff["timecode_extracted_at"] = tc_info["extracted_at"]

            if args.with_location and not has_loc:
                existing = rec.get("location") if isinstance(rec.get("location"), dict) else None
                loc_ff = _extract_location_from_tags(merged_tags)
                merged = _pick_richer_location(existing, loc_ff)
                if exiftool_bin:
                    n_exiftool += 1
                    loc_et = _exiftool_gps(str(src), exiftool_bin)
                    merged = _pick_richer_location(merged, loc_et)
                if merged:
                    rec["location"] = merged

            # Guard against pathological hangs: skip/continue if one file is too slow.
            if args.per_file_timeout_sec and (time.time() - t_file0) > args.per_file_timeout_sec:
                n_errors += 1
                continue

        except Exception:
            n_errors += 1
            continue

        after = _json_sig((rec.get("ffprobe") or {}).get("timecode")) + "|" + _json_sig(rec.get("location"))
        if after != before:
            n_changed += 1
            if args.write and not args.dry_run:
                try:
                    atomic_write_json(p, rec)
                except PermissionError:
                    # Windows can deny replace if the file is read-only or temporarily locked.
                    # Treat as a per-file failure and continue; the run is safe to resume.
                    n_errors += 1
                    continue

    print(
        "tc backfill done:",
        f"examined={n_seen}",
        f"changed={n_changed}",
        f"missing_source={n_missing_source}",
        f"ffprobe_runs={n_ffprobe}",
        f"exiftool_runs={n_exiftool}",
        f"errors={n_errors}",
        f"dry_run={args.dry_run}",
        f"write={args.write and not args.dry_run}",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

