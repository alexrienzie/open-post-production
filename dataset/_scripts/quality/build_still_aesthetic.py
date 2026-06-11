#!/usr/bin/env python3
"""build_still_aesthetic.py — Per-still NIMA aesthetic scoring.

Companion to `build_aesthetic.py` (which scores video shots). Same NIMA model,
same calibration approach, but operates on cataloged stills.

Scores now live in
catalog JSON under `still.json["still_aesthetic"]`. Run logs go to
`_runs/ingest_pipeline/still_aesthetic/<timestamp>Z.run.json`.

Stills the runner can read: JPG, JPEG, PNG, WebP (~897 of 1,243 stills).
Skipped: ARW, DNG (Sony / Adobe RAW need rawpy), HEIC (needs pillow-heif).

Subcommands:
  run     Full pass over readable stills
  status  Coverage + score distribution
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import warnings
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from _paths import (  # noqa: E402
    DERIVATIVE_MEDIA, STILLS_CATALOG, derivative_relative,
    resolve_proxy_via_asset_map,
)
from _catalog_layer_io import (  # noqa: E402
    now_iso, is_layer_processed, update_layer,
    start_run_log, finish_run_log,
)

warnings.filterwarnings("ignore", message="urllib3 v2 only supports OpenSSL")

AESTHETIC_THRESHOLD = 4.05
SUPPORTED_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
LAYER = "still_aesthetic"


def _resolve_still_path(rec: dict) -> Path | None:
    aid = rec.get("asset_id")
    sp = rec.get("source_path") or ""
    if not sp:
        return None
    ext = sp.rsplit(".", 1)[-1].lower() if "." in sp else ""
    if f".{ext}" not in SUPPORTED_EXTS:
        return None
    if aid:
        from_map = resolve_proxy_via_asset_map(aid, kind="still")
        if from_map and from_map.exists():
            return from_map
    try:
        rel = derivative_relative(sp)
    except Exception:
        return None
    candidate = DERIVATIVE_MEDIA / rel
    if candidate.exists():
        return candidate
    return None


def cmd_run(args: argparse.Namespace) -> None:
    import torch
    import pyiqa
    from PIL import Image

    run_path = start_run_log(LAYER, vars(args))
    print(f"=== build_still_aesthetic | {now_iso()} ===")
    print(f"  device: {args.device}    threshold: {args.threshold}")

    print("  loading NIMA model...")
    t0 = time.time()
    iqa = pyiqa.create_metric("nima", device=args.device)
    iqa.eval()
    print(f"    loaded in {time.time() - t0:.1f}s")

    print("  walking stills catalog...")
    work: list[tuple[str, Path, str]] = []
    n_skip_ext = 0
    n_skip_missing = 0
    n_skip_already = 0
    for f in STILLS_CATALOG.glob("*.still.json"):
        if f.name.startswith("._"):
            continue
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        aid = d.get("asset_id")
        if not aid:
            continue
        if is_layer_processed(aid, "still", LAYER):
            n_skip_already += 1
            continue
        sp = d.get("source_path") or ""
        ext = sp.rsplit(".", 1)[-1].lower() if "." in sp else ""
        if f".{ext}" not in SUPPORTED_EXTS:
            n_skip_ext += 1
            continue
        path = _resolve_still_path(d)
        if path is None:
            n_skip_missing += 1
            continue
        work.append((aid, path, ext))
    print(f"  already-scored: {n_skip_already}")
    print(f"  skipped (RAW/HEIC/other): {n_skip_ext}")
    print(f"  skipped (file missing on disk): {n_skip_missing}")
    print(f"  effective work: {len(work)}")
    if args.limit:
        work = work[: args.limit]
        print(f"  --limit: {len(work)}")
    if not work:
        finish_run_log(run_path, {"processed": 0, "note": "no_work"})
        return

    n_done = 0
    n_errors = 0
    t_start = time.time()
    for i, (aid, path, ext) in enumerate(work):
        try:
            img = Image.open(path).convert("RGB")
            arr = np.array(img)
            tensor = (
                torch.from_numpy(arr).permute(2, 0, 1).unsqueeze(0).float() / 255.0
            ).to(args.device)
            with torch.no_grad():
                score = float(iqa(tensor).item())
        except Exception:
            n_errors += 1
            continue
        is_aes = int(score >= args.threshold)
        update_layer(aid, "still", LAYER, {
            "processed_at": now_iso(),
            "metrics": {
                "aesthetic_score": score,
                "is_aesthetic": is_aes,
                "image_ext": ext,
            },
        })
        n_done += 1
        if (i + 1) % 100 == 0:
            elapsed = time.time() - t_start
            rate = (i + 1) / elapsed
            eta = (len(work) - i - 1) / rate / 60 if rate > 0 else 0
            print(f"  [{i+1:>4}/{len(work)}] done={n_done} err={n_errors} "
                  f"rate={rate*60:.0f}/min ETA={eta:.0f}m")
    elapsed = time.time() - t_start
    summary = {"processed": n_done, "errors": n_errors,
               "elapsed_sec": round(elapsed, 1), "total_assets": len(work)}
    finish_run_log(run_path, summary)
    print(f"\nrun complete: {n_done} scored, {n_errors} errors, "
          f"{elapsed/60:.1f}m wall-clock")


def cmd_status(args: argparse.Namespace) -> None:
    scores: list[float] = []
    n = 0
    n_pub = 0
    for f in STILLS_CATALOG.glob("*.still.json"):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        m = (d.get("still_aesthetic") or {}).get("metrics") or {}
        if not m or m.get("aesthetic_score") is None:
            continue
        n += 1
        scores.append(m["aesthetic_score"])
        if m.get("is_aesthetic"):
            n_pub += 1
    print(f"still_aesthetic rows (catalog JSON): {n:,}   is_aesthetic=1: {n_pub:,}")
    if n:
        scores.sort()
        m = len(scores)
        pct = lambda p: scores[int(m * p)]
        print(f"  score min={scores[0]:.2f} p25={pct(0.25):.2f} median={pct(0.5):.2f} "
              f"p75={pct(0.75):.2f} p95={pct(0.95):.2f} max={scores[-1]:.2f}")


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
