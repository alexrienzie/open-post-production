#!/usr/bin/env python3
"""build_shot_quality.py — Per-shot editorial-usability metrics.

For each shot in each video's `video.json["shots"]["items"]`, sample 3-5 frames
at quartile positions and compute four signals an editor cares about when
filtering B-roll:

  sharpness_score    Laplacian variance avg (higher = sharper)
  motion_score       mean absolute diff between consecutive sampled frames
                     (higher = more camera/subject motion)
  exposure_mean      mean luminance (0-255); flags very dark / blown out
  clipping_ratio     fraction of pixels at 0 or 255 (flags blown highlights /
                     crushed shadows)

Plus a heuristic `is_setup_or_teardown` flag for short shots at the start or
end of an asset's shot sequence — typically operator setting up / tearing
down the camera, not editorial content.

Signals now live in
catalog JSON under `video.json["shot_quality"]`. Run logs go to
`_runs/ingest_pipeline/shot_quality/<timestamp>Z.run.json`. Idempotent at the
asset level (skips assets whose shot_quality.processed_at is set).

Subcommands:
  run     Full pass over video catalog (reads each video.json["shots"]["items"])
  status  Coverage + threshold-flag distribution
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import warnings
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from _paths import (  # noqa: E402
    VIDEO_CATALOG,
    resolve_proxy_via_asset_map,
)
from _catalog_layer_io import (  # noqa: E402
    now_iso, is_layer_processed, update_layer, load_catalog,
    start_run_log, finish_run_log,
)

warnings.filterwarnings("ignore", message="urllib3 v2 only supports OpenSSL")

FFMPEG = "/opt/homebrew/bin/ffmpeg"

# --- thresholds (calibrated defaults; adjust after first pass) ---
SHARPNESS_BLURRY_THRESHOLD = 100.0
EXPOSURE_DARK_THRESHOLD = 30.0
EXPOSURE_BRIGHT_THRESHOLD = 225.0
CLIPPING_RATIO_THRESHOLD = 0.05
SETUP_MAX_DURATION_SEC = 5.0
SETUP_FIRST_LAST_K = 1

# --- sampling ---
SAMPLE_N_PER_SHOT = 4
SAMPLE_MIN_SECONDS = 0.5

LAYER = "shot_quality"


# ---------------- frame extraction (batched per shot) ----------------

def _sample_times(start_sec: float, end_sec: float, duration_sec: float) -> list[float]:
    if duration_sec < SAMPLE_MIN_SECONDS:
        return [(start_sec + end_sec) / 2.0]
    if duration_sec < 2.0:
        return [start_sec + duration_sec * 0.5]
    if duration_sec < 4.0:
        return [start_sec + duration_sec * 0.25, start_sec + duration_sec * 0.75]
    return [
        start_sec + duration_sec * 0.10,
        start_sec + duration_sec * 0.35,
        start_sec + duration_sec * 0.65,
        start_sec + duration_sec * 0.90,
    ]


def _extract_frame(proxy: Path, t: float, timeout_sec: int = 20):
    import cv2
    cmd = [
        FFMPEG, "-hide_banner", "-loglevel", "error", "-nostdin",
        "-ss", f"{t:.3f}", "-i", str(proxy),
        "-frames:v", "1",
        "-vf", "scale='if(gt(iw,ih),min(720,iw),-2)':'if(gt(iw,ih),-2,min(720,ih))'",
        "-f", "image2pipe", "-vcodec", "mjpeg", "-",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout_sec)
    except subprocess.TimeoutExpired:
        return None
    if proc.returncode != 0 or not proc.stdout:
        return None
    arr = np.frombuffer(proc.stdout, dtype=np.uint8)
    return cv2.imdecode(arr, cv2.IMREAD_COLOR)


# ---------------- metric computations ----------------

def _sharpness(img_bgr) -> float:
    import cv2
    gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
    return float(cv2.Laplacian(gray, cv2.CV_64F).var())


def _motion_between(img_a, img_b) -> float:
    import cv2
    if img_a is None or img_b is None:
        return 0.0
    if img_a.shape != img_b.shape:
        img_b = cv2.resize(img_b, (img_a.shape[1], img_a.shape[0]))
    a = cv2.cvtColor(img_a, cv2.COLOR_BGR2GRAY).astype(np.int16)
    b = cv2.cvtColor(img_b, cv2.COLOR_BGR2GRAY).astype(np.int16)
    return float(np.mean(np.abs(a - b)))


def _exposure_stats(img_bgr) -> tuple[float, float]:
    import cv2
    gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
    mean = float(np.mean(gray))
    total = gray.size
    clipped = int(np.sum((gray == 0) | (gray == 255)))
    return mean, clipped / max(1, total)


# ---------------- per-shot pipeline ----------------

def _process_shot(asset_id: str, shot_idx: int, proxy: Path,
                  start_sec: float, end_sec: float, duration_sec: float,
                  is_first_or_last: bool) -> dict:
    times = _sample_times(start_sec, end_sec, duration_sec)
    frames = []
    for t in times:
        img = _extract_frame(proxy, t)
        if img is not None:
            frames.append(img)
    if not frames:
        return {"shot_idx": shot_idx, "success": False}

    sharps = [_sharpness(f) for f in frames]
    sharpness_score = float(np.mean(sharps))
    if len(frames) >= 2:
        motions = [_motion_between(frames[i], frames[i + 1]) for i in range(len(frames) - 1)]
        motion_score = float(np.mean(motions))
    else:
        motion_score = 0.0
    exps = [_exposure_stats(f) for f in frames]
    exposure_mean = float(np.mean([e[0] for e in exps]))
    clipping_ratio = float(np.mean([e[1] for e in exps]))

    is_blurry = sharpness_score < SHARPNESS_BLURRY_THRESHOLD
    is_dark = exposure_mean < EXPOSURE_DARK_THRESHOLD
    is_blown = exposure_mean > EXPOSURE_BRIGHT_THRESHOLD or clipping_ratio > CLIPPING_RATIO_THRESHOLD
    is_setup = bool(is_first_or_last and duration_sec <= SETUP_MAX_DURATION_SEC)

    return {
        "success": True,
        "shot_idx": shot_idx,
        "n_frames_sampled": len(frames),
        "sharpness_score": sharpness_score,
        "motion_score": motion_score,
        "exposure_mean": exposure_mean,
        "clipping_ratio": clipping_ratio,
        "is_blurry": int(is_blurry),
        "is_in_focus": int(not is_blurry),
        "is_dark": int(is_dark),
        "is_blown": int(is_blown),
        "is_setup_or_teardown": int(is_setup),
    }


# ---------------- run ----------------

def cmd_run(args: argparse.Namespace) -> None:
    run_path = start_run_log(LAYER, vars(args))
    print(f"=== build_shot_quality run | {now_iso()} ===")

    # Discover unprocessed video assets that have a `shots` block populated.
    candidates = []
    for p in VIDEO_CATALOG.glob("*.video.json"):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        aid = d.get("asset_id")
        if not aid:
            continue
        shots = (d.get("shots") or {}).get("items") or []
        if not shots:
            continue
        if is_layer_processed(aid, "video", LAYER):
            continue
        candidates.append((aid, d))
    print(f"  unprocessed video assets with shots: {len(candidates)}")
    if args.limit:
        candidates = candidates[: args.limit]
        print(f"  --limit: {len(candidates)}")
    if not candidates:
        print("nothing to do.")
        finish_run_log(run_path, {"processed": 0, "note": "no_work"})
        return

    counters = {"assets_processed": 0, "shots_processed": 0, "shots_errors": 0,
                "blurry": 0, "setup": 0, "dark": 0, "blown": 0, "no_proxy": 0}
    t_start = time.time()
    last_print = [time.time()]

    pool = ThreadPoolExecutor(max_workers=args.workers, thread_name_prefix="sq")

    for asset_idx, (aid, doc) in enumerate(candidates, start=1):
        proxy = resolve_proxy_via_asset_map(aid)
        if proxy is None or not proxy.exists():
            counters["no_proxy"] += 1
            # Mark processed with empty items so we don't retry indefinitely
            update_layer(aid, "video", LAYER, {
                "processed_at": now_iso(),
                "error": "no_proxy_on_disk",
                "items": [],
            })
            continue
        shot_items = (doc.get("shots") or {}).get("items") or []
        if not shot_items:
            continue
        min_idx = min(s["shot_idx"] for s in shot_items)
        max_idx = max(s["shot_idx"] for s in shot_items)
        work = [(aid, s["shot_idx"], proxy, s["start_sec"], s["end_sec"], s["duration_sec"],
                 s["shot_idx"] == min_idx or s["shot_idx"] == max_idx)
                for s in shot_items]
        # Parallelize within an asset
        futures = [pool.submit(_process_shot, *w) for w in work]
        out_items = []
        for fut in futures:
            try:
                res = fut.result()
            except Exception:
                counters["shots_errors"] += 1
                continue
            if not res["success"]:
                counters["shots_errors"] += 1
                continue
            counters["shots_processed"] += 1
            counters["blurry"] += res["is_blurry"]
            counters["setup"] += res["is_setup_or_teardown"]
            counters["dark"] += res["is_dark"]
            counters["blown"] += res["is_blown"]
            out_items.append({k: v for k, v in res.items() if k != "success"})
        out_items.sort(key=lambda r: r["shot_idx"])
        update_layer(aid, "video", LAYER, {
            "processed_at": now_iso(),
            "items": out_items,
        })
        counters["assets_processed"] += 1

        now = time.time()
        if now - last_print[0] >= 15:
            el = now - t_start
            rate = counters["assets_processed"] / el if el else 0
            eta_min = (len(candidates) - asset_idx) / rate / 60 if rate else 0
            print(
                f"[{asset_idx:>5}/{len(candidates)}] {100*asset_idx/len(candidates):5.1f}%  "
                f"assets={counters['assets_processed']}  shots={counters['shots_processed']}  "
                f"blurry={counters['blurry']}  setup={counters['setup']}  "
                f"err={counters['shots_errors']}  no_proxy={counters['no_proxy']}  "
                f"elapsed={el/60:5.1f}m ETA={eta_min:5.1f}m",
                flush=True,
            )
            last_print[0] = now

    pool.shutdown(wait=True)
    elapsed = time.time() - t_start
    summary = {**counters, "elapsed_sec": round(elapsed, 1), "total_assets": len(candidates)}
    finish_run_log(run_path, summary)
    print(f"\n=== Summary ===")
    print(f"Elapsed: {elapsed/60:.1f} min")
    for k, v in counters.items():
        print(f"  {k:<18s}: {v}")


# ---------------- status ----------------

def cmd_status(args: argparse.Namespace) -> None:
    n_assets = 0
    n_processed = 0
    n_shots = 0
    n_blurry = 0
    n_setup = 0
    n_dark = 0
    n_blown = 0
    n_focus_usable = 0
    sharps: list[float] = []
    for p in VIDEO_CATALOG.glob("*.video.json"):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        n_assets += 1
        block = d.get("shot_quality") or {}
        if not block.get("processed_at"):
            continue
        n_processed += 1
        for it in block.get("items") or []:
            n_shots += 1
            n_blurry += it.get("is_blurry") or 0
            n_setup += it.get("is_setup_or_teardown") or 0
            n_dark += it.get("is_dark") or 0
            n_blown += it.get("is_blown") or 0
            if it.get("is_in_focus") and not it.get("is_setup_or_teardown"):
                n_focus_usable += 1
            s = it.get("sharpness_score")
            if s is not None:
                sharps.append(s)
    print(f"=== shot_quality coverage (per-asset video.json) ===")
    print(f"  video assets:                 {n_assets}")
    print(f"  processed:                    {n_processed}")
    print(f"  total shots scored:           {n_shots}")
    if n_shots:
        print(f"  blurry:                       {n_blurry}  ({100*n_blurry/n_shots:.1f}%)")
        print(f"  setup/teardown:               {n_setup}  ({100*n_setup/n_shots:.1f}%)")
        print(f"  dark:                         {n_dark}  ({100*n_dark/n_shots:.1f}%)")
        print(f"  blown:                        {n_blown}  ({100*n_blown/n_shots:.1f}%)")
        print(f"  in-focus & not setup (usable):{n_focus_usable}  ({100*n_focus_usable/n_shots:.1f}%)")
    if sharps:
        sharps.sort()
        print(f"\n  sharpness percentiles:")
        for p in (10, 25, 50, 75, 90, 99):
            idx = min(len(sharps) - 1, len(sharps) * p // 100)
            print(f"    p{p:>2d}: {sharps[idx]:8.1f}")


# ---------------- main ----------------

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_run = sub.add_parser("run", help="Compute quality metrics for unprocessed video assets")
    p_run.add_argument("--workers", type=int, default=4)
    p_run.add_argument("--limit", type=int, default=None)
    p_run.set_defaults(func=cmd_run)

    p_status = sub.add_parser("status", help="Coverage + flag stats")
    p_status.set_defaults(func=cmd_status)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
