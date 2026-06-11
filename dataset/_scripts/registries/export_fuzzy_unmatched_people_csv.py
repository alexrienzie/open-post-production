"""
Emit a CSV of fuzzy (phonetic + normalized Levenshtein) match proposals for
root-level `_unmatched_people` entries still on transcripts.

Uses the same matching core as `find_correction_candidates.py`. One row per
queue entry that has a registry hit within `--max-distance`. Includes an
`ambiguous` flag when a second *different* person is nearly as close as the best.

Output: `_runs/fuzzy_unmatched_people_<UTC_ts>.csv` (override with `--out`).

Usage:
  python _scripts/registries/export_fuzzy_unmatched_people_csv.py
  python _scripts/registries/export_fuzzy_unmatched_people_csv.py --max-distance 0.25 --out _runs/review.csv
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import re
import sys
from pathlib import Path
from typing import Any

# Reuse phonetic pipeline + candidate extraction from Phase-0 cleanup script.
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent / "transcripts"))  # find_correction_candidates lives there
from find_correction_candidates import (
    extract_candidate_string,
    load_registry_targets,
    normalized_distance,
    phonetic_key,
)

ROOT = Path(__file__).resolve().parents[2]
TRANSCRIPTS = ROOT / "assets/transcripts"
RUNS_DIR = ROOT / "_runs"


def entry_context(entry: Any) -> str:
    if isinstance(entry, dict):
        return str(entry.get("context") or entry.get("evidence") or "").strip()
    return ""


def best_fuzzy_person_match(
    candidate: str,
    p_targets: list[dict],
    max_distance: float,
) -> dict[str, Any] | None:
    cand_key = phonetic_key(candidate)
    if not cand_key:
        return None

    hits: list[tuple[float, dict, str]] = []
    for t in p_targets:
        for n in t["names"]:
            t_key = phonetic_key(n)
            if not t_key:
                continue
            d = 0.0 if t_key == cand_key else normalized_distance(cand_key, t_key)
            if d <= max_distance:
                hits.append((d, t, n))
    if not hits:
        return None

    hits.sort(key=lambda x: (x[0], x[1]["id"], x[2]))
    best_d, best_t, best_n = hits[0]

    second: tuple[float, dict, str] | None = None
    for h in hits[1:]:
        if h[1]["id"] != best_t["id"]:
            second = h
            break

    ambiguous = bool(second is not None and (second[0] - best_d) < 0.051)

    return {
        "best_dist": best_d,
        "target": best_t,
        "matched_name": best_n,
        "second": second,
        "ambiguous": ambiguous,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-distance", type=float, default=0.30)
    ap.add_argument(
        "--out",
        type=Path,
        default=None,
        help="CSV path (default: _runs/fuzzy_unmatched_people_<ts>.csv)",
    )
    ap.add_argument(
        "--min-name-len",
        type=int,
        default=3,
        help="Skip candidates shorter than this (default 3)",
    )
    args = ap.parse_args()

    if not TRANSCRIPTS.exists():
        print(f"ERROR: {TRANSCRIPTS} not found", file=sys.stderr)
        return 1

    p_targets, _ = load_registry_targets()

    ts = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d_%H%M%S")
    out_path = args.out or (RUNS_DIR / f"fuzzy_unmatched_people_{ts}.csv")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    fieldnames = [
        "asset_id",
        "transcript_file",
        "unmatched_name",
        "context",
        "suggested_p_id",
        "suggested_canonical",
        "matched_registry_term",
        "phonetic_distance",
        "ambiguous",
        "second_best_p_id",
        "second_best_distance",
        "matched_registry_term_2",
    ]

    rows_written = 0
    files_scanned = 0

    with out_path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()

        for path in sorted(TRANSCRIPTS.glob("*.transcript.json")):
            try:
                rec = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            files_scanned += 1
            aid = str(rec.get("asset_id") or path.stem.replace(".transcript", ""))

            for entry in rec.get("_unmatched_people") or []:
                cand = extract_candidate_string(entry)
                if len(cand.strip()) < args.min_name_len:
                    continue

                m = best_fuzzy_person_match(cand, p_targets, args.max_distance)
                if m is None:
                    continue

                second = m["second"]
                ctx = entry_context(entry)
                if len(ctx) > 400:
                    ctx = ctx[:397] + "..."

                w.writerow(
                    {
                        "asset_id": aid,
                        "transcript_file": path.name,
                        "unmatched_name": cand,
                        "context": ctx,
                        "suggested_p_id": m["target"]["id"],
                        "suggested_canonical": m["target"]["canonical_name"],
                        "matched_registry_term": m["matched_name"],
                        "phonetic_distance": f"{m['best_dist']:.4f}",
                        "ambiguous": "yes" if m["ambiguous"] else "no",
                        "second_best_p_id": second[1]["id"] if second else "",
                        "second_best_distance": f"{second[0]:.4f}" if second else "",
                        "matched_registry_term_2": second[2] if second else "",
                    }
                )
                rows_written += 1

    try:
        rel = out_path.relative_to(ROOT)
    except ValueError:
        rel = out_path
    print(json.dumps({"csv": str(rel), "rows": rows_written, "files_scanned": files_scanned}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
