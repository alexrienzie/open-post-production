#!/usr/bin/env python3
"""rebuild_asset_map.py — Walk catalog JSON + `derivative media/` and rebuild
`derivative media/_index/asset_map.json` from disk.

The asset_map maps asset_id → {kind: {relative_path, size_bytes, ...}} so the
production runners and K-layer scripts can resolve a proxy or WAV on-disk
without scanning per-call.

Run this whenever new proxies / WAVs land — typically after `make_proxies.py`,
`extract_audio.py`, or an ad-hoc ingest (the Pardon-clip one-off, etc.). It's
idempotent and safe to re-run.

Kinds tracked (mirroring the existing map):
  video_video_proxy  — `derivative media/<shoot>/<file>.mp4`
  video_audio_proxy  — `derivative media/<shoot>/<stem>.wav` (extracted from video)
  audio_audio_proxy  — same path for audio-only catalog records
  still_still_proxy  — `derivative media/<shoot>/<file>.<ext>` for stills

Each entry inherits `catalog_source_path` from the catalog JSON for traceability.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))  # shared modules live at _scripts root
from _paths import (  # noqa: E402
    ASSET_MAP, DERIVATIVE_MEDIA, VIDEO_CATALOG, AUDIO_CATALOG, STILLS_CATALOG,
    iter_catalog_jsons, derivative_relative,
)


def workspace_rel(p: Path) -> str:
    """Convert absolute path under DERIVATIVE_MEDIA to backslash-relative form
    matching the existing map convention."""
    rel = p.relative_to(DERIVATIVE_MEDIA)
    return str(rel).replace("/", "\\")


def _entry_for(p: Path, catalog_source_path: str, stamp: str) -> dict | None:
    if not p.exists():
        return None
    try:
        size = p.stat().st_size
    except OSError:
        return None
    return {
        "relative_path": workspace_rel(p),
        "md5": None,
        "size_bytes": size,
        "embedded_asset_id_in": "ffmpeg comment",
        "catalog_source_path": catalog_source_path,
        "last_migrated_utc": f"(rebuilt from filesystem {stamp})",
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--dry-run", action="store_true", help="don't write")
    ap.add_argument("--replace", action="store_true",
                    help="overwrite existing map entirely (default: merge "
                         "rediscovered entries into the current map, "
                         "preserving anything that didn't resurface from disk)")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M")
    entries: dict[str, dict] = {}
    counts = {
        "video_seen": 0, "video_proxy": 0, "video_wav": 0,
        "audio_seen": 0, "audio_wav": 0,
        "still_seen": 0, "still_proxy": 0,
        "no_source_path": 0,
    }

    # Videos
    for jp in iter_catalog_jsons(VIDEO_CATALOG, ".video.json"):
        try:
            rec = json.loads(jp.read_text())
        except Exception:
            continue
        aid = rec.get("asset_id")
        sp = rec.get("source_path") or ""
        if not aid or not sp:
            counts["no_source_path"] += 1
            continue
        counts["video_seen"] += 1
        rel = derivative_relative(sp)
        parts = rel.parts
        if parts and parts[0] == "Project":
            rel = Path(*parts[1:])
        proxy_path = DERIVATIVE_MEDIA / rel
        if rel.suffix.lower() == ".r3d":
            proxy_path = proxy_path.with_suffix(".mp4")
        wav_path = (DERIVATIVE_MEDIA / rel).with_suffix(".wav")

        slot = {}
        e = _entry_for(proxy_path, sp, stamp)
        if e:
            slot["video_video_proxy"] = e
            counts["video_proxy"] += 1
        e = _entry_for(wav_path, sp, stamp)
        if e:
            slot["video_audio_proxy"] = e
            counts["video_wav"] += 1
        if slot:
            entries[aid] = slot
        elif args.verbose:
            print(f"  no on-disk derivatives: {aid[:12]}  {sp}")

    # Audio-only
    for jp in iter_catalog_jsons(AUDIO_CATALOG, ".audio.json"):
        try:
            rec = json.loads(jp.read_text())
        except Exception:
            continue
        aid = rec.get("asset_id")
        sp = rec.get("source_path") or ""
        if not aid or not sp:
            counts["no_source_path"] += 1
            continue
        counts["audio_seen"] += 1
        rel = derivative_relative(sp)
        parts = rel.parts
        if parts and parts[0] == "Project":
            rel = Path(*parts[1:])
        wav_path = (DERIVATIVE_MEDIA / rel).with_suffix(".wav")
        e = _entry_for(wav_path, sp, stamp)
        if e:
            slot = entries.get(aid, {})
            slot["audio_audio_proxy"] = e
            entries[aid] = slot
            counts["audio_wav"] += 1

    # Stills
    for jp in iter_catalog_jsons(STILLS_CATALOG, ".still.json"):
        try:
            rec = json.loads(jp.read_text())
        except Exception:
            continue
        aid = rec.get("asset_id")
        sp = rec.get("source_path") or ""
        if not aid or not sp:
            counts["no_source_path"] += 1
            continue
        counts["still_seen"] += 1
        rel = derivative_relative(sp)
        parts = rel.parts
        if parts and parts[0] == "Project":
            rel = Path(*parts[1:])
        still_path = DERIVATIVE_MEDIA / rel
        e = _entry_for(still_path, sp, stamp)
        if e:
            slot = entries.get(aid, {})
            slot["still_still_proxy"] = e
            entries[aid] = slot
            counts["still_proxy"] += 1

    # Merge with existing unless --replace
    preserved = 0
    if not args.replace and ASSET_MAP.exists():
        try:
            existing = json.loads(ASSET_MAP.read_text()).get("entries", {})
        except Exception:
            existing = {}
        for aid, slot in existing.items():
            if aid not in entries:
                entries[aid] = slot
                preserved += 1

    out = {
        "last_run_utc": stamp,
        "generator": "rebuild_asset_map.py",
        "entries": entries,
    }

    print(f"=== rebuild_asset_map | {stamp} ===")
    print(f"  catalog records scanned:   video={counts['video_seen']}  "
          f"audio={counts['audio_seen']}  still={counts['still_seen']}")
    print(f"  derivative files indexed:  proxies={counts['video_proxy']}  "
          f"video_wavs={counts['video_wav']}  audio_wavs={counts['audio_wav']}  "
          f"stills={counts['still_proxy']}")
    print(f"  rediscovered asset_ids:    {len(entries) - preserved:,}")
    if preserved:
        print(f"  preserved from existing:   {preserved}  (not rediscovered "
              f"on disk; use --replace to drop)")
    print(f"  total asset_ids in map:    {len(entries):,}")
    print(f"  catalog records with no source_path: {counts['no_source_path']}")

    if args.dry_run:
        print("  (--dry-run; not writing)")
        return 0

    ASSET_MAP.parent.mkdir(parents=True, exist_ok=True)
    tmp = ASSET_MAP.with_suffix(ASSET_MAP.suffix + ".tmp")
    tmp.write_text(json.dumps(out, indent=2, ensure_ascii=False))
    os.replace(tmp, ASSET_MAP)
    print(f"  wrote {ASSET_MAP}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
