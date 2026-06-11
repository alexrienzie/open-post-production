"""
Audit people/org/place registries and common catalog references.

Checks:
- registry ID format and uniqueness
- _meta.total_count consistency
- relationship and parent_id targets resolve
- common record-level people_ids/org_ids/place_ids references resolve

The place registry path is auto-detected to support the locations -> places
rename migration.

Usage:
    python _scripts/registries/audit_entity_registries.py
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[2]
PEOPLE_JSON = ROOT / "people/people.json"
ORGS_JSON = ROOT / "organizations/orgs.json"
PLACE_JSON_CANDIDATES = [
    ROOT / "places/places.json",
]

ID_PATTERNS = {
    "people": re.compile(r"^p_[a-z0-9_]+$"),
    "orgs": re.compile(r"^o_[a-z0-9_]+$"),
    "places": re.compile(r"^pl_[a-z0-9_]+$"),
}


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def place_json_path() -> Path:
    for path in PLACE_JSON_CANDIDATES:
        if path.exists():
            return path
    raise FileNotFoundError("No place registry found at places/places.json")


def registry_rows() -> dict[str, tuple[Path, str, list[dict[str, Any]]]]:
    people = load_json(PEOPLE_JSON)
    orgs = load_json(ORGS_JSON)
    places_path = place_json_path()
    places = load_json(places_path)
    return {
        "people": (PEOPLE_JSON, "people", people.get("people") or []),
        "orgs": (ORGS_JSON, "organizations", orgs.get("organizations") or []),
        "places": (places_path, "places", places.get("places") or []),
    }


def all_ids(rows_by_domain: dict[str, tuple[Path, str, list[dict[str, Any]]]]) -> dict[str, set[str]]:
    return {
        domain: {str(row.get("id")) for row in rows if row.get("id")}
        for domain, (_, _, rows) in rows_by_domain.items()
    }


def target_exists(target_id: str, ids: dict[str, set[str]]) -> bool:
    if target_id.startswith("p_"):
        return target_id in ids["people"]
    if target_id.startswith("o_"):
        return target_id in ids["orgs"]
    if target_id.startswith("pl_"):
        return target_id in ids["places"]
    return False


def audit_registry_docs() -> tuple[list[str], dict[str, int]]:
    errors: list[str] = []
    stats: dict[str, int] = {}
    rows_by_domain = registry_rows()
    ids = all_ids(rows_by_domain)

    for domain, (path, array_key, rows) in rows_by_domain.items():
        doc = load_json(path)
        seen: set[str] = set()
        pattern = ID_PATTERNS[domain]
        stats[f"{domain}_count"] = len(rows)

        meta_count = (doc.get("_meta") or {}).get("total_count")
        if meta_count is not None and meta_count != len(rows):
            errors.append(f"{path}: _meta.total_count={meta_count}, actual={len(rows)}")

        for idx, row in enumerate(rows):
            entity_id = row.get("id")
            if not entity_id:
                errors.append(f"{path}: {array_key}[{idx}] missing id")
                continue
            if entity_id in seen:
                errors.append(f"{path}: duplicate id {entity_id}")
            seen.add(entity_id)
            if not pattern.match(str(entity_id)):
                errors.append(f"{path}: invalid {domain} id {entity_id}")

            for rel in row.get("relationships") or []:
                target_id = str((rel or {}).get("to_id") or "")
                if not target_id:
                    errors.append(f"{path}: {entity_id} relationship missing to_id")
                elif not target_exists(target_id, ids):
                    errors.append(f"{path}: {entity_id} relationship target missing: {target_id}")

            parent_id = row.get("parent_id")
            if parent_id and parent_id not in ids["places"]:
                errors.append(f"{path}: {entity_id} parent_id target missing: {parent_id}")

    return errors, stats


def iter_json_files(root: Path) -> Iterable[Path]:
    if not root.exists():
        return
    for path in root.rglob("*.json"):
        if path.is_file():
            yield path


def iter_reference_records() -> Iterable[tuple[Path, dict[str, Any]]]:
    roots = [
        ROOT / "assets/transcripts",
        ROOT / "documents/press/articles",
        ROOT / "documents/press/comments",
        ROOT / "documents/press/social_posts",
    ]
    for root in roots:
        for path in iter_json_files(root):
            try:
                record = load_json(path)
            except Exception:
                continue
            if isinstance(record, dict):
                yield path, record

    for path in [ROOT / "timeline/us_events.jsonl"]:
        if not path.exists():
            continue
        for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except Exception:
                continue
            if isinstance(record, dict):
                yield Path(f"{path}:{line_no}"), record


def audit_record_references(ids: dict[str, set[str]]) -> tuple[list[str], dict[str, int]]:
    errors: list[str] = []
    stats = {
        "records_examined": 0,
        "people_refs": 0,
        "org_refs": 0,
        "place_refs": 0,
    }
    fields = [
        ("people_ids", "people", "people_refs"),
        ("org_ids", "orgs", "org_refs"),
        ("place_ids", "places", "place_refs"),
    ]
    for path, record in iter_reference_records():
        stats["records_examined"] += 1
        for field, domain, stat_name in fields:
            values = record.get(field) or []
            if not isinstance(values, list):
                errors.append(f"{path}: {field} is not a list")
                continue
            for value in values:
                stats[stat_name] += 1
                if value not in ids[domain]:
                    errors.append(f"{path}: missing {field} target {value}")
    return errors, stats


def main() -> int:
    rows_by_domain = registry_rows()
    ids = all_ids(rows_by_domain)
    registry_errors, registry_stats = audit_registry_docs()
    reference_errors, reference_stats = audit_record_references(ids)
    errors = registry_errors + reference_errors

    summary = {
        **registry_stats,
        **reference_stats,
        "errors": len(errors),
        "place_registry": str(place_json_path().relative_to(ROOT)),
    }
    print(json.dumps(summary, indent=2))
    if errors:
        print("\nERRORS:")
        for error in errors[:200]:
            print(f"- {error}")
        if len(errors) > 200:
            print(f"- ... {len(errors) - 200} more")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
