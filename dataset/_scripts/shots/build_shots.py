#!/usr/bin/env python3
"""build_shots.py — Per-asset shot-boundary detection via PySceneDetect.


Walks the video catalog, resolves each asset's proxy via asset_map, runs
PySceneDetect ContentDetector, writes the shot boundaries into the per-asset
catalog JSON under `video.json["shots"]` (atomic merge). Idempotent at the
asset level: skips assets whose `shots.processed_at` is already set.

Subcommands:
  run       Detect shots for unprocessed assets (default scope: all video except timelapse)
  status    Coverage stats

Signals now live in catalog
JSON. Run logs land at `_runs/ingest_pipeline/shots/<timestamp>Z.run.json`.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from _paths import (  # noqa: E402
    INDEXES_DIR, VIDEO_CATALOG,
    resolve_proxy_via_asset_map,
)
from _catalog_layer_io import (  # noqa: E402
    now_iso, is_layer_processed, update_layer, load_catalog,
    start_run_log, finish_run_log,
)

warnings.filterwarnings("ignore", message="urllib3 v2 only supports OpenSSL")

DEFAULT_THRESHOLD = 27.0
DEFAULT_MIN_SCENE_LEN = 15
SKIP_ASSET_TYPES = ("timelapse",)
LAYER = "shots"


def _detect_one(args_tuple):
    """Run scene detection on one proxy. Returns dict with shots or error."""
    asset_id, proxy_path, threshold, min_scene_len = args_tuple
    from scenedetect import open_video, SceneManager, ContentDetector
    t0 = time.time()
    try:
        video = open_video(str(proxy_path))
        sm = SceneManager()
        sm.add_detector(ContentDetector(threshold=threshold, min_scene_len=min_scene_len))
        sm.detect_scenes(video, show_progress=False)
        scenes = sm.get_scene_list()
    except Exception as e:
        return {"asset_id": asset_id, "error": f"{type(e).__name__}: {e}",
                "detect_time_sec": time.time() - t0}
    duration_sec = video.duration.get_seconds() if hasattr(video, "duration") else None
    return {
        "asset_id": asset_id,
        "proxy_path": str(proxy_path),
        "duration_sec": duration_sec,
        "shots": [(s.get_seconds(), e.get_seconds()) for s, e in scenes],
        "detect_time_sec": time.time() - t0,
    }


def _candidate_asset_ids(skip_types: tuple[str, ...]) -> list[str]:
    """Pull `record_kind='video'` asset_ids from editorial_catalog.sqlite,
    excluding `asset_type` in skip_types. Falls back to walking the catalog
    directory if editorial_catalog.sqlite isn't available."""
    ec_path = INDEXES_DIR / "editorial_catalog.sqlite"
    if ec_path.exists():
        con = sqlite3.connect(f"file:{ec_path}?mode=ro", uri=True)
        skip_clause = ",".join(f"'{t}'" for t in skip_types)
        rows = con.execute(f"""
            SELECT asset_id FROM asset
            WHERE record_kind='video'
              AND (asset_type IS NULL OR asset_type NOT IN ({skip_clause}))
            ORDER BY duration_sec DESC NULLS LAST
        """).fetchall()
        con.close()
        return [r[0] for r in rows]
    # Fallback: walk catalog dir directly
    out = []
    for p in VIDEO_CATALOG.glob("*.video.json"):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        atype = (d.get("asset_classifications") or {}).get("type")
        if atype in skip_types:
            continue
        out.append(d.get("asset_id"))
    return [x for x in out if x]


def cmd_run(args: argparse.Namespace) -> None:
    run_path = start_run_log(LAYER, vars(args))
    print(f"=== build_shots run | {now_iso()} ===")
    print(f"  threshold={args.threshold}  min_scene_len={args.min_scene_len}  workers={args.workers}")

    candidates = _candidate_asset_ids(SKIP_ASSET_TYPES)
    print(f"  candidates from catalog: {len(candidates)}")

    # Skip already-processed (check catalog JSON for shots.processed_at)
    candidates = [a for a in candidates if not is_layer_processed(a, "video", LAYER)]
    print(f"  remaining to process: {len(candidates)}")

    if args.asset_ids:
        wanted = {ln.strip() for ln in open(args.asset_ids) if ln.strip()}
        candidates = [a for a in candidates if a in wanted]
        print(f"  --asset-ids filter: {len(candidates)}")
    if args.limit:
        candidates = candidates[: args.limit]
        print(f"  --limit: {len(candidates)}")
    if not candidates:
        print("nothing to do.")
        finish_run_log(run_path, {"processed": 0, "note": "no_work"})
        return

    # Resolve proxies
    work = []
    no_proxy = 0
    for aid in candidates:
        proxy = resolve_proxy_via_asset_map(aid)
        if proxy is None or not proxy.exists():
            no_proxy += 1
            continue
        work.append((aid, proxy, args.threshold, args.min_scene_len))
    if no_proxy:
        print(f"  skipped (no proxy on disk): {no_proxy}")
    print(f"  effective work: {len(work)}")

    counters = {"processed": 0, "errors": 0, "shots_total": 0, "single_shot": 0}
    t_start = time.time()
    last_print = [time.time()]

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futures = {ex.submit(_detect_one, w): w[0] for w in work}
        for fut in as_completed(futures):
            res = fut.result()
            aid = res["asset_id"]
            if "error" in res:
                counters["errors"] += 1
                # Mark processed even on error so we don't retry indefinitely.
                # Errors are visible via n_shots=-1 in the catalog JSON.
                update_layer(aid, "video", LAYER, {
                    "processed_at": now_iso(),
                    "detector": "PySceneDetect_ContentDetector",
                    "params": {"threshold": args.threshold, "min_scene_len": args.min_scene_len},
                    "n_shots": -1,
                    "error": res["error"],
                    "detect_time_sec": res.get("detect_time_sec"),
                    "items": [],
                })
            else:
                raw_scenes = res["shots"]
                dur = res.get("duration_sec")
                # PySceneDetect returns empty list for single-take footage;
                # emit a synthetic shot covering [0, duration_sec] so joins
                # like `shots × frame_face` don't silently drop them.
                if not raw_scenes and dur and dur > 0:
                    items = [{"shot_idx": 0, "start_sec": 0.0, "end_sec": dur, "duration_sec": dur}]
                else:
                    items = [{"shot_idx": i, "start_sec": s, "end_sec": e, "duration_sec": e - s}
                             for i, (s, e) in enumerate(raw_scenes)]
                n = len(items)
                counters["processed"] += 1
                counters["shots_total"] += n
                if n <= 1:
                    counters["single_shot"] += 1
                update_layer(aid, "video", LAYER, {
                    "processed_at": now_iso(),
                    "detector": "PySceneDetect_ContentDetector",
                    "params": {"threshold": args.threshold, "min_scene_len": args.min_scene_len},
                    "proxy_path": res["proxy_path"],
                    "duration_sec": dur,
                    "detect_time_sec": res["detect_time_sec"],
                    "n_shots": n,
                    "items": items,
                })

            done = counters["processed"] + counters["errors"]
            now = time.time()
            if now - last_print[0] >= 15:
                el = now - t_start
                rate = done / el if el else 0
                eta_min = (len(work) - done) / rate / 60 if rate else 0
                print(
                    f"[{done:>5}/{len(work)}] {100*done/len(work):5.1f}%  "
                    f"rate={rate:5.2f}/s  shots={counters['shots_total']:6d}  "
                    f"single={counters['single_shot']:4d}  err={counters['errors']}  "
                    f"elapsed={el/60:5.1f}m  ETA={eta_min:5.1f}m",
                    flush=True,
                )
                last_print[0] = now

    elapsed = time.time() - t_start
    summary = {**counters, "elapsed_sec": round(elapsed, 1), "total_assets": len(work)}
    finish_run_log(run_path, summary)
    print(f"\n=== Summary ===")
    print(f"Elapsed: {elapsed/60:.1f} min")
    for k, v in counters.items():
        print(f"  {k:<14s}: {v}")


def cmd_status(args: argparse.Namespace) -> None:
    """Coverage stats — walks the video catalog and counts assets with a populated `shots` block."""
    n_assets = 0
    n_processed = 0
    n_err = 0
    n_single = 0
    n_shots_total = 0
    top = []  # (n_shots, asset_id, duration, shoot_label)
    for p in VIDEO_CATALOG.glob("*.video.json"):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        n_assets += 1
        block = d.get("shots") or {}
        if not block.get("processed_at"):
            continue
        n_processed += 1
        n = block.get("n_shots", len(block.get("items") or []))
        if n == -1:
            n_err += 1
        if n <= 1:
            n_single += 1
        if n > 0:
            n_shots_total += n
            shoot = (d.get("path_metadata") or {}).get("shoot_label") or "?"
            top.append((n, d.get("asset_id", ""), block.get("duration_sec") or 0, shoot,
                        (d.get("asset_classifications") or {}).get("type")))
    print(f"=== shots coverage (per-asset video.json) ===")
    print(f"  catalog video assets: {n_assets}")
    print(f"  processed:            {n_processed}  (errors: {n_err}, single-shot: {n_single})")
    print(f"  total shots:          {n_shots_total}")
    if n_processed:
        avg = n_shots_total / max(1, n_processed - n_err)
        print(f"  avg shots/asset:      {avg:.1f}")
    top.sort(key=lambda x: -x[0])
    print(f"\n  top 10 assets by shot count:")
    for n, aid, dur, shoot, atype in top[:10]:
        print(f"    {aid[:12]}  {n:4d} shots  {dur:6.0f}s  {shoot:26s}  {atype or '?'}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_run = sub.add_parser("run", help="Detect shots for unprocessed video assets")
    p_run.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD,
                       help=f"ContentDetector threshold (default: {DEFAULT_THRESHOLD})")
    p_run.add_argument("--min-scene-len", type=int, default=DEFAULT_MIN_SCENE_LEN,
                       help=f"Min frames per scene; suppresses flash double-counts (default: {DEFAULT_MIN_SCENE_LEN})")
    p_run.add_argument("--workers", type=int, default=4,
                       help="Parallel detection workers (default: 4)")
    p_run.add_argument("--limit", type=int, default=None)
    p_run.add_argument("--asset-ids", type=str, default=None,
                       help="File with one asset_id per line; restrict to these")
    p_run.set_defaults(func=cmd_run)

    p_status = sub.add_parser("status", help="Coverage stats")
    p_status.set_defaults(func=cmd_status)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
