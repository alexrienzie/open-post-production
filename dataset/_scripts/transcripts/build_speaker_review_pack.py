#!/usr/bin/env python3
"""
Build a compact review pack for manual speaker QA.

Inputs:
- A speaker_accuracy_*.json run produced by review_speaker_accuracy.py (with focus enabled)
- Corresponding *_samples.jsonl mismatch samples

Output:
- _runs/speaker_review_pack_<run_id>.json
"""

from __future__ import annotations

import argparse
import json
import os
from collections import defaultdict
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
RUNS = ROOT / "_runs"


def atomic_write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-json", required=True, help="Path to speaker_accuracy_*.json")
    ap.add_argument("--samples-jsonl", required=True, help="Path to speaker_accuracy_*_samples.jsonl")
    ap.add_argument("--top-pairs", type=int, default=10)
    ap.add_argument("--samples-per-pair", type=int, default=20)
    ap.add_argument("--worst-assets", type=int, default=10)
    ap.add_argument("--min-focused-scored", type=int, default=50)
    args = ap.parse_args()

    run_path = Path(args.run_json)
    samples_path = Path(args.samples_jsonl)
    run = json.loads(run_path.read_text(encoding="utf-8"))
    focus = run.get("focus") or {}
    conf = focus.get("confusion") or {}
    per_asset = focus.get("per_asset") or {}

    # top confusion pairs (off-diagonal)
    pairs = []
    for t, preds in conf.items():
        for p, n in preds.items():
            if t == p:
                continue
            pairs.append((int(n), t, p))
    pairs.sort(reverse=True)
    top_pairs = [(t, p, n) for n, t, p in pairs[: int(args.top_pairs)]]

    # worst assets list
    assets = []
    for aid, s in per_asset.items():
        scored = int(s.get("utterances_scored") or 0)
        acc = s.get("accuracy")
        if acc is None or scored < int(args.min_focused_scored):
            continue
        assets.append((float(acc), scored, aid, s.get("roster_ids") or []))
    assets.sort()
    worst_assets = [
        {"asset_id": aid, "accuracy": acc, "utterances_scored": scored, "roster_ids": roster}
        for acc, scored, aid, roster in assets[: int(args.worst_assets)]
    ]

    # collect mismatch samples for top pairs + worst assets
    per_pair_samples = {f"{t}->{p}": [] for t, p, _ in top_pairs}
    worst_asset_set = {w["asset_id"] for w in worst_assets}
    worst_asset_samples = defaultdict(list)

    if samples_path.exists():
        for line in samples_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            r = json.loads(line)
            true_pid = r.get("human", {}).get("p_id")
            pred_pid = r.get("machine", {}).get("pred_p_id")
            aid = r.get("asset_id")
            key = f"{true_pid}->{pred_pid}"
            if key in per_pair_samples and len(per_pair_samples[key]) < int(args.samples_per_pair):
                per_pair_samples[key].append(r)
            if aid in worst_asset_set and len(worst_asset_samples[aid]) < 25:
                worst_asset_samples[aid].append(r)

    out = {
        "source_run": run.get("run_id"),
        "focus_params": {
            "min_utt_dur_sec": focus.get("min_utt_dur_sec"),
            "min_pred_support_sec": focus.get("min_pred_support_sec"),
        },
        "focus_totals": focus.get("totals"),
        "focus_accuracy": focus.get("accuracy"),
        "top_confusion_pairs": [
            {"true_pid": t, "pred_pid": p, "count": n} for (t, p, n) in top_pairs
        ],
        "worst_assets": worst_assets,
        "samples_by_pair": per_pair_samples,
        "samples_by_worst_asset": dict(worst_asset_samples),
    }

    out_path = RUNS / f"speaker_review_pack_{run.get('run_id')}.json"
    atomic_write_json(out_path, out)
    print(str(out_path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

