#!/usr/bin/env python3
"""
extract_audio_from_proxy.py — Extract 16 kHz mono PCM WAV from existing H.264 proxies.

Companion to extract_audio.py for asset_ids whose original source can't be read by
ffmpeg (RED .R3D files). For these, the canonical extract_audio.py returns
red_raw_unsupported. This script reads the AAC audio from the catalog's `proxy.path`
mp4 instead — the encoder + downstream Whisper pipeline is identical to what the main path's
existing 412 extracts went through.

Same FFMPEG_CMD_TEMPLATE + CMD_HASH as extract_audio.py so the audio_extract block
is schema-compatible. Adds two discriminator fields:
    extraction_source:        "proxy_h264_aac"
    extraction_source_path:   <path to the .mp4 the WAV was pulled from>

`source_clip_path_at_extraction` and `source_ssd_path_at_extraction` still record the
original catalog source (the .R3D), so cross-referencing stays consistent.

WAVs land per-shoot under `derivative media/<shoot>/<stem>.wav`, mirroring the
catalog source_path tree (same layout as extract_audio.py).

Usage:
    python3 extract_audio_from_proxy.py --asset-ids /tmp/r3d_asset_ids.txt
                                        [--limit N] [--force]
"""
import os, sys, json, hashlib, subprocess, time, shutil, argparse
from pathlib import Path
from datetime import datetime, timezone

import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))  # shared modules live at _scripts root
from _paths import (
    VIDEO_CATALOG, DERIVATIVE_MEDIA, RUNS_DIR,
    wav_output_path, workspace_tilde, resolve_proxy_via_asset_map,
)

ERRORS_LOG = RUNS_DIR / "extract_audio_errors.jsonl"
RUNS_LOG = RUNS_DIR / "extract_audio_runs.jsonl"

FFMPEG = "/opt/homebrew/bin/ffmpeg"
FFPROBE = "/opt/homebrew/bin/ffprobe"

# Byte-identical to extract_audio.py — produces the same CMD_HASH.
FFMPEG_CMD_TEMPLATE = (
    "ffmpeg -hide_banner -loglevel error -nostdin "
    "-i {src} -vn -ar 16000 -ac 1 -c:a pcm_s16le -f wav {dst}"
)
CMD_HASH = hashlib.sha256(FFMPEG_CMD_TEMPLATE.encode()).hexdigest()


def now_utc_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def log_jsonl(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(obj, default=str) + "\n")


def atomic_write_json(path, obj):
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=2)
    os.replace(tmp, path)


def probe_duration(wav_path):
    try:
        out = subprocess.run(
            [FFPROBE, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(wav_path)],
            capture_output=True, text=True, timeout=30,
        )
        if out.returncode == 0 and out.stdout.strip():
            return float(out.stdout.strip())
    except Exception:
        pass
    return None


def extract_one(asset_id, force=False):
    rec_path = VIDEO_CATALOG / f"{asset_id}.video.json"
    if not rec_path.exists():
        return {"status": "error", "asset_id": asset_id, "reason": "catalog_missing"}

    try:
        rec = json.loads(rec_path.read_text())
    except Exception as e:
        return {"status": "error", "asset_id": asset_id, "reason": "record_read_failed", "detail": str(e)}

    proxy = rec.get("proxy") or {}
    raw_proxy_path = proxy.get("path") or ""

    # Catalog `proxy.path` may be flat-legacy (e.g. `~/derivative media/proxy
    # videos/<aid>.mp4`) or absolute Mac-legacy; resolve via asset_map first,
    # then fall back to the catalog string.
    resolved = resolve_proxy_via_asset_map(asset_id)
    if resolved and resolved.is_file():
        proxy_path = str(resolved)
    elif raw_proxy_path:
        expanded = os.path.expanduser(
            raw_proxy_path.replace("~/", str(DERIVATIVE_MEDIA.parent) + "/", 1)
            if raw_proxy_path.startswith("~/") else raw_proxy_path
        )
        proxy_path = expanded
    else:
        return {"status": "error", "asset_id": asset_id, "reason": "no_proxy_path"}

    if not os.path.exists(proxy_path):
        return {"status": "error", "asset_id": asset_id, "reason": "proxy_missing", "proxy_path": proxy_path}

    if not proxy.get("audio_codec"):
        return {"status": "skipped", "asset_id": asset_id, "reason": "proxy_has_no_audio"}

    try:
        wav_path = wav_output_path(rec)
    except ValueError as e:
        return {"status": "error", "asset_id": asset_id, "reason": "source_path_missing", "detail": str(e)}
    wav_path.parent.mkdir(parents=True, exist_ok=True)

    if not force:
        ae = rec.get("audio_extract")
        if (ae and ae.get("ffmpeg_command_hash") == CMD_HASH
                and wav_path.exists() and wav_path.stat().st_size > 0):
            return {"status": "skipped", "asset_id": asset_id, "reason": "already_current"}

    t0 = time.time()
    try:
        proc = subprocess.run(
            [FFMPEG, "-hide_banner", "-loglevel", "error", "-nostdin",
             "-i", proxy_path,
             "-vn", "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", "-f", "wav",
             "-y", str(wav_path)],
            capture_output=True, text=True, timeout=3600,
        )
    except subprocess.TimeoutExpired:
        return {"status": "error", "asset_id": asset_id, "reason": "ffmpeg_timeout", "proxy_path": proxy_path}
    except Exception as e:
        return {"status": "error", "asset_id": asset_id, "reason": "ffmpeg_exception", "detail": str(e)}
    extract_secs = time.time() - t0

    if proc.returncode != 0:
        return {"status": "error", "asset_id": asset_id, "reason": "ffmpeg_failed",
                "returncode": proc.returncode, "stderr": (proc.stderr or "").strip()[:2000],
                "proxy_path": proxy_path}

    if not wav_path.exists() or wav_path.stat().st_size == 0:
        return {"status": "error", "asset_id": asset_id, "reason": "wav_empty_or_missing"}

    duration = probe_duration(wav_path)
    if duration is None:
        return {"status": "error", "asset_id": asset_id, "reason": "wav_probe_failed"}

    rec["audio_extract"] = {
        "path": workspace_tilde(wav_path),
        "format": "wav",
        "sample_rate": 16000,
        "channels": 1,
        "codec": "pcm_s16le",
        "filesize_bytes": wav_path.stat().st_size,
        "duration_sec": round(duration, 4),
        "extracted_at": now_utc_iso(),
        "ffmpeg_command_hash": CMD_HASH,
        "source_clip_path_at_extraction": rec.get("source_path"),
        "source_ssd_path_at_extraction": proxy.get("source_ssd_path_at_encoding"),
        "extraction_source": "proxy_h264_aac",
        "extraction_source_path": proxy_path,
    }

    try:
        atomic_write_json(rec_path, rec)
    except Exception as e:
        return {"status": "error", "asset_id": asset_id, "reason": "record_write_failed", "detail": str(e)}

    return {"status": "extracted", "asset_id": asset_id,
            "duration_sec": duration, "wav_size": wav_path.stat().st_size,
            "extract_secs": round(extract_secs, 2)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--asset-ids", required=True, help="File with one asset_id per line")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    print(f"=== extract_audio_from_proxy.py | {now_utc_iso()} ===")
    print(f"WAV root: {DERIVATIVE_MEDIA} (per-shoot subfolders)")
    print(f"ffmpeg_command_hash: {CMD_HASH[:16]}... (matches extract_audio.py)")

    DERIVATIVE_MEDIA.mkdir(parents=True, exist_ok=True)
    RUNS_DIR.mkdir(parents=True, exist_ok=True)

    free_gb = shutil.disk_usage(DERIVATIVE_MEDIA).free / 1e9
    print(f"Free space on WAV destination: {free_gb:.1f} GB")

    with open(args.asset_ids) as f:
        wanted = [line.strip() for line in f if line.strip()]
    if args.limit:
        wanted = wanted[:args.limit]
    print(f"Queue: {len(wanted)} asset_ids")
    print()

    log_jsonl(RUNS_LOG, {
        "event": "run_start_proxy_source", "timestamp": now_utc_iso(),
        "ffmpeg_command_hash": CMD_HASH,
        "total_entries": len(wanted), "limit": args.limit, "force": args.force,
    })

    summary = {"extracted": 0, "skipped": 0, "error": 0}
    t_start = time.time()

    for i, aid in enumerate(wanted, 1):
        result = extract_one(aid, force=args.force)
        result["ts"] = now_utc_iso()
        log_jsonl(RUNS_LOG, result)
        if result["status"] == "error":
            log_jsonl(ERRORS_LOG, result)

        summary[result["status"]] = summary.get(result["status"], 0) + 1
        tag = result["status"].upper()
        extra = ""
        if result["status"] == "extracted":
            extra = f" {result['duration_sec']:.1f}s, {result['wav_size']/1e6:.1f}MB, " \
                    f"{result['extract_secs']:.1f}s"
        elif result["status"] in ("skipped", "error"):
            extra = f" ({result['reason']})"
        print(f"[{i:3d}/{len(wanted)}] {aid[:16]} {tag}{extra}")

    elapsed = time.time() - t_start
    print()
    print("=" * 60)
    print(f"Done in {elapsed/60:.1f} min")
    for k, v in summary.items():
        print(f"  {k:<10}: {v}")
    print(f"Run log:    {RUNS_LOG}")
    print(f"Error log:  {ERRORS_LOG}")


if __name__ == "__main__":
    main()
