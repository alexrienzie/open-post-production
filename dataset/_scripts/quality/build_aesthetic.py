#!/usr/bin/env python3
"""build_aesthetic.py — Per-shot NIMA aesthetic scoring.

Mutates each video's `video.json["shot_quality"]["items"][i]` in place to add
three fields per shot:
  aesthetic_score REAL  -- 1.0-10.0 mean (NIMA outputs a 10-bin distribution)
  is_aesthetic    INTEGER -- score >= AESTHETIC_THRESHOLD = publishable B-roll
  aesthetic_at    TEXT   -- ISO8601 timestamp of the scoring

NIMA (Neural Image Assessment) was trained on the AVA dataset (255K photographer
ratings). Scores in roughly [3.0..8.0]; ~5.5 is the typical 'good photo' cutoff.

Caveats:
  - NIMA trained on photography, not video. Motion-blur, rolling shutter and
    proxy compression can drag scores. Expect ~10% noise.
  - Aesthetic ≠ editorial. A tightly-framed talking head scores low aesthetically
    but is exactly what you want for an interview. Use this in combination with
    asset_type filter, not alone.

Operates on per-asset
catalog JSON. Idempotent: skips shots whose item already has `aesthetic_score`.

Subcommands:
  run     Score every shot_quality item missing aesthetic_score. Resumable.
  status  Distribution + count by is_aesthetic.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import warnings
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from _paths import (  # noqa: E402
    VIDEO_CATALOG, resolve_proxy_via_asset_map,
)
from _catalog_layer_io import (  # noqa: E402
    now_iso, load_catalog, update_layer,
    start_run_log, finish_run_log,
)

warnings.filterwarnings("ignore", message="urllib3 v2 only supports OpenSSL")

FFMPEG = "/opt/homebrew/bin/ffmpeg"
AESTHETIC_THRESHOLD = 4.05  # Calibrated p85 of doc-footage NIMA distribution
SAMPLE_FRAME_WIDTH = 384
LAYER = "shot_quality"  # we mutate the existing shot_quality block


def extract_frame_rgb(proxy_path: Path, t_sec: float):
    try:
        proc = subprocess.run(
            [FFMPEG, "-hide_banner", "-loglevel", "error", "-nostdin",
             "-ss", f"{max(0.0, t_sec):.2f}", "-i", str(proxy_path),
             "-frames:v", "1",
             "-vf", f"scale={SAMPLE_FRAME_WIDTH}:-1",
             "-f", "image2pipe", "-c:v", "mjpeg", "-"],
            capture_output=True, timeout=10,
        )
    except subprocess.TimeoutExpired:
        return None
    if proc.returncode != 0 or not proc.stdout:
        return None
    import cv2
    arr = np.frombuffer(proc.stdout, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        return None
    return cv2.cvtColor(img, cv2.COLOR_BGR2RGB)


def _shot_sample_times(start_sec: float, end_sec: float) -> list[float]:
    dur = max(0.0, end_sec - start_sec)
    if dur < 0.5:
        return []
    if dur < 5.0:
        return [start_sec + dur * 0.5]
    return [start_sec + dur * 0.25, start_sec + dur * 0.5, start_sec + dur * 0.75]


def cmd_run(args: argparse.Namespace) -> None:
    import torch
    import pyiqa

    run_path = start_run_log("aesthetic", vars(args))
    print(f"=== build_aesthetic | {now_iso()} ===")
    print(f"  device: {args.device}    threshold: {args.threshold}")

    print("  loading NIMA model (first run downloads ~17 MB)...")
    t0 = time.time()
    iqa = pyiqa.create_metric("nima", device=args.device)
    iqa.eval()
    print(f"    loaded in {time.time() - t0:.1f}s")

    # Walk catalog: find shots needing aesthetic score
    candidates: list[tuple[str, dict]] = []  # (asset_id, doc)
    n_already_scored = 0
    for p in VIDEO_CATALOG.glob("*.video.json"):
        if p.name.startswith("._"):
            continue
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        sq = (d.get("shot_quality") or {}).get("items") or []
        if not sq:
            continue
        unscored = [it for it in sq if it.get("aesthetic_score") is None]
        if not unscored:
            n_already_scored += 1
            continue
        candidates.append((d.get("asset_id"), d))
    print(f"  assets fully-scored already: {n_already_scored}")
    print(f"  assets with unscored shots:  {len(candidates)}")
    if args.limit:
        candidates = candidates[: args.limit]
        print(f"  --limit: {len(candidates)}")
    if not candidates:
        finish_run_log(run_path, {"scored": 0, "note": "no_work"})
        return

    n_scored = 0
    n_skipped = 0
    t_start = time.time()
    for asset_idx, (aid, doc) in enumerate(candidates, start=1):
        proxy = resolve_proxy_via_asset_map(aid)
        if proxy is None or not proxy.exists():
            n_skipped += 1
            continue

        # Map shot_idx -> (start_sec, end_sec) from the shots block
        shot_times = {s["shot_idx"]: (s["start_sec"], s["end_sec"])
                      for s in ((doc.get("shots") or {}).get("items") or [])}
        sq_items = (doc.get("shot_quality") or {}).get("items") or []
        modified = False
        for it in sq_items:
            if it.get("aesthetic_score") is not None:
                continue
            st = shot_times.get(it["shot_idx"])
            if st is None:
                continue
            sample_ts = _shot_sample_times(*st)
            if not sample_ts:
                continue
            scores = []
            for t_sec in sample_ts:
                img = extract_frame_rgb(proxy, t_sec)
                if img is None:
                    continue
                tensor = (
                    torch.from_numpy(img).permute(2, 0, 1).unsqueeze(0).float() / 255.0
                ).to(args.device)
                with torch.no_grad():
                    s = float(iqa(tensor).item())
                scores.append(s)
            if not scores:
                continue
            mean_score = float(np.mean(scores))
            it["aesthetic_score"] = mean_score
            it["is_aesthetic"] = int(mean_score >= args.threshold)
            it["aesthetic_at"] = now_iso()
            modified = True
            n_scored += 1
        if modified:
            sq_block = doc.get("shot_quality") or {}
            sq_block["items"] = sq_items
            update_layer(aid, "video", LAYER, sq_block)

        if asset_idx % 50 == 0:
            elapsed = time.time() - t_start
            rate = asset_idx / elapsed if elapsed else 0
            eta = (len(candidates) - asset_idx) / rate / 60 if rate else 0
            print(f"  [{asset_idx:>5}/{len(candidates)}] scored={n_scored} "
                  f"skip={n_skipped} rate={rate*60:.0f} assets/min ETA={eta:.0f}m")

    elapsed = time.time() - t_start
    finish_run_log(run_path, {
        "scored": n_scored, "skipped": n_skipped,
        "elapsed_sec": round(elapsed, 1), "total_assets": len(candidates),
    })
    print(f"\nrun complete: scored {n_scored:,}, skipped {n_skipped:,} "
          f"in {elapsed/60:.1f}m")


def cmd_status(args: argparse.Namespace) -> None:
    n_total = 0
    n_scored = 0
    n_pub = 0
    scores: list[float] = []
    for p in VIDEO_CATALOG.glob("*.video.json"):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        for it in (d.get("shot_quality") or {}).get("items") or []:
            n_total += 1
            s = it.get("aesthetic_score")
            if s is not None:
                n_scored += 1
                scores.append(s)
                if it.get("is_aesthetic"):
                    n_pub += 1
    print(f"shot_quality items: {n_total:,}   aesthetic-scored: {n_scored:,}   "
          f"is_aesthetic=1: {n_pub:,}")
    if scores:
        scores.sort()
        pct = lambda p: scores[int(len(scores) * p)]
        print(f"  score: min={scores[0]:.2f} p25={pct(0.25):.2f} "
              f"median={pct(0.5):.2f} p75={pct(0.75):.2f} max={scores[-1]:.2f}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("run")
    sp.add_argument("--limit", type=int)
    sp.add_argument("--device", default="mps", choices=["mps", "cpu", "cuda"])
    sp.add_argument("--threshold", type=float, default=AESTHETIC_THRESHOLD)
    sp.set_defaults(func=cmd_run)
    sp = sub.add_parser("status")
    sp.set_defaults(func=cmd_status)
    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
