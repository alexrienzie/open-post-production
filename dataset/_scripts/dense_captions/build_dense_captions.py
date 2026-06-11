#!/usr/bin/env python3
"""build_dense_captions.py — Per-shot dense visual captions.

Phase A pilot: 15 stratified frames × 3 engines × 2 meta-modes = 90 cells.
User picks (engine, meta-on/off) for the full run.

Phase B run: per-shot midpoint + length-aware extras, asset-type aware sampling
(verite/b_roll/aerial/archival get 3 quartile frames; third_party gets
midpoint only; interview gets 1/shot; timelapse skipped).

Captions now live
in catalog JSON under `video.json["dense_captions"]` with per-shot processed
tracker. Run logs go to `_runs/ingest_pipeline/dense_captions/`. The pilot report
(pilot.md + frames) still writes to `_runs/ingest_pipeline/dense_captions/<ts>/`.

Subcommands:
  pilot   Phase A — stratified pilot, side-by-side caption report
  run     Phase B — full corpus, picked engine + meta mode
  status  Coverage + per-engine counts
"""
from __future__ import annotations

import argparse
import json
import random
import sqlite3
import sys
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from _paths import (  # noqa: E402
    INDEXES_DIR, RUNS_DIR, VIDEO_CATALOG,
    PEOPLE_REGISTRY, resolve_proxy_via_asset_map,
)
from _catalog_layer_io import (  # noqa: E402
    now_iso as _now_iso, load_catalog, update_layer,
    start_run_log, finish_run_log,
)

EDITORIAL_DB = INDEXES_DIR / "editorial_catalog.sqlite"
LAYER = "dense_captions"


def now_iso() -> str:
    return _now_iso()


def ts_slug() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


# ============================================================ catalog helpers

def _flatview(rec: dict) -> dict:
    pm = rec.get("path_metadata") or {}
    ac = rec.get("asset_classifications") or {}
    s = rec.get("asset_semantic_summary") or {}
    chunks = s.get("chunks") or []
    ch0 = chunks[0] if chunks else {}
    return {
        "asset_id": rec.get("asset_id"),
        "shoot_label": pm.get("shoot_label") or "",
        "camera_id": pm.get("camera_id") or "",
        "asset_type": ac.get("type") or "",
        "bucket": ac.get("bucket") or "",
        "chunk_subject": (ch0.get("subject") or "")[:300],
        "chunk_action": (ch0.get("action") or "")[:300],
        "n_chunks": len(chunks),
    }


def _load_asset_index() -> dict[str, dict]:
    """asset_id → flatview() for every video asset."""
    out = {}
    for f in VIDEO_CATALOG.glob("*.video.json"):
        if f.name.startswith("._"):
            continue
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        aid = d.get("asset_id")
        if aid:
            out[aid] = _flatview(d)
    return out


def _load_people_aliases() -> dict[str, str]:
    if not PEOPLE_REGISTRY.exists():
        return {}
    pd = json.loads(PEOPLE_REGISTRY.read_text())
    people = pd.get("people", pd) if isinstance(pd, dict) else pd
    return {p.get("id") or p.get("p_id"): p.get("canonical_name", "")
            for p in people if (p.get("id") or p.get("p_id"))}


# ============================================================ meta gatherer

def _gather_meta(con_ed: sqlite3.Connection, aview: dict, asset_id: str,
                 shot_idx: int, frame_time_sec: float,
                 people_aliases: dict[str, str], n_shots_in_asset: int) -> dict:
    """Pull per-shot enrichment metadata from editorial_catalog tables."""
    people = []
    rows = con_ed.execute(
        "SELECT DISTINCT p_id FROM frame_face "
        "WHERE asset_id=? AND ABS(frame_time_sec - ?) <= 2.0 "
        "AND p_id LIKE 'p_%' LIMIT 5",
        (asset_id, frame_time_sec),
    ).fetchall()
    for r in rows:
        name = people_aliases.get(r[0])
        if name:
            people.append(name)

    rows = con_ed.execute(
        "SELECT DISTINCT text FROM frame_text "
        "WHERE asset_id=? AND ABS(frame_time_sec - ?) <= 2.0 "
        "ORDER BY confidence DESC LIMIT 5",
        (asset_id, frame_time_sec),
    ).fetchall()
    ocr = [r[0] for r in rows if r[0]]

    rows = con_ed.execute(
        "SELECT tag, MAX(score) FROM audio_event "
        "WHERE asset_id=? AND window_start_sec <= ? + 5 AND window_end_sec >= ? - 5 "
        "GROUP BY tag ORDER BY 2 DESC LIMIT 3",
        (asset_id, frame_time_sec, frame_time_sec),
    ).fetchall()
    ae = [(r[0], r[1]) for r in rows]

    rows = con_ed.execute(
        "SELECT text FROM segment "
        "WHERE asset_id=? AND start_sec <= ? + 5 AND end_sec >= ? - 5 "
        "ORDER BY start_sec LIMIT 3",
        (asset_id, frame_time_sec, frame_time_sec),
    ).fetchall()
    transcript = " ".join(r[0] for r in rows if r[0])

    from _captions import build_meta_block
    return {
        "block": build_meta_block(
            shoot_label=aview["shoot_label"], asset_type=aview["asset_type"],
            camera_id=aview["camera_id"], chunk_subject=aview["chunk_subject"],
            chunk_action=aview["chunk_action"], people_in_frame=people,
            ocr_phrases_nearby=ocr, audio_events_nearby=ae,
            transcript_snippet=transcript, shot_idx=shot_idx,
            n_shots_in_asset=n_shots_in_asset,
        ),
        "people": people, "ocr": ocr, "audio_events": ae,
        "transcript": transcript[:120],
    }


# ============================================================ catalog-JSON I/O helpers

def _read_existing_captions(asset_id: str) -> dict:
    """Returns {(shot_idx, engine, mode, sample_pos): caption_item, ...} from
    existing video.json dense_captions block. Used for idempotency dedup."""
    cat = load_catalog(asset_id, "video")
    if not cat:
        return {}
    items = (cat.get(LAYER) or {}).get("items") or []
    return {(it.get("shot_idx"), it["model_engine"], it["prompt_variant"], it.get("sample_pos")): it
            for it in items}


def _flush_asset(asset_id: str, new_items: list[dict], new_processed: list[dict]) -> None:
    """Merge new captions + processed records into existing block, atomic write."""
    cat = load_catalog(asset_id, "video")
    if cat is None:
        return
    existing = cat.get(LAYER) or {}
    items = (existing.get("items") or []) + new_items
    proc = (existing.get("processed_shots") or []) + new_processed
    update_layer(asset_id, "video", LAYER, {
        "processed_at": now_iso(),
        "processed_shots": proc,
        "items": items,
    })


# ============================================================ pilot

PILOT_STRATA = [
    ("verite", lambda v: v["asset_type"] == "verite", 4),
    ("b_roll", lambda v: v["asset_type"] == "b_roll", 4),
    ("aerial", lambda v: "Aeriel" in v["shoot_label"] or "DJI" in v["camera_id"], 2),
    ("archival", lambda v: v["asset_type"] == "archival", 2),
]


def _pick_pilot_shots(con_ed: sqlite3.Connection, asset_index: dict,
                      rng: random.Random) -> list[tuple]:
    rows = con_ed.execute("""
        SELECT s.asset_id, s.shot_idx, s.start_sec, s.end_sec,
               (SELECT COUNT(*) FROM shot s2 WHERE s2.asset_id=s.asset_id) AS n_shots
        FROM shot s
        WHERE s.end_sec - s.start_sec >= 2.0
    """).fetchall()
    by_stratum = {label: [] for label, _p, _t in PILOT_STRATA}
    used_aids = set()
    for aid, sidx, ss, se, n_shots in rows:
        if aid in used_aids:
            continue
        view = asset_index.get(aid)
        if not view:
            continue
        for label, pred, _t in PILOT_STRATA:
            try:
                if pred(view):
                    by_stratum[label].append((view, sidx, (ss + se) / 2.0, n_shots))
                    used_aids.add(aid)
                    break
            except Exception:
                continue
    picks = []
    for label, _pred, target in PILOT_STRATA:
        pool = by_stratum[label]
        rng.shuffle(pool)
        for view, sidx, tmid, n_shots in pool[:target]:
            picks.append((view, sidx, tmid, n_shots, label))
    return picks


def cmd_pilot(args: argparse.Namespace) -> None:
    from _captions import (
        extract_frame_jpeg, build_prompt, load_engine, SAMPLE_FRAME_WIDTH,
    )

    out_root = RUNS_DIR / "dense_captions" / ts_slug()
    out_root.mkdir(parents=True, exist_ok=True)
    jpeg_dir = out_root / "frames"
    jpeg_dir.mkdir(exist_ok=True)

    run_path = start_run_log("dense_captions", {"phase": "pilot", **vars(args)})
    print(f"=== dense_captions PILOT | {now_iso()} ===")
    print(f"  out_dir: {out_root}")

    con_ed = sqlite3.connect(f"file:{EDITORIAL_DB}?mode=ro", uri=True)
    print("  loading asset index from catalog...")
    asset_index = _load_asset_index()
    people_aliases = _load_people_aliases()
    print(f"    {len(asset_index):,} video assets indexed   {len(people_aliases):,} labeled people")

    rng = random.Random(args.seed)
    picks = _pick_pilot_shots(con_ed, asset_index, rng)
    if args.limit:
        picks = picks[: args.limit]
    print(f"  pilot picks: {len(picks)} shots across strata: "
          f"{', '.join(f'{label}={sum(1 for p in picks if p[4]==label)}' for label,_p,_t in PILOT_STRATA)}")

    frame_records = []
    for i, (view, sidx, tmid, n_shots, label) in enumerate(picks):
        aid = view["asset_id"]
        proxy = resolve_proxy_via_asset_map(aid)
        if proxy is None or not proxy.exists():
            print(f"  [skip {i+1}] {aid[:12]} no proxy"); continue
        jpeg_bytes = extract_frame_jpeg(proxy, tmid, width=SAMPLE_FRAME_WIDTH)
        if not jpeg_bytes:
            print(f"  [skip {i+1}] {aid[:12]} frame extract failed"); continue
        jpeg_path = jpeg_dir / f"{i+1:02d}_{label}_{aid[:12]}_t{int(tmid)}.jpg"
        jpeg_path.write_bytes(jpeg_bytes)
        meta = _gather_meta(con_ed, view, aid, sidx, tmid, people_aliases, n_shots)
        frame_records.append({
            "i": i + 1, "stratum": label, "view": view,
            "shot_idx": sidx, "frame_time_sec": tmid, "n_shots": n_shots,
            "jpeg_path": jpeg_path, "jpeg_bytes": jpeg_bytes,
            "meta_block": meta["block"], "meta_dbg": meta,
        })
    print(f"  frames extracted + metadata gathered: {len(frame_records)}")

    engines_to_run = args.engines.split(",")
    print(f"  engines: {engines_to_run}")
    modes = ["meta"] if args.skip_meta_off else ["no-meta", "meta"]
    print(f"  modes: {modes}")

    results: dict[int, dict[str, dict[str, dict]]] = {fr["i"]: {} for fr in frame_records}

    # Buffer captions per asset for atomic catalog-JSON write at end
    per_asset_items: dict[str, list[dict]] = defaultdict(list)

    for ename in engines_to_run:
        print(f"\n=== loading {ename} ===")
        t0 = time.time()
        try:
            engine = load_engine(ename)
        except Exception as e:
            print(f"  ERROR loading {ename}: {e}"); continue
        print(f"  loaded in {time.time() - t0:.1f}s")

        for fr in frame_records:
            results[fr["i"]].setdefault(ename, {})
            for mode in modes:
                prompt = build_prompt(meta_block=fr["meta_block"] if mode == "meta" else None)
                cap = engine.caption(fr["jpeg_bytes"], prompt)
                results[fr["i"]][ename][mode] = cap
                per_asset_items[fr["view"]["asset_id"]].append({
                    "shot_idx": fr["shot_idx"],
                    "frame_time_sec": fr["frame_time_sec"],
                    "sample_pos": "midpoint",
                    "caption_text": (cap or {}).get("text", ""),
                    "caption_json": json.dumps((cap or {}).get("json")) if (cap or {}).get("json") else None,
                    "model_engine": ename,
                    "prompt_variant": mode,
                })
            print(f"  [{ename}] [{fr['i']:02d}/{len(frame_records)}] {fr['view']['asset_id'][:12]} {fr['stratum']} "
                  f"meta-latency={results[fr['i']][ename].get('meta',{}).get('latency_sec', 0):.2f}s")

    # Flush all assets' pilot captions to catalog JSON
    for aid, items in per_asset_items.items():
        _flush_asset(aid, items, [])

    report = out_root / "pilot.md"
    _write_pilot_report(report, frame_records, results, engines_to_run, modes)
    print(f"\nreport: {report}")
    finish_run_log(run_path, {
        "n_frames": len(frame_records), "engines": engines_to_run,
        "modes": modes, "report": str(report),
    })


def _write_pilot_report(md_path: Path, frame_records: list,
                        results: dict, engines: list[str], modes: list[str]) -> None:
    lines = [
        f"# Dense captions pilot ({now_iso()})", "",
        f"**Frames:** {len(frame_records)} stratified across asset types  ",
        f"**Engines:** {', '.join(engines)}  ",
        f"**Modes:** {', '.join(modes)}  ",
        f"**Output dir:** `{md_path.parent}`  ", "",
        "## How to read this", "",
        "For each pilot frame: the JPEG preview, asset/scene context, then a "
        "table with one row per (engine × mode) caption. Read across rows to "
        "compare engine quality; read across columns (meta vs no-meta) to test "
        "whether metadata enrichment helps or just paraphrases the chunk summary.", "",
        "## Per-engine + per-mode latency summary", "",
        "| engine | mode | mean latency (sec) | n |",
        "|---|---|---:|---:|",
    ]
    import statistics
    for e in engines:
        for m in modes:
            lats = [r[e].get(m, {}).get("latency_sec", 0) for r in results.values() if e in r]
            lats = [x for x in lats if x > 0]
            if lats:
                lines.append(f"| {e} | {m} | {statistics.mean(lats):.2f} | {len(lats)} |")

    lines += ["", "---", ""]
    for fr in frame_records:
        v = fr["view"]; i = fr["i"]; meta = fr["meta_dbg"]
        lines.append(f"## Frame {i:02d} · `{v['asset_id'][:12]}` · stratum=**{fr['stratum']}**")
        lines.append("")
        lines.append(f"![frame {i:02d}](frames/{fr['jpeg_path'].name})")
        lines.append("")
        lines.append(f"- **shoot:** {v.get('shoot_label') or '—'}")
        lines.append(f"- **asset_type:** {v.get('asset_type') or '—'} · **camera:** {v.get('camera_id') or '—'}")
        lines.append(f"- **shot_idx:** {fr['shot_idx']} of {fr['n_shots']} in asset · **t:** {fr['frame_time_sec']:.1f}s")
        if v.get("chunk_subject"): lines.append(f"- **Gemini subject:** {v['chunk_subject']}")
        if v.get("chunk_action"): lines.append(f"- **Gemini action:** {v['chunk_action']}")
        if meta["people"]: lines.append(f"- **People in frame (face cluster):** {', '.join(meta['people'])}")
        if meta["ocr"]: lines.append(f"- **OCR nearby:** {', '.join(meta['ocr'])}")
        if meta["audio_events"]: lines.append(f"- **Audio events nearby:** {', '.join(f'{t} ({s:.2f})' for t,s in meta['audio_events'])}")
        if meta["transcript"]: lines.append(f"- **Transcript nearby:** {meta['transcript']}")
        lines.append("")
        lines.append("| engine | mode | caption | latency |")
        lines.append("|---|---|---|---:|")
        for e in engines:
            for m in modes:
                cap = results[i].get(e, {}).get(m)
                if not cap:
                    lines.append(f"| {e} | {m} | _(not run)_ | — |"); continue
                txt = (cap.get("text") or "").replace("|", "\\|").replace("\n", " ")[:400]
                lat = cap.get("latency_sec", 0)
                lines.append(f"| {e} | {m} | {txt} | {lat:.2f}s |")
        lines += ["", "---", ""]
    md_path.write_text("\n".join(lines))


# ============================================================ run (full corpus)

SAMPLING_BY_TYPE = {
    "verite":      [0.25, 0.50, 0.75],
    "b_roll":      [0.25, 0.50, 0.75],
    "aerial":      [0.25, 0.50, 0.75],
    "archival":    [0.25, 0.50, 0.75],
    "third_party": [0.50],
    "interview":   [0.50],
    "timelapse":   [],
}
MIN_SHOT_DURATION_FOR_SAMPLING = 2.0


def _classify_asset_type(view: dict) -> str:
    at = (view.get("asset_type") or "").lower()
    shoot = view.get("shoot_label") or ""
    if "Aeriel" in shoot or "aerial" in shoot.lower():
        return "aerial"
    return at if at in SAMPLING_BY_TYPE else ""


def cmd_run(args: argparse.Namespace) -> None:
    from _captions import extract_frame_jpeg, build_prompt, load_engine

    run_path = start_run_log("dense_captions", {"phase": "run", **vars(args)})
    print(f"=== dense_captions RUN | {now_iso()} ===")
    print(f"  engine: {args.engine}    mode: {args.mode}    workers: {args.workers}")

    con_ed = sqlite3.connect(f"file:{EDITORIAL_DB}?mode=ro", uri=True)
    print("  loading asset index from catalog...")
    asset_index = _load_asset_index()
    people_aliases = _load_people_aliases()
    print(f"    {len(asset_index):,} assets, {len(people_aliases):,} labeled people")

    print("  walking video catalog for shots, applying asset-type sampling rules...")
    work: list[dict] = []
    n_skipped_type = 0
    n_skipped_short = 0
    n_skipped_already = 0
    n_skipped_no_view = 0
    for p in VIDEO_CATALOG.glob("*.video.json"):
        if p.name.startswith("._"):
            continue
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        aid = d.get("asset_id")
        if not aid:
            continue
        view = asset_index.get(aid)
        if not view:
            n_skipped_no_view += 1
            continue
        atype = _classify_asset_type(view)
        positions = SAMPLING_BY_TYPE.get(atype, [])
        if not positions:
            n_skipped_type += 1
            continue
        shots = (d.get("shots") or {}).get("items") or []
        if not shots:
            continue
        n_shots = len(shots)
        existing = _read_existing_captions(aid)
        for s in shots:
            ss, se = s["start_sec"], s["end_sec"]
            sidx = s["shot_idx"]
            dur = float(se) - float(ss)
            if dur < MIN_SHOT_DURATION_FOR_SAMPLING:
                n_skipped_short += 1
                continue
            for pct in positions:
                pos_label = f"{int(pct*100)}%"
                t_sec = float(ss) + dur * pct
                key = (sidx, args.engine, args.mode, pos_label)
                if key in existing:
                    n_skipped_already += 1
                    continue
                work.append({
                    "asset_id": aid, "shot_idx": sidx, "n_shots": n_shots,
                    "frame_time_sec": t_sec, "sample_pos": pos_label, "view": view,
                })
    print(f"    skipped (asset_type out of scope / timelapse): {n_skipped_type:,}")
    print(f"    skipped (shot < {MIN_SHOT_DURATION_FOR_SAMPLING}s): {n_skipped_short:,}")
    print(f"    skipped (no catalog view): {n_skipped_no_view:,}")
    print(f"    skipped (already captioned): {n_skipped_already:,}")
    print(f"  effective work: {len(work):,} captions to compute")
    if args.limit:
        work = work[: args.limit]
        print(f"  --limit: {len(work):,}")
    if not work:
        finish_run_log(run_path, {"captions": 0, "note": "no_work"})
        return

    # Thread-local engine + editorial-catalog read connection
    _local = threading.local()

    def _engine():
        if not hasattr(_local, "engine"):
            _local.engine = load_engine(args.engine)
        return _local.engine

    def _ed_con():
        if not hasattr(_local, "ed_con"):
            _local.ed_con = sqlite3.connect(f"file:{EDITORIAL_DB}?mode=ro", uri=True)
        return _local.ed_con

    # Per-asset buffer for atomic catalog-JSON writes
    buf_lock = threading.Lock()
    asset_total: dict[str, int] = defaultdict(int)
    asset_done: dict[str, int] = defaultdict(int)
    asset_items: dict[str, list[dict]] = defaultdict(list)
    asset_proc: dict[str, list[dict]] = defaultdict(list)
    for w in work:
        asset_total[w["asset_id"]] += 1

    t_start = time.time()
    counters = {"done": 0, "errors": 0, "captions": 0, "assets_flushed": 0}

    def worker(w: dict) -> None:
        aid = w["asset_id"]; sidx = w["shot_idx"]; t_sec = w["frame_time_sec"]
        view = w["view"]
        success = 0
        try:
            proxy = resolve_proxy_via_asset_map(aid)
            if proxy is None or not proxy.exists():
                with buf_lock:
                    counters["errors"] += 1
                _record_done(aid, sidx, args.engine, 0, success=0,
                             buf_lock=buf_lock, asset_done=asset_done,
                             asset_total=asset_total, asset_items=asset_items,
                             asset_proc=asset_proc, counters=counters)
                return
            jpeg = extract_frame_jpeg(proxy, t_sec)
            if jpeg is None:
                with buf_lock:
                    counters["errors"] += 1
                _record_done(aid, sidx, args.engine, 0, success=0,
                             buf_lock=buf_lock, asset_done=asset_done,
                             asset_total=asset_total, asset_items=asset_items,
                             asset_proc=asset_proc, counters=counters)
                return
            if args.mode == "meta":
                meta = _gather_meta(_ed_con(), view, aid, sidx, t_sec, people_aliases, w["n_shots"])
                prompt = build_prompt(meta_block=meta["block"])
            else:
                prompt = build_prompt(meta_block=None)
            cap = _engine().caption(jpeg, prompt)
            success = 1 if cap else 0
        except Exception as e:
            with buf_lock:
                counters["errors"] += 1
            print(f"  [worker error] {aid[:12]} shot {sidx}: {type(e).__name__}: {e}",
                  file=sys.stderr, flush=True)
            _record_done(aid, sidx, args.engine, 0, success=0,
                         buf_lock=buf_lock, asset_done=asset_done,
                         asset_total=asset_total, asset_items=asset_items,
                         asset_proc=asset_proc, counters=counters)
            return

        item = {
            "shot_idx": sidx, "frame_time_sec": t_sec,
            "sample_pos": w["sample_pos"],
            "caption_text": (cap or {}).get("text", ""),
            "caption_json": json.dumps((cap or {}).get("json")) if (cap or {}).get("json") else None,
            "model_engine": args.engine, "prompt_variant": args.mode,
        }
        with buf_lock:
            asset_items[aid].append(item)
            counters["captions"] += 1
        _record_done(aid, sidx, args.engine, 1, success=success,
                     buf_lock=buf_lock, asset_done=asset_done,
                     asset_total=asset_total, asset_items=asset_items,
                     asset_proc=asset_proc, counters=counters)

    with ThreadPoolExecutor(max_workers=args.workers, thread_name_prefix="cap") as ex:
        futures = [ex.submit(worker, w) for w in work]
        last_print = time.time()
        for f in as_completed(futures):
            try:
                f.result()
            except Exception as e:
                print(f"  [future error] {type(e).__name__}: {e}", file=sys.stderr, flush=True)
            now = time.time()
            if now - last_print >= 15:
                with buf_lock:
                    d = counters["done"]
                el = now - t_start
                rate = d / el if el else 0
                eta = (len(work) - d) / rate / 60 if rate else 0
                print(f"  [{d:>5}/{len(work)}] captions={counters['captions']:,} "
                      f"flushed={counters['assets_flushed']} errors={counters['errors']} "
                      f"rate={rate*60:.0f}/min ETA={eta:.0f}m", flush=True)
                last_print = now

    # Flush any assets that didn't reach full completion
    with buf_lock:
        for aid in list(asset_items.keys()):
            _flush_asset(aid, asset_items[aid], asset_proc[aid])
            counters["assets_flushed"] += 1
            asset_items[aid] = []
            asset_proc[aid] = []

    elapsed = time.time() - t_start
    finish_run_log(run_path, {
        "n_work": len(work), **counters,
        "wall_clock_sec": round(elapsed, 1),
    })
    print(f"\nrun complete: {counters['captions']:,} captions, "
          f"{counters['errors']} errors, {elapsed/60:.1f}m wall-clock")


def _record_done(aid: str, sidx: int, engine: str, n_captions: int, success: int,
                 *, buf_lock, asset_done, asset_total, asset_items, asset_proc, counters) -> None:
    """Append per-shot processed record; flush asset to disk when complete."""
    with buf_lock:
        asset_proc[aid].append({
            "shot_idx": sidx, "engine": engine, "n_captions": n_captions,
            "success": success, "processed_at": now_iso(),
        })
        asset_done[aid] += 1
        counters["done"] += 1
        complete = asset_done[aid] >= asset_total[aid]
        if complete:
            items = asset_items.pop(aid)
            proc = asset_proc.pop(aid)
    if complete:
        _flush_asset(aid, items, proc)
        with buf_lock:
            counters["assets_flushed"] += 1


def cmd_status(args: argparse.Namespace) -> None:
    n_assets = 0
    n_captions = 0
    per_engine: dict[str, int] = defaultdict(int)
    for p in VIDEO_CATALOG.glob("*.video.json"):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        block = d.get(LAYER) or {}
        items = block.get("items") or []
        if not items:
            continue
        n_assets += 1
        for it in items:
            n_captions += 1
            per_engine[it.get("model_engine") or "?"] += 1
    print(f"=== dense_captions coverage (catalog JSON) ===")
    print(f"  assets with captions: {n_assets:,}   total captions: {n_captions:,}")
    for engine, c in sorted(per_engine.items(), key=lambda x: -x[1]):
        print(f"  {engine}: {c:,}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("pilot")
    sp.add_argument("--seed", type=int, default=20260525)
    sp.add_argument("--limit", type=int)
    sp.add_argument("--engines", default="gemini_flash,gemini_pro,minicpm_v_2_6")
    sp.add_argument("--skip-meta-off", action="store_true")
    sp.set_defaults(func=cmd_pilot)

    sp = sub.add_parser("run")
    sp.add_argument("--engine", default="gemini_flash",
                    choices=["gemini_flash", "gemini_pro", "qwen2_vl_2b"])
    sp.add_argument("--mode", default="meta", choices=["meta", "no-meta"])
    sp.add_argument("--workers", type=int, default=8)
    sp.add_argument("--limit", type=int)
    sp.set_defaults(func=cmd_run)

    sp = sub.add_parser("status")
    sp.set_defaults(func=cmd_status)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
