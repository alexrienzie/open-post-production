#!/usr/bin/env python3
"""Backfill full audio_extract metadata via ffprobe.

Walks all video/audio asset JSONs under dataset/assets/, finds entries
whose audio_extract block is a minimal stub (missing duration_sec,
sample_rate, channels, codec, filesize_bytes, or bit_rate), runs ffprobe on
the extracted audio file, and rewrites the audio_extract dict with the full
metadata.

Preserves non-ffprobe fields: path, ffmpeg_command_hash,
source_ssd_path_at_extraction, extracted_at, etc.

Usage:
  py backfill_audio_extract_metadata.py [--dry-run] [--limit N]

Run from a machine that has the derivative media tree mounted (e.g. Dell with
E:\open-post-stack\). Resolves ~/derivative media/ to the actual derivative media
folder via workspace_paths.derivative_media_root().
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[2]
CATALOG = ROOT / "assets" / "catalog"

# Fields that must be present (and non-null/non-zero where numeric) for
# audio_extract to be considered "complete." Anything missing → re-probe.
REQUIRED = ("duration_sec", "sample_rate", "channels", "codec", "filesize_bytes")


def derivative_media_root() -> Path:
    """Resolve derivative media root from workspace_paths if available, else
    fall back to E:\\open-post-stack\\derivative media\\."""
    try:
        sys.path.insert(0, str(ROOT / "_scripts"))
        from workspace_paths import derivative_media_root as _dmr
        return Path(_dmr())
    except Exception:
        return ROOT.parent / "derivative media"


def resolve_audio_path(audio_extract_path: str, dm_root: Path) -> Optional[Path]:
    """Convert '~/derivative media/foo.wav' or absolute path to a real Path."""
    if not audio_extract_path:
        return None
    if audio_extract_path.startswith("~/derivative media/"):
        rel = audio_extract_path[len("~/derivative media/"):]
        return dm_root / rel
    p = Path(audio_extract_path)
    return p if p.is_absolute() else dm_root / p


def is_stub(ae: dict) -> bool:
    for k in REQUIRED:
        v = ae.get(k)
        if v is None:
            return True
        if isinstance(v, (int, float)) and v == 0:
            return True
    return False


def ffprobe_audio(path: Path) -> Optional[dict]:
    """Return ffprobe-derived metadata dict, or None if probe fails."""
    cmd = [
        "ffprobe", "-v", "error", "-show_format", "-show_streams",
        "-of", "json", str(path),
    ]
    try:
        out = subprocess.check_output(cmd, stderr=subprocess.STDOUT, timeout=30)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        return None
    try:
        data = json.loads(out.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        return None

    fmt = data.get("format") or {}
    streams = data.get("streams") or []
    # Pick the first audio stream
    aud = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if not aud:
        return None

    def _f(x):
        try:
            return float(x)
        except (TypeError, ValueError):
            return None

    def _i(x):
        try:
            return int(x)
        except (TypeError, ValueError):
            return None

    return {
        "format": (fmt.get("format_name") or "").split(",")[0] or None,
        "duration_sec": _f(fmt.get("duration") or aud.get("duration")),
        "sample_rate": _i(aud.get("sample_rate")),
        "channels": _i(aud.get("channels")),
        "codec": aud.get("codec_name"),
        "bit_rate": _i(aud.get("bit_rate") or fmt.get("bit_rate")),
        "filesize_bytes": path.stat().st_size if path.exists() else None,
    }


def atomic_write(p: Path, data: dict):
    """Atomic JSON write — temp file in same dir, fsync, rename."""
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=p.stem + ".", suffix=".tmp", dir=str(p.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        shutil.move(tmp, p)
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=None, help="cap on # files to process")
    args = ap.parse_args()

    dm_root = derivative_media_root()
    if not dm_root.exists():
        print(f"derivative media root not found: {dm_root}", file=sys.stderr)
        return 2

    candidates = []
    for sub in ("video", "audio"):
        for p in (CATALOG / sub).glob("*.json"):
            try:
                rec = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                continue
            ae = rec.get("audio_extract")
            if not ae:
                continue
            if is_stub(ae):
                candidates.append((p, rec, ae))

    print(f"found {len(candidates)} asset JSONs with stub audio_extract")
    if args.limit:
        candidates = candidates[: args.limit]

    n_probed, n_updated, n_missing_file, n_probe_failed = 0, 0, 0, 0
    for p, rec, ae in candidates:
        n_probed += 1
        audio_path = resolve_audio_path(ae.get("path", ""), dm_root)
        if not audio_path or not audio_path.exists():
            n_missing_file += 1
            continue
        probed = ffprobe_audio(audio_path)
        if not probed:
            n_probe_failed += 1
            continue
        merged = dict(ae)
        for k, v in probed.items():
            if v is not None:
                merged[k] = v
        if merged == ae:
            continue
        rec["audio_extract"] = merged
        if not args.dry_run:
            atomic_write(p, rec)
        n_updated += 1
        if n_updated % 100 == 0:
            print(f"  {n_updated} updated...")

    print(f"\nsummary:")
    print(f"  candidates probed: {n_probed}")
    print(f"  updated:           {n_updated}{' (dry-run)' if args.dry_run else ''}")
    print(f"  missing audio:     {n_missing_file}")
    print(f"  probe failed:      {n_probe_failed}")
    return 0 if n_probe_failed == 0 and n_missing_file == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
