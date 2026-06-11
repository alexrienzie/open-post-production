#!/usr/bin/env python3
"""build_audio_quality.py — Per-asset audio quality metrics.

For each asset that has an `audio_extract` block in the catalog, run a DSP pass
over the extracted WAV and tag it with editorial-usability flags.

Signals now live in
catalog JSON. For videos: `video.json["audio_extract"]["audio_quality"]`. For
audios: `audio.json["audio_quality"]`. Run logs go to
`_runs/ingest_pipeline/audio_quality/<timestamp>Z.run.json`.

Subcommands:
  run     Full pass over all WAVs implied by catalog audio_extract blocks
  status  Coverage + flag distribution
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from _paths import (  # noqa: E402
    DERIVATIVE_MEDIA, VIDEO_CATALOG, AUDIO_CATALOG, WORKSPACE_ROOT,
)
from _catalog_layer_io import (  # noqa: E402
    now_iso, is_layer_processed, update_layer,
    start_run_log, finish_run_log,
)

warnings.filterwarnings("ignore", message="urllib3 v2 only supports OpenSSL")

FFMPEG = "/opt/homebrew/bin/ffmpeg"
FFPROBE = "/opt/homebrew/bin/ffprobe"

# --- thresholds (calibrated defaults; tune after a sample run) ---
SILENCE_DBFS = -45.0
QUIET_DBFS = -30.0
CLIPPING_RATIO_THRESHOLD = 0.001
PEAK_DBFS_CLIPPING = -1.0
LOW_FREQ_RATIO_THRESHOLD = 0.4
SILENCE_RATIO_THRESHOLD = 0.9
DC_OFFSET_THRESHOLD = 0.01
USABLE_MIN_DURATION = 1.0

LAYER = "audio_quality"


# ---------------- WAV path resolution ----------------

def _resolve_wav_path(catalog_record: dict) -> Path | None:
    ae = catalog_record.get("audio_extract") or {}
    p = ae.get("path") or ""
    if p:
        if p.startswith("~/"):
            cand = WORKSPACE_ROOT / p[2:]
            if cand.exists():
                return cand
        elif p.startswith("/"):
            cand = Path(p)
            if cand.exists():
                return cand
    sp = catalog_record.get("source_path") or ""
    if sp:
        from _paths import derivative_relative
        try:
            rel = derivative_relative(sp)
        except ValueError:
            return None
        cand = DERIVATIVE_MEDIA / rel.with_suffix(".wav")
        if cand.exists():
            return cand
    return None


def _iter_catalog_assets():
    """Yield (kind, record) for each catalog asset with an `audio_extract` block."""
    for cat_dir, suffix, kind in (
        (VIDEO_CATALOG, ".video.json", "video"),
        (AUDIO_CATALOG, ".audio.json", "audio"),
    ):
        for f in cat_dir.glob(f"*{suffix}"):
            if f.name.startswith("._"):
                continue
            try:
                d = json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                continue
            if not d.get("audio_extract"):
                continue
            yield kind, d


# ---------------- DSP ----------------

def _compute_metrics(wav_path: Path) -> dict | None:
    duration_sec = None
    try:
        proc = subprocess.run(
            [FFPROBE, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(wav_path)],
            capture_output=True, text=True, timeout=15,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            duration_sec = float(proc.stdout.strip())
    except Exception:
        pass

    try:
        proc = subprocess.run(
            [FFMPEG, "-hide_banner", "-loglevel", "error", "-nostdin",
             "-i", str(wav_path), "-ac", "1", "-f", "f32le", "-"],
            capture_output=True, timeout=180,
        )
    except subprocess.TimeoutExpired:
        return None
    if proc.returncode != 0 or not proc.stdout:
        return None

    samples = np.frombuffer(proc.stdout, dtype=np.float32)
    if samples.size == 0:
        return None

    rms = float(np.sqrt(np.mean(samples * samples)))
    rms_dbfs = 20.0 * np.log10(rms) if rms > 0 else -120.0
    peak = float(np.max(np.abs(samples)))
    peak_dbfs = 20.0 * np.log10(peak) if peak > 0 else -120.0
    clipping_ratio = float(np.sum(np.abs(samples) >= 0.99) / samples.size)
    silence_floor = 10 ** (SILENCE_DBFS / 20.0)
    silence_ratio = float(np.sum(np.abs(samples) < silence_floor) / samples.size)
    dc_offset = float(np.mean(samples))

    sr = 16000
    if samples.size > sr * 60:
        win = sr
        step = (samples.size - win) // 10
        starts = [i * step for i in range(10)]
        chunk = np.concatenate([samples[s:s + win] for s in starts])
    else:
        chunk = samples
    spec = np.abs(np.fft.rfft(chunk))
    freqs = np.fft.rfftfreq(len(chunk), 1.0 / sr)
    total_energy = float(np.sum(spec * spec))
    if total_energy > 0:
        low_mask = freqs < 250.0
        low_energy = float(np.sum((spec[low_mask] * spec[low_mask])))
        low_freq_ratio = low_energy / total_energy
    else:
        low_freq_ratio = 0.0

    return {
        "duration_sec": duration_sec or float(samples.size) / sr,
        "rms_dbfs": rms_dbfs,
        "peak_dbfs": peak_dbfs,
        "clipping_ratio": clipping_ratio,
        "silence_ratio": silence_ratio,
        "dc_offset": dc_offset,
        "low_freq_ratio": low_freq_ratio,
    }


def _derive_flags(m: dict) -> dict:
    is_silent = (m["rms_dbfs"] < SILENCE_DBFS) or (m["silence_ratio"] > SILENCE_RATIO_THRESHOLD)
    is_quiet = (not is_silent) and m["rms_dbfs"] < QUIET_DBFS
    is_clippy = (m["clipping_ratio"] > CLIPPING_RATIO_THRESHOLD) or (m["peak_dbfs"] > PEAK_DBFS_CLIPPING)
    is_windy = (m["low_freq_ratio"] > LOW_FREQ_RATIO_THRESHOLD) and not is_silent
    is_usable = (not is_silent) and (not is_clippy) and (m["duration_sec"] >= USABLE_MIN_DURATION)
    return {
        "is_silent": int(is_silent), "is_quiet": int(is_quiet),
        "is_clippy": int(is_clippy), "is_windy": int(is_windy),
        "is_usable": int(is_usable),
    }


# ---------------- per-asset worker ----------------

def _process_one(asset_id: str, kind: str, wav_path: Path) -> dict:
    m = _compute_metrics(wav_path)
    if m is None:
        return {"asset_id": asset_id, "kind": kind, "wav_path": str(wav_path), "success": False}
    flags = _derive_flags(m)
    return {"asset_id": asset_id, "kind": kind, "wav_path": str(wav_path),
            "success": True, **m, **flags}


# ---------------- run ----------------

def cmd_run(args: argparse.Namespace) -> None:
    run_path = start_run_log(LAYER, vars(args))
    print(f"=== build_audio_quality run | {now_iso()} ===")

    print("  walking catalog for assets with audio_extract blocks...")
    work = []
    skipped_processed = 0
    no_wav = 0
    for kind, rec in _iter_catalog_assets():
        aid = rec.get("asset_id")
        if not aid:
            continue
        if is_layer_processed(aid, kind, LAYER):
            skipped_processed += 1
            continue
        wav = _resolve_wav_path(rec)
        if wav is None or not wav.exists():
            no_wav += 1
            # Mark processed-with-error so we don't retry forever
            update_layer(aid, kind, LAYER, {
                "processed_at": now_iso(),
                "wav_path": None,
                "error": "no_wav_on_disk",
            })
            continue
        work.append((aid, kind, wav))
    print(f"  already processed: {skipped_processed}")
    print(f"  skipped (no WAV on disk): {no_wav}")
    print(f"  effective work: {len(work)}")
    if args.limit:
        work = work[: args.limit]
        print(f"  --limit: {len(work)}")
    if not work:
        print("nothing to do.")
        finish_run_log(run_path, {"processed": 0, "note": "no_work"})
        return

    counters = {"processed": 0, "errors": 0,
                "silent": 0, "quiet": 0, "clippy": 0, "windy": 0, "usable": 0}
    t_start = time.time()
    last_print = [time.time()]

    pool = ThreadPoolExecutor(max_workers=args.workers, thread_name_prefix="aq")
    futures = {pool.submit(_process_one, *args_tuple): args_tuple for args_tuple in work}
    for fut in as_completed(futures):
        args_tuple = futures[fut]
        aid, kind, wav = args_tuple
        res = fut.result()
        if not res["success"]:
            counters["errors"] += 1
            update_layer(aid, kind, LAYER, {
                "processed_at": now_iso(),
                "wav_path": str(wav),
                "error": "compute_failed",
            })
            continue
        update_layer(aid, kind, LAYER, {
            "processed_at": now_iso(),
            "wav_path": res["wav_path"],
            "duration_sec": res["duration_sec"],
            "metrics": {
                "rms_dbfs": res["rms_dbfs"],
                "peak_dbfs": res["peak_dbfs"],
                "clipping_ratio": res["clipping_ratio"],
                "silence_ratio": res["silence_ratio"],
                "dc_offset": res["dc_offset"],
                "low_freq_ratio": res["low_freq_ratio"],
                "is_silent": res["is_silent"],
                "is_quiet": res["is_quiet"],
                "is_clippy": res["is_clippy"],
                "is_windy": res["is_windy"],
                "is_usable": res["is_usable"],
            },
        })
        counters["processed"] += 1
        counters["silent"] += res["is_silent"]
        counters["quiet"] += res["is_quiet"]
        counters["clippy"] += res["is_clippy"]
        counters["windy"] += res["is_windy"]
        counters["usable"] += res["is_usable"]

        done = counters["processed"] + counters["errors"]
        now = time.time()
        if now - last_print[0] >= 15:
            el = now - t_start
            rate = done / el if el else 0
            eta_min = (len(work) - done) / rate / 60 if rate else 0
            print(
                f"[{done:>4}/{len(work)}] {100*done/len(work):5.1f}%  "
                f"rate={rate:5.2f}/s  silent={counters['silent']:4d}  "
                f"clippy={counters['clippy']:4d}  windy={counters['windy']:4d}  "
                f"usable={counters['usable']:4d}  err={counters['errors']}  "
                f"elapsed={el/60:5.1f}m ETA={eta_min:5.1f}m",
                flush=True,
            )
            last_print[0] = now
    pool.shutdown(wait=True)

    elapsed = time.time() - t_start
    summary = {**counters, "elapsed_sec": round(elapsed, 1), "total_assets": len(work)}
    finish_run_log(run_path, summary)
    print(f"\n=== Summary ===")
    print(f"Elapsed: {elapsed/60:.1f} min")
    for k, v in counters.items():
        print(f"  {k:<10s}: {v}")


# ---------------- status ----------------

def cmd_status(args: argparse.Namespace) -> None:
    n = 0
    flags = {"silent": 0, "quiet": 0, "clippy": 0, "windy": 0, "usable": 0}
    rms_values: list[float] = []
    for cat_dir, suffix, kind in (
        (VIDEO_CATALOG, ".video.json", "video"),
        (AUDIO_CATALOG, ".audio.json", "audio"),
    ):
        for p in cat_dir.glob(f"*{suffix}"):
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                continue
            if kind == "video":
                aq = (d.get("audio_extract") or {}).get("audio_quality") or {}
            else:
                aq = d.get("audio_quality") or {}
            m = aq.get("metrics")
            if not m:
                continue
            n += 1
            for f in flags:
                flags[f] += m.get(f"is_{f}") or 0
            if m.get("rms_dbfs") is not None:
                rms_values.append(m["rms_dbfs"])
    print(f"=== audio_quality coverage (catalog JSON) ===")
    print(f"  rows: {n}")
    for label, c in flags.items():
        print(f"  {label:<10s}: {c:5d}  ({100*c/max(1,n):.1f}%)")
    if rms_values:
        rms_values.sort()
        print(f"\n  rms_dbfs percentiles:")
        for p in (10, 25, 50, 75, 90):
            idx = min(len(rms_values) - 1, len(rms_values) * p // 100)
            print(f"    p{p:>2d}: {rms_values[idx]:6.1f} dBFS")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_run = sub.add_parser("run", help="Compute quality metrics for all WAV-extracted assets")
    p_run.add_argument("--workers", type=int, default=4)
    p_run.add_argument("--limit", type=int, default=None)
    p_run.set_defaults(func=cmd_run)
    p_status = sub.add_parser("status", help="Coverage + flag stats")
    p_status.set_defaults(func=cmd_status)
    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
