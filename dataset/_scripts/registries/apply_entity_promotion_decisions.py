"""
Apply reviewed entity promotion decisions.

Consumes a curated decisions JSON produced from
`_scripts/registries/propose_entity_promotions.py` output. This updates root registries,
adds approved IDs to matching transcript records, and removes resolved queue
entries from `_unmatched_people`, `_unmatched_orgs`, or `_unmatched_locations`.

Usage:
    python _scripts/registries/apply_entity_promotion_decisions.py path/to/decisions.json
    python _scripts/registries/apply_entity_promotion_decisions.py path/to/decisions.json --dry-run
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import unicodedata
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
TRANSCRIPTS = ROOT / "assets/transcripts"

REGISTRY_SPECS = {
    "people": {
        "path": ROOT / "people/people.json",
        "array_key": "people",
        "id_prefix": "p_",
        "id_field": "people_ids",
        "queue_field": "_unmatched_people",
    },
    "orgs": {
        "path": ROOT / "organizations/orgs.json",
        "array_key": "organizations",
        "id_prefix": "o_",
        "id_field": "org_ids",
        "queue_field": "_unmatched_orgs",
    },
    "places": {
        "path": ROOT / "places/places.json",
        "array_key": "places",
        "id_prefix": "pl_",
        "id_field": "place_ids",
        "queue_field": "_unmatched_places",
    },
}


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def atomic_write_json(path: Path, data: Any) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    try:
        os.replace(tmp, path)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        finally:
            raise


def strip_accents(s: str) -> str:
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()


def normalize_name(s: str) -> str:
    s = strip_accents(s or "").lower()
    s = re.sub(r"[`'\".,;:!?()\[\]{}]", " ", s)
    s = re.sub(r"\b(the|a|an)\b", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


_LEADING_NAME_RE = re.compile(r"^([A-Z][A-Za-z'\.\-]+(?:\s+[A-Z][A-Za-z'\.\-]+){0,5})")


def extract_entry_name(entry: Any) -> str:
    if isinstance(entry, dict):
        return str(
            entry.get("name")
            or entry.get("canonical_name")
            or entry.get("label")
            or entry.get("text")
            or ""
        ).strip()
    if not isinstance(entry, str):
        return ""
    raw = entry.strip()
    if not raw:
        return ""
    match = _LEADING_NAME_RE.match(raw)
    if match:
        return match.group(1).strip()
    return re.split(r"\s+[-\u2014:]\s+|\s+\(|:", raw, maxsplit=1)[0].strip()


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def append_source_pass(doc: dict[str, Any], pass_name: str) -> None:
    meta = doc.setdefault("_meta", {})
    source_passes = meta.setdefault("source_passes", [])
    if pass_name not in source_passes:
        source_passes.append(pass_name)


def update_confidence_counts(doc: dict[str, Any], array_key: str) -> None:
    meta = doc.setdefault("_meta", {})
    rows = doc.get(array_key) or []
    meta["total_count"] = len(rows)
    counts: dict[str, int] = {}
    for row in rows:
        confidence = row.get("confidence")
        if confidence:
            counts[confidence] = counts.get(confidence, 0) + 1
    if counts:
        meta["by_confidence"] = counts


def registry_row_for_new_entity(domain: str, decision: dict[str, Any]) -> dict[str, Any]:
    entity_id = decision["target_id"]
    canonical_name = decision.get("canonical_name") or decision["name"]
    aliases = list(dict.fromkeys(decision.get("aliases") or []))
    confidence = decision.get("confidence") or "low"
    notes = decision.get("notes") or ""

    if domain == "people":
        return {
            "id": entity_id,
            "canonical_name": canonical_name,
            "aliases": aliases,
            "roles": decision.get("roles") or [],
            "sources": ["transcript_unmatched"],
            "first_appearance_context": decision.get("first_appearance_context")
            or "Promoted from transcript _unmatched_people review.",
            "relationships": decision.get("relationships") or [],
            "notes": notes,
            "confidence": confidence,
        }

    if domain == "orgs":
        return {
            "id": entity_id,
            "canonical_name": canonical_name,
            "aliases": aliases,
            "type": decision.get("type") or "unknown",
            "sources": ["transcript_unmatched"],
            "mention_count": int(decision.get("mention_count") or 0),
            "relationships": decision.get("relationships") or [],
            "notes": notes,
            "confidence": confidence,
        }

    if domain == "places":
        row = {
            "id": entity_id,
            "canonical_name": canonical_name,
            "aliases": aliases,
            "type": decision.get("type") or "unknown",
            "tags": decision.get("tags") or [],
            "sources": ["assets/transcripts:_proposed_places"],
            "mention_count": int(decision.get("mention_count") or 0),
            "relationships": decision.get("relationships") or [],
            "notes": notes,
            "confidence": decision.get("confidence") or "medium",
        }
        if decision.get("parent_id"):
            row["parent_id"] = decision["parent_id"]
        return row

    raise ValueError(f"Unsupported domain: {domain}")


def apply_registry_decisions(decisions: list[dict[str, Any]], dry_run: bool) -> dict[str, int]:
    stats = {"new_entities": 0, "aliases_added": 0}
    pass_name = "entity_promotion_review_2026_05_08"

    by_domain: dict[str, list[dict[str, Any]]] = {domain: [] for domain in REGISTRY_SPECS}
    for decision in decisions:
        by_domain[decision["domain"]].append(decision)

    for domain, domain_decisions in by_domain.items():
        if not domain_decisions:
            continue
        spec = REGISTRY_SPECS[domain]
        doc = load_json(spec["path"])
        rows = doc.setdefault(spec["array_key"], [])
        by_id = {row.get("id"): row for row in rows if isinstance(row, dict)}
        changed = False

        for decision in domain_decisions:
            action = decision["decision"]
            target_id = decision.get("target_id")
            if action == "ignore":
                continue
            if not target_id or not target_id.startswith(spec["id_prefix"]):
                raise ValueError(f"{decision['name']}: invalid target_id {target_id!r}")

            if action == "new_entity":
                if target_id in by_id:
                    row = by_id[target_id]
                else:
                    row = registry_row_for_new_entity(domain, decision)
                    rows.append(row)
                    by_id[target_id] = row
                    stats["new_entities"] += 1
                    changed = True
            elif action in {"add_id_existing", "alias_existing"}:
                if target_id not in by_id:
                    raise ValueError(f"{decision['name']}: target_id {target_id!r} does not exist")
                row = by_id[target_id]
            else:
                raise ValueError(f"{decision['name']}: unsupported decision {action!r}")

            for alias in decision.get("aliases") or []:
                alias = str(alias).strip()
                if not alias or alias == row.get("canonical_name"):
                    continue
                aliases = row.setdefault("aliases", [])
                if alias not in aliases:
                    aliases.append(alias)
                    stats["aliases_added"] += 1
                    changed = True

        if changed:
            doc["last_updated_at"] = utc_now()
            append_source_pass(doc, pass_name)
            update_confidence_counts(doc, spec["array_key"])
            if not dry_run:
                atomic_write_json(spec["path"], doc)

    return stats


def apply_transcript_decisions(decisions: list[dict[str, Any]], dry_run: bool) -> dict[str, int]:
    stats = {
        "transcripts_examined": 0,
        "transcripts_updated": 0,
        "transcript_write_errors": 0,
        "queue_entries_removed": 0,
        "ids_added": 0,
    }

    decisions_by_domain_and_name: dict[tuple[str, str], dict[str, Any]] = {}
    for decision in decisions:
        if not decision.get("apply_to_transcripts", True):
            continue
        key = (decision["domain"], normalize_name(decision["name"]))
        decisions_by_domain_and_name[key] = decision

    for path in sorted(TRANSCRIPTS.glob("*.transcript.json")):
        try:
            transcript = load_json(path)
        except Exception:
            continue

        stats["transcripts_examined"] += 1
        changed = False

        for domain, spec in REGISTRY_SPECS.items():
            queue_field = spec["queue_field"]
            entries = transcript.get(queue_field)
            if not isinstance(entries, list) or not entries:
                continue

            kept_entries = []
            for entry in entries:
                normalized = normalize_name(extract_entry_name(entry))
                decision = decisions_by_domain_and_name.get((domain, normalized))
                if not decision:
                    kept_entries.append(entry)
                    continue

                stats["queue_entries_removed"] += 1
                changed = True
                if decision["decision"] != "ignore":
                    id_field = spec["id_field"]
                    existing = transcript.setdefault(id_field, [])
                    target_id = decision["target_id"]
                    if target_id not in existing:
                        existing.append(target_id)
                        existing.sort()
                        stats["ids_added"] += 1

            if kept_entries != entries:
                transcript[queue_field] = kept_entries

        if changed:
            if not dry_run:
                try:
                    atomic_write_json(path, transcript)
                except PermissionError as exc:
                    stats["transcript_write_errors"] += 1
                    print(f"WARNING: skipped locked transcript {path}: {exc}")
                    continue
            stats["transcripts_updated"] += 1

    return stats


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("decisions_json", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    doc = load_json(args.decisions_json)
    decisions = doc.get("decisions") or []
    approved = [d for d in decisions if d.get("decision") != "review"]

    registry_stats = apply_registry_decisions(approved, args.dry_run)
    transcript_stats = apply_transcript_decisions(approved, args.dry_run)

    print(json.dumps({
        "dry_run": args.dry_run,
        "decisions": len(approved),
        "registry": registry_stats,
        "transcripts": transcript_stats,
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
