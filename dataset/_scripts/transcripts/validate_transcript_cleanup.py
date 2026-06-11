"""
Validate cleanup-pass LLM output before merging into a transcript record.

Checks:
  1. Substring presence — `original` must appear verbatim in the named field at
     (or overlapping) the claimed timestamp range. Per-span check.
  2. Slug resolution — `people_id` / `org_id` / `place_id` must resolve to the
     workspace registries; no inventing canonical names.
  3. Enum closure — `type` and `confidence` come from closed lists.
  4. Confidence routing — split `high` from `medium`/`low`. The runner uses this
     to route corrections vs `_correction_candidates`.

Usage as a library:

    from validate_transcript_cleanup import CleanupValidator
    cv = CleanupValidator.from_workspace()
    result = cv.validate(llm_out, transcript_record)
    if result.ok:
        merged = cv.merge(transcript_record, llm_out)

Standalone CLI:

    python _scripts/transcripts/validate_transcript_cleanup.py --check path/to/llm_output.json --transcript path/to/transcript.json
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

CORRECTION_TYPES = {"name_substitution", "term_substitution", "missing_word", "hallucinated_word"}
CONFIDENCE_LEVELS = {"high", "medium", "low"}

_SEG_RE = re.compile(r"^segments\[(\d+)\]\.text$")


@dataclass
class CleanupValidationResult:
    ok: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    high_confidence: list[dict] = field(default_factory=list)   # routed to corrections[]
    candidates: list[dict] = field(default_factory=list)         # routed to _correction_candidates[]


class CleanupValidator:
    def __init__(self, people_ids: set[str], org_ids: set[str], place_ids: set[str]):
        self.people_ids = people_ids
        self.org_ids = org_ids
        self.place_ids = place_ids

    @classmethod
    def from_workspace(cls, root: Path = ROOT) -> "CleanupValidator":
        people = json.loads((root / "people/people.json").read_text(encoding="utf-8"))
        people_ids = {p["id"] for p in (people.get("people") or []) if p.get("id")}

        orgs = json.loads((root / "organizations/orgs.json").read_text(encoding="utf-8"))
        org_ids = {o["id"] for o in (orgs.get("organizations") or []) if o.get("id")}

        place_ids: set[str] = set()
        loc_path = root / "places/places.json"
        if loc_path.exists():
            try:
                locs = json.loads(loc_path.read_text(encoding="utf-8"))
                place_ids = {pl["id"] for pl in (locs.get("places") or []) if pl.get("id")}
            except Exception:
                pass

        return cls(people_ids, org_ids, place_ids)

    def _check_span(self, span: dict, original: str, transcript: dict) -> str | None:
        """Return None if the span is valid, otherwise an error string."""
        if not isinstance(span, dict):
            return f"span is not a dict: {span!r}"
        field_name = span.get("field")
        if not field_name:
            return "span missing 'field'"
        if field_name == "full_text":
            text = transcript.get("full_text") or ""
            if original not in text:
                return f"'original' not found in full_text: {original!r}"
            return None
        m = _SEG_RE.match(field_name)
        if m:
            idx = int(m.group(1))
            segments = transcript.get("segments") or []
            if idx >= len(segments) or not isinstance(segments[idx], dict):
                return f"span field references missing segment index {idx}"
            seg_text = segments[idx].get("text") or ""
            if original not in seg_text:
                return f"'original' not found in segments[{idx}].text: {original!r}"
            # Optional timestamp consistency check
            ss, es = span.get("start_sec"), span.get("end_sec")
            if ss is not None and es is not None:
                seg_ss, seg_es = segments[idx].get("start_sec"), segments[idx].get("end_sec")
                if seg_ss is not None and seg_es is not None:
                    if not (seg_ss - 0.5 <= ss and es <= seg_es + 0.5):
                        return (f"span timestamps {ss}-{es} don't overlap segments[{idx}] "
                                f"actual {seg_ss}-{seg_es}")
            return None
        return f"unrecognized span field: {field_name!r}"

    def validate(self, llm_out: dict, transcript: dict) -> CleanupValidationResult:
        errors: list[str] = []
        warnings: list[str] = []
        high_conf: list[dict] = []
        candidates: list[dict] = []

        corrections = llm_out.get("corrections")
        if corrections is None:
            errors.append("output missing 'corrections' key")
            return CleanupValidationResult(ok=False, errors=errors)
        if not isinstance(corrections, list):
            errors.append(f"'corrections' is not a list: {type(corrections).__name__}")
            return CleanupValidationResult(ok=False, errors=errors)

        for i, c in enumerate(corrections):
            if not isinstance(c, dict):
                errors.append(f"corrections[{i}] is not a dict")
                continue

            # Type check
            ctype = c.get("type")
            if ctype not in CORRECTION_TYPES:
                errors.append(f"corrections[{i}].type unknown: {ctype!r}")
                continue

            # Confidence check
            conf = c.get("confidence")
            if conf not in CONFIDENCE_LEVELS:
                errors.append(f"corrections[{i}].confidence unknown: {conf!r}")
                continue

            # Original/corrected presence
            original = c.get("original")
            corrected = c.get("corrected")
            if not isinstance(original, str) or not original:
                errors.append(f"corrections[{i}].original missing or empty")
                continue
            if not isinstance(corrected, str) or not corrected:
                errors.append(f"corrections[{i}].corrected missing or empty")
                continue

            # Slug resolution
            pid = c.get("people_id")
            oid = c.get("org_id")
            plid = c.get("place_id")
            if pid is not None and pid not in self.people_ids:
                errors.append(f"corrections[{i}].people_id unknown: {pid!r}")
                continue
            if oid is not None and oid not in self.org_ids:
                errors.append(f"corrections[{i}].org_id unknown: {oid!r}")
                continue
            if plid is not None and self.place_ids and plid not in self.place_ids:
                errors.append(f"corrections[{i}].place_id unknown: {plid!r}")
                continue
            if ctype == "name_substitution" and pid is None:
                errors.append(f"corrections[{i}] type=name_substitution but people_id is null")
                continue

            # Span check
            spans = c.get("spans") or []
            if not spans:
                errors.append(f"corrections[{i}].spans missing or empty")
                continue
            span_errors: list[str] = []
            for s in spans:
                err = self._check_span(s, original, transcript)
                if err:
                    span_errors.append(err)
            if span_errors:
                errors.append(f"corrections[{i}] span errors: {span_errors}")
                continue

            # Route by confidence
            if conf == "high":
                high_conf.append(c)
            else:
                candidates.append(c)

        return CleanupValidationResult(
            ok=(len(errors) == 0),
            errors=errors,
            warnings=warnings,
            high_confidence=high_conf,
            candidates=candidates,
        )

    def build_retry_prompt(self, llm_out: dict, errors: list[str]) -> str:
        lines = ["Your previous corrections output had validation errors. Fix and re-emit."]
        lines.append("")
        for e in errors:
            lines.append(f"- {e}")
        lines.append("")
        lines.append("Reminders: `original` must appear verbatim in the named field. "
                     "`people_id`/`org_id`/`place_id` must resolve to existing registry slugs. "
                     "Don't invent slugs — leave unmatched names for human review.")
        return "\n".join(lines)

    def merge(self, transcript: dict, validated: CleanupValidationResult,
              *, applied_at: str, model_str: str) -> dict:
        """Merge validated corrections into a transcript record (returns new dict).

        Stamps each correction with applied_at + model. Routes confidence-high
        to `corrections[]` and medium/low to `_correction_candidates[]`.
        Idempotent: existing entries are not duplicated (matched on
        (type, original, people_id|org_id|place_id, span list)).
        """
        merged = dict(transcript)
        existing = list(merged.get("corrections") or [])
        existing_cands = list(merged.get("_correction_candidates") or [])

        def stamp(c: dict) -> dict:
            out = dict(c)
            out["applied_at"] = applied_at
            out["model"] = model_str
            return out

        def fingerprint(c: dict) -> tuple:
            return (
                c.get("type"),
                c.get("original"),
                c.get("people_id"),
                c.get("org_id"),
                c.get("place_id"),
                tuple(sorted((s.get("field"), s.get("start_sec"), s.get("end_sec"))
                             for s in (c.get("spans") or []))),
            )

        existing_fps = {fingerprint(c) for c in existing}
        existing_cand_fps = {fingerprint(c) for c in existing_cands}

        for c in validated.high_confidence:
            if fingerprint(c) not in existing_fps:
                existing.append(stamp(c))
        for c in validated.candidates:
            if fingerprint(c) not in existing_cand_fps:
                existing_cands.append(stamp(c))

        merged["corrections"] = existing
        merged["_correction_candidates"] = existing_cands
        return merged


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", required=True, help="Path to LLM output JSON")
    ap.add_argument("--transcript", required=True, help="Path to transcript record JSON")
    args = ap.parse_args()

    out = json.loads(Path(args.check).read_text(encoding="utf-8"))
    transcript = json.loads(Path(args.transcript).read_text(encoding="utf-8"))
    cv = CleanupValidator.from_workspace()
    r = cv.validate(out, transcript)
    print(json.dumps({
        "ok": r.ok,
        "errors": r.errors,
        "warnings": r.warnings,
        "high_confidence_count": len(r.high_confidence),
        "candidate_count": len(r.candidates),
    }, indent=2))
    return 0 if r.ok else 1


if __name__ == "__main__":
    sys.exit(main())
