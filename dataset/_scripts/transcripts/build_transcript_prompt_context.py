"""
Generate _prompts/transcript_analysis_prompt.md — controlled-vocabulary
context document the LLM loads at start of every transcript-analysis batch.

Pulls live data from:
- story/moments.json (moment IDs + summaries + themes)
- people/people.json (top-N by mention count + roles)
- organizations/orgs.json (canonical names + types, mention_count >= 2)
- places/places.json (high-confidence places + disambiguation rules)
- documents/press/articles/*.json (topics vocabulary seen in the wild)

Run after any registry changes so the prompt stays current.

Also exposes `canonical_prompt_sha(text)` — the runner uses this instead of a
raw SHA of the file so that timestamp-only regenerations don't churn the
prompt SHA and force corpus-wide re-analysis.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PROMPTS_DIR = ROOT / "_prompts"
PROMPTS_DIR.mkdir(exist_ok=True)
OUT = PROMPTS_DIR / "transcript_analysis_prompt.md"

# Lines matching this pattern are stripped before SHA computation so that
# regenerating the prompt with no underlying registry changes does not bump
# the SHA and trigger a corpus-wide re-analysis.
_VOLATILE_LINE_RE = re.compile(r"^_Generated\s.+_$")


def canonical_prompt_sha(text: str) -> str:
    """SHA256 over the prompt body excluding volatile lines (e.g. the
    generated-at timestamp). Stable across regenerations that don't change
    any of the underlying registry content."""
    lines = [ln for ln in text.splitlines() if not _VOLATILE_LINE_RE.match(ln)]
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def atomic_write(path: Path, body: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(body, encoding="utf-8")
    os.replace(tmp, path)


# ----------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------
def main() -> int:
    moments_path = ROOT / "story/moments.json"  # your story-spine registry (optional)
    moments = json.loads(moments_path.read_text(encoding="utf-8")) if moments_path.exists() else {}
    people = json.loads((ROOT / "people/people.json").read_text(encoding="utf-8"))
    orgs = json.loads((ROOT / "organizations/orgs.json").read_text(encoding="utf-8"))

    places_doc: dict = {}
    places_path = ROOT / "places/places.json"
    if places_path.exists():
        try:
            places_doc = json.loads(places_path.read_text(encoding="utf-8"))
        except Exception:
            places_doc = {}

    # Top-N people / orgs / places by mention count. Tight selection — the
    # model gets the most-mentioned slugs verbatim; rare entities go through
    # _unmatched_* and are merged in a post-pass.
    PEOPLE_TOP_N = 50
    ORGS_TOP_N = 50
    PLACES_TOP_N = 30

    people_list = people.get("people") or []
    def people_score(p):
        return p.get("mention_count", 0) or len(p.get("sources") or [])
    top_people = sorted(people_list, key=people_score, reverse=True)[:PEOPLE_TOP_N]

    org_list = sorted(orgs.get("organizations") or [], key=lambda o: -o.get("mention_count", 0))
    top_orgs = org_list[:ORGS_TOP_N]

    # Top-30 places by mention count (any confidence). Long tail handled via
    # _unmatched_places[] and the post-pass merge. Disambiguation rules are
    # intentionally NOT surfaced — the model resolves these inline from
    # context, with the post-pass cleaning up edge cases.
    place_list = places_doc.get("places") or []
    candidate_places = [
        p for p in place_list
        if p.get("id") and p.get("canonical_name")
    ]
    candidate_places.sort(key=lambda p: -int(p.get("mention_count") or 0))
    high_places = candidate_places[:PLACES_TOP_N]

    # Theme + topic vocabularies from beats + press articles
    theme_counter: Counter = Counter()
    for b in moments.get("moments_outline") or []:
        for t in b.get("themes") or []:
            theme_counter[t] += 1

    topic_counter: Counter = Counter()
    article_dir = ROOT / "documents/press/articles"
    if article_dir.exists():
        for p in article_dir.glob("*.json"):
            try:
                a = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                continue
            for t in (a.get("analysis") or {}).get("topics") or []:
                topic_counter[t] += 1

    # Build the markdown
    now = datetime.now(timezone.utc).isoformat()
    lines: list[str] = []
    lines.append("# Transcript Analysis — Controlled-Vocabulary Prompt Context\n")
    lines.append(
        f"_Generated {now} from current registries. Regenerate via_ `_scripts/transcripts/build_transcript_prompt_context.py` _whenever moments/people/orgs/places change._\n\n"
    )

    # ---- How to use ----
    lines.append("## How to use this in your prompt\n\n")
    lines.append(
        "Match against the controlled vocabularies below. The runner enforces strict JSON output (`response_mime_type: application/json`); return the schema object only.\n\n"
        "- `people_ids[]`: only `p_*` slugs. Copy-paste verbatim. Unknown people → `_unmatched_people[]`.\n"
        "- `org_ids[]`: only `o_*` slugs. Use the EXACT slug (e.g. `o_doj`, not `o_department_of_justice`). Unknown orgs → `_unmatched_orgs[]`.\n"
        "- `place_ids[]`: only `pl_*` slugs. Unknown places → `_unmatched_places[]`. Some entities (courthouses, agencies tied to a building) live in BOTH `org_ids[]` and `place_ids[]`; prefer `org_ids[]` for institutional context (rulings, jurisdiction), `place_ids[]` for physical-location context (filming there, going to the building).\n"
        "- `moment_ids[]`: only `mom_*` IDs. Include only moments with relevance >= 0.6.\n"
        "- `themes[]`: prefer the existing vocabulary; new themes must be snake_case and project-specific.\n"
        "- `topics[]`: prefer the existing vocabulary; new topics OK.\n"
        "- `storylines[]`: closed whitelist from `story/storylines.json` only. Concepts like `overcriminalization`, `first_amendment`, `permit_regime` are TOPICS.\n"
        "- `craft.audio_quality`: one of `clean | low_quality | multiple_speakers`.\n"
        "- `tone.mood`: one of `serious | reflective | celebratory | tense | frustrated | hopeful | resigned | playful | grim | analytical`. For utility/non-emotional clips, default to `analytical` or `reflective`.\n"
        "- `tone.energy`: one of `low | medium | high`.\n"
        "- `tone.formality`: one of `casual | conversational | formal | legal_register`.\n\n"
        "**Pre-resolved speakers:** if `speakers_raw` already maps Whisper guids to a person slug or canonical name, an earlier human-transcript-resolution pass populated it — trust those mappings as authoritative. Use them directly to populate `people_ids[]` and `key_quotes[].speaker`; do not override or re-derive.\n\n"
        "**Optional semantic context:** the runner may include a `_context` object on the input record with:\n"
        "- `asset_semantic_summary`: a compact, machine-generated summary of what’s happening in the clip (from catalog `asset_semantic_summary` on the sibling video/still JSON, or `editorial_catalog.asset_semantic_chunk`).\n"
        "- `linked_assets_semantic_summaries[]`: the same type of summaries for a few linked co-recordings (A-cam/B-cam, dual audio, audio↔video).\n"
        "Use this semantic context to improve disambiguation (who/where/what’s on camera) and to write better `analysis.summary_*` — but do **not** copy it verbatim as if it were transcript text. If semantic context conflicts with transcript text, prefer transcript text for anything spoken; use semantic context for visuals/camera/action/setting.\n\n"
    )

    # ---- Beats ----
    lines.append("## Story moments (target for `moment_ids[]` scoring)\n\n")
    lines.append("| moment_id | act | title | summary |\n|---|---|---|---|\n")
    for b in moments.get("moments_outline") or []:
        lines.append(f"| `{b.get('moment_id')}` | {b.get('act')} | **{b.get('title')}** | {b.get('summary_one_line')} |\n")
    lines.append("\n")

    # ---- Storylines ----
    storylines_path = ROOT / "story/storylines.json"
    storylines_doc = json.loads(storylines_path.read_text(encoding="utf-8")) if storylines_path.exists() else {}
    storyline_rows = storylines_doc.get("storylines") or []
    sl_ids = [s.get("id") for s in storyline_rows if s.get("id")]
    sl_whitelist = ", ".join(f"`{sid}`" for sid in sl_ids) if sl_ids else "`<your storyline ids>`"
    lines.append("## Your film storylines whitelist\n\n")
    lines.append(
        f"Define these in `story/storylines.json` — the sample ships four. "
        f"Closed whitelist: {sl_whitelist}.\n\n"
    )
    for s in storyline_rows:
        sid = s.get("id") or "?"
        desc = s.get("description") or "<description>"
        lines.append(f"- `{sid}` — {desc}\n")
    lines.append("\n")

    # ---- People ----
    lines.append(f"## People registry — top {len(top_people)} by mention count\n\n")
    lines.append(
        f"Use these slugs verbatim in `people_ids[]`. The full registry "
        f"({len(people_list)} people) is at `people/people.json`; for anyone not in this list, "
        "add the canonical name to `_unmatched_people[]`.\n\n"
    )
    for p in top_people:
        lines.append(f"- `{p.get('id')}` — {p.get('canonical_name')}\n")
    lines.append("\n")

    # ---- Orgs ----
    lines.append(f"## Organizations registry — top {len(top_orgs)} by mention count\n\n")
    lines.append(
        f"Use these slugs verbatim in `org_ids[]`. The full registry "
        f"({len(org_list)} organizations) is at `organizations/orgs.json`; "
        "for anything not in this list, add to `_unmatched_orgs[]`.\n\n"
    )
    for o in top_orgs:
        lines.append(f"- `{o.get('id')}` — {o.get('canonical_name')}\n")
    lines.append("\n")

    # ---- Locations ----
    lines.append(f"## Places registry — top {len(high_places)} by mention count\n\n")
    lines.append(
        f"Use these slugs verbatim in `place_ids[]`. The full registry "
        f"({len(place_list)} places) is at `places/places.json`; "
        "for places not in this list, add the canonical name to `_unmatched_places[]`.\n\n"
    )
    for p in high_places:
        lines.append(f"- `{p.get('id')}` — {p.get('canonical_name')}\n")
    lines.append("\n")

    # ---- Themes ----
    if theme_counter:
        lines.append(f"## Theme vocabulary — {len(theme_counter)} themes from moments\n\n")
        lines.append(
            "Prefer reusing these. New themes allowed but must be snake_case, project-specific "
            "(e.g., `father_son_dynamic`, not `family_dynamics`), and evocative enough that an "
            "editor would actually filter on them.\n\n"
        )
        for t, _c in theme_counter.most_common():
            lines.append(f"- `{t}`\n")
        lines.append("\n")

    # ---- Topics ----
    if topic_counter:
        lines.append(f"## Topic vocabulary — top {min(50, len(topic_counter))} from press articles\n\n")
        lines.append("Use these for `topics[]`. New topics allowed if needed; prefer existing.\n\n")
        for t, c in topic_counter.most_common(50):
            lines.append(f"- `{t}` ({c}x)\n")
        lines.append("\n")

    # ---- Output schema ----
    lines.append("## Output schema (transcript record)\n\n")
    lines.append(
        "Add/update these blocks on the transcript record. Don't modify `segments`, `full_text`, "
        "`manifest`, `speakers`, `speakers_raw`, `asset_id`, `schema_version`. Don't output "
        "`craft.shot_kind`, `craft.framing`, `craft.usability`, or `craft.circle_take` — "
        "shot kind comes from semantic video review. Do not output a `relations` object "
        "(removed from the transcript schema). Put `subject_of_interview` at the **top level** "
        "(immediately before `people_ids`), not inside `analysis`.\n\n"
        "```json\n"
        "{\n"
        '  "subject_of_interview": "p_* | null",\n'
        '  "people_ids": ["p_*", ...],\n'
        '  "org_ids": ["o_*", ...],\n'
        '  "place_ids": ["pl_*", ...],\n'
        '  "moment_ids": ["mom_001", ...],\n'
        '  "analysis": {\n'
        '    "summary_one_line": "...",\n'
        '    "summary_paragraph": "...",\n'
        '    "topics": ["..."],\n'
        '    "themes": ["..."],\n'
        '    "tone": {"mood": "...", "energy": "...", "formality": "..."},\n'
        '    "key_quotes": [{\n'
        '      "start_sec": float, "end_sec": float,\n'
        '      "speaker": "p_* | null",\n'
        '      "speaker_label": "Speaker 2 | null",   // fallback when speaker is unmapped\n'
        '      "text": "...", "why": "..."\n'
        "    }],\n"
        '    "key_moments": [{"start_sec": float, "end_sec": float, "description": "..."}],\n'
        '    "storylines": ["..."]\n'
        "  },\n"
        '  "craft": {\n'
        '    "audio_quality": "clean|low_quality|multiple_speakers"\n'
        "  },\n"
        '  "_unmatched_people": [{"name": "...", "context": "..."}],\n'
        '  "_unmatched_orgs":   [{"name": "...", "context": "..."}],\n'
        '  "_unmatched_places": ["..."]\n'
        "}\n"
        "```\n\n"
        "The runner stamps `analysis.analyzed_at`, `analysis.prompt_sha256`, and "
        "`analysis.analyzer` at commit time — don't populate them; any value provided "
        "will be overwritten.\n\n"
    )

    # ---- Confidence rules ----
    lines.append("## Confidence rules\n\n")
    lines.append(
        "- For `key_quotes`: only include quotes an editor might actually pull. Don't dump the whole transcript.\n"
        "- For `key_quotes[].speaker`: set the `p_*` slug only when you can confidently map a "
        "Whisper `speaker_raw` guid to a person via context. When you can't, leave `speaker: null` "
        "and set `speaker_label` to the human-readable label from `speakers_raw` (e.g., `\"Speaker 2\"`); "
        "a downstream pass resolves these.\n"
        "- For `subject_of_interview`: only set when this is clearly an interview (one primary subject doing most of the talking, asked questions). For verite footage with mixed speakers, leave `null`.\n"
        "- For `moment_ids`: only include moments with relevance >= 0.6. Don't over-tag.\n"
        "- For `_unmatched_places`: ONLY include places explicitly named in the transcript text or in `path_metadata` (shoot_label, scene_name, sub_location, etc.). Do NOT infer probable venues from external knowledge — e.g., don't add \"Big Sky Resort\" just because The Rut race usually happens there.\n"
        "- For `_unmatched_people` / `_unmatched_orgs`: each entry is an object. `name` is required; `context` should be one short clause that helps a later merge pass disambiguate (e.g., `\"interviewee says 'we drove to Helen's place' — Helen Johnson, Connor's girlfriend\"`).\n"
        "- For sparse utility clips (slates, camera checks, off-camera cues with no story content), default `tone.mood` to `analytical` rather than `playful`.\n"
    )

    body = "".join(lines)
    atomic_write(OUT, body)
    sha = canonical_prompt_sha(body)
    print(f"Wrote {OUT.relative_to(ROOT)}: {len(body):,} bytes")
    print(f"  canonical SHA: {sha}")
    print(f"  moments:         {len(moments.get('moments_outline') or [])}")
    print(f"  people (top):    {len(top_people)} of {len(people_list)}")
    print(f"  orgs (top):      {len(top_orgs)} of {len(org_list)}")
    print(f"  places (top):    {len(high_places)} of {len(place_list)}")
    print(f"  themes:          {len(theme_counter)}")
    print(f"  topics:          {len(topic_counter)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
