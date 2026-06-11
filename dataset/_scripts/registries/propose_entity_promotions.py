"""
Dry-run promotion report for transcript entity queues.

Scans transcript `_unmatched_people`, `_unmatched_orgs`, and `_unmatched_locations`
entries, groups repeated candidates, compares them with the canonical root
registries, and writes review artifacts under `_runs/entity_promotions_<ts>/`.

This script is intentionally non-mutating. Use its review template to decide
which candidates should become aliases, new registry rows, transcript ID
backfills, or ignored noise.

Usage:
    python _scripts/registries/propose_entity_promotions.py
    python _scripts/registries/propose_entity_promotions.py --min-occurrences 2
    python _scripts/registries/propose_entity_promotions.py --max-examples 8
"""

from __future__ import annotations

import argparse
import datetime as dt
import difflib
import json
import os
import re
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
TRANSCRIPTS = ROOT / "assets/transcripts"
RUNS_DIR = ROOT / "_runs"

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


@dataclass
class Candidate:
    domain: str
    normalized_name: str
    names: Counter[str] = field(default_factory=Counter)
    contexts: Counter[str] = field(default_factory=Counter)
    types: Counter[str] = field(default_factory=Counter)
    transcript_ids: set[str] = field(default_factory=set)
    existing_ids_on_records: Counter[str] = field(default_factory=Counter)

    @property
    def occurrence_count(self) -> int:
        return sum(self.names.values())

    @property
    def transcript_count(self) -> int:
        return len(self.transcript_ids)

    @property
    def display_name(self) -> str:
        if not self.names:
            return self.normalized_name
        return self.names.most_common(1)[0][0]


def utc_slug() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d_%H%M%S")


def atomic_write_json(path: Path, data: Any) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def atomic_write_text(path: Path, body: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(body, encoding="utf-8")
    os.replace(tmp, path)


def strip_accents(s: str) -> str:
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()


def normalize_name(s: str) -> str:
    s = strip_accents(s or "").lower()
    s = re.sub(r"[`'\".,;:!?()\[\]{}]", " ", s)
    s = re.sub(r"\b(the|a|an)\b", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def slugify(prefix: str, name: str) -> str:
    slug = strip_accents(name).lower().strip()
    slug = re.sub(r"[^a-z0-9]+", "_", slug).strip("_")
    slug = re.sub(r"_+", "_", slug)
    if not slug:
        slug = "unknown"
    return f"{prefix}{slug}"


_LEADING_NAME_RE = re.compile(r"^([A-Z][A-Za-z'\.\-]+(?:\s+[A-Z][A-Za-z'\.\-]+){0,5})")


def extract_entry(entry: Any, domain: str) -> tuple[str, str, str]:
    """Return (name, context, place_type). Handles legacy string and object shapes."""
    place_type = ""
    if isinstance(entry, dict):
        name = (
            entry.get("name")
            or entry.get("canonical_name")
            or entry.get("label")
            or entry.get("text")
            or ""
        )
        context = (
            entry.get("context")
            or entry.get("evidence")
            or entry.get("reason")
            or entry.get("source_text")
            or ""
        )
        if domain == "places":
            place_type = entry.get("type") or entry.get("place_type") or ""
        return str(name).strip(), str(context).strip(), str(place_type).strip()

    if not isinstance(entry, str):
        return "", "", ""

    raw = entry.strip()
    if not raw:
        return "", "", ""

    name = raw
    context = ""
    match = _LEADING_NAME_RE.match(raw)
    if match:
        name = match.group(1)
        context = raw[len(name) :].strip(" -:()[]")
    else:
        parts = re.split(r"\s+[-\u2014:]\s+|\s+\(|:", raw, maxsplit=1)
        name = parts[0].strip()
        context = parts[1].strip(" )") if len(parts) > 1 else ""

    return name.strip(), context.strip(), ""


def load_registry(domain: str) -> list[dict[str, Any]]:
    spec = REGISTRY_SPECS[domain]
    path = spec["path"]
    if not path.exists():
        return []
    doc = json.loads(path.read_text(encoding="utf-8"))
    rows = doc.get(spec["array_key"]) or []
    return [row for row in rows if isinstance(row, dict) and row.get("id")]


def build_registry_index(rows: list[dict[str, Any]]) -> tuple[dict[str, list[dict[str, str]]], dict[str, dict[str, Any]]]:
    term_index: dict[str, list[dict[str, str]]] = defaultdict(list)
    by_id: dict[str, dict[str, Any]] = {}

    for row in rows:
        entity_id = str(row.get("id") or "")
        if not entity_id:
            continue
        by_id[entity_id] = row
        terms = [row.get("canonical_name") or ""]
        terms.extend(row.get("aliases") or [])
        for term in terms:
            term = str(term).strip()
            norm = normalize_name(term)
            if not norm:
                continue
            term_index[norm].append(
                {
                    "id": entity_id,
                    "term": term,
                    "canonical_name": str(row.get("canonical_name") or ""),
                }
            )

    return dict(term_index), by_id


def collect_candidates(max_examples_per_candidate: int) -> dict[str, dict[str, Candidate]]:
    candidates: dict[str, dict[str, Candidate]] = {
        "people": {},
        "orgs": {},
        "places": {},
    }

    for path in sorted(TRANSCRIPTS.glob("*.transcript.json")):
        try:
            transcript = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue

        asset_id = str(transcript.get("asset_id") or path.stem.replace(".transcript", ""))
        for domain, spec in REGISTRY_SPECS.items():
            entries = transcript.get(spec["queue_field"]) or []
            if not isinstance(entries, list):
                continue

            existing_ids = transcript.get(spec["id_field"]) or []
            for entry in entries:
                name, context, place_type = extract_entry(entry, domain)
                norm = normalize_name(name)
                if not norm:
                    continue

                bucket = candidates[domain].setdefault(norm, Candidate(domain=domain, normalized_name=norm))
                bucket.names[name] += 1
                bucket.transcript_ids.add(asset_id)
                if context and len(bucket.contexts) < max_examples_per_candidate:
                    bucket.contexts[context[:500]] += 1
                if place_type:
                    bucket.types[place_type] += 1
                for entity_id in existing_ids:
                    bucket.existing_ids_on_records[str(entity_id)] += 1

    return candidates


def top_registry_matches(
    candidate: Candidate,
    term_index: dict[str, list[dict[str, str]]],
    by_id: dict[str, dict[str, Any]],
    limit: int,
) -> list[dict[str, Any]]:
    exact = term_index.get(candidate.normalized_name) or []
    if exact:
        return [
            {
                "id": item["id"],
                "canonical_name": item["canonical_name"],
                "matched_term": item["term"],
                "score": 1.0,
                "match_type": "exact",
            }
            for item in exact[:limit]
        ]

    scores: dict[str, dict[str, Any]] = {}
    for norm_term, entries in term_index.items():
        score = difflib.SequenceMatcher(None, candidate.normalized_name, norm_term).ratio()
        if score < 0.72:
            continue
        for item in entries:
            prev = scores.get(item["id"])
            if prev is None or score > prev["score"]:
                scores[item["id"]] = {
                    "id": item["id"],
                    "canonical_name": item["canonical_name"],
                    "matched_term": item["term"],
                    "score": round(score, 3),
                    "match_type": "fuzzy",
                }

    matches = sorted(scores.values(), key=lambda m: (-m["score"], m["canonical_name"]))[:limit]
    for match in matches:
        row = by_id.get(match["id"]) or {}
        if row.get("type"):
            match["type"] = row.get("type")
    return matches


def suggest_decision(candidate: Candidate, matches: list[dict[str, Any]], prefix: str) -> dict[str, Any]:
    best = matches[0] if matches else None
    if best and best["score"] == 1.0:
        return {
            "decision": "add_id_existing",
            "target_id": best["id"],
            "reason": "Candidate exactly matches an existing canonical name or alias.",
        }
    if best and best["score"] >= 0.92:
        return {
            "decision": "alias_existing",
            "target_id": best["id"],
            "reason": "Candidate is a very close spelling variant of an existing registry entry.",
        }
    if best and best["score"] >= 0.84:
        return {
            "decision": "review_possible_alias",
            "target_id": best["id"],
            "reason": "Candidate resembles an existing entry but needs human review.",
        }
    return {
        "decision": "new_entity",
        "target_id": slugify(prefix, candidate.display_name),
        "reason": "No strong registry match found.",
    }


def candidate_to_json(
    candidate: Candidate,
    matches: list[dict[str, Any]],
    suggestion: dict[str, Any],
) -> dict[str, Any]:
    return {
        "domain": candidate.domain,
        "name": candidate.display_name,
        "normalized_name": candidate.normalized_name,
        "occurrence_count": candidate.occurrence_count,
        "transcript_count": candidate.transcript_count,
        "name_variants": [
            {"name": name, "count": count}
            for name, count in candidate.names.most_common(10)
        ],
        "place_type_hints": [
            {"type": place_type, "count": count}
            for place_type, count in candidate.types.most_common()
        ],
        "existing_ids_on_records": [
            {"id": entity_id, "count": count}
            for entity_id, count in candidate.existing_ids_on_records.most_common(10)
        ],
        "example_contexts": [
            {"context": context, "count": count}
            for context, count in candidate.contexts.most_common(5)
        ],
        "top_matches": matches,
        "suggestion": suggestion,
    }


def review_decision_stub(candidate: dict[str, Any]) -> dict[str, Any]:
    suggestion = candidate["suggestion"]
    return {
        "domain": candidate["domain"],
        "name": candidate["name"],
        "normalized_name": candidate["normalized_name"],
        "occurrence_count": candidate["occurrence_count"],
        "decision": "review",
        "allowed_decisions": [
            "add_id_existing",
            "alias_existing",
            "new_entity",
            "ignore",
        ],
        "suggested_decision": suggestion["decision"],
        "target_id": suggestion["target_id"],
        "canonical_name": candidate["name"] if suggestion["decision"] == "new_entity" else "",
        "aliases": [] if suggestion["decision"] == "new_entity" else [candidate["name"]],
        "confidence": "low",
        "notes": suggestion["reason"],
        "apply_to_transcripts": True,
    }


def render_markdown(out_dir: Path, stats: dict[str, Any], candidates_by_domain: dict[str, list[dict[str, Any]]]) -> str:
    lines: list[str] = []
    lines.append("# Entity Promotion Dry Run\n\n")
    lines.append(f"Generated: `{stats['generated_at']}`\n\n")
    lines.append("This report is non-mutating. Review `review_decisions.template.json` before applying any registry or transcript changes.\n\n")

    lines.append("## Summary\n\n")
    lines.append("| domain | queue entries | grouped candidates | exact existing | strong alias | possible alias | likely new |\n")
    lines.append("|---|---:|---:|---:|---:|---:|---:|\n")
    for domain in ("people", "orgs", "places"):
        s = stats["domains"][domain]
        lines.append(
            f"| {domain} | {s['queue_entries']} | {s['grouped_candidates']} | "
            f"{s['add_id_existing']} | {s['alias_existing']} | "
            f"{s['review_possible_alias']} | {s['new_entity']} |\n"
        )
    lines.append("\n")

    lines.append("## Suggested Commands\n\n")
    lines.append("After approving decisions, the usual follow-up sequence is:\n\n")
    lines.append("```powershell\n")
    lines.append("python _scripts/registries/backfill_entity_ids.py --domain all\n")
    lines.append("python _scripts/transcripts/build_transcript_prompt_context.py\n")
    lines.append("```\n\n")

    for domain in ("people", "orgs", "places"):
        rows = candidates_by_domain[domain][:25]
        lines.append(f"## Top {domain.title()} Candidates\n\n")
        if not rows:
            lines.append("_No candidates above the occurrence threshold._\n\n")
            continue
        lines.append("| count | candidate | suggestion | target / match | example context |\n")
        lines.append("|---:|---|---|---|---|\n")
        for row in rows:
            suggestion = row["suggestion"]
            best_match = row["top_matches"][0] if row["top_matches"] else {}
            target = suggestion.get("target_id") or best_match.get("id") or ""
            context = ""
            if row["example_contexts"]:
                context = row["example_contexts"][0]["context"].replace("|", "\\|")
            if len(context) > 140:
                context = context[:137] + "..."
            lines.append(
                f"| {row['occurrence_count']} | {row['name']} | "
                f"{suggestion['decision']} | `{target}` | {context} |\n"
            )
        lines.append("\n")

    lines.append("## Artifacts\n\n")
    lines.append(f"- `candidates.json`: full grouped candidates with examples and top matches.\n")
    lines.append(f"- `review_decisions.template.json`: editable approval file for a future apply step.\n")
    lines.append(f"- `stats.json`: machine-readable summary counts.\n")
    lines.append(f"- Output directory: `{out_dir}`\n")
    return "".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--min-occurrences", type=int, default=1)
    parser.add_argument("--max-examples", type=int, default=5)
    parser.add_argument("--match-limit", type=int, default=5)
    args = parser.parse_args()

    timestamp = utc_slug()
    out_dir = RUNS_DIR / f"entity_promotions_{timestamp}"
    out_dir.mkdir(parents=True, exist_ok=False)

    raw_candidates = collect_candidates(max_examples_per_candidate=args.max_examples)
    registry_indexes = {}
    for domain in REGISTRY_SPECS:
        rows = load_registry(domain)
        registry_indexes[domain] = build_registry_index(rows)

    candidates_by_domain: dict[str, list[dict[str, Any]]] = {}
    stats: dict[str, Any] = {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "min_occurrences": args.min_occurrences,
        "domains": {},
    }

    for domain, grouped in raw_candidates.items():
        term_index, by_id = registry_indexes[domain]
        prefix = REGISTRY_SPECS[domain]["id_prefix"]
        rows: list[dict[str, Any]] = []
        decision_counts: Counter[str] = Counter()
        queue_entries = 0

        for candidate in grouped.values():
            queue_entries += candidate.occurrence_count
            if candidate.occurrence_count < args.min_occurrences:
                continue
            matches = top_registry_matches(candidate, term_index, by_id, args.match_limit)
            suggestion = suggest_decision(candidate, matches, prefix)
            decision_counts[suggestion["decision"]] += 1
            rows.append(candidate_to_json(candidate, matches, suggestion))

        rows.sort(key=lambda row: (-row["occurrence_count"], row["name"].lower()))
        candidates_by_domain[domain] = rows
        stats["domains"][domain] = {
            "queue_entries": queue_entries,
            "grouped_candidates": len(rows),
            "registry_entries": len(by_id),
            "add_id_existing": decision_counts["add_id_existing"],
            "alias_existing": decision_counts["alias_existing"],
            "review_possible_alias": decision_counts["review_possible_alias"],
            "new_entity": decision_counts["new_entity"],
        }

    all_candidates = {
        "schema_version": 1,
        "generated_at": stats["generated_at"],
        "min_occurrences": args.min_occurrences,
        "domains": candidates_by_domain,
    }
    review_template = {
        "schema_version": 1,
        "generated_at": stats["generated_at"],
        "source_candidates": str(out_dir / "candidates.json"),
        "instructions": [
            "Set decision to add_id_existing, alias_existing, new_entity, or ignore.",
            "For add_id_existing and alias_existing, target_id must be an existing registry id.",
            "For new_entity, target_id should be the proposed new p_/o_/pl_ slug.",
            "Leave apply_to_transcripts true when the candidate should be removed from the queue and added to *_ids.",
        ],
        "decisions": [
            review_decision_stub(row)
            for domain in ("people", "orgs", "places")
            for row in candidates_by_domain[domain]
        ],
    }

    atomic_write_json(out_dir / "stats.json", stats)
    atomic_write_json(out_dir / "candidates.json", all_candidates)
    atomic_write_json(out_dir / "review_decisions.template.json", review_template)
    atomic_write_text(out_dir / "candidate_report.md", render_markdown(out_dir, stats, candidates_by_domain))

    print(f"Wrote {out_dir}")
    for domain in ("people", "orgs", "places"):
        s = stats["domains"][domain]
        print(
            f"{domain}: {s['queue_entries']} queue entries, "
            f"{s['grouped_candidates']} grouped candidates, "
            f"{s['add_id_existing']} exact, {s['alias_existing']} strong aliases, "
            f"{s['review_possible_alias']} possible aliases, {s['new_entity']} likely new"
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
