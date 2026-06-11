"""
Validate LLM output against controlled vocabularies before merging into a
transcript record. Used by whatever batch runner you build (Opus subagent,
direct API + Batch endpoint, etc.).

Usage as a library:

    from _scripts.validate_transcript_analysis import Validator
    v = Validator.from_workspace()  # loads people, orgs, beats, locations, schemas
    result = v.validate(llm_output, transcript_record)
    if result.ok:
        # safe to merge into the record
        merged = v.merge(transcript_record, llm_output)
    else:
        # retry the LLM with the validation errors as feedback
        retry_prompt = v.build_retry_prompt(llm_output, result.errors)

Standalone CLI for spot-checking a single output JSON:

    python _scripts/transcripts/validate_transcript_analysis.py --check path/to/llm_output.json

Returns exit code 0 (valid) or 1 (invalid). Prints errors as JSON.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]

# ----------------------------------------------------------------------
# Controlled vocabularies (closed enums — drift = error)
# ----------------------------------------------------------------------
def _load_storylines(root: Path = ROOT) -> set[str]:
    path = root / "story/storylines.json"
    if not path.exists():
        return set()
    doc = json.loads(path.read_text(encoding="utf-8"))
    return {s["id"] for s in (doc.get("storylines") or []) if s.get("id")}


STORYLINES = _load_storylines()
# Kept for backward compatibility with pre-existing records; prompt no longer
# requests `craft.shot_kind` (semantic video review handles it). Validator
# tolerates both pre-existing values from this set and absence of the key.
SHOT_KINDS = {"interview", "b-roll", "verite", "archival", "phone_capture", "drone"}
AUDIO_QUALITY = {"clean", "low_quality", "multiple_speakers"}
TONE_MOODS = {"serious", "reflective", "celebratory", "tense", "frustrated",
              "hopeful", "resigned", "playful", "grim", "analytical"}
TONE_ENERGY = {"low", "medium", "high"}
TONE_FORMALITY = {"casual", "conversational", "formal", "legal_register"}
# Simplified place-type taxonomy. Legacy types (city, town, county, region,
# state, country, trailhead, residence, infrastructure, airport) are mapped
# loosely below — they remain valid on existing records but new records emit
# from the simplified set.
PLACE_TYPES = {"political", "natural_feature", "protected_area",
               "route", "establishment", "unknown"}
LEGACY_PLACE_TYPES = {"country", "state", "region", "county", "city", "town",
                      "trailhead", "residence", "infrastructure", "airport"}

_INSERT_BEFORE = ("people_ids", "org_ids", "place_ids")


def reorder_subject_of_interview_root(d: dict) -> None:
    """Place `subject_of_interview` immediately before `people_ids` (or org_ids / place_ids)."""
    if "subject_of_interview" not in d:
        return
    val = d.pop("subject_of_interview")
    keys = list(d.keys())
    anchor = next((a for a in _INSERT_BEFORE if a in keys), None)
    if anchor is None:
        d["subject_of_interview"] = val
        return
    new: dict = {}
    for k in keys:
        if k == anchor:
            new["subject_of_interview"] = val
        new[k] = d[k]
    d.clear()
    d.update(new)


@dataclass
class ValidationResult:
    ok: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


class Validator:
    def __init__(self, people_ids: set[str], org_ids: set[str],
                 moment_ids: set[str], place_ids: set[str]):
        self.people_ids = people_ids
        self.org_ids = org_ids
        self.moment_ids = moment_ids
        self.place_ids = place_ids

    @classmethod
    def from_workspace(cls, root: Path = ROOT) -> "Validator":
        people = json.loads((root / "people/people.json").read_text(encoding="utf-8"))
        people_ids = {p["id"] for p in (people.get("people") or []) if p.get("id")}

        orgs = json.loads((root / "organizations/orgs.json").read_text(encoding="utf-8"))
        org_ids = {o["id"] for o in (orgs.get("organizations") or []) if o.get("id")}

        moments = json.loads((root / "story/moments.json").read_text(encoding="utf-8"))
        moment_ids = {b["moment_id"] for b in (moments.get("moments_outline") or []) if b.get("moment_id")}

        place_ids: set[str] = set()
        places_path = root / "places/places.json"
        if places_path.exists():
            try:
                locs = json.loads(places_path.read_text(encoding="utf-8"))
                place_ids = {pl["id"] for pl in (locs.get("places") or []) if pl.get("id")}
            except Exception:
                pass

        return cls(people_ids, org_ids, moment_ids, place_ids)

    def validate(self, llm_out: dict, transcript_rec: dict | None = None) -> ValidationResult:
        errors: list[str] = []
        warnings: list[str] = []

        # ----- ID slug validation -----
        for pid in (llm_out.get("people_ids") or []):
            if pid not in self.people_ids:
                errors.append(f"unknown people_id: {pid!r}")
        for oid in (llm_out.get("org_ids") or []):
            if oid not in self.org_ids:
                errors.append(f"unknown org_id: {oid!r}")
        for plid in (llm_out.get("place_ids") or []):
            # Place IDs may be empty registry; accept _unmatched_locations instead
            if self.place_ids and plid not in self.place_ids:
                errors.append(f"unknown place_id: {plid!r}")
        for bid in (llm_out.get("moment_ids") or []):
            if bid not in self.moment_ids:
                errors.append(f"unknown beat_id: {bid!r}")

        # ----- analysis block -----
        an = llm_out.get("analysis") or {}
        for sl in (an.get("storylines") or []):
            if sl not in STORYLINES:
                errors.append(f"unknown storyline: {sl!r}")
        in_top = "subject_of_interview" in llm_out
        in_an = isinstance(an, dict) and "subject_of_interview" in an
        if in_top and in_an and llm_out["subject_of_interview"] != an["subject_of_interview"]:
            errors.append(
                "subject_of_interview: top-level and analysis disagree — emit only at top level"
            )
        if in_top:
            soi = llm_out["subject_of_interview"]
        elif in_an:
            soi = an["subject_of_interview"]
        else:
            soi = None
        if soi:
            if soi not in self.people_ids:
                errors.append(f"unknown subject_of_interview: {soi!r}")
        tone = an.get("tone") or {}
        if tone.get("mood") and tone["mood"] not in TONE_MOODS:
            errors.append(f"unknown tone.mood: {tone['mood']!r} (allowed: {sorted(TONE_MOODS)})")
        if tone.get("energy") and tone["energy"] not in TONE_ENERGY:
            errors.append(f"unknown tone.energy: {tone['energy']!r}")
        if tone.get("formality") and tone["formality"] not in TONE_FORMALITY:
            errors.append(f"unknown tone.formality: {tone['formality']!r}")

        # ----- key_quotes / key_moments -----
        for q in (an.get("key_quotes") or []):
            if not isinstance(q, dict):
                errors.append(f"key_quotes entry not a dict: {q!r}")
                continue
            if "start_sec" not in q or "end_sec" not in q:
                errors.append(f"key_quote missing start/end timestamps: {q.get('text', '?')[:60]}")
            speaker = q.get("speaker")
            if speaker is not None and speaker not in self.people_ids:
                errors.append(f"key_quote speaker unknown: {speaker!r}")
            # speaker_label is the optional fallback for unmapped speakers
            # (e.g., "Speaker 2"); only type-check it.
            sl = q.get("speaker_label")
            if sl is not None and not isinstance(sl, str):
                errors.append(f"key_quote speaker_label not a string: {sl!r}")
        for m in (an.get("key_moments") or []):
            if not isinstance(m, dict):
                errors.append(f"key_moments entry not a dict: {m!r}")
                continue
            if "start_sec" not in m or "end_sec" not in m:
                errors.append(f"key_moment missing start/end timestamps")

        # ----- craft block -----
        craft = llm_out.get("craft") or {}
        if craft.get("shot_kind") and craft["shot_kind"] not in SHOT_KINDS:
            errors.append(f"unknown craft.shot_kind: {craft['shot_kind']!r}")
        if "framing" in craft:
            errors.append("craft.framing is deprecated — omit the key (use proxy / semantic review for composition).")
        if craft.get("audio_quality") and craft["audio_quality"] not in AUDIO_QUALITY:
            errors.append(f"unknown craft.audio_quality: {craft['audio_quality']!r}")
        if "usability" in craft:
            errors.append("craft.usability is removed — omit the key.")
        if "circle_take" in craft:
            errors.append("craft.circle_take is removed — omit the key.")

        # ----- relations: removed from transcript schema; warn if model emitted -----
        if "relations" in llm_out:
            warnings.append(
                "top-level `relations` is removed from transcripts — omit it."
            )
        an_pre = llm_out.get("analysis")
        if isinstance(an_pre, dict) and "relations" in an_pre:
            warnings.append(
                "`analysis.relations` is invalid — omit it (relations are not stored on transcripts)."
            )

        # ----- _unmatched_places format -----
        # Preferred shape: list[str] of canonical names.
        # Legacy object-shape entries {"name", "type", "context"} are tolerated
        # as warnings so re-validation of older runs doesn't fail.
        for loc in (llm_out.get("_unmatched_places") or []):
            if isinstance(loc, str):
                continue
            if isinstance(loc, dict):
                name = loc.get("name")
                if not name:
                    errors.append("_unmatched_places entry missing 'name'")
                t = loc.get("type")
                if t and t not in PLACE_TYPES and t not in LEGACY_PLACE_TYPES:
                    warnings.append(f"_unmatched_places entry has unknown type: {t!r}")
                warnings.append(
                    "_unmatched_places legacy object shape detected — "
                    "prefer a string list of names"
                )
                continue
            errors.append("_unmatched_places entry not a string (or legacy dict)")

        # Back-compat: tolerate legacy `_unmatched_locations` in LLM output.
        for loc in (llm_out.get("_unmatched_locations") or []):
            if isinstance(loc, str):
                warnings.append(
                    f"_unmatched_locations legacy field is deprecated; use _unmatched_places instead: {loc[:60]!r}"
                )
            elif isinstance(loc, dict):
                if not loc.get("name"):
                    errors.append("_unmatched_locations entry missing 'name' (deprecated field)")
            else:
                errors.append("_unmatched_locations entry not a dict or string (deprecated field)")

        # Back-compat: tolerate legacy `_proposed_places` in LLM output
        # (treat it as if it were `_unmatched_places`).
        for pp in (llm_out.get("_proposed_places") or []):
            if isinstance(pp, str):
                warnings.append(
                    f"_proposed_places legacy field is deprecated; use _unmatched_places instead: {pp[:60]!r}"
                )
            elif isinstance(pp, dict):
                if not pp.get("name"):
                    errors.append("_proposed_places entry missing 'name' (deprecated field)")
            else:
                errors.append("_proposed_places entry not a dict or string (deprecated field)")

        # ----- _unmatched_people / _unmatched_orgs format -----
        # New object shape {"name", "context"}. Legacy string entries on
        # previously-analyzed records are tolerated as warnings.
        for field_name in ("_unmatched_people", "_unmatched_orgs"):
            for entry in (llm_out.get(field_name) or []):
                if isinstance(entry, str):
                    warnings.append(
                        f"{field_name} legacy string shape: {entry[:60]!r} — "
                        "regenerate with object form {name, context?}"
                    )
                    continue
                if not isinstance(entry, dict):
                    errors.append(f"{field_name} entry not a dict or string")
                    continue
                if not entry.get("name"):
                    errors.append(f"{field_name} entry missing 'name'")

        # ----- Theme/topic format checks (warn-only, soft vocabulary) -----
        for t in (an.get("themes") or []):
            if not isinstance(t, str) or " " in t or t != t.lower():
                warnings.append(f"theme should be snake_case lowercase: {t!r}")
        for t in (an.get("topics") or []):
            if not isinstance(t, str) or " " in t or t != t.lower():
                warnings.append(f"topic should be snake_case lowercase: {t!r}")

        return ValidationResult(ok=(len(errors) == 0), errors=errors, warnings=warnings)

    def build_retry_prompt(self, llm_out: dict, errors: list[str]) -> str:
        """Render a feedback string the runner can append to a retry call."""
        lines = ["Your previous output had validation errors. Fix them and return a corrected JSON object."]
        lines.append("")
        for e in errors:
            lines.append(f"- {e}")
        lines.append("")
        lines.append("Use only IDs and enum values listed in the prompt context.")
        lines.append("If you encountered an entity not in the registry, add to _unmatched_people / _unmatched_orgs / _unmatched_places (don't invent slugs).")
        return "\n".join(lines)

    def merge(self, transcript_rec: dict, llm_out: dict) -> dict:
        """Safely merge validated LLM output into a transcript record (returns new dict).

        `relations` is not part of the transcript schema; any legacy field is dropped.
        """
        merged = dict(transcript_rec)
        for top_field in ("people_ids", "org_ids", "place_ids", "moment_ids"):
            if top_field in llm_out:
                merged[top_field] = sorted(set((merged.get(top_field) or []) + (llm_out.get(top_field) or [])))
        for block in ("analysis", "craft"):
            if block in llm_out:
                if block in merged and isinstance(merged[block], dict):
                    merged[block] = {**merged[block], **llm_out[block]}
                else:
                    merged[block] = llm_out[block]
        an_merged = merged.get("analysis")
        if isinstance(an_merged, dict):
            an_merged.pop("relations", None)
            if "subject_of_interview" in an_merged:
                merged["subject_of_interview"] = an_merged.pop("subject_of_interview")
        if "subject_of_interview" in llm_out:
            merged["subject_of_interview"] = llm_out["subject_of_interview"]
        reorder_subject_of_interview_root(merged)
        for field_name in ("_unmatched_people", "_unmatched_orgs", "_unmatched_places"):
            if field_name in llm_out:
                merged[field_name] = llm_out[field_name]

        # Back-compat: if the model emitted legacy `_unmatched_locations`, map it.
        if "_unmatched_locations" in llm_out and "_unmatched_places" not in llm_out:
            mapped: list[str] = []
            for entry in (llm_out.get("_unmatched_locations") or []):
                if isinstance(entry, str) and entry.strip():
                    mapped.append(entry.strip())
                elif isinstance(entry, dict):
                    name = entry.get("name")
                    if isinstance(name, str) and name.strip():
                        mapped.append(name.strip())
            merged["_unmatched_places"] = mapped

        # Back-compat: if the model emitted legacy `_proposed_places`, map it too.
        if "_proposed_places" in llm_out and "_unmatched_places" not in llm_out:
            mapped: list[str] = []
            for entry in (llm_out.get("_proposed_places") or []):
                if isinstance(entry, str) and entry.strip():
                    mapped.append(entry.strip())
                elif isinstance(entry, dict):
                    name = entry.get("name")
                    if isinstance(name, str) and name.strip():
                        mapped.append(name.strip())
            merged["_unmatched_places"] = mapped
        merged.pop("relations", None)
        return merged


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", required=True, help="Path to LLM output JSON to validate")
    args = ap.parse_args()

    out = json.loads(Path(args.check).read_text(encoding="utf-8"))
    v = Validator.from_workspace()
    r = v.validate(out)
    print(json.dumps({"ok": r.ok, "errors": r.errors, "warnings": r.warnings}, indent=2))
    return 0 if r.ok else 1


if __name__ == "__main__":
    sys.exit(main())
