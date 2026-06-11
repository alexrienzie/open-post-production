"""
Promote root-level `_unmatched_people` entries to `people_ids` when the entry
name matches `people.json` uniquely (canonical_name / aliases), or when
`name_resolution_rules` supplies a default `p_*` resolution.

Skips:
- normalized terms shorter than 3 characters (except rule patterns can still apply if len>=1)
- alias stems listed in `name_resolution_rules` patterns (same as backfill_entity_ids)
- any normalized alias shared by more than one person (collision)
- rules whose `default_resolution` is not a `p_*` slug
- queue entries whose raw `name` is listed in that rule's `exceptions` (exact match)

Does not modify `analysis._unmatched_people` (nested snapshot); only the
canonical root fields.

Usage:
  python _scripts/registries/reconcile_unmatched_people_registry.py --dry-run
  python _scripts/registries/reconcile_unmatched_people_registry.py
"""

from __future__ import annotations

import argparse
import json
import os
import re
import unicodedata
from collections import defaultdict
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
TRANSCRIPTS = ROOT / "assets/transcripts"
PEOPLE_JSON = ROOT / "people/people.json"


def atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return s.lower().strip()


def entry_name(entry: Any) -> str:
    if isinstance(entry, dict):
        return str(entry.get("name") or entry.get("canonical_name") or entry.get("text") or "").strip()
    if isinstance(entry, str):
        raw = entry.strip()
        return re.split(r"\s+[-\u2014:]\s+|\s+\(|:", raw, maxsplit=1)[0].strip()
    return ""


def load_people_index() -> tuple[dict[str, str], list[dict[str, Any]]]:
    """
    Returns (unique_term_lower -> p_*, rules) using the same stem skip list
    as _scripts/registries/backfill_entity_ids.py. Colliding terms are omitted.
    """
    people = json.loads(PEOPLE_JSON.read_text(encoding="utf-8"))
    rules = list(people.get("name_resolution_rules") or [])
    ambiguous: set[str] = set()
    for r in rules:
        ambiguous.add(norm(r.get("pattern", "")))

    term_to_pids: dict[str, set[str]] = defaultdict(set)
    for p in people.get("people") or []:
        pid = p.get("id")
        if not pid:
            continue
        terms = [p.get("canonical_name", "")] + list(p.get("aliases") or [])
        for t in terms:
            tn = norm(t)
            if not tn or len(tn) < 3:
                continue
            if tn in ambiguous:
                continue
            term_to_pids[tn].add(pid)

    term_to_pid: dict[str, str] = {}
    for tn, pids in term_to_pids.items():
        if len(pids) == 1:
            term_to_pid[tn] = next(iter(pids))
    return term_to_pid, rules


def resolve_queue_name(
    raw: str, term_to_pid: dict[str, str], rules: list[dict[str, Any]]
) -> tuple[str | None, str]:
    """Return (p_* or None, provenance: 'rule'|'alias'|'')."""
    raw = (raw or "").strip()
    if not raw:
        return None, ""
    ln = norm(raw)

    # 1) name_resolution_rules — exact pattern match on normalized label
    for r in rules:
        pat = norm(r.get("pattern", ""))
        if not pat or pat != ln:
            continue
        exceptions = set(r.get("exceptions") or [])
        if raw in exceptions:
            return None, ""
        dr = (r.get("default_resolution") or "").strip()
        if dr.startswith("p_"):
            return dr, "rule"
        return None, ""

    # 2) unique alias / canonical map
    if len(ln) >= 3 and ln in term_to_pid:
        return term_to_pid[ln], "alias"
    return None, ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    term_to_pid, rules = load_people_index()

    stats: dict[str, Any] = {
        "dry_run": args.dry_run,
        "transcripts_examined": 0,
        "transcripts_updated": 0,
        "queue_entries_removed": 0,
        "people_ids_added_total": 0,
        "unresolved_left": 0,
        "resolved_via_rule": 0,
        "resolved_via_alias": 0,
    }

    for path in sorted(TRANSCRIPTS.glob("*.transcript.json")):
        try:
            transcript = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        stats["transcripts_examined"] += 1

        entries = transcript.get("_unmatched_people")
        if not isinstance(entries, list) or not entries:
            continue

        kept: list[Any] = []
        changed = False
        for entry in entries:
            name = entry_name(entry)
            pid, provenance = resolve_queue_name(name, term_to_pid, rules)
            if not pid:
                kept.append(entry)
                stats["unresolved_left"] += 1
                continue

            if provenance == "rule":
                stats["resolved_via_rule"] += 1
            elif provenance == "alias":
                stats["resolved_via_alias"] += 1

            plist = transcript.setdefault("people_ids", [])
            if pid not in plist:
                plist.append(pid)
                plist.sort()
                stats["people_ids_added_total"] += 1
                changed = True
            else:
                changed = True

            stats["queue_entries_removed"] += 1

        if changed:
            transcript["_unmatched_people"] = kept
            stats["transcripts_updated"] += 1
            if not args.dry_run:
                atomic_write_json(path, transcript)

    print(json.dumps(stats, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
