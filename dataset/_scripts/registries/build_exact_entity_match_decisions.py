"""
Build an apply-ready decisions file for exact existing entity matches.

Input is a `candidates.json` artifact from propose_entity_promotions.py.
By default, only candidates whose suggestion is `add_id_existing` are included.
With `--suggestion alias_existing`, this can also build a reviewed strong-alias
file that adds the candidate name as an alias on the target registry row.

Usage:
    python _scripts/registries/build_exact_entity_match_decisions.py _runs/entity_promotions_.../candidates.json
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from pathlib import Path
from typing import Any


def atomic_write_json(path: Path, data: Any) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("candidates_json", type=Path)
    parser.add_argument(
        "--suggestion",
        choices=["add_id_existing", "alias_existing"],
        default="add_id_existing",
        help="Suggestion bucket to convert into apply decisions.",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Output decisions JSON. Defaults beside candidates.json.",
    )
    args = parser.parse_args()

    candidates_path = args.candidates_json
    doc = json.loads(candidates_path.read_text(encoding="utf-8"))
    decisions = []

    for domain, rows in (doc.get("domains") or {}).items():
        for row in rows or []:
            suggestion = row.get("suggestion") or {}
            if suggestion.get("decision") != args.suggestion:
                continue
            top_matches = row.get("top_matches") or []
            if args.suggestion == "add_id_existing" and (
                not top_matches or top_matches[0].get("match_type") != "exact"
            ):
                continue
            decisions.append(
                {
                    "domain": domain,
                    "name": row["name"],
                    "normalized_name": row.get("normalized_name"),
                    "decision": args.suggestion,
                    "target_id": suggestion["target_id"],
                    "aliases": [row["name"]] if args.suggestion == "alias_existing" else [],
                    "notes": f"Bulk {args.suggestion} cleanup from candidates.json.",
                    "apply_to_transcripts": True,
                }
            )

    out = args.out or (candidates_path.parent / "review_decisions.exact_existing.json")
    payload = {
        "schema_version": 1,
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "source_candidates": str(candidates_path),
        "review_notes": [
            f"Bulk cleanup for {args.suggestion} registry matches.",
            "For add_id_existing, no new entities or aliases are created.",
        ],
        "decisions": decisions,
    }
    atomic_write_json(out, payload)
    print(f"Wrote {out}")
    print(f"decisions: {len(decisions)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
