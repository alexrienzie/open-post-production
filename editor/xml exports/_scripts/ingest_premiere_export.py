#!/usr/bin/env python3
"""Ingest a raw Premiere FCP7 XML export into the workspace-canonical form.

Performs four transformations in one pass:
  1) Pathurl rewrite: source media -> the workspace derivative-media proxy
     (looks up the right proxy_kind per asset_type: video_video_proxy for
     video, audio_audio_proxy for audio, still_still_proxy for stills).
  2) Placeholder creation: for assets not in the catalog (SFX library,
     a few missing camera-card files), creates a stub media file at a
     path mirroring your workstation layout under derivative media/ so the
     director can later "Link Media" against the RAID with matching
     folder layout. Stub content depends on extension (silent audio,
     1-frame black video, 1x1 jpg).
  3) Frame-size sync: sequence <format> and every <file>'s video
     samplecharacteristics rewritten from source (often 3840x2160) to
     proxy resolution (1280x720). Sequence MZ preview attrs also updated.
  4) Truncate (optional): drop clipitems/transitions starting at/after
     `--truncate-at-frame N`, update sequence <duration>.

Outputs a new timestamped xml alongside the source (never overwrites).

Usage:
    py editor/xml exports/_scripts/ingest_premiere_export.py \
        --xml "editor/xml exports/project_act I_premiere export_20260521.xml" \
        --truncate-at-frame 64102

Run with --dry-run first to see the change summary without writes.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from lxml import etree

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))
import _pproticks as ticks  # noqa: E402

REPO = _HERE.parent.parent.parent
CATALOG_DB = REPO / "indexes" / "editorial_catalog.sqlite"
ASSET_MAP_JSON = REPO / "derivative media" / "_index" / "asset_map.json"
DERIVATIVE_MEDIA_ROOT = REPO / "derivative media"
DERIVATIVE_MEDIA_PATHURL_ROOT = r"E:\open-post-stack\derivative media"
SOURCE_TREE_DIRNAME = "Project"  # the folder your camera originals live under on the source drive

PROXY_WIDTH = 1280
PROXY_HEIGHT = 720

# Extension -> ordered list of proxy_kinds to try
EXT_TO_PROXY_KINDS = {
    "mp4": ["video_video_proxy"],
    "mov": ["video_video_proxy"],
    "r3d": ["video_video_proxy"],
    "m4v": ["video_video_proxy"],
    "wav": ["audio_audio_proxy", "video_audio_proxy"],
    "m4a": ["audio_audio_proxy"],
    "mp3": ["audio_audio_proxy"],
    "jpg": ["still_still_proxy"],
    "jpeg": ["still_still_proxy"],
    "heic": ["still_still_proxy"],
    "png": ["still_still_proxy"],
}


# ---------------------------------------------------------------------------
# Catalog + asset_map loading

def _load_catalog_by_relpath() -> dict[str, tuple[str, str, str]]:
    """rel_under_grand_project (lowercase, fwd-slash) -> (asset_id, source_path, filename)."""
    out: dict[str, tuple[str, str, str]] = {}
    con = sqlite3.connect(str(CATALOG_DB))
    for aid, sp, fn in con.execute("SELECT asset_id, source_path, filename FROM asset"):
        if sp and SOURCE_TREE_DIRNAME in sp:
            parts = sp.replace("\\", "/").split(SOURCE_TREE_DIRNAME + "/", 1)
            if len(parts) == 2:
                out[parts[1].lower()] = (aid, sp, fn)
    con.close()
    return out


def _load_asset_map() -> dict:
    with open(ASSET_MAP_JSON, "r", encoding="utf-8") as fh:
        return json.load(fh)["entries"]


# ---------------------------------------------------------------------------
# Pathurl resolution

def _pathurl_to_decoded_tail(purl: str) -> Optional[str]:
    """Return the '<source tree>/...' tail (lowercased) or None."""
    decoded = urllib.parse.unquote(purl)
    if SOURCE_TREE_DIRNAME + "/" in decoded:
        return decoded.split(SOURCE_TREE_DIRNAME + "/", 1)[1].lower()
    return None


def _ext_of(purl_or_path: str) -> str:
    decoded = urllib.parse.unquote(purl_or_path)
    if "." in decoded:
        return decoded.rsplit(".", 1)[-1].lower()
    return ""


def resolve_pathurl(
    purl: str,
    by_relpath: dict,
    asset_map: dict,
) -> Optional[dict]:
    """Return resolution result or None if asset not in catalog.

    Result dict: {asset_id, proxy_relative_path, proxy_kind, source_filename}
    If asset is in catalog but no matching proxy_kind found, proxy_relative_path
    is None (caller decides whether to use a placeholder).
    """
    decoded_tail = _pathurl_to_decoded_tail(purl)
    if not decoded_tail or decoded_tail not in by_relpath:
        return None
    aid, sp, fn = by_relpath[decoded_tail]
    ext = _ext_of(purl)
    pm = asset_map.get(aid, {}) or {}

    for kind in EXT_TO_PROXY_KINDS.get(ext, ["video_video_proxy"]):
        v = pm.get(kind)
        if isinstance(v, dict) and v.get("relative_path"):
            return {
                "asset_id": aid,
                "proxy_relative_path": v["relative_path"],
                "proxy_kind": kind,
                "source_filename": fn,
                "catalog_source_path": sp,
            }

    # Asset is catalogued but the expected proxy kind is missing.
    return {
        "asset_id": aid,
        "proxy_relative_path": None,
        "proxy_kind": None,
        "source_filename": fn,
        "catalog_source_path": sp,
    }


# Characters illegal in Windows filenames (path separator / and \ excluded since
# they're legitimate). Mac/HFS allows ':' in directory names but NTFS does not,
# and the rest are NTFS-forbidden.
_NTFS_FORBIDDEN = ':*?"<>|'
_NTFS_FORBIDDEN_TABLE = str.maketrans({c: "_" for c in _NTFS_FORBIDDEN})


def _sanitize_for_windows(path_tail: str) -> str:
    """Replace NTFS-forbidden chars (:, *, ?, ", <, >, |) with '_' while keeping
    path separators intact. Required for mirroring Mac paths under E:\\."""
    return path_tail.translate(_NTFS_FORBIDDEN_TABLE)


def _path_tail_after_grand_project(purl: str) -> str:
    """For unresolved files, get the natural mirror path under derivative media\\.
    Sanitizes NTFS-illegal characters in folder/file names."""
    decoded = urllib.parse.unquote(purl)
    if SOURCE_TREE_DIRNAME + "/" in decoded:
        tail = decoded.split(SOURCE_TREE_DIRNAME + "/", 1)[1]
    else:
        tail = decoded.rsplit("/", 1)[-1]
    return _sanitize_for_windows(tail)


# ---------------------------------------------------------------------------
# Placeholder media creation

def _ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def create_placeholder(target_path: Path, ext: str, *, dry_run: bool = False) -> str:
    """Create a placeholder media file at target_path. Returns status string."""
    target_path.parent.mkdir(parents=True, exist_ok=True)
    if target_path.exists() and target_path.stat().st_size > 0:
        return "exists"
    if dry_run:
        return "would-create"

    ext = ext.lower()
    use_ffmpeg = _ffmpeg_available()

    if ext in {"wav"} and use_ffmpeg:
        cmd = ["ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
               "-t", "1", str(target_path)]
    elif ext in {"m4a"} and use_ffmpeg:
        cmd = ["ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
               "-t", "1", "-c:a", "aac", str(target_path)]
    elif ext in {"mp3"} and use_ffmpeg:
        cmd = ["ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
               "-t", "1", "-c:a", "libmp3lame", str(target_path)]
    elif ext in {"mp4", "mov", "m4v"} and use_ffmpeg:
        cmd = ["ffmpeg", "-y", "-f", "lavfi", "-i", f"color=black:s={PROXY_WIDTH}x{PROXY_HEIGHT}:r=24000/1001",
               "-t", "1", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(target_path)]
    elif ext == "png":
        # 1x1 transparent PNG (89 bytes)
        png_bytes = bytes([
            0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A, 0x00, 0x00, 0x00, 0x0D,
            0x49, 0x48, 0x44, 0x52, 0x00, 0x00, 0x00, 0x01, 0x00, 0x00, 0x00, 0x01,
            0x08, 0x06, 0x00, 0x00, 0x00, 0x1F, 0x15, 0xC4, 0x89, 0x00, 0x00, 0x00,
            0x0D, 0x49, 0x44, 0x41, 0x54, 0x78, 0x9C, 0x62, 0x00, 0x01, 0x00, 0x00,
            0x05, 0x00, 0x01, 0x0D, 0x0A, 0x2D, 0xB4, 0x00, 0x00, 0x00, 0x00, 0x49,
            0x45, 0x4E, 0x44, 0xAE, 0x42, 0x60, 0x82,
        ])
        target_path.write_bytes(png_bytes)
        return "created-png-stub"
    elif ext in {"jpg", "jpeg"}:
        # 1x1 black JPG bytes (no external tool needed)
        jpg_bytes = bytes([
            0xFF, 0xD8, 0xFF, 0xE0, 0x00, 0x10, 0x4A, 0x46, 0x49, 0x46, 0x00, 0x01,
            0x01, 0x00, 0x00, 0x01, 0x00, 0x01, 0x00, 0x00, 0xFF, 0xDB, 0x00, 0x43,
            0x00, 0x08, 0x06, 0x06, 0x07, 0x06, 0x05, 0x08, 0x07, 0x07, 0x07, 0x09,
            0x09, 0x08, 0x0A, 0x0C, 0x14, 0x0D, 0x0C, 0x0B, 0x0B, 0x0C, 0x19, 0x12,
            0x13, 0x0F, 0x14, 0x1D, 0x1A, 0x1F, 0x1E, 0x1D, 0x1A, 0x1C, 0x1C, 0x20,
            0x24, 0x2E, 0x27, 0x20, 0x22, 0x2C, 0x23, 0x1C, 0x1C, 0x28, 0x37, 0x29,
            0x2C, 0x30, 0x31, 0x34, 0x34, 0x34, 0x1F, 0x27, 0x39, 0x3D, 0x38, 0x32,
            0x3C, 0x2E, 0x33, 0x34, 0x32, 0xFF, 0xC0, 0x00, 0x0B, 0x08, 0x00, 0x01,
            0x00, 0x01, 0x01, 0x01, 0x11, 0x00, 0xFF, 0xC4, 0x00, 0x1F, 0x00, 0x00,
            0x01, 0x05, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x00, 0x00, 0x00, 0x00,
            0x00, 0x00, 0x00, 0x00, 0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07, 0x08,
            0x09, 0x0A, 0x0B, 0xFF, 0xC4, 0x00, 0xB5, 0x10, 0x00, 0x02, 0x01, 0x03,
            0x03, 0x02, 0x04, 0x03, 0x05, 0x05, 0x04, 0x04, 0x00, 0x00, 0x01, 0x7D,
            0x01, 0x02, 0x03, 0x00, 0x04, 0x11, 0x05, 0x12, 0x21, 0x31, 0x41, 0x06,
            0x13, 0x51, 0x61, 0x07, 0x22, 0x71, 0x14, 0x32, 0x81, 0x91, 0xA1, 0x08,
            0x23, 0x42, 0xB1, 0xC1, 0x15, 0x52, 0xD1, 0xF0, 0x24, 0x33, 0x62, 0x72,
            0x82, 0x09, 0x0A, 0x16, 0x17, 0x18, 0x19, 0x1A, 0x25, 0x26, 0x27, 0x28,
            0x29, 0x2A, 0x34, 0x35, 0x36, 0x37, 0x38, 0x39, 0x3A, 0x43, 0x44, 0x45,
            0x46, 0x47, 0x48, 0x49, 0x4A, 0x53, 0x54, 0x55, 0x56, 0x57, 0x58, 0x59,
            0x5A, 0x63, 0x64, 0x65, 0x66, 0x67, 0x68, 0x69, 0x6A, 0x73, 0x74, 0x75,
            0x76, 0x77, 0x78, 0x79, 0x7A, 0x83, 0x84, 0x85, 0x86, 0x87, 0x88, 0x89,
            0x8A, 0x92, 0x93, 0x94, 0x95, 0x96, 0x97, 0x98, 0x99, 0x9A, 0xA2, 0xA3,
            0xA4, 0xA5, 0xA6, 0xA7, 0xA8, 0xA9, 0xAA, 0xB2, 0xB3, 0xB4, 0xB5, 0xB6,
            0xB7, 0xB8, 0xB9, 0xBA, 0xC2, 0xC3, 0xC4, 0xC5, 0xC6, 0xC7, 0xC8, 0xC9,
            0xCA, 0xD2, 0xD3, 0xD4, 0xD5, 0xD6, 0xD7, 0xD8, 0xD9, 0xDA, 0xE1, 0xE2,
            0xE3, 0xE4, 0xE5, 0xE6, 0xE7, 0xE8, 0xE9, 0xEA, 0xF1, 0xF2, 0xF3, 0xF4,
            0xF5, 0xF6, 0xF7, 0xF8, 0xF9, 0xFA, 0xFF, 0xDA, 0x00, 0x08, 0x01, 0x01,
            0x00, 0x00, 0x3F, 0x00, 0xFB, 0xD0, 0xFF, 0xD9,
        ])
        target_path.write_bytes(jpg_bytes)
        return "created-jpg-stub"
    else:
        # Last resort: 0-byte file; Premiere will mark offline.
        target_path.write_bytes(b"")
        return "created-empty"

    try:
        result = subprocess.run(cmd, capture_output=True, timeout=30)
        if result.returncode != 0:
            return f"ffmpeg-failed:{result.returncode}"
        return "created-ffmpeg"
    except Exception as e:
        return f"ffmpeg-error:{e}"


# ---------------------------------------------------------------------------
# XML transforms

def windows_path_to_pathurl(win_path: str) -> str:
    return ticks.windows_path_to_pathurl(win_path)


def proxy_relpath_to_pathurl(relpath: str) -> str:
    return windows_path_to_pathurl(DERIVATIVE_MEDIA_PATHURL_ROOT + "\\" + relpath.replace("/", "\\").lstrip("\\"))


def _probe_video_dims(filesystem_path: Path, cache: dict) -> Optional[tuple[int, int]]:
    """Run ffprobe to read the first video stream's dimensions. Cached by path."""
    key = str(filesystem_path)
    if key in cache:
        return cache[key]
    if not filesystem_path.exists():
        cache[key] = None
        return None
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height", "-of", "csv=p=0",
             str(filesystem_path)],
            capture_output=True, text=True, timeout=10,
        )
        line = (out.stdout or "").strip().splitlines()[0] if out.stdout else ""
        parts = line.split(",") if line else []
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            w, h = int(parts[0]), int(parts[1])
            if w > 0 and h > 0:
                cache[key] = (w, h)
                return (w, h)
    except Exception:
        pass
    cache[key] = None
    return None


def rewrite_pathurls(
    seq,
    by_relpath: dict,
    asset_map: dict,
    *,
    dry_run: bool,
) -> dict:
    """Rewrite every <file>'s <pathurl>. Return stats + placeholder plan.

    Video samplecharacteristics are updated by probing the actual proxy file
    (ffprobe). This keeps landscape video proxies at 1280x720 while preserving
    portrait phone proxies (720x1280) and other non-standard sizes.

    Stills' dimensions in their <file>'s <media><video> block are also
    updated to match the proxy (stills proxies are often a different size
    than the source, e.g. 8192x4320 source -> 4718x7337 jpg proxy).
    """
    stats = {
        "rewrote_to_proxy": 0,
        "already_pointed_at_proxy": 0,
        "placeholder_needed": 0,
        "by_kind": {},
        "dim_updates": 0,
        "dim_probe_failures": 0,
    }
    placeholders: list[dict] = []
    rewrites: list[dict] = []
    probe_cache: dict = {}

    def _set_dims_from_proxy(fdef, fs_path: Path) -> None:
        """Probe proxy file and update file def's video samplecharacteristics."""
        nonlocal stats
        vid_sc = fdef.find("media/video/samplecharacteristics")
        if vid_sc is None:
            return  # audio-only file def, no video block
        dims = _probe_video_dims(fs_path, probe_cache)
        if dims is None:
            stats["dim_probe_failures"] += 1
            return
        w_el = vid_sc.find("width")
        h_el = vid_sc.find("height")
        new_w, new_h = str(dims[0]), str(dims[1])
        if (w_el is not None and w_el.text != new_w) or (h_el is not None and h_el.text != new_h):
            if not dry_run:
                if w_el is not None:
                    w_el.text = new_w
                if h_el is not None:
                    h_el.text = new_h
            stats["dim_updates"] += 1

    def _proxy_relpath_to_fs(relpath: str) -> Path:
        return DERIVATIVE_MEDIA_ROOT / relpath

    for fdef in seq.findall(".//file"):
        purl_el = fdef.find("pathurl")
        if purl_el is None or not purl_el.text:
            continue
        purl = purl_el.text
        if "E%3a/open-post-stack/derivative%20media" in purl or "E%3A/open-post-stack/derivative%20media" in purl:
            stats["already_pointed_at_proxy"] += 1
            # Trust existing dims (Act II case — already correct from prior ingest).
            continue

        ext = _ext_of(purl)
        res = resolve_pathurl(purl, by_relpath, asset_map)
        if res and res["proxy_relative_path"]:
            new_purl = proxy_relpath_to_pathurl(res["proxy_relative_path"])
            rewrites.append({"old": purl, "new": new_purl, "kind": res["proxy_kind"]})
            stats["rewrote_to_proxy"] += 1
            stats["by_kind"][res["proxy_kind"]] = stats["by_kind"].get(res["proxy_kind"], 0) + 1
            if not dry_run:
                purl_el.text = new_purl
            # Probe + update dims from the actual proxy file.
            _set_dims_from_proxy(fdef, _proxy_relpath_to_fs(res["proxy_relative_path"]))
        else:
            # Placeholder: mirror the source-tree path under derivative media.
            tail = _path_tail_after_grand_project(purl)
            target_relpath = tail
            target_path = DERIVATIVE_MEDIA_ROOT / target_relpath
            new_purl = proxy_relpath_to_pathurl(target_relpath)
            placeholders.append({
                "old_pathurl": purl,
                "new_pathurl": new_purl,
                "target_path": str(target_path),
                "extension": ext,
                "reason": "catalog-miss" if res is None else "no-proxy-kind",
            })
            stats["placeholder_needed"] += 1
            if not dry_run:
                purl_el.text = new_purl
            # For placeholders, set dims to 1280x720 if video extension, else leave.
            if ext in {"mp4", "mov", "m4v"}:
                vid_sc = fdef.find("media/video/samplecharacteristics")
                if vid_sc is not None and not dry_run:
                    w_el = vid_sc.find("width")
                    h_el = vid_sc.find("height")
                    if w_el is not None:
                        w_el.text = str(PROXY_WIDTH)
                    if h_el is not None:
                        h_el.text = str(PROXY_HEIGHT)
                    stats["dim_updates"] += 1

    return {"stats": stats, "rewrites": rewrites, "placeholders": placeholders}


def update_sequence_format(seq, *, dry_run: bool) -> dict:
    """Set sequence <format> samplecharacteristics + MZ preview attrs to 1280x720."""
    changed = {}
    sc = seq.find("media/video/format/samplecharacteristics")
    if sc is not None:
        w = sc.find("width")
        h = sc.find("height")
        if w is not None:
            changed["sequence_width"] = (w.text, str(PROXY_WIDTH))
            if not dry_run:
                w.text = str(PROXY_WIDTH)
        if h is not None:
            changed["sequence_height"] = (h.text, str(PROXY_HEIGHT))
            if not dry_run:
                h.text = str(PROXY_HEIGHT)

    # MZ.Sequence.PreviewFrameSizeHeight / Width
    for attr, val in [
        ("MZ.Sequence.PreviewFrameSizeWidth", str(PROXY_WIDTH)),
        ("MZ.Sequence.PreviewFrameSizeHeight", str(PROXY_HEIGHT)),
    ]:
        old = seq.get(attr)
        if old is not None and old != val:
            changed[attr] = (old, val)
            if not dry_run:
                seq.set(attr, val)
    return changed


def prune_dangling_links(seq, *, dry_run: bool) -> int:
    """Remove <link> elements whose <linkclipref> points at a clipitem id
    that doesn't exist in the sequence. Premiere refuses to import an xmeml
    with dangling linkclipref. Pre-existing source XMLs often carry these
    (clipitems deleted in Premiere without link cleanup); we always run this
    pass for safety. Returns count pruned."""
    valid_ids = {ci.get("id") for ci in seq.findall(".//clipitem") if ci.get("id")}
    pruned = 0
    for ln in list(seq.findall(".//link")):
        ref = ln.findtext("linkclipref")
        if ref and ref not in valid_ids:
            if not dry_run:
                parent = ln.getparent()
                if parent is not None:
                    parent.remove(ln)
            pruned += 1
    return pruned


def truncate_at_frame(seq, cutoff_frame: int, *, dry_run: bool) -> dict:
    """Drop clipitems with start >= cutoff_frame and transitionitems crossing
    cutoff. Update sequence <duration>. (Dangling-link cleanup is done by a
    separate unconditional pass after this — see prune_dangling_links().)"""
    stats = {"clipitems_dropped": 0, "transitionitems_dropped": 0, "tracks": 0}
    dropped_ids: set[str] = set()

    def _process_track(track):
        nonlocal stats
        for ci in list(track.findall("clipitem")):
            s_text = ci.findtext("start")
            if s_text is None:
                continue
            try:
                s = int(s_text)
            except ValueError:
                continue
            drop = False
            if s == -1:
                e_text = ci.findtext("end")
                try:
                    e = int(e_text) if e_text is not None else -1
                except ValueError:
                    e = -1
                if e >= cutoff_frame:
                    drop = True
            elif s >= cutoff_frame:
                drop = True
            if drop:
                cid = ci.get("id")
                if cid:
                    dropped_ids.add(cid)
                if not dry_run:
                    track.remove(ci)
                stats["clipitems_dropped"] += 1
        for ti in list(track.findall("transitionitem")):
            s_text = ti.findtext("start")
            e_text = ti.findtext("end")
            try:
                ts = int(s_text) if s_text is not None else -1
                te = int(e_text) if e_text is not None else -1
            except ValueError:
                continue
            if ts >= cutoff_frame or te > cutoff_frame:
                if not dry_run:
                    track.remove(ti)
                stats["transitionitems_dropped"] += 1

    for t in seq.findall("media/video/track"):
        stats["tracks"] += 1
        _process_track(t)
    for t in seq.findall("media/audio/track"):
        stats["tracks"] += 1
        _process_track(t)

    dur_el = seq.find("duration")
    if dur_el is not None:
        old = dur_el.text
        if not dry_run:
            dur_el.text = str(cutoff_frame)
        stats["duration_was"] = old
        stats["duration_now"] = str(cutoff_frame)
    return stats


# ---------------------------------------------------------------------------
# Safe atomic write

def _atomic_safe_write(path: Path, data: bytes) -> None:
    sha_expected = hashlib.sha256(data).hexdigest()
    with tempfile.NamedTemporaryFile(prefix=path.stem + "_", suffix=path.suffix, delete=False) as tmp:
        tmp.write(data)
        tmp_path = Path(tmp.name)
    last_err = None
    try:
        for attempt in range(1, 4):
            shutil.copyfile(tmp_path, path)
            try:
                with path.open("rb") as fh:
                    on_disk = fh.read()
            except Exception as e:
                last_err = e
                time.sleep(0.5)
                continue
            sha_actual = hashlib.sha256(on_disk).hexdigest()
            if sha_actual == sha_expected and len(on_disk) == len(data):
                return
            last_err = RuntimeError(
                f"sha mismatch attempt {attempt}: expected len={len(data)}, got len={len(on_disk)}"
            )
            time.sleep(0.5)
        raise RuntimeError(f"safe_write failed after 3 attempts: {last_err}")
    finally:
        try:
            tmp_path.unlink()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Main

def run(
    xml_path: Path,
    *,
    output_path: Optional[Path] = None,
    truncate_at_frame_n: Optional[int] = None,
    dry_run: bool = False,
    report_json: Optional[Path] = None,
) -> int:
    print(f"[input ] {xml_path.name}")
    by_relpath = _load_catalog_by_relpath()
    asset_map = _load_asset_map()

    parser = etree.XMLParser(remove_blank_text=False, strip_cdata=False)
    tree = etree.parse(str(xml_path), parser)
    seq = tree.getroot().find("sequence")

    # Step 1+2: pathurl rewrites + placeholder identification + per-file dim update
    rewrite_result = rewrite_pathurls(seq, by_relpath, asset_map, dry_run=dry_run)
    stats = rewrite_result["stats"]
    print(f"[paths ] rewrote_to_proxy={stats['rewrote_to_proxy']}  "
          f"already_proxy={stats['already_pointed_at_proxy']}  "
          f"placeholder_needed={stats['placeholder_needed']}")
    for kind, n in sorted(stats["by_kind"].items()):
        print(f"           by kind: {kind:24s} {n}")

    # Step 3: sequence format + preview attrs
    seq_changes = update_sequence_format(seq, dry_run=dry_run)
    print(f"[seqfmt] {seq_changes}")

    # Step 4: truncate
    if truncate_at_frame_n is not None:
        trunc = truncate_at_frame(seq, truncate_at_frame_n, dry_run=dry_run)
        print(f"[trunc ] cutoff_frame={truncate_at_frame_n}  {trunc}")
    else:
        trunc = None

    # Step 4b: unconditional safety pass — strip dangling <link> refs.
    # Premiere refuses to import xmeml with any <linkclipref> pointing at a
    # nonexistent clipitem id, whether the dangle came from our truncation or
    # was pre-existing in the source export (deletions in Premiere don't
    # always clean their links). Cheap, always safe.
    dangling_pruned = prune_dangling_links(seq, dry_run=dry_run)
    if dangling_pruned:
        print(f"[links ] pruned {dangling_pruned} dangling <link> ref(s)")

    # Step 5: create placeholder media on disk (mirror Mac path under derivative media)
    placeholder_results = []
    for ph in rewrite_result["placeholders"]:
        status = create_placeholder(Path(ph["target_path"]), ph["extension"], dry_run=dry_run)
        placeholder_results.append({**ph, "status": status})
    if placeholder_results:
        verb = "would-create" if dry_run else "processed"
        print(f"[placeholders] {verb} {len(placeholder_results)} (details in report.json)")

    # Build output path. Match convention: "project_act I_<timestamp>_ingested.xml".
    if output_path is None:
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M")
        name = (seq.findtext("name") or "sequence").strip()
        # Lowercase "Act I" -> "act I" to match insert_video_clips.py style
        safe = name.replace("Act ", "act ")
        output_path = xml_path.parent / f"ingested_{safe}_{ts}.xml"

    # Premiere's xmeml uses mixed empty-tag conventions:
    #   - REFERENCE STUBS (empty element with an `id` attribute like
    #     <file id="X"/> or <sequence id="X"/>) MUST stay self-closing.
    #     These point at a prior full definition; Premiere parses them as
    #     refs, not as empty containers.
    #   - EMPTY CONTENT ELEMENTS (<description>, <scene>, <lognote>,
    #     <lut>, <good>, etc.) MUST use open/close form `<tag></tag>`.
    #     lxml's serializer defaults these to self-closing, which Premiere
    #     rejects with a silent "File Import Failure".
    # Detection rule: presence of an `id` attribute identifies a ref stub.
    # Verified by cataloging every self-closing tag in three known-good
    # Premiere exports — only <file id="X"/> and <sequence id="X"/> appear.
    n_forced = 0
    for el in tree.iter():
        if el.text is None and len(el) == 0:
            if "id" in el.attrib:
                continue  # reference stub — leave self-closing
            el.text = ""
            n_forced += 1
    if n_forced:
        print(f"[empty ] forced open/close form on {n_forced} empty elements "
              f"(ref-stub elements with id= left self-closing)")

    # Serialize. NOTE: lxml's xml_declaration=True emits single-quoted
    # `<?xml version='1.0' encoding='UTF-8'?>` and Premiere's xmeml parser
    # rejects that. Emit the declaration manually with double quotes.
    body = etree.tostring(
        tree, xml_declaration=False, encoding="UTF-8", doctype='<!DOCTYPE xmeml>'
    )
    serialized = b'<?xml version="1.0" encoding="UTF-8"?>\n' + body

    if not dry_run:
        _atomic_safe_write(output_path, serialized)
        print(f"[write ] {output_path}  ({len(serialized)} bytes)")
    else:
        print(f"[dry-run] would write {output_path}  ({len(serialized)} bytes)")

    # Always write a report
    report = {
        "xml_source": str(xml_path),
        "xml_output": str(output_path),
        "dry_run": dry_run,
        "pathurl_stats": stats,
        "rewrites_sample": rewrite_result["rewrites"][:10],
        "placeholders": placeholder_results,
        "sequence_format_changes": seq_changes,
        "truncate": trunc,
        "truncate_at_frame": truncate_at_frame_n,
        "dangling_links_pruned": dangling_pruned,
    }
    if report_json is None:
        # Report lives next to OUTPUT (not source — source may be in archive/).
        report_json = output_path.parent / "_plans" / (output_path.stem + "_report.json")
    report_json.parent.mkdir(parents=True, exist_ok=True)
    with open(report_json, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2)
    print(f"[report] {report_json}")

    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--xml", required=True, type=Path)
    p.add_argument("--output", type=Path, default=None)
    p.add_argument("--truncate-at-frame", type=int, default=None)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--report-json", type=Path, default=None)
    args = p.parse_args(argv)
    return run(
        args.xml,
        output_path=args.output,
        truncate_at_frame_n=args.truncate_at_frame,
        dry_run=args.dry_run,
        report_json=args.report_json,
    )


if __name__ == "__main__":
    sys.exit(main())
