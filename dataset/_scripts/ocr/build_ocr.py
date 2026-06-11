#!/usr/bin/env python3
"""build_ocr.py — Shot-aware OCR pipeline.

Runs BOTH engines (RapidOCR + Apple Vision) over a shot-aware keyframe sample
of the video corpus + all cataloged stills.

Detections now live in catalog
JSON under `video.json["ocr_detections"]` and `still.json["ocr_detections"]`,
with `bib_hits` as a sibling top-level block on the same JSON. Idempotency
tracker `processed_frames[]` lives inside the `ocr_detections` block, keyed by
(frame_time_sec, ocr_engine). Run logs go to `_runs/ingest_pipeline/ocr/`.

Subcommands:
  run         Full corpus pass (video shots + stills)
  still       Re-run only on cataloged stills
  status      Coverage stats
  derive-bibs Apply the numeric bib post-filter to existing ocr_detections
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import warnings
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from _ocr import (  # noqa: E402
    run_rapidocr, run_apple_vision, extract_frame_at,
    get_rapidocr, is_bib_text, normalized_bbox_to_pixels,
)
from _paths import (  # noqa: E402
    DERIVATIVE_MEDIA, VIDEO_CATALOG, STILLS_CATALOG,
    derivative_relative, resolve_proxy_via_asset_map,
)
from _catalog_layer_io import (  # noqa: E402
    now_iso, load_catalog, update_layer,
    start_run_log, finish_run_log,
)

warnings.filterwarnings("ignore", message="urllib3 v2 only supports OpenSSL")

LAYER = "ocr_detections"
BIB_LAYER = "bib_hits"

_IMG_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff", ".webp"}


# ---------------- frame fetch helpers ----------------

def _fetch_frame_video(asset_id: str, frame_time_sec: float):
    proxy = resolve_proxy_via_asset_map(asset_id)
    if proxy is None or not proxy.exists():
        return None, None
    img = extract_frame_at(proxy, frame_time_sec)
    return proxy, img


def _fetch_frame_still(source_path: str):
    import cv2
    try:
        rel = derivative_relative(source_path)
    except ValueError:
        return None, None
    disk = DERIVATIVE_MEDIA / rel
    if not disk.exists():
        return None, None
    ext = disk.suffix.lower()
    if ext in (".arw", ".dng", ".cr2", ".cr3", ".nef", ".raf", ".orf"):
        return disk, None  # RAW, skipped
    if ext in (".heic", ".heif"):
        img = extract_frame_at(disk, 0.0)
        return disk, img
    return disk, cv2.imread(str(disk))


# ---------------- engine dispatch ----------------

def _run_one(engine: str, src_path, img) -> list[dict]:
    if engine == "rapidocr":
        return run_rapidocr(img)
    elif engine == "apple_vision":
        use_path = (src_path is not None and src_path.exists()
                    and src_path.suffix.lower() in _IMG_EXTS)
        if use_path:
            hits = run_apple_vision(path=src_path)
        else:
            hits = run_apple_vision(img_bgr=img)
        h, w = img.shape[:2]
        for hit in hits:
            hit["bbox"] = normalized_bbox_to_pixels(hit.pop("bbox_norm"), w, h)
        return hits
    else:
        raise ValueError(f"unknown engine {engine!r}")


# ---------------- per-asset work generation ----------------

def _video_sample_times(start: float, end: float, dur: float) -> list[float]:
    """Original sampling rule: midpoint, +25%/75% if dur>=30, +30s intervals if dur>=120."""
    times = {round((start + end) / 2.0, 2)}
    if dur >= 30.0:
        times.add(round(start + 0.25 * dur, 2))
        times.add(round(start + 0.75 * dur, 2))
    if dur >= 120.0:
        t = start + 30.0
        while t < end - 1.0:
            times.add(round(t, 2))
            t += 30.0
    return sorted(times)


def _build_video_work(engines: list[str], asset_filter: set[str] | None) -> list[tuple]:
    """Yield (asset_id, shot_idx, frame_time_sec, engines_to_run, doc) tuples
    for video frames that aren't yet processed for ALL requested engines."""
    work = []
    for p in VIDEO_CATALOG.glob("*.video.json"):
        if p.name.startswith("._"):
            continue
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        aid = d.get("asset_id")
        if not aid or (asset_filter and aid not in asset_filter):
            continue
        shots = (d.get("shots") or {}).get("items") or []
        if not shots:
            continue
        block = d.get("ocr_detections") or {}
        proc = {(round(f["frame_time_sec"], 2), f["ocr_engine"])
                for f in (block.get("processed_frames") or [])}
        for s in shots:
            for ft in _video_sample_times(s["start_sec"], s["end_sec"], s["duration_sec"]):
                missing = [e for e in engines if (ft, e) not in proc]
                if missing:
                    work.append((aid, s["shot_idx"], ft, missing, "video"))
    return work


def _build_still_work(engines: list[str], asset_filter: set[str] | None) -> list[tuple]:
    work = []
    for p in STILLS_CATALOG.glob("*.still.json"):
        if p.name.startswith("._"):
            continue
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        aid = d.get("asset_id")
        if not aid or (asset_filter and aid not in asset_filter):
            continue
        block = d.get("ocr_detections") or {}
        proc = {(round(f["frame_time_sec"], 2), f["ocr_engine"])
                for f in (block.get("processed_frames") or [])}
        missing = [e for e in engines if (0.0, e) not in proc]
        if missing:
            work.append((aid, None, 0.0, missing, "still", d.get("source_path") or ""))
    return work


# ---------------- per-asset write buffer ----------------

class _AssetBuffer:
    """Accumulates OCR results per asset for atomic write at completion."""
    def __init__(self):
        # asset_id -> {"new_items": [...], "new_processed": [...], "kind": ...}
        self.bufs: dict[str, dict] = defaultdict(lambda: {"new_items": [], "new_processed": []})
        # frames_total[aid] / frames_done[aid] tracks completion
        self.frames_total: dict[str, int] = defaultdict(int)
        self.frames_done: dict[str, int] = defaultdict(int)
        self.kinds: dict[str, str] = {}

    def register(self, aid: str, kind: str, n_frames: int):
        self.frames_total[aid] += n_frames
        self.kinds[aid] = kind

    def add_frame_result(self, aid: str, frame_results: list[dict], proc_records: list[dict]) -> bool:
        """Buffer one frame's results. Returns True if this asset is now complete."""
        b = self.bufs[aid]
        b["new_items"].extend(frame_results)
        b["new_processed"].extend(proc_records)
        self.frames_done[aid] += 1
        return self.frames_done[aid] >= self.frames_total[aid]

    def flush_asset(self, aid: str) -> tuple[int, int]:
        """Merge buffered results into catalog JSON, atomic write. Returns (n_items, n_proc)."""
        cat = load_catalog(aid, self.kinds[aid])
        if cat is None:
            return 0, 0
        existing = cat.get(LAYER) or {}
        items = (existing.get("items") or []) + self.bufs[aid]["new_items"]
        proc = (existing.get("processed_frames") or []) + self.bufs[aid]["new_processed"]
        engines = sorted({it["ocr_engine"] for it in items})
        update_layer(aid, self.kinds[aid], LAYER, {
            "processed_at": now_iso(),
            "engines": engines,
            "processed_frames": proc,
            "items": items,
        })
        n_i, n_p = len(self.bufs[aid]["new_items"]), len(self.bufs[aid]["new_processed"])
        del self.bufs[aid]
        del self.frames_total[aid]
        del self.frames_done[aid]
        return n_i, n_p


# ---------------- run ----------------

def cmd_run(args: argparse.Namespace) -> None:
    import cv2  # noqa: F401

    run_path = start_run_log("ocr", vars(args))
    print(f"=== build_ocr run | {now_iso()} ===")
    print(f"  engines: {args.engines}    workers: {args.workers}")

    engines = args.engines
    if "rapidocr" in engines:
        get_rapidocr()

    asset_filter = None
    if args.asset_ids:
        asset_filter = {ln.strip() for ln in open(args.asset_ids) if ln.strip()}
        print(f"  --asset-ids filter: {len(asset_filter)} ids")

    work: list[tuple] = []
    if not args.stills_only:
        work += _build_video_work(engines, asset_filter)
    if not args.video_only:
        work += _build_still_work(engines, asset_filter)
    if args.limit:
        work = work[: args.limit]
    print(f"  remaining frame-engine pairs to process: {len(work)}")
    if not work:
        finish_run_log(run_path, {"frames_done": 0, "note": "no_work"})
        return

    # Group by (asset_id, frame_time_sec) to fetch once + run multiple engines
    by_frame: dict = defaultdict(list)
    for item in work:
        aid, shot_idx, ft, engines_to_run, kind = item[0], item[1], item[2], item[3], item[4]
        sp = item[5] if len(item) > 5 else None
        by_frame[(aid, ft, kind)].append({"shot_idx": shot_idx, "engines": engines_to_run, "source_path": sp})
    frame_keys = list(by_frame.keys())
    print(f"  distinct frames to fetch: {len(frame_keys)}")

    # Register frames-per-asset for completion tracking
    buf = _AssetBuffer()
    asset_frame_counts: dict[str, int] = defaultdict(int)
    asset_kinds: dict[str, str] = {}
    for (aid, ft, kind) in frame_keys:
        asset_frame_counts[aid] += 1
        asset_kinds[aid] = kind
    for aid, n in asset_frame_counts.items():
        buf.register(aid, asset_kinds[aid], n)

    # Pre-fetch frames in parallel, then run engines synchronously per frame
    extract_pool = ThreadPoolExecutor(max_workers=args.workers, thread_name_prefix="ff")
    in_flight: dict = {}
    PREFETCH = max(args.workers * 4, 16)
    iter_keys = iter(frame_keys)

    def submit_fetch(key):
        aid, ft, kind = key
        items = by_frame[key]
        if kind == "video":
            return extract_pool.submit(_fetch_frame_video, aid, ft)
        else:
            sp = items[0].get("source_path") or ""
            return extract_pool.submit(_fetch_frame_still, sp)

    def prefetch():
        while len(in_flight) < PREFETCH:
            try:
                key = next(iter_keys)
            except StopIteration:
                return
            in_flight[submit_fetch(key)] = key

    counters = {"frames_done": 0, "frames_skipped": 0, "errors": 0, "assets_flushed": 0}
    per_engine = {eng: {"hits": 0, "frames_with_hits": 0} for eng in engines}
    t_start = time.time()
    last_print = [time.time()]

    try:
        prefetch()
        while in_flight:
            done_fut = next(iter(in_flight))
            for f in list(in_flight.keys()):
                if f.done():
                    done_fut = f
                    break
            src_path, img = done_fut.result()
            key = in_flight.pop(done_fut)
            prefetch()
            aid, ft, kind = key
            items = by_frame[key]

            if img is None:
                # Mark skipped for every requested engine
                ts = now_iso()
                proc_records = []
                for item in items:
                    for eng in item["engines"]:
                        proc_records.append({"frame_time_sec": ft, "ocr_engine": eng,
                                             "shot_idx": item.get("shot_idx"),
                                             "n_hits": 0, "processed_at": ts})
                if buf.add_frame_result(aid, [], proc_records):
                    ni, np_ = buf.flush_asset(aid)
                    counters["assets_flushed"] += 1
                counters["frames_skipped"] += 1
                continue

            ts = now_iso()
            new_items = []
            proc_records = []
            for item in items:
                for eng in item["engines"]:
                    try:
                        hits = _run_one(eng, src_path, img)
                    except Exception as e:
                        counters["errors"] += 1
                        print(f"  ERR {aid[:12]} t={ft:.1f} eng={eng}: {e}")
                        continue
                    per_engine[eng]["hits"] += len(hits)
                    if hits:
                        per_engine[eng]["frames_with_hits"] += 1
                    for hit in hits:
                        new_items.append({
                            "shot_idx": item.get("shot_idx"),
                            "frame_time_sec": ft,
                            "bbox_json": json.dumps(hit["bbox"]),
                            "text": hit["text"],
                            "confidence": float(hit["confidence"]),
                            "ocr_engine": eng,
                        })
                    proc_records.append({"frame_time_sec": ft, "ocr_engine": eng,
                                         "shot_idx": item.get("shot_idx"),
                                         "n_hits": len(hits), "processed_at": ts})

            if buf.add_frame_result(aid, new_items, proc_records):
                ni, np_ = buf.flush_asset(aid)
                counters["assets_flushed"] += 1
            counters["frames_done"] += 1

            now = time.time()
            if now - last_print[0] >= 15:
                el = now - t_start
                rate = counters["frames_done"] / el if el else 0
                eta = (len(frame_keys) - counters["frames_done"]) / rate / 60 if rate else 0
                eng_summary = "  ".join(f"{e}: {per_engine[e]['hits']} hits" for e in engines)
                print(
                    f"[{counters['frames_done']:>5}/{len(frame_keys)}] "
                    f"{100*counters['frames_done']/len(frame_keys):5.1f}%  "
                    f"rate={rate:5.2f} frames/s  {eng_summary}  "
                    f"flushed={counters['assets_flushed']} "
                    f"skip={counters['frames_skipped']} err={counters['errors']}  "
                    f"elapsed={el/60:5.1f}m ETA={eta:5.1f}m",
                    flush=True,
                )
                last_print[0] = now
    finally:
        extract_pool.shutdown(wait=True)
        # Flush any assets that didn't complete (partial — should be rare)
        for aid in list(buf.bufs.keys()):
            buf.flush_asset(aid)

    # Derive bib_hits from new ocr_detections
    n_bib = _derive_bibs_full()

    elapsed = time.time() - t_start
    summary = {**counters, "per_engine": per_engine, "elapsed_sec": round(elapsed, 1),
               "total_frames": len(frame_keys), "bibs_derived": n_bib}
    finish_run_log(run_path, summary)
    print(f"\n=== Summary ===")
    print(f"Elapsed: {elapsed/60:.1f} min")
    for k, v in counters.items():
        print(f"  {k:<16s}: {v}")
    print(f"  per engine:")
    for e, c in per_engine.items():
        print(f"    {e:<14s} hits={c['hits']:6d}  frames_with_hits={c['frames_with_hits']:5d}")
    print(f"  bibs derived: {n_bib}")


# ---------------- bib derivation (catalog-JSON pass) ----------------

def _derive_bibs_full() -> int:
    """Walk all catalog JSONs with ocr_detections, derive bib_hits block fresh.

    Idempotent — overwrites bib_hits if present, keeps p_id values where they
    persisted on the existing items (matched on frame_time_sec + bib_number).
    """
    n_total = 0
    for cat_dir, suffix, kind in (
        (VIDEO_CATALOG, ".video.json", "video"),
        (STILLS_CATALOG, ".still.json", "still"),
    ):
        for p in cat_dir.glob(f"*{suffix}"):
            if p.name.startswith("._"):
                continue
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                continue
            aid = d.get("asset_id")
            block = d.get("ocr_detections") or {}
            ocr_items = block.get("items") or []
            if not aid or not ocr_items:
                continue
            # Preserve p_id values from prior bib_hits where (frame_time_sec, bib_number) matches
            prior = d.get("bib_hits") or {}
            prior_pids = {(round(it["frame_time_sec"], 2), it["bib_number"]): it.get("p_id")
                          for it in (prior.get("items") or [])}
            bib_items = []
            for it in ocr_items:
                t = (it.get("text") or "").strip()
                if not is_bib_text(t):
                    continue
                ft = round(it["frame_time_sec"], 2)
                bib_items.append({
                    "shot_idx": it.get("shot_idx"),
                    "frame_time_sec": it["frame_time_sec"],
                    "bib_number": t,
                    "confidence": it["confidence"],
                    "source_ocr_engine": it["ocr_engine"],
                    "p_id": prior_pids.get((ft, t)),
                })
            if bib_items:
                update_layer(aid, kind, BIB_LAYER, {
                    "processed_at": now_iso(),
                    "items": bib_items,
                })
                n_total += len(bib_items)
    return n_total


def cmd_derive_bibs(args: argparse.Namespace) -> None:
    n = _derive_bibs_full()
    print(f"derived {n} bib_hit items across catalog")


# ---------------- still-only (re-run convenience) ----------------

def cmd_still(args: argparse.Namespace) -> None:
    args.video_only = False
    args.stills_only = True
    cmd_run(args)


# ---------------- status ----------------

def cmd_status(args: argparse.Namespace) -> None:
    n_proc_total = 0
    n_proc_assets = 0
    n_proc_with_hits = 0
    n_hits = 0
    n_bibs = 0
    distinct_bib = set()
    per_engine_hits: dict[str, int] = defaultdict(int)
    per_engine_assets: dict[str, set] = defaultdict(set)

    for cat_dir, suffix, kind in (
        (VIDEO_CATALOG, ".video.json", "video"),
        (STILLS_CATALOG, ".still.json", "still"),
    ):
        for p in cat_dir.glob(f"*{suffix}"):
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                continue
            block = d.get("ocr_detections") or {}
            if not block:
                continue
            aid = d.get("asset_id")
            n_proc_assets += 1
            for frame in block.get("processed_frames") or []:
                n_proc_total += 1
                if frame.get("n_hits", 0) > 0:
                    n_proc_with_hits += 1
            for it in block.get("items") or []:
                n_hits += 1
                eng = it.get("ocr_engine")
                if eng:
                    per_engine_hits[eng] += 1
                    per_engine_assets[eng].add(aid)
            for it in (d.get("bib_hits") or {}).get("items") or []:
                n_bibs += 1
                distinct_bib.add(it.get("bib_number"))

    print(f"=== ocr coverage (catalog JSON) ===")
    print(f"  frame-engine pairs processed: {n_proc_total} (across {n_proc_assets} assets)")
    print(f"  pairs with text hits:         {n_proc_with_hits}")
    print(f"  total ocr_detection items:    {n_hits}")
    print(f"  bib_hit items / distinct bibs:{n_bibs} / {len(distinct_bib)}")
    print(f"\n  per engine:")
    for e, n in per_engine_hits.items():
        print(f"    {e:<14s} hits={n:6d}  assets={len(per_engine_assets[e]):4d}")


# ---------------- main ----------------

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_run = sub.add_parser("run", help="Phase B — full corpus OCR pass")
    p_run.add_argument("--engines", nargs="+", default=["apple_vision", "rapidocr"],
                       choices=["apple_vision", "rapidocr"])
    p_run.add_argument("--workers", type=int, default=4)
    p_run.add_argument("--limit", type=int, default=None)
    p_run.add_argument("--asset-ids", type=str, default=None)
    p_run.add_argument("--video-only", action="store_true", default=False)
    p_run.add_argument("--stills-only", action="store_true", default=False)
    p_run.set_defaults(func=cmd_run)

    p_still = sub.add_parser("still", help="Run only on cataloged stills")
    p_still.add_argument("--engines", nargs="+", default=["apple_vision", "rapidocr"],
                         choices=["apple_vision", "rapidocr"])
    p_still.add_argument("--workers", type=int, default=4)
    p_still.add_argument("--limit", type=int, default=None)
    p_still.add_argument("--asset-ids", type=str, default=None)
    p_still.add_argument("--video-only", action="store_true", default=False)
    p_still.add_argument("--stills-only", action="store_true", default=True)
    p_still.set_defaults(func=cmd_still)

    p_status = sub.add_parser("status", help="Coverage stats")
    p_status.set_defaults(func=cmd_status)

    p_bibs = sub.add_parser("derive-bibs", help="Apply bib regex over catalog JSON")
    p_bibs.set_defaults(func=cmd_derive_bibs)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
