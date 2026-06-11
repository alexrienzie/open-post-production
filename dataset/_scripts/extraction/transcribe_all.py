#!/usr/bin/env python3
"""
transcribe_all.py — Drive `mw transcribe --persist` over every catalog record whose
per-shoot WAV exists but doesn't yet have a machine transcript.

The MacWhisper CLI talks to MacWhisper.app via a Unix socket; jobs run serially.
Output (incl. word timestamps + diarization) lands in
~/Library/Application Support/MacWhisper/Database/main.sqlite. Use
build_transcripts.py afterward to extract per-asset artifacts into the canonical v5
canonical transcript format.

Usage:
    python3 dataset/_scripts/extraction/transcribe_all.py [--limit N] [--model ENGINE:ID]
                                                  [--force] [--min-duration 5.0]
"""
import os, sys, json, sqlite3, subprocess, time, argparse, shutil
from pathlib import Path
from collections import Counter
from datetime import datetime, timezone

import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))  # shared modules live at _scripts root
from _paths import (
    VIDEO_CATALOG, AUDIO_CATALOG, TRANSCRIPT_CATALOG, DERIVATIVE_MEDIA, RUNS_DIR,
    wav_output_path,
)

RUNS_LOG = RUNS_DIR / "transcribe_runs.jsonl"
ERRORS_LOG = RUNS_DIR / "transcribe_errors.jsonl"

MW_CLI = "/usr/local/bin/mw"
MW_DB = Path(os.path.expanduser(
    "~/Library/Application Support/MacWhisper/Database/main.sqlite"))

CONSECUTIVE_FAIL_LIMIT = 3
MACWHISPER_RELAUNCH_WAIT_SEC = 8


def now_utc_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def log_jsonl(path, obj):
    with open(path, "a") as f:
        f.write(json.dumps(obj, default=str) + "\n")


def cycle_macwhisper(reason):
    print(f"  >> cycling MacWhisper.app ({reason})", flush=True)
    subprocess.run(["pkill", "-x", "MacWhisper"], capture_output=True)
    time.sleep(MACWHISPER_RELAUNCH_WAIT_SEC)


def get_active_model():
    out = subprocess.run([MW_CLI, "models"], capture_output=True, text=True)
    for line in out.stdout.splitlines():
        if "▸" in line:
            parts = line.replace("▸", "").split()
            return parts[0] if parts else None
    return None


def existing_sessions_for_asset(conn, asset_id, model_id):
    """Match against MacWhisper's bare model id (strip engine prefix like 'whisperkit:')."""
    bare_model = model_id.split(":", 1)[-1]
    cur = conn.execute(
        "SELECT hex(id), transcriptionDidSucceed FROM session "
        "WHERE originalFilename = ? AND modelIdentifer = ? "
        "AND (dateDeleted IS NULL OR dateDeleted = 0)",
        (asset_id, bare_model),
    )
    return cur.fetchall()


def transcribe_one(wav_path, model_id, audio_duration_sec=0, asset_id=None):
    # MacWhisper's session.originalFilename is the WAV's basename stripped of its
    # extension. build_transcripts.py only picks up sessions with a 64-char
    # asset_id-shaped originalFilename. extract_audio.py writes WAVs as
    # `<shoot>/<stem>.wav` (human-readable, not asset_id), so we transcribe a
    # symlink at /tmp/<asset_id>.wav to coerce MW's stored filename.
    timeout_sec = max(120.0, audio_duration_sec / 3.0)  # 120s floor absorbs cold-start
    target = wav_path
    sym = None
    if asset_id:
        sym = Path("/tmp") / f"{asset_id}.wav"
        try:
            if sym.is_symlink() or sym.exists():
                sym.unlink()
            sym.symlink_to(wav_path)
            target = sym
        except OSError:
            sym = None
            target = wav_path
    t0 = time.time()
    try:
        proc = subprocess.run(
            [MW_CLI, "transcribe", str(target), "--persist", "--model", model_id],
            capture_output=True, text=True, timeout=timeout_sec,
        )
    except subprocess.TimeoutExpired:
        return ("timeout", time.time() - t0,
                f"subprocess timeout ({timeout_sec:.0f}s for {audio_duration_sec:.0f}s audio)")
    finally:
        if sym is not None:
            try: sym.unlink()
            except OSError: pass
    elapsed = time.time() - t0
    if proc.returncode != 0:
        return "failed", elapsed, (proc.stderr or proc.stdout or "")[:2000]
    return "ok", elapsed, None


def load_duration_for_asset(asset_id):
    """Find audio_extract.duration_sec by checking the video record then audio record."""
    for d in [VIDEO_CATALOG, AUDIO_CATALOG]:
        suffix = ".video.json" if d == VIDEO_CATALOG else ".audio.json"
        p = d / f"{asset_id}{suffix}"
        if p.exists():
            rec = json.loads(p.read_text())
            ae = rec.get("audio_extract") or {}
            return ae.get("duration_sec", 0) or 0
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--model", type=str, default=None)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--min-duration", type=float, default=5.0)
    args = ap.parse_args()

    print(f"=== transcribe_all.py | {now_utc_iso()} ===")
    if not Path(MW_CLI).exists():
        print(f"ABORT: {MW_CLI} missing"); sys.exit(1)
    if not MW_DB.exists():
        print(f"ABORT: {MW_DB} missing"); sys.exit(1)
    if not DERIVATIVE_MEDIA.exists():
        print(f"ABORT: {DERIVATIVE_MEDIA} missing (is the workspace SSD mounted?)"); sys.exit(1)
    RUNS_DIR.mkdir(parents=True, exist_ok=True)

    model_id = args.model or get_active_model()
    if not model_id:
        print("ABORT: could not determine active model")
        sys.exit(1)
    print(f"Model: {model_id}")

    free_gb = shutil.disk_usage(MW_DB.parent).free / 1e9
    print(f"Free space on MacWhisper volume: {free_gb:.1f} GB")

    # Build work list: WAVs that exist + match a catalog asset that doesn't yet have a transcript
    print("Scanning catalog for transcribe candidates...")
    candidates = []
    for d in [VIDEO_CATALOG, AUDIO_CATALOG]:
        suffix = ".video.json" if d == VIDEO_CATALOG else ".audio.json"
        for p in d.glob(f"*{suffix}"):
            if p.name.startswith("._"): continue  # macOS AppleDouble sidecar
            rec = json.loads(p.read_text())
            ae = rec.get("audio_extract") or {}
            if not ae.get("ffmpeg_command_hash"):
                continue  # no WAV yet
            dur = ae.get("duration_sec") or 0
            if dur < args.min_duration:
                continue
            if rec.get("has_machine_transcript") and not args.force:
                continue
            try:
                wav = wav_output_path(rec)
            except ValueError:
                continue
            if not wav.exists():
                continue
            candidates.append({
                "asset_id": rec["asset_id"],
                "wav": wav,
                "duration_sec": dur,
            })
    print(f"Total candidates (have WAV, no transcript, ≥{args.min_duration}s): {len(candidates)}")

    # Filter by SQLite idempotency
    conn = sqlite3.connect(MW_DB.as_uri() + "?mode=ro", uri=True)
    work = []
    skipped = 0
    for c in candidates:
        if args.force:
            work.append(c)
            continue
        existing = existing_sessions_for_asset(conn, c["asset_id"], model_id)
        if any(succ == 1 for (_sid, succ) in existing):
            skipped += 1
        else:
            work.append(c)
    conn.close()
    print(f"Already in MacWhisper SQLite (skip): {skipped}")
    print(f"To transcribe this run:              {len(work)}")
    if args.limit:
        work = work[:args.limit]
        print(f"Limited to first {len(work)}")
    if not work:
        print("Nothing to do.")
        return

    log_jsonl(RUNS_LOG, {
        "event": "run_start", "timestamp": now_utc_iso(),
        "model_id": model_id, "total_to_transcribe": len(work),
        "skipped_already_done": skipped, "limit": args.limit,
        "min_duration": args.min_duration, "force": args.force,
    })

    counters = Counter()
    consecutive_fails = 0
    t_start = time.time()
    audio_sec_done = 0.0

    for i, c in enumerate(work, 1):
        status, elapsed, err = transcribe_one(c["wav"], model_id, c["duration_sec"], asset_id=c["asset_id"])
        counters[status] += 1
        audio_sec_done += c["duration_sec"]

        if status == "ok":
            consecutive_fails = 0
        else:
            consecutive_fails += 1
            if consecutive_fails >= CONSECUTIVE_FAIL_LIMIT:
                cycle_macwhisper(f"{consecutive_fails} consecutive failures")
                log_jsonl(RUNS_LOG, {
                    "event": "macwhisper_cycled", "timestamp": now_utc_iso(),
                    "after_consecutive_failures": consecutive_fails,
                    "current_file_index": i,
                })
                consecutive_fails = 0

        elapsed_total = time.time() - t_start
        rate = audio_sec_done / elapsed_total if elapsed_total > 0 else 0
        remaining_audio = sum(x["duration_sec"] for x in work[i:])
        eta_min = (remaining_audio / rate / 60) if rate > 0 else 0
        total_errs = sum(v for k, v in counters.items() if k != "ok")

        if status == "ok":
            ratio = (c["duration_sec"] / elapsed) if elapsed > 0 else 0
            line = (f"[{i:>5}/{len(work)}] ok  "
                    f"audio={c['duration_sec']:7.1f}s took={elapsed:5.1f}s "
                    f"{ratio:5.1f}xRT  | done {audio_sec_done/3600:5.1f}hr "
                    f"{rate:5.1f}xRT avg  ETA {eta_min/60:4.1f}h  errs={total_errs}")
        else:
            line = (f"[{i:>5}/{len(work)}] {status:<6} took={elapsed:5.1f}s "
                    f"err={(err or '')[:80]}  consec={consecutive_fails}")
            log_jsonl(ERRORS_LOG, {
                "asset_id": c["asset_id"], "wav": str(c["wav"]),
                "status": status, "elapsed_sec": round(elapsed, 2),
                "error": err, "timestamp": now_utc_iso(),
            })
        print(line, flush=True)

    elapsed_total = time.time() - t_start
    log_jsonl(RUNS_LOG, {
        "event": "run_end", "timestamp": now_utc_iso(),
        "elapsed_sec": round(elapsed_total, 1),
        "audio_sec_processed": round(audio_sec_done, 1),
        "by_status": dict(counters),
    })

    print(f"\n=== Summary ===")
    print(f"Elapsed: {elapsed_total/60:.1f} min")
    print(f"Audio processed: {audio_sec_done/3600:.2f} hr")
    print(f"Avg RT: {(audio_sec_done/elapsed_total) if elapsed_total else 0:.1f}x")
    for k, v in counters.most_common():
        print(f"  {k:<8}: {v}")


if __name__ == "__main__":
    main()
