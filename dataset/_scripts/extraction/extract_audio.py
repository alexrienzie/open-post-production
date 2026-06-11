#!/usr/bin/env python3
"""
extract_audio.py — Extract 16 kHz mono PCM WAV from cataloged video / audio assets
that don't yet have an `audio_extract` block. WAVs land per-shoot mirroring the
catalog source_path tree (e.g. `derivative media/<shoot>/<stem>.wav`) and the
matching video / audio catalog record is updated atomically.

Reads `dataset/_scripts/verify_ssd_match_report.json` for SSD locations.
Skips B-roll (already filtered by verify_ssd_match) and any record whose
audio_extract block is already present and matches the current ffmpeg command hash.

Usage:
    python3 dataset/_scripts/extraction/extract_audio.py [--limit N] [--dry-run] [--force] [--workers-per-ssd 2]
"""
import os, sys, json, hashlib, subprocess, time, shutil, argparse, threading
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import defaultdict, Counter
from datetime import datetime, timezone

import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))  # shared modules live at _scripts root
from _paths import (
    VIDEO_CATALOG, AUDIO_CATALOG, DERIVATIVE_MEDIA, RUNS_DIR,
    wav_output_path, workspace_tilde,
)

SCRIPT_DIR = Path(__file__).resolve().parent
ERRORS_LOG = RUNS_DIR / "extract_audio_errors.jsonl"
RUNS_LOG = RUNS_DIR / "extract_audio_runs.jsonl"
REPORT_IN = SCRIPT_DIR / "verify_ssd_match_report.json"

FFMPEG = "/opt/homebrew/bin/ffmpeg"
FFPROBE = "/opt/homebrew/bin/ffprobe"

FFMPEG_CMD_TEMPLATE = (
    "ffmpeg -hide_banner -loglevel error -nostdin "
    "-i {src} -vn -ar 16000 -ac 1 -c:a pcm_s16le -f wav {dst}"
)
CMD_HASH = hashlib.sha256(FFMPEG_CMD_TEMPLATE.encode()).hexdigest()

R3D_EXT = ".r3d"
DISK_SPACE_REQUIRED_GB = 5  # the workspace SSD needs ~5 GB headroom for the new ~700 WAVs

_log_lock = threading.Lock()


def now_utc_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def log_jsonl(path, obj):
    line = json.dumps(obj, default=str)
    with _log_lock:
        with open(path, "a") as f:
            f.write(line + "\n")


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


def record_path(asset_id, kind):
    if kind == "video":
        return VIDEO_CATALOG / f"{asset_id}.video.json"
    return AUDIO_CATALOG / f"{asset_id}.audio.json"


def extract_one(entry, force=False, dry_run=False):
    asset_id = entry["asset_id"]
    kind = entry["kind"]
    ssd_label = entry.get("_ssd")
    ssd_path = entry.get("_ssd_path")
    catalog_source_path = entry.get("source_path")
    filename = entry.get("filename") or ""

    rec_path = record_path(asset_id, kind)

    if filename.lower().endswith(R3D_EXT):
        return {"status": "r3d_skipped", "asset_id": asset_id, "kind": kind,
                "ssd": ssd_label, "filename": filename}

    try:
        rec = json.loads(rec_path.read_text())
    except Exception as e:
        return {"status": "error", "asset_id": asset_id, "kind": kind, "ssd": ssd_label,
                "reason": "record_read_failed", "detail": str(e)}

    try:
        wav_path = wav_output_path(rec)
    except ValueError as e:
        return {"status": "error", "asset_id": asset_id, "kind": kind, "ssd": ssd_label,
                "reason": "source_path_missing", "detail": str(e)}
    wav_path.parent.mkdir(parents=True, exist_ok=True)

    if not force:
        ae = rec.get("audio_extract")
        if (ae and ae.get("ffmpeg_command_hash") == CMD_HASH
                and wav_path.exists() and wav_path.stat().st_size > 0):
            return {"status": "skipped", "asset_id": asset_id, "kind": kind,
                    "ssd": ssd_label, "reason": "already_current"}

    if not ssd_path or not os.path.exists(ssd_path):
        return {"status": "error", "asset_id": asset_id, "kind": kind, "ssd": ssd_label,
                "reason": "ssd_source_missing", "ssd_path": ssd_path}

    if dry_run:
        return {"status": "dry_run", "asset_id": asset_id, "kind": kind, "ssd": ssd_label,
                "ssd_path": ssd_path, "wav_path": str(wav_path)}

    t0 = time.time()
    try:
        proc = subprocess.run(
            [FFMPEG, "-hide_banner", "-loglevel", "error", "-nostdin",
             "-i", ssd_path,
             "-vn", "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", "-f", "wav",
             "-y", str(wav_path)],
            capture_output=True, text=True, timeout=3600,
        )
    except subprocess.TimeoutExpired:
        return {"status": "error", "asset_id": asset_id, "kind": kind, "ssd": ssd_label,
                "reason": "ffmpeg_timeout", "ssd_path": ssd_path}
    except Exception as e:
        return {"status": "error", "asset_id": asset_id, "kind": kind, "ssd": ssd_label,
                "reason": "ffmpeg_exception", "detail": str(e)}
    extract_secs = time.time() - t0

    if proc.returncode != 0:
        return {"status": "error", "asset_id": asset_id, "kind": kind, "ssd": ssd_label,
                "reason": "ffmpeg_failed", "returncode": proc.returncode,
                "stderr": (proc.stderr or "").strip()[:2000],
                "ssd_path": ssd_path}

    if not wav_path.exists() or wav_path.stat().st_size == 0:
        return {"status": "error", "asset_id": asset_id, "kind": kind, "ssd": ssd_label,
                "reason": "wav_empty_or_missing", "ssd_path": ssd_path}

    duration = probe_duration(wav_path)
    if duration is None:
        return {"status": "error", "asset_id": asset_id, "kind": kind, "ssd": ssd_label,
                "reason": "wav_probe_failed", "ssd_path": ssd_path}

    wav_size = wav_path.stat().st_size

    rec["audio_extract"] = {
        "path": workspace_tilde(wav_path),
        "format": "wav",
        "sample_rate": 16000,
        "channels": 1,
        "codec": "pcm_s16le",
        "filesize_bytes": wav_size,
        "duration_sec": round(duration, 4),
        "extracted_at": now_utc_iso(),
        "ffmpeg_command_hash": CMD_HASH,
        "source_clip_path_at_extraction": catalog_source_path,
        "source_ssd_path_at_extraction": ssd_path,
    }
    # has_machine_transcript stays whatever it was (false unless transcribe ran)

    try:
        atomic_write_json(rec_path, rec)
    except Exception as e:
        return {"status": "error", "asset_id": asset_id, "kind": kind, "ssd": ssd_label,
                "reason": "record_write_failed", "detail": str(e)}

    return {"status": "extracted", "asset_id": asset_id, "kind": kind, "ssd": ssd_label,
            "duration_sec": duration, "wav_size": wav_size,
            "extract_secs": round(extract_secs, 2)}


def sample_across_ssds(work_list, n):
    by_ssd = defaultdict(list)
    for e in work_list:
        by_ssd[e["_ssd"]].append(e)
    ssds = sorted(by_ssd.keys())
    out, i = [], 0
    while len(out) < n:
        any_left = False
        for s in ssds:
            if i < len(by_ssd[s]):
                out.append(by_ssd[s][i])
                any_left = True
                if len(out) >= n:
                    break
        if not any_left:
            break
        i += 1
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--workers-per-ssd", type=int, default=2)
    args = ap.parse_args()

    print(f"=== extract_audio.py | {now_utc_iso()} ===")
    print(f"WAV root: {DERIVATIVE_MEDIA} (per-shoot subfolders)")
    print(f"ffmpeg_command_hash: {CMD_HASH[:16]}...")

    if not REPORT_IN.exists():
        print(f"ABORT: {REPORT_IN} missing — run verify_ssd_match.py first")
        sys.exit(1)
    DERIVATIVE_MEDIA.mkdir(parents=True, exist_ok=True)
    RUNS_DIR.mkdir(parents=True, exist_ok=True)

    free_gb = shutil.disk_usage(DERIVATIVE_MEDIA).free / 1e9
    print(f"Free space on WAV destination: {free_gb:.1f} GB")
    if free_gb < DISK_SPACE_REQUIRED_GB and not args.dry_run:
        print(f"ABORT: <{DISK_SPACE_REQUIRED_GB} GB free")
        sys.exit(1)

    report = json.loads(REPORT_IN.read_text())
    matched = report["hash_matched"] + report["fallback_matched"]
    # Filter to records that actually need extraction (idempotency — skip already-current)
    work = [e for e in matched if not e.get("has_audio_extract")]
    print(f"Matched (extract candidates): {len(matched)}")
    print(f"Already extracted (skip):     {len(matched) - len(work)}")
    print(f"To extract this run:          {len(work)}")

    if args.limit:
        work = sample_across_ssds(work, args.limit)
        print(f"Sampled {len(work)} for smoke run")

    by_ssd = defaultdict(list)
    for e in work:
        by_ssd[e["_ssd"]].append(e)
    print("\nWork by SSD:")
    for ssd in sorted(by_ssd):
        print(f"  {ssd:<14}: {len(by_ssd[ssd])}")

    log_jsonl(RUNS_LOG, {
        "event": "run_start", "timestamp": now_utc_iso(),
        "ffmpeg_command_hash": CMD_HASH,
        "total_entries": len(work),
        "by_ssd": {ssd: len(L) for ssd, L in by_ssd.items()},
        "limit": args.limit, "dry_run": args.dry_run, "force": args.force,
    })

    counters = Counter()
    per_ssd_done = Counter()
    t_start = time.time()
    last_print = [0]

    def handle(res):
        counters[res["status"]] += 1
        ssd = res.get("ssd") or "?"
        per_ssd_done[ssd] += 1
        done = sum(counters.values())
        if res["status"] == "error":
            log_jsonl(ERRORS_LOG, {**res, "timestamp": now_utc_iso()})
        elif res["status"] == "r3d_skipped":
            log_jsonl(ERRORS_LOG, {**res, "reason": "red_raw_unsupported",
                                   "timestamp": now_utc_iso()})
        if done - last_print[0] >= 25 or done == len(work):
            elapsed = time.time() - t_start
            rate = done / elapsed if elapsed > 0 else 0
            eta = (len(work) - done) / rate if rate > 0 else 0
            ssd_str = "  ".join(f"{s}: {per_ssd_done[s]}/{len(by_ssd[s])}"
                                for s in sorted(by_ssd))
            print(f"[{done:>5}/{len(work)}] {100*done/len(work):5.1f}%  "
                  f"elapsed {elapsed/60:5.1f}m  ETA {eta/60:5.1f}m  "
                  f"{ssd_str}  errors: {counters['error']}", flush=True)
            last_print[0] = done

    executors = {}
    futures = []
    for ssd, entries in by_ssd.items():
        ex = ThreadPoolExecutor(max_workers=args.workers_per_ssd,
                                thread_name_prefix=f"ssd-{ssd}")
        executors[ssd] = ex
        for e in entries:
            futures.append(ex.submit(extract_one, e, args.force, args.dry_run))

    try:
        for fut in as_completed(futures):
            try:
                res = fut.result()
            except Exception as e:
                res = {"status": "error", "reason": "future_exception", "detail": str(e)}
            handle(res)
    finally:
        for ex in executors.values():
            ex.shutdown(wait=True)

    elapsed = time.time() - t_start
    log_jsonl(RUNS_LOG, {
        "event": "run_end", "timestamp": now_utc_iso(),
        "elapsed_sec": round(elapsed, 1),
        "total": len(work),
        "by_status": dict(counters),
        "by_ssd_completed": dict(per_ssd_done),
    })

    print("\n=== Summary ===")
    print(f"Elapsed: {elapsed/60:.1f} min")
    for k, v in counters.most_common():
        print(f"  {k:<18}: {v}")
    print(f"Errors: {ERRORS_LOG}")
    print(f"Run log: {RUNS_LOG}")


if __name__ == "__main__":
    main()
