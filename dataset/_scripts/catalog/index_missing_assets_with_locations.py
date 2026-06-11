#!/usr/bin/env python3
"""
Index unlisted media files under D:\Project into the workspace catalog:
  assets/video/{asset_id}.video.json
  assets/audio/{asset_id}.audio.json
  assets/stills/{asset_id}.still.json

Also (optionally) backfills a unified `location` block onto existing asset JSONs.

  --backfill-empty-primary-metadata — Re-run ffprobe/exiftool for catalog rows with
    missing/unusable ``primary_timeline_date`` (e.g. D: reconnected); refreshes
    ffprobe/exif, path_metadata, primary + date_source, location merge, classifications.

  --normalize-placeholder-primary-dates — Set ``primary_timeline_date`` and
    ``date_source`` to null when the stored date is a placeholder (0000-…, etc.).

The indexing logic mirrors the original RAID indexer recovered under:
  _archive/scripts_recovery_2026-05-06/index_raid.py
  _archive/scripts_recovery_2026-05-06/index_assets.py

but writes records in the *current* catalog layout under assets/catalog/* and
keeps schema_version numbers unchanged (additive fields only).

Usage:
  python _scripts/catalog/index_missing_assets_with_locations.py --dry-run
  python _scripts/catalog/index_missing_assets_with_locations.py --limit 50
  python _scripts/catalog/index_missing_assets_with_locations.py --write-new
  python _scripts/catalog/index_missing_assets_with_locations.py --write-new --backfill-locations
  python _scripts/catalog/index_missing_assets_with_locations.py --backfill-raid-metadata
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from _lib.asset_classifications import (  # noqa: E402
    build_asset_classifications,
    load_human_transcript_asset_ids,
)
from _lib.timeline_date import (  # noqa: E402
    calendar_day_is_usable_for_primary,
    timeline_date_fields_for_new_asset,
)

GRAND = Path(r"D:\Project")

VIDEO_DIR = ROOT / "assets/video"
AUDIO_DIR = ROOT / "assets/audio"
STILL_DIR = ROOT / "assets/stills"

VIDEO_SCHEMA_VERSION = 7
AUDIO_SCHEMA_VERSION = 5
STILL_SCHEMA_VERSION = 4

_EMPTY_LINKED_ASSETS = {"video": [], "audio": [], "stills": []}

_human_roster_ids_cache: set[str] | None = None


def _human_roster_asset_ids() -> set[str]:
    global _human_roster_ids_cache
    if _human_roster_ids_cache is None:
        _human_roster_ids_cache = load_human_transcript_asset_ids(ROOT)
    return _human_roster_ids_cache

# Top-level editorial buckets only — same list as compare_project_index.py.
EXCLUDE_TOP_SEGMENTS_CF = frozenset(
    s.casefold()
    for s in (
        "Transitions and effects",
        "Trailer",
        "Teaser",
        "Exports",
        "Project Folder",
        "Recap",
    )
)


VIDEO_EXTS = {".mp4", ".mov", ".r3d", ".mxf", ".mts", ".mkv", ".avi", ".m4v", ".braw"}
AUDIO_EXTS = {".wav", ".mp3", ".m4a", ".flac", ".aac", ".ogg", ".aiff", ".aif", ".opus", ".caf"}
STILL_EXTS = {
    ".jpg",
    ".jpeg",
    ".heic",
    ".heif",
    ".arw",
    ".dng",
    ".tiff",
    ".tif",
    ".png",
    ".cr2",
    ".cr3",
    ".nef",
    ".gif",
    ".bmp",
    ".webp",
}

HASH_HEAD_TAIL_BYTES = 1_000_000


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def atomic_write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def partial_hash(path: str, size: int) -> str:
    """sha256 of (first 1MB || last 1MB || filesize_bytes_be). For files <= 2MB, hashes whole file."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        if size <= 2 * HASH_HEAD_TAIL_BYTES:
            while True:
                b = f.read(1 << 20)
                if not b:
                    break
                h.update(b)
        else:
            h.update(f.read(HASH_HEAD_TAIL_BYTES))
            f.seek(-HASH_HEAD_TAIL_BYTES, os.SEEK_END)
            h.update(f.read(HASH_HEAD_TAIL_BYTES))
    h.update(size.to_bytes(8, "big"))
    return h.hexdigest()


def find_ffprobe() -> str:
    p = shutil.which("ffprobe")
    if not p:
        raise RuntimeError("ffprobe not found on PATH")
    return p


def find_exiftool() -> str:
    p = shutil.which("exiftool")
    if not p:
        raise RuntimeError("exiftool not found on PATH")
    return p


def classify_color_profile(color_primaries: Any, color_transfer: Any, color_space: Any) -> str:
    cp = (color_primaries or "").lower()
    ct = (color_transfer or "").lower()
    if ct == "smpte2084":
        return "hdr_pq"
    if ct == "arib-std-b67":
        return "hdr_hlg"
    if ct == "bt709":
        return "rec709"
    if cp == "bt2020" and ct in ("", "unknown", "reserved"):
        return "log_likely"
    if cp == "bt2020":
        return "wide_gamut"
    if cp in ("bt709", "smpte170m") and ct in ("", "unknown", "reserved", "smpte170m", "iec61966-2-1"):
        return "rec709"
    return "unknown"


def _ffprobe_json(path: str, ffprobe_bin: str) -> dict:
    cmd = [ffprobe_bin, "-v", "error", "-show_format", "-show_streams", "-of", "json", "-i", path]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=60, check=False)
    if out.returncode != 0:
        return {"_error": f"ffprobe_rc_{out.returncode}", "_stderr": out.stderr[:500]}
    try:
        return json.loads(out.stdout)
    except json.JSONDecodeError:
        return {"_error": "ffprobe_json_parse"}


def ffprobe_video_compact(path: str, ffprobe_bin: str) -> tuple[dict, dict]:
    """Returns (compact_fields, tags) where tags are merged format+video stream tags."""
    data = _ffprobe_json(path, ffprobe_bin)
    if "_error" in data:
        return data, {}

    fmt = data.get("format", {}) or {}
    streams = data.get("streams", []) or []
    vstream = next((s for s in streams if s.get("codec_type") == "video"), None)
    astream = next((s for s in streams if s.get("codec_type") == "audio"), None)

    def fps_from(stream: dict | None) -> float | None:
        if not stream:
            return None
        rate = stream.get("avg_frame_rate") or stream.get("r_frame_rate")
        if not rate or rate == "0/0":
            return None
        try:
            num_s, den_s = str(rate).split("/")
            num, den = float(num_s), float(den_s)
            return round(num / den, 4) if den else None
        except Exception:
            return None

    fmt_tags = fmt.get("tags") or {}
    vstream_tags = (vstream.get("tags") if vstream else None) or {}
    creation_time = fmt_tags.get("creation_time") or vstream_tags.get("creation_time")

    color_primaries = vstream.get("color_primaries") if vstream else None
    color_transfer = vstream.get("color_transfer") if vstream else None
    color_space = vstream.get("color_space") if vstream else None
    color_range = vstream.get("color_range") if vstream else None
    pix_fmt = vstream.get("pix_fmt") if vstream else None

    compact = {
        "duration_sec": float(fmt["duration"]) if fmt.get("duration") else None,
        "width": vstream.get("width") if vstream else None,
        "height": vstream.get("height") if vstream else None,
        "fps": fps_from(vstream),
        "codec": vstream.get("codec_name") if vstream else None,
        "audio_channels": astream.get("channels") if astream else None,
        "audio_sample_rate": int(astream["sample_rate"]) if astream and astream.get("sample_rate") else None,
        "audio_codec": astream.get("codec_name") if astream else None,
        "format_name": fmt.get("format_name"),
        "creation_time": creation_time,
        "color_primaries": color_primaries,
        "color_transfer": color_transfer,
        "color_space": color_space,
        "color_range": color_range,
        "pix_fmt": pix_fmt,
        "color_profile": classify_color_profile(color_primaries, color_transfer, color_space),
    }
    tags = {}
    tags.update(fmt_tags)
    tags.update(vstream_tags)
    return compact, tags


def ffprobe_audio_compact(path: str, ffprobe_bin: str) -> tuple[dict, dict]:
    data = _ffprobe_json(path, ffprobe_bin)
    if "_error" in data:
        return data, {}
    fmt = data.get("format", {}) or {}
    streams = data.get("streams", []) or []
    astream = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if not astream:
        return {"_error": "no_audio_stream"}, {}

    fmt_tags = fmt.get("tags") or {}
    astream_tags = (astream.get("tags") or {})
    creation_time = fmt_tags.get("creation_time") or astream_tags.get("creation_time")

    compact = {
        "duration_sec": float(fmt["duration"]) if fmt.get("duration") else None,
        "sample_rate": int(astream["sample_rate"]) if astream.get("sample_rate") else None,
        "channels": astream.get("channels"),
        "codec": astream.get("codec_name"),
        "bit_rate": int(fmt["bit_rate"]) if fmt.get("bit_rate") else None,
        "format_name": fmt.get("format_name"),
        "creation_time": creation_time,
    }
    tags = {}
    tags.update(fmt_tags)
    tags.update(astream_tags)
    return compact, tags


EXIFTOOL_TAGS = [
    "-DateTimeOriginal",
    "-CreateDate",
    "-ImageWidth",
    "-ImageHeight",
    "-Make",
    "-Model",
    "-LensModel",
    "-ISO",
    "-FNumber",
    "-ShutterSpeed",
    "-FocalLength",
    "-Orientation",
    "-GPSLatitude",
    "-GPSLongitude",
    "-GPSAltitude",
    "-ColorSpace",
    "-Software",
]

# Extra tags for video / QuickTime-style GPS (cheap single exiftool pass).
EXIFTOOL_EXTRA_MEDIA = [
    "-MediaCreateDate",
    "-TrackCreateDate",
    "-HandlerType",
    "-CompressorID",
]


def exiftool_compact(path: str, exiftool_bin: str, timeout: int = 90) -> dict:
    cmd = (
        [exiftool_bin, "-json", "-fast", "-n", "-d", "%Y-%m-%dT%H:%M:%S"]
        + EXIFTOOL_TAGS
        + EXIFTOOL_EXTRA_MEDIA
        + [path]
    )
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    if out.returncode != 0:
        return {"_error": f"exiftool_rc_{out.returncode}", "_stderr": out.stderr[:500]}
    try:
        lst = json.loads(out.stdout)
        d = lst[0] if lst else {}
    except Exception:
        return {"_error": "exiftool_json_parse"}

    return {
        "date_taken": d.get("DateTimeOriginal") or d.get("CreateDate"),
        "width": d.get("ImageWidth"),
        "height": d.get("ImageHeight"),
        "camera_make": d.get("Make"),
        "camera_model": d.get("Model"),
        "lens": d.get("LensModel"),
        "iso": d.get("ISO"),
        "aperture": d.get("FNumber"),
        "shutter_speed": d.get("ShutterSpeed"),
        "focal_length": d.get("FocalLength"),
        "orientation": d.get("Orientation"),
        "gps_lat": d.get("GPSLatitude"),
        "gps_lon": d.get("GPSLongitude"),
        "gps_alt": d.get("GPSAltitude"),
        "color_space": d.get("ColorSpace"),
        "software": d.get("Software"),
    }


def location_score(loc: dict | None) -> int:
    if not loc or not isinstance(loc, dict):
        return 0
    if isinstance(loc.get("lat"), (int, float)) and isinstance(loc.get("lon"), (int, float)):
        return 2
    if loc.get("raw"):
        return 1
    return 0


def pick_richer_location(old: dict | None, new: dict | None) -> dict | None:
    """Prefer a dict with parsed lat/lon; otherwise keep the higher-information one."""
    if not new:
        return old
    if not old:
        return new
    so, sn = location_score(old), location_score(new)
    if sn > so:
        return new
    if sn < so:
        return old
    return old


def make_model_from_ffprobe_tags(tags: dict) -> tuple[str | None, str | None]:
    if not tags:
        return None, None
    make = (
        tags.get("com.apple.quicktime.make")
        or tags.get("Make")
        or tags.get("make")
    )
    model = (
        tags.get("com.apple.quicktime.model")
        or tags.get("Model")
        or tags.get("model")
    )
    if isinstance(make, str):
        make = make.strip() or None
    if isinstance(model, str):
        model = model.strip() or None
    return make, model


def refine_camera_fields(
    pm: dict,
    filename: str,
    source_path: str,
    make: str | None,
    model: str | None,
) -> None:
    """Fill camera_make / camera_model / device_category; refine DJI camera_id."""
    if not isinstance(pm, dict):
        return

    mk = (make or "").strip() or None
    md = (model or "").strip() or None
    if mk:
        pm["camera_make"] = mk
    if md:
        pm["camera_model"] = md

    mku = (mk or "").upper()
    mdu = (md or "").upper()
    fnu = filename.upper()
    spu = source_path.upper()

    # Curated folder: keep legacy id; still record device_category when we can.
    if "<DJI-FOLDER>" in spu or pm.get("category_name") == "<dji-category>":
        pm["camera_id"] = "DJI_osmo"
        if "OSMO ACTION" in mdu or "ACTION 2" in mdu or "ACTION 3" in mdu or "ACTION 4" in mdu:
            pm["device_category"] = "action_camera"
        elif "OSMO POCKET" in mdu or "POCKET 2" in mdu or "POCKET 3" in mdu:
            pm["device_category"] = "gimbal_camera"
        elif any(x in mdu for x in ("MAVIC", "MINI 2", "MINI 3", "MINI 4", "AIR 2", "AIR 3", "INSPIRE", "PHANTOM", "AVATA", "FPV", "NEO")):
            pm["device_category"] = "drone"
        elif fnu.startswith("DJI_"):
            pm["device_category"] = "unknown"
        return

    is_dji = "DJI" in mku or "DJI" in mdu or fnu.startswith("DJI_")
    if not is_dji:
        return

    if "OSMO ACTION" in mdu or "ACTION 2" in mdu or "ACTION 3" in mdu or "ACTION 4" in mdu:
        pm["camera_id"] = "dji_action"
        pm["device_category"] = "action_camera"
    elif "OSMO POCKET" in mdu or "POCKET 2" in mdu or "POCKET 3" in mdu:
        pm["camera_id"] = "dji_gimbal"
        pm["device_category"] = "gimbal_camera"
    elif any(x in mdu for x in ("MAVIC", "MINI 2", "MINI 3", "MINI 4", "AIR 2", "AIR 3", "INSPIRE", "PHANTOM", "AVATA", "FPV", "NEO")):
        pm["camera_id"] = "dji_drone"
        pm["device_category"] = "drone"
    elif fnu.startswith("DJI_"):
        pm["camera_id"] = "dji"
        pm["device_category"] = "unknown"


DATE_HYPHEN_RE = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})(?:_(.+))?$")
CATEGORY_RE = re.compile(r"^(\d+)\.\s+(.+)$")


def derive_camera_id(filename: str) -> str:
    base = filename.upper()
    if base.startswith("IMG_"):
        return "iphone"
    if base.startswith("DJI_"):
        return "dji"
    if base.startswith("GX") or base.startswith("GH") or base.startswith("GP"):
        return "gopro"
    m = re.match(r"^([A-Z])(\d{4})", base)
    if m:
        first = m.group(2)[0]
        return f"sony_{m.group(1).lower()}{first}xxx"
    if base.endswith(".R3D"):
        return "red"
    return "unknown"


def parse_path_metadata(full_path: str, ffprobe_compact: dict | None = None) -> dict:
    p = Path(full_path)
    rel = p.relative_to(GRAND) if str(p).lower().startswith(str(GRAND).lower()) else p
    parts = list(rel.parts)
    filename = parts[-1] if parts else ""
    folder_parts = parts[:-1]

    md = {
        "top_level": "other",
        "shoot_date": None,
        "shoot_date_source": None,
        "shoot_label": None,
        "category_number": None,
        "category_name": None,
        "sub_location": None,
        "scene_number": None,
        "scene_name": None,
        "camera_id": derive_camera_id(filename),
        "camera_make": None,
        "camera_model": None,
        "device_category": None,
    }

    if not folder_parts:
        # fallback: creation_time
        ct = (ffprobe_compact or {}).get("creation_time")
        if isinstance(ct, str) and len(ct) >= 10:
            md["shoot_date"] = ct[:10]
            md["shoot_date_source"] = "ffprobe_creation_time"
        refine_camera_fields(md, filename, str(p), None, None)
        return md

    top = folder_parts[0]
    m = DATE_HYPHEN_RE.match(top)
    if m:
        y, mo, d, label = m.groups()
        try:
            iso = f"{int(y):04d}-{int(mo):02d}-{int(d):02d}"
            datetime.strptime(iso, "%Y-%m-%d")
            md["shoot_date"] = iso
            md["shoot_date_source"] = "folder"
            md["shoot_label"] = label
            md["top_level"] = "shoot_day"
        except ValueError:
            pass
        if md["shoot_date"] is None:
            ct = (ffprobe_compact or {}).get("creation_time")
            if isinstance(ct, str) and len(ct) >= 10:
                md["shoot_date"] = ct[:10]
                md["shoot_date_source"] = "ffprobe_creation_time"
        refine_camera_fields(md, filename, str(p), None, None)
        return md

    m = CATEGORY_RE.match(top)
    if m:
        md["top_level"] = "category"
        md["category_number"] = int(m.group(1))
        md["category_name"] = m.group(2).strip()
        if len(folder_parts) >= 2:
            md["sub_location"] = folder_parts[1]
        ct = (ffprobe_compact or {}).get("creation_time")
        if isinstance(ct, str) and len(ct) >= 10:
            md["shoot_date"] = ct[:10]
            md["shoot_date_source"] = "ffprobe_creation_time"
        refine_camera_fields(md, filename, str(p), None, None)
        return md

    # fallback: creation_time
    ct = (ffprobe_compact or {}).get("creation_time")
    if isinstance(ct, str) and len(ct) >= 10:
        md["shoot_date"] = ct[:10]
        md["shoot_date_source"] = "ffprobe_creation_time"
    refine_camera_fields(md, filename, str(p), None, None)
    return md


def is_excluded(rel_under_grand: Path) -> bool:
    parts_cf = [p.casefold() for p in rel_under_grand.parts]
    return bool(parts_cf and parts_cf[0] in EXCLUDE_TOP_SEGMENTS_CF)


def _parse_iso6709(s: str) -> tuple[float | None, float | None, float | None]:
    """
    Parse ISO6709 like '+37.1234-122.1234+10.0/' or '+37.12-122.3/'.
    Returns (lat, lon, alt_m).
    """
    s = s.strip()
    # Common iOS: +37.3317-122.0307/ or +42.5693-000.5482+000.000/
    # Be tolerant: extract signed numeric chunks.
    chunks = re.findall(r"[+-]\d+(?:\.\d+)?", s)
    if len(chunks) < 2:
        return None, None, None
    try:
        lat = float(chunks[0])
        lon = float(chunks[1])
        alt = float(chunks[2]) if len(chunks) >= 3 else None
        return lat, lon, alt
    except Exception:
        return None, None, None


def extract_location_from_tags(tags: dict) -> dict | None:
    """Best-effort location extraction from ffprobe tags."""
    if not tags:
        return None
    # Known keys we’ve seen in the wild across iOS/GoPro/etc.
    candidates = [
        "location",
        "com.apple.quicktime.location.ISO6709",
        "com.apple.quicktime.location.iso6709",
        "LOCATION",
    ]
    raw_val = None
    raw_key = None
    for k in candidates:
        if k in tags and isinstance(tags[k], str) and tags[k].strip():
            raw_key = k
            raw_val = tags[k].strip()
            break
    if not raw_val:
        return None

    lat, lon, alt = _parse_iso6709(raw_val)
    if lat is None or lon is None:
        return {"raw": {raw_key: raw_val}, "lat": None, "lon": None, "alt_m": None, "source": "ffprobe_tag"}
    return {"raw": {raw_key: raw_val}, "lat": lat, "lon": lon, "alt_m": alt, "source": "ffprobe_tag"}


def location_from_exif(exif: dict) -> dict | None:
    if not isinstance(exif, dict):
        return None
    lat = exif.get("gps_lat")
    lon = exif.get("gps_lon")
    alt = exif.get("gps_alt")
    if lat is None or lon is None:
        return None
    try:
        return {
            "raw": {"gps_lat": lat, "gps_lon": lon, "gps_alt": alt},
            "lat": float(lat),
            "lon": float(lon),
            "alt_m": float(alt) if alt is not None else None,
            "source": "exif",
        }
    except Exception:
        return {"raw": {"gps_lat": lat, "gps_lon": lon, "gps_alt": alt}, "lat": None, "lon": None, "alt_m": None, "source": "exif"}


def ensure_dji_osmo_camera_id(source_path: str, pm: dict) -> None:
    if not isinstance(pm, dict):
        return
    if "<dji-folder>" in source_path or pm.get("category_name") == "<dji-category>":
        pm["camera_id"] = "DJI_osmo"


def _iter_catalog_records() -> list[Path]:
    out: list[Path] = []
    for d in (VIDEO_DIR, AUDIO_DIR, STILL_DIR):
        if not d.is_dir():
            continue
        out.extend(p for p in d.glob("*.json") if p.is_file())
    # Deterministic ordering helps batching/resume with --offset.
    return sorted(out, key=lambda p: p.name)


def _iter_catalog_records_batched(*, offset: int, limit: int) -> list[Path]:
    """Return a deterministic slice of catalog records."""
    all_p = _iter_catalog_records()
    if offset:
        all_p = all_p[offset:]
    if limit:
        all_p = all_p[:limit]
    return all_p


def _norm_path(p: str) -> str:
    try:
        return str(Path(p).resolve()).lower()
    except OSError:
        return p.replace("/", "\\").lower()


def compute_unlisted_paths(limit: int = 0) -> list[Path]:
    indexed = set()
    for p in _iter_catalog_records():
        try:
            rec = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        sp = rec.get("source_path")
        if isinstance(sp, str) and sp:
            indexed.add(_norm_path(sp))

    unlisted: list[Path] = []
    if not GRAND.is_dir():
        raise RuntimeError(f"Project path not found: {GRAND}")

    exts = VIDEO_EXTS | AUDIO_EXTS | STILL_EXTS
    grand_resolved = GRAND.resolve()
    for dirpath, _dn, filenames in os.walk(GRAND):
        for fn in filenames:
            ext = Path(fn).suffix.lower()
            if ext not in exts:
                continue
            fp = (Path(dirpath) / fn)
            try:
                r = fp.resolve()
                rel = r.relative_to(grand_resolved)
            except Exception:
                continue
            if is_excluded(rel):
                continue
            n = _norm_path(str(r))
            if n in indexed:
                continue
            unlisted.append(r)
            if limit and len(unlisted) >= limit:
                return unlisted
    return unlisted


def classify_media_by_ext(path: Path) -> str:
    e = path.suffix.lower()
    if e in VIDEO_EXTS:
        return "video"
    if e in AUDIO_EXTS:
        return "audio"
    if e in STILL_EXTS:
        return "still"
    return "unknown"


def build_video_record(path: Path, ffprobe_bin: str, exiftool_bin: str) -> dict:
    st = path.stat()
    aid = partial_hash(str(path), st.st_size)
    ff, tags = ffprobe_video_compact(str(path), ffprobe_bin)
    pm = parse_path_metadata(str(path), ffprobe_compact=ff if "_error" not in ff else None)
    loc_ff = extract_location_from_tags(tags)
    et = exiftool_compact(str(path), exiftool_bin)
    loc_et = location_from_exif(et) if isinstance(et, dict) and "_error" not in et else None
    loc = pick_richer_location(loc_ff, loc_et)
    mk, md = make_model_from_ffprobe_tags(tags)
    emk = et.get("camera_make") if isinstance(et, dict) and "_error" not in et else None
    emd = et.get("camera_model") if isinstance(et, dict) and "_error" not in et else None
    refine_camera_fields(pm, path.name, str(path), emk or mk, emd or md)
    ensure_dji_osmo_camera_id(str(path), pm)
    primary, date_source = timeline_date_fields_for_new_asset(
        "video",
        pm,
        ff if isinstance(ff, dict) and "_error" not in ff else None,
        None,
        str(path),
    )
    return {
        "schema_version": VIDEO_SCHEMA_VERSION,
        "asset_id": aid,
        "source_path": str(path),
        "filename": path.name,
        "filesize_bytes": st.st_size,
        "mtime": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat().replace("+00:00", "Z"),
        "ffprobe": ff,
        "path_metadata": pm,
        "indexed_at": now_iso(),
        "primary_timeline_date": primary,
        "date_source": date_source,
        "asset_classifications": build_asset_classifications(aid, str(path), _human_roster_asset_ids()),
        "embeddings": {"semantic": False, "vector": False},
        "human_transcript": False,
        "record_kind": "video",
        "machine_transcript": False,
        "shoot_location": {"place": None, "source": None},
        "location": loc,
        "linked_assets": dict(_EMPTY_LINKED_ASSETS),
    }


def build_audio_record(path: Path, ffprobe_bin: str) -> dict:
    st = path.stat()
    aid = partial_hash(str(path), st.st_size)
    ff, tags = ffprobe_audio_compact(str(path), ffprobe_bin)
    pm = parse_path_metadata(str(path), ffprobe_compact=ff if "_error" not in ff else None)
    loc = extract_location_from_tags(tags)
    primary, date_source = timeline_date_fields_for_new_asset(
        "audio",
        pm,
        ff if isinstance(ff, dict) and "_error" not in ff else None,
        None,
        str(path),
    )
    return {
        "schema_version": AUDIO_SCHEMA_VERSION,
        "asset_id": aid,
        "source_path": str(path),
        "filename": path.name,
        "filesize_bytes": st.st_size,
        "mtime": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat().replace("+00:00", "Z"),
        "ffprobe": ff,
        "path_metadata": pm,
        "indexed_at": now_iso(),
        "audio_recorder": None,
        "linked_assets": dict(_EMPTY_LINKED_ASSETS),
        "primary_timeline_date": primary,
        "date_source": date_source,
        "asset_classifications": build_asset_classifications(aid, str(path), _human_roster_asset_ids()),
        "embeddings": {"semantic": False, "vector": False},
        "record_kind": "audio",
        "shoot_location": {"place": None, "source": None},
        "location": loc,
    }


def build_still_record(path: Path, exiftool_bin: str) -> dict:
    st = path.stat()
    aid = partial_hash(str(path), st.st_size)
    exif = exiftool_compact(str(path), exiftool_bin)
    pm = parse_path_metadata(str(path), ffprobe_compact=None)
    refine_camera_fields(
        pm,
        path.name,
        str(path),
        exif.get("camera_make") if isinstance(exif, dict) else None,
        exif.get("camera_model") if isinstance(exif, dict) else None,
    )
    loc = location_from_exif(exif)
    primary, date_source = timeline_date_fields_for_new_asset(
        "still",
        pm,
        None,
        exif if isinstance(exif, dict) and "_error" not in exif else None,
        str(path),
    )
    return {
        "schema_version": STILL_SCHEMA_VERSION,
        "asset_id": aid,
        "source_path": str(path),
        "filename": path.name,
        "filesize_bytes": st.st_size,
        "mtime": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat().replace("+00:00", "Z"),
        "exif": exif,
        "path_metadata": pm,
        "indexed_at": now_iso(),
        "primary_timeline_date": primary,
        "date_source": date_source,
        "embeddings": {"semantic": False, "vector": False},
        "phash": None,
        "linked_assets": dict(_EMPTY_LINKED_ASSETS),
        "record_kind": "still",
        "shoot_location": {"place": None, "source": None},
        "location": loc,
    }


def backfill_locations(ffprobe_bin: str, exiftool_bin: str, dry_run: bool, limit: int) -> None:
    n_seen = 0
    n_changed = 0
    for p in _iter_catalog_records():
        try:
            rec = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        n_seen += 1
        if limit and n_seen > limit:
            break

        if "location" in rec and rec["location"]:
            continue

        sp = rec.get("source_path")
        if not isinstance(sp, str) or not sp:
            continue
        src = Path(sp)
        if not src.is_file():
            continue

        kind = rec.get("record_kind")
        loc = None
        if kind == "still":
            ex = rec.get("exif") or {}
            loc = location_from_exif(ex)
        elif kind in ("video", "audio"):
            # Only re-ffprobe when it’s likely to carry GPS tags (mostly iPhone/DJI).
            cam = ((rec.get("path_metadata") or {}) if isinstance(rec.get("path_metadata"), dict) else {}).get("camera_id")
            if cam not in {"iphone", "dji", "DJI_osmo"}:
                continue
            if kind == "video":
                _ff, tags = ffprobe_video_compact(str(src), ffprobe_bin)
            else:
                _ff, tags = ffprobe_audio_compact(str(src), ffprobe_bin)
            loc = extract_location_from_tags(tags)
        else:
            continue

        if not loc:
            continue

        rec["location"] = loc
        if not dry_run:
            atomic_write_json(p, rec)
        n_changed += 1

    print(f"location backfill: examined={n_seen} changed={n_changed} dry_run={dry_run}")


def backfill_locations_all(ffprobe_bin: str, exiftool_bin: str, dry_run: bool, limit: int) -> None:
    """
    Backfill `location` for ALL assets (slower):
    - stills: re-run exiftool if GPS missing in existing exif block
    - video/audio: re-run ffprobe and parse GPS-ish tags
    """
    n_seen = 0
    n_changed = 0
    n_ffprobe = 0
    n_exiftool = 0
    n_missing_source = 0
    n_errors = 0

    for p in _iter_catalog_records():
        try:
            rec = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue

        n_seen += 1
        if limit and n_seen > limit:
            break

        # Don’t redo work if we already have a non-empty location.
        if rec.get("location"):
            continue

        sp = rec.get("source_path")
        if not isinstance(sp, str) or not sp:
            continue
        src = Path(sp)
        if not src.is_file():
            n_missing_source += 1
            continue

        kind = rec.get("record_kind")
        loc = None

        try:
            if kind == "still":
                # Prefer existing exif; if GPS missing, refresh exif from exiftool.
                ex = rec.get("exif") if isinstance(rec.get("exif"), dict) else None
                loc = location_from_exif(ex or {})
                if not loc:
                    n_exiftool += 1
                    ex2 = exiftool_compact(str(src), exiftool_bin)
                    if isinstance(ex2, dict) and "_error" not in ex2:
                        rec["exif"] = ex2
                    loc = location_from_exif(ex2)
            elif kind == "video":
                n_ffprobe += 1
                _ff, tags = ffprobe_video_compact(str(src), ffprobe_bin)
                loc_ff = extract_location_from_tags(tags)
                n_exiftool += 1
                et2 = exiftool_compact(str(src), exiftool_bin)
                loc_et = location_from_exif(et2) if isinstance(et2, dict) and "_error" not in et2 else None
                loc = pick_richer_location(loc_ff, loc_et)
            elif kind == "audio":
                n_ffprobe += 1
                _ff, tags = ffprobe_audio_compact(str(src), ffprobe_bin)
                loc = extract_location_from_tags(tags)
            else:
                continue
        except Exception:
            n_errors += 1
            continue

        if not loc:
            continue

        rec["location"] = loc
        if not dry_run:
            atomic_write_json(p, rec)
        n_changed += 1

    print(
        "location backfill (ALL):",
        f"examined={n_seen}",
        f"changed={n_changed}",
        f"ffprobe_runs={n_ffprobe}",
        f"exiftool_runs={n_exiftool}",
        f"missing_source={n_missing_source}",
        f"errors={n_errors}",
        f"dry_run={dry_run}",
    )


def _json_sig(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, default=str) if obj is not None else ""


def backfill_raid_metadata(
    ffprobe_bin: str,
    exiftool_bin: str,
    dry_run: bool,
    *,
    offset: int,
    limit: int,
) -> None:
    """Re-scan RAID sources: merge richer GPS into `location`, refresh `path_metadata` camera fields."""
    n_seen = 0
    n_changed = 0
    n_ffprobe = 0
    n_exiftool = 0
    n_missing_source = 0
    n_errors = 0

    t0 = time.time()
    for p in _iter_catalog_records_batched(offset=offset, limit=limit):
        try:
            rec = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue

        n_seen += 1
        # Print a heartbeat so PowerShell doesn't look "stalled".
        if n_seen == 1 or (n_seen % 25 == 0):
            elapsed = time.time() - t0
            rate = (n_seen / elapsed) if elapsed > 0 else 0.0
            print(f"[raid-metadata] {n_seen} examined, {n_changed} changed ({rate:.2f}/s) last={p.name}")

        sp = rec.get("source_path")
        if not isinstance(sp, str) or not sp:
            continue
        src = Path(sp)
        if not src.is_file():
            n_missing_source += 1
            continue

        kind = rec.get("record_kind")
        before = _json_sig(rec.get("location")) + "|" + _json_sig(rec.get("path_metadata"))

        try:
            t_one = time.time()
            if kind == "video":
                n_ffprobe += 1
                _ff, tags = ffprobe_video_compact(str(src), ffprobe_bin)
                loc_ff = extract_location_from_tags(tags)
                n_exiftool += 1
                et = exiftool_compact(str(src), exiftool_bin)
                loc_et = location_from_exif(et) if isinstance(et, dict) and "_error" not in et else None
                merged = pick_richer_location(pick_richer_location(rec.get("location"), loc_ff), loc_et)
                if merged:
                    rec["location"] = merged

                pm = rec.get("path_metadata")
                if not isinstance(pm, dict):
                    pm = {}
                    rec["path_metadata"] = pm
                mk, md = make_model_from_ffprobe_tags(tags)
                emk = et.get("camera_make") if isinstance(et, dict) and "_error" not in et else None
                emd = et.get("camera_model") if isinstance(et, dict) and "_error" not in et else None
                refine_camera_fields(pm, Path(sp).name, sp, emk or mk, emd or md)
                ensure_dji_osmo_camera_id(sp, pm)

            elif kind == "still":
                n_exiftool += 1
                et = exiftool_compact(str(src), exiftool_bin)
                if isinstance(et, dict) and "_error" not in et:
                    rec["exif"] = et
                    loc_et = location_from_exif(et)
                    merged = pick_richer_location(rec.get("location"), loc_et)
                    if merged:
                        rec["location"] = merged
                    pm = rec.get("path_metadata")
                    if not isinstance(pm, dict):
                        pm = {}
                        rec["path_metadata"] = pm
                    refine_camera_fields(pm, Path(sp).name, sp, et.get("camera_make"), et.get("camera_model"))

            elif kind == "audio":
                n_ffprobe += 1
                _ff, tags = ffprobe_audio_compact(str(src), ffprobe_bin)
                loc_ff = extract_location_from_tags(tags)
                merged = pick_richer_location(rec.get("location"), loc_ff)
                if merged:
                    rec["location"] = merged
            else:
                continue
            dt = time.time() - t_one
            if dt > 10:
                print(f"[raid-metadata] slow_file {dt:.1f}s kind={kind} src={sp}")
        except Exception:
            n_errors += 1
            continue

        after = _json_sig(rec.get("location")) + "|" + _json_sig(rec.get("path_metadata"))
        if after != before:
            n_changed += 1
            if not dry_run:
                atomic_write_json(p, rec)

    print(
        "raid metadata backfill:",
        f"examined={n_seen}",
        f"changed={n_changed}",
        f"ffprobe_runs={n_ffprobe}",
        f"exiftool_runs={n_exiftool}",
        f"missing_source={n_missing_source}",
        f"errors={n_errors}",
        f"dry_run={dry_run}",
    )


def _primary_timeline_is_empty_or_unusable(ptd: object) -> bool:
    if ptd is None:
        return True
    if not isinstance(ptd, str) or not ptd.strip():
        return True
    raw = ptd.strip()
    day = raw[:10]
    if len(raw) >= 10 and raw[4] == ":" and raw[7] == ":":
        day = f"{raw[0:4]}-{raw[5:7]}-{raw[8:10]}"
    return not calendar_day_is_usable_for_primary(day)


def backfill_normalize_placeholder_primary_dates(*, dry_run: bool) -> None:
    """Set primary_timeline_date and date_source to null when date is unusable (0000-…, etc.)."""
    n_changed = 0
    for p in _iter_catalog_records():
        try:
            rec = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        ptd = rec.get("primary_timeline_date")
        if not _primary_timeline_is_empty_or_unusable(ptd):
            continue
        dirty = False
        if rec.get("primary_timeline_date") is not None:
            rec["primary_timeline_date"] = None
            dirty = True
        if rec.get("date_source") is not None:
            rec["date_source"] = None
            dirty = True
        if dirty:
            n_changed += 1
            if not dry_run:
                atomic_write_json(p, rec)
    print(f"normalize placeholder primary dates: changed={n_changed} dry_run={dry_run}")


def backfill_refetch_metadata_empty_primary_timeline(
    ffprobe_bin: str,
    exiftool_bin: str,
    *,
    dry_run: bool,
    max_refetch: int,
) -> None:
    """Re-extract metadata from disk for assets with empty/unusable primary_timeline_date."""
    human_ids = load_human_transcript_asset_ids(ROOT)
    n_seen = 0
    n_matched = 0
    n_refetched = 0
    n_missing_file = 0
    n_errors = 0

    for p in _iter_catalog_records():
        if max_refetch and n_refetched >= max_refetch:
            break
        n_seen += 1
        try:
            rec = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not _primary_timeline_is_empty_or_unusable(rec.get("primary_timeline_date")):
            continue
        n_matched += 1
        sp = rec.get("source_path")
        if not isinstance(sp, str) or not sp:
            continue
        src = Path(sp)
        if not src.is_file():
            n_missing_file += 1
            continue

        kind = rec.get("record_kind")
        aid = rec.get("asset_id")
        aid_s = aid if isinstance(aid, str) and len(aid) == 64 else ""

        try:
            if kind == "video":
                ff, tags = ffprobe_video_compact(str(src), ffprobe_bin)
                et = exiftool_compact(str(src), exiftool_bin)
                pm = parse_path_metadata(str(src), ffprobe_compact=ff if "_error" not in ff else None)
                mk, md = make_model_from_ffprobe_tags(tags)
                emk = et.get("camera_make") if isinstance(et, dict) and "_error" not in et else None
                emd = et.get("camera_model") if isinstance(et, dict) and "_error" not in et else None
                refine_camera_fields(pm, src.name, str(src), emk or mk, emd or md)
                ensure_dji_osmo_camera_id(str(src), pm)
                rec["ffprobe"] = ff
                rec["path_metadata"] = pm
                loc_ff = extract_location_from_tags(tags)
                loc_et = location_from_exif(et) if isinstance(et, dict) and "_error" not in et else None
                loc = pick_richer_location(loc_ff, loc_et)
                if loc:
                    rec["location"] = pick_richer_location(rec.get("location"), loc)
                pr, ds = timeline_date_fields_for_new_asset(
                    "video",
                    pm,
                    ff if isinstance(ff, dict) and "_error" not in ff else None,
                    None,
                    str(src),
                )
                rec["primary_timeline_date"] = pr
                rec["date_source"] = ds
                if aid_s:
                    rec["asset_classifications"] = build_asset_classifications(aid_s, str(src), human_ids)
                st = src.stat()
                rec["mtime"] = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat().replace("+00:00", "Z")
                rec["filesize_bytes"] = st.st_size
            elif kind == "audio":
                ff, tags = ffprobe_audio_compact(str(src), ffprobe_bin)
                pm = parse_path_metadata(str(src), ffprobe_compact=ff if "_error" not in ff else None)
                rec["ffprobe"] = ff
                rec["path_metadata"] = pm
                loc_ff = extract_location_from_tags(tags)
                if loc_ff:
                    rec["location"] = pick_richer_location(rec.get("location"), loc_ff)
                pr, ds = timeline_date_fields_for_new_asset(
                    "audio",
                    pm,
                    ff if isinstance(ff, dict) and "_error" not in ff else None,
                    None,
                    str(src),
                )
                rec["primary_timeline_date"] = pr
                rec["date_source"] = ds
                if aid_s:
                    rec["asset_classifications"] = build_asset_classifications(aid_s, str(src), human_ids)
                st = src.stat()
                rec["mtime"] = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat().replace("+00:00", "Z")
                rec["filesize_bytes"] = st.st_size
            elif kind == "still":
                et = exiftool_compact(str(src), exiftool_bin)
                pm = parse_path_metadata(str(src), ffprobe_compact=None)
                if isinstance(et, dict) and "_error" not in et:
                    rec["exif"] = et
                    refine_camera_fields(pm, src.name, str(src), et.get("camera_make"), et.get("camera_model"))
                else:
                    refine_camera_fields(pm, src.name, str(src), None, None)
                rec["path_metadata"] = pm
                loc_et = location_from_exif(et) if isinstance(et, dict) and "_error" not in et else None
                if loc_et:
                    rec["location"] = pick_richer_location(rec.get("location"), loc_et)
                pr, ds = timeline_date_fields_for_new_asset(
                    "still",
                    pm,
                    None,
                    et if isinstance(et, dict) and "_error" not in et else None,
                    str(src),
                )
                rec["primary_timeline_date"] = pr
                rec["date_source"] = ds
                st = src.stat()
                rec["mtime"] = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat().replace("+00:00", "Z")
                rec["filesize_bytes"] = st.st_size
            else:
                continue
        except Exception:
            n_errors += 1
            continue

        n_refetched += 1
        if not dry_run:
            atomic_write_json(p, rec)

    print(
        "empty-primary metadata refetch:",
        f"catalog_seen={n_seen}",
        f"matched_empty_primary={n_matched}",
        f"refetched_written={n_refetched}",
        f"missing_source_file={n_missing_file}",
        f"errors={n_errors}",
        f"dry_run={dry_run}",
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--offset", type=int, default=0, help="Skip first N catalog JSONs (deterministic order).")
    ap.add_argument("--write-new", action="store_true", help="Write new catalog JSONs for unlisted files.")
    ap.add_argument("--backfill-locations", action="store_true", help="Add location blocks to existing asset JSONs.")
    ap.add_argument(
        "--backfill-locations-all",
        action="store_true",
        help="Add location blocks to existing asset JSONs by rescanning ALL sources (slow).",
    )
    ap.add_argument(
        "--backfill-raid-metadata",
        action="store_true",
        help="Re-scan RAID files: merge GPS (ffprobe+exiftool on video), refresh path_metadata camera fields (slow).",
    )
    ap.add_argument(
        "--backfill-empty-primary-metadata",
        action="store_true",
        help="Re-run ffprobe/exiftool for rows with empty/unusable primary_timeline_date (needs source files on disk).",
    )
    ap.add_argument(
        "--normalize-placeholder-primary-dates",
        action="store_true",
        help="Set primary_timeline_date and date_source to null for placeholder/bad calendar dates (0000-…, etc.).",
    )
    ap.add_argument(
        "--max-empty-primary-refetch",
        type=int,
        default=0,
        help="With --backfill-empty-primary-metadata: max rows to refetch (0 = no limit).",
    )
    args = ap.parse_args()

    if args.write_new and not GRAND.is_dir():
        print(f"ERROR: not found: {GRAND}")
        return 2

    ffprobe_bin = find_ffprobe()
    exiftool_bin = find_exiftool()
    print("ffprobe:", ffprobe_bin)
    print("exiftool:", exiftool_bin)

    if args.write_new:
        unlisted = compute_unlisted_paths(limit=args.limit)
        print("unlisted (post exclusions):", len(unlisted))

        written = 0
        skipped_exists = 0
        for fp in unlisted:
            media = classify_media_by_ext(fp)
            if media == "unknown":
                continue
            st = fp.stat()
            aid = partial_hash(str(fp), st.st_size)
            if media == "video":
                outp = VIDEO_DIR / f"{aid}.video.json"
            elif media == "audio":
                outp = AUDIO_DIR / f"{aid}.audio.json"
            else:
                outp = STILL_DIR / f"{aid}.still.json"

            if outp.exists():
                skipped_exists += 1
                continue

            if media == "video":
                rec = build_video_record(fp, ffprobe_bin, exiftool_bin)
            elif media == "audio":
                rec = build_audio_record(fp, ffprobe_bin)
            else:
                rec = build_still_record(fp, exiftool_bin)

            if args.dry_run:
                written += 1
                continue

            atomic_write_json(outp, rec)
            written += 1

        print(f"new records: would_write={written} skipped_already_present={skipped_exists} dry_run={args.dry_run}")

    if args.backfill_locations:
        backfill_locations(ffprobe_bin=ffprobe_bin, exiftool_bin=exiftool_bin, dry_run=args.dry_run, limit=args.limit)

    if args.backfill_locations_all:
        backfill_locations_all(
            ffprobe_bin=ffprobe_bin,
            exiftool_bin=exiftool_bin,
            dry_run=args.dry_run,
            limit=args.limit,
        )

    if args.backfill_raid_metadata:
        backfill_raid_metadata(
            ffprobe_bin=ffprobe_bin,
            exiftool_bin=exiftool_bin,
            dry_run=args.dry_run,
            offset=args.offset,
            limit=args.limit,
        )

    if args.normalize_placeholder_primary_dates:
        backfill_normalize_placeholder_primary_dates(dry_run=args.dry_run)

    if args.backfill_empty_primary_metadata:
        backfill_refetch_metadata_empty_primary_timeline(
            ffprobe_bin=ffprobe_bin,
            exiftool_bin=exiftool_bin,
            dry_run=args.dry_run,
            max_refetch=args.max_empty_primary_refetch,
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

