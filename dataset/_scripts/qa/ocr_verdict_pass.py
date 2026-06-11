#!/usr/bin/env python3
"""ocr_verdict_pass.py — Gemini-Flash QA verdict for every OCR row.

Mutates each OCR item in catalog JSON (`video.json["ocr_detections"]["items"][i]`
and `still.json[...]`) to add per-item fields:
  qa_verdict      TEXT  -- 'consistent' | 'suspicious' | 'unknown' | NULL
  qa_reason       TEXT  -- short Gemini-Flash justification
  qa_model        TEXT  -- e.g. 'gemini-2.5-flash'
  qa_verdicted_at TEXT

Strategy: group OCR items by asset_id so each asset's Gemini scene summary is
fetched once. For each asset, batch its OCR texts (up to BATCH_PER_CALL per
Gemini call). Concurrent workers issue calls in parallel. Idempotent: skip
items that already have qa_verdict set.

Expected cost ~$2-3 with Flash across ~60K rows (250 input + 50 output tokens
per row at $0.075 / $0.30 per M).

Verdicts now persisted into
per-asset catalog JSON. Idempotent at the item-key (frame_time_sec, ocr_engine).

Subcommands:
  run     Full corpus pass over un-verdicted ocr_detections items
  status  Verdict distribution
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from _paths import VIDEO_CATALOG, STILLS_CATALOG  # noqa: E402
from _catalog_layer_io import (  # noqa: E402
    now_iso, load_catalog, update_layer,
    start_run_log, finish_run_log,
)

GEMINI_MODEL = "gemini-2.5-flash"
BATCH_PER_CALL = 12
WORKERS = 8
LAYER = "ocr_detections"


def _api_key() -> str:
    k = os.environ.get("GEMINI_API_KEY")
    if k:
        return k
    rc = Path.home() / ".zshrc"
    if rc.exists():
        for line in rc.read_text().splitlines():
            m = re.match(r'^\s*export\s+GEMINI_API_KEY\s*=\s*"?([^"\s]+)"?', line)
            if m:
                return m.group(1)
    raise RuntimeError("GEMINI_API_KEY not in env nor .zshrc")


def _client():
    from google import genai
    return genai.Client(api_key=_api_key())


def _build_asset_summary_index() -> dict[str, dict]:
    """Walk video catalog JSONs, build {asset_id: compact summary} once."""
    out = {}
    for f in VIDEO_CATALOG.glob("*.video.json"):
        if f.name.startswith("._"):
            continue
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        aid = d.get("asset_id")
        if not aid:
            continue
        s = d.get("asset_semantic_summary") or {}
        chunks = s.get("chunks") or []
        if not chunks:
            continue
        subjects = [c.get("subject", "") for c in chunks if c.get("subject")]
        actions = [c.get("action", "") for c in chunks if c.get("action")]
        settings = []
        for c in chunks:
            setting = c.get("setting")
            if isinstance(setting, dict):
                loc = setting.get("location")
                if loc:
                    settings.append(loc)
        out[aid] = {
            "subject": " | ".join(subjects[:3])[:300],
            "action": " | ".join(actions[:3])[:300],
            "location": " | ".join(settings[:3])[:200],
            "shoot_label": (d.get("path_metadata") or {}).get("shoot_label", ""),
        }
    return out


def _batch_prompt(summary: dict, ocr_items: list[tuple[str, str]]) -> str:
    """Build a Gemini call. ocr_items: list of (item_key, text) where item_key
    encodes (frame_time_sec, ocr_engine) for write-back lookup."""
    lines = [
        "You are a documentary editor doing QA on automated OCR.",
        f"Asset shoot: {summary.get('shoot_label', '')}",
        f"Scene subject: {summary.get('subject', '')}",
        f"Scene action: {summary.get('action', '')}",
        f"Scene location: {summary.get('location', '')}",
        "",
        "For each OCR-extracted text below, judge whether it plausibly appears "
        "on-screen in the described scene (signs, lower-thirds, graphics, screens, "
        "T-shirts, bibs, brand names, vehicle plates, etc).",
        "",
        "Reply with a JSON object: {\"verdicts\": [{\"id\": <int>, \"v\": "
        "\"consistent\"|\"suspicious\"|\"unknown\", \"r\": \"<short reason>\"}, ...]}",
        "",
        "Use 'consistent' if the text plausibly fits the scene. "
        "Use 'suspicious' if it looks like OCR hallucination from clothing texture, "
        "motion blur, bedding patterns, or random pixel noise (e.g. nonsense "
        "pseudo-words like 'SAIARE', 'WINCHE POGS'). "
        "Use 'unknown' if the scene description is too vague to judge.",
        "",
        "OCR rows:",
    ]
    for i, (_key, text) in enumerate(ocr_items):
        safe = text.replace("\n", " ").replace("\r", " ")[:120]
        lines.append(f"  {i}: {safe!r}")
    return "\n".join(lines)


def _verdict_batch(client, summary: dict, ocr_items: list[tuple[str, str]]) -> dict[str, dict]:
    """Call Gemini Flash; return {item_key: {'verdict':..., 'reason':...}}."""
    from google.genai import types
    prompt = _batch_prompt(summary, ocr_items)
    cfg = types.GenerateContentConfig(response_mime_type="application/json", temperature=0.0)
    for attempt in range(3):
        try:
            resp = client.models.generate_content(model=GEMINI_MODEL, contents=prompt, config=cfg)
            raw = resp.text
            if not raw:
                print(f"  [warn] empty response on batch of {len(ocr_items)}", file=sys.stderr)
                return {}
            parsed = json.loads(raw)
            verdicts_arr = parsed.get("verdicts") or []
            if not verdicts_arr:
                print(f"  [warn] parsed but verdicts=[] in {raw[:200]!r}", file=sys.stderr)
                return {}
            out = {}
            for v in verdicts_arr:
                idx = v.get("id")
                if isinstance(idx, str) and idx.isdigit():
                    idx = int(idx)
                if not isinstance(idx, int) or idx < 0 or idx >= len(ocr_items):
                    continue
                key = ocr_items[idx][0]
                out[key] = {
                    "verdict": str(v.get("v", "unknown")).strip().lower(),
                    "reason": str(v.get("r", ""))[:200],
                }
            return out
        except Exception as e:
            if attempt == 2:
                print(f"  [error] {type(e).__name__}: {e}", file=sys.stderr)
                return {}
            time.sleep(2 ** attempt)
    return {}


def _has_cyrillic(t: str) -> bool:
    return any("Ѐ" <= c <= "ԯ" for c in t)


def _walk_unverdicted_items():
    """Yield (asset_id, kind, item_key, item_dict) for each OCR item without qa_verdict."""
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
            if not aid:
                continue
            items = (d.get(LAYER) or {}).get("items") or []
            for it in items:
                if it.get("qa_verdict") is not None:
                    continue
                txt = (it.get("text") or "").strip()
                if len(txt) < 3 or _has_cyrillic(txt):
                    continue
                # Item key: (frame_time_sec, ocr_engine) — unique within an asset
                key = f"{it['frame_time_sec']}|{it['ocr_engine']}"
                yield aid, kind, key, it


def _apply_verdicts_to_asset(aid: str, kind: str, verdicts_by_key: dict[str, dict]) -> int:
    """Apply verdicts to existing items in the catalog JSON's ocr_detections block.
    Returns count of items mutated."""
    cat = load_catalog(aid, kind)
    if cat is None:
        return 0
    block = cat.get(LAYER) or {}
    items = block.get("items") or []
    n = 0
    ts = now_iso()
    for it in items:
        key = f"{it['frame_time_sec']}|{it['ocr_engine']}"
        if key in verdicts_by_key and it.get("qa_verdict") is None:
            v = verdicts_by_key[key]
            it["qa_verdict"] = v["verdict"]
            it["qa_reason"] = v["reason"]
            it["qa_model"] = GEMINI_MODEL
            it["qa_verdicted_at"] = ts
            n += 1
    if n > 0:
        block["items"] = items
        update_layer(aid, kind, LAYER, block)
    return n


def cmd_run(args: argparse.Namespace) -> None:
    run_path = start_run_log("ocr_verdict", vars(args))
    print(f"=== ocr_verdict_pass | {now_iso()} ===")
    print(f"  model: {GEMINI_MODEL}   workers: {args.workers}   batch: {args.batch}")

    # Group un-verdicted items by asset
    by_asset: dict[tuple[str, str], list[tuple[str, str]]] = defaultdict(list)
    n_total = 0
    for aid, kind, key, it in _walk_unverdicted_items():
        by_asset[(aid, kind)].append((key, it["text"]))
        n_total += 1
    print(f"  unverdicted items: {n_total:,} across {len(by_asset):,} assets")
    if args.limit:
        keys = list(by_asset.keys())[: args.limit]
        by_asset = {k: by_asset[k] for k in keys}
        print(f"  --limit (asset count): {len(by_asset):,}")
    if not by_asset:
        finish_run_log(run_path, {"rows_persisted": 0, "note": "no_work"})
        return

    summaries = _build_asset_summary_index()
    print(f"  loaded {len(summaries):,} asset summaries")

    # Build call queue: (asset_id, kind, summary, batch_items)
    units = []
    skipped_no_summary = 0
    for (aid, kind), items in by_asset.items():
        if aid not in summaries:
            skipped_no_summary += 1
            continue
        s = summaries[aid]
        for i in range(0, len(items), args.batch):
            units.append((aid, kind, s, items[i:i + args.batch]))
    print(f"  skipped (no Gemini summary on asset): {skipped_no_summary:,}")
    print(f"  total Gemini calls: {len(units):,}")
    if not units:
        finish_run_log(run_path, {"rows_persisted": 0, "note": "no_summary_match"})
        return

    _local = threading.local()

    def _thread_client():
        if not hasattr(_local, "client"):
            _local.client = _client()
        return _local.client

    t_start = time.time()
    counters = {"done": 0, "rows_persisted": 0, "errors": 0}
    cnt_lock = threading.Lock()

    # Group verdicts per asset so we can flush atomically
    per_asset_verdicts: dict[tuple[str, str], dict[str, dict]] = defaultdict(dict)
    flush_lock = threading.Lock()
    units_per_asset_total: dict[tuple[str, str], int] = defaultdict(int)
    units_per_asset_done: dict[tuple[str, str], int] = defaultdict(int)
    for u in units:
        units_per_asset_total[(u[0], u[1])] += 1

    def worker(unit):
        aid, kind, summary, batch = unit
        try:
            client = _thread_client()
            verdicts = _verdict_batch(client, summary, batch)
        except Exception as e:
            print(f"  [worker error] {type(e).__name__}: {e}", file=sys.stderr, flush=True)
            verdicts = {}
        with flush_lock:
            per_asset_verdicts[(aid, kind)].update(verdicts)
            units_per_asset_done[(aid, kind)] += 1
            ready = units_per_asset_done[(aid, kind)] >= units_per_asset_total[(aid, kind)]
            if ready:
                pending = per_asset_verdicts.pop((aid, kind))
            else:
                pending = None
        if pending:
            n_mutated = _apply_verdicts_to_asset(aid, kind, pending)
            with cnt_lock:
                counters["rows_persisted"] += n_mutated
        with cnt_lock:
            counters["done"] += 1
            if len(verdicts) == 0:
                counters["errors"] += 1
            d = counters["done"]
            if d % 10 == 0 or d == len(units):
                elapsed = time.time() - t_start
                rate = d / elapsed if elapsed else 0
                eta = (len(units) - d) / rate / 60 if rate else 0
                print(f"  [{d:>5}/{len(units)}] persisted={counters['rows_persisted']:,} "
                      f"errors={counters['errors']} rate={rate*60:.0f}/min ETA={eta:.0f}m",
                      flush=True)

    with ThreadPoolExecutor(max_workers=args.workers, thread_name_prefix="w") as ex:
        futures = [ex.submit(worker, u) for u in units]
        for f in as_completed(futures):
            try:
                f.result()
            except Exception as e:
                print(f"  [future error] {type(e).__name__}: {e}", file=sys.stderr, flush=True)

    # Final flush for any per-asset buffers that didn't complete cleanly
    with flush_lock:
        for (aid, kind), pending in per_asset_verdicts.items():
            n = _apply_verdicts_to_asset(aid, kind, pending)
            counters["rows_persisted"] += n

    elapsed = time.time() - t_start
    finish_run_log(run_path, {
        **counters, "wall_clock_sec": round(elapsed, 1), "total_calls": len(units),
    })
    print(f"\nrun complete: {counters['rows_persisted']:,} verdicts persisted, "
          f"{counters['errors']} batch errors, wall-clock {elapsed/60:.1f}m")


def cmd_status(args: argparse.Namespace) -> None:
    total = 0
    by_verdict: dict[str, int] = defaultdict(int)
    suspicious_by_engine: dict[str, int] = defaultdict(int)
    for cat_dir, suffix in ((VIDEO_CATALOG, ".video.json"), (STILLS_CATALOG, ".still.json")):
        for p in cat_dir.glob(f"*{suffix}"):
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                continue
            for it in (d.get(LAYER) or {}).get("items") or []:
                total += 1
                v = it.get("qa_verdict") or "(unverdicted)"
                by_verdict[v] += 1
                if v == "suspicious":
                    suspicious_by_engine[it.get("ocr_engine") or "?"] += 1
    print(f"=== ocr_verdict_pass status (catalog JSON) ===")
    print(f"  ocr_detections total: {total:,}")
    print(f"  by qa_verdict:")
    for v, n in sorted(by_verdict.items(), key=lambda x: -x[1]):
        print(f"    {v}: {n:,}")
    print(f"  suspicious by engine:")
    for engine, n in suspicious_by_engine.items():
        print(f"    {engine}: {n:,}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("run")
    sp.add_argument("--workers", type=int, default=WORKERS)
    sp.add_argument("--batch", type=int, default=BATCH_PER_CALL)
    sp.add_argument("--limit", type=int, help="limit asset count")
    sp.set_defaults(func=cmd_run)
    sp = sub.add_parser("status")
    sp.set_defaults(func=cmd_status)
    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
