# Transcript Analysis: Controlled-Vocabulary Prompt Context
_Generated 2026-06-11T01:25:02.820321+00:00 from current registries. Regenerate via_ `_scripts/transcripts/build_transcript_prompt_context.py` _whenever moments/people/orgs/places change._

## How to use this in your prompt

Match against the controlled vocabularies below. The runner enforces strict JSON output (`response_mime_type: application/json`); return the schema object only.

- `people_ids[]`: only `p_*` slugs. Copy-paste verbatim. Unknown people → `_unmatched_people[]`.
- `org_ids[]`: only `o_*` slugs. Use the EXACT slug (e.g. `o_doj`, not `o_department_of_justice`). Unknown orgs → `_unmatched_orgs[]`.
- `place_ids[]`: only `pl_*` slugs. Unknown places → `_unmatched_places[]`. Some entities (courthouses, agencies tied to a building) live in BOTH `org_ids[]` and `place_ids[]`; prefer `org_ids[]` for institutional context (rulings, jurisdiction), `place_ids[]` for physical-location context (filming there, going to the building).
- `moment_ids[]`: only `mom_*` IDs. Include only moments with relevance >= 0.6.
- `themes[]`: prefer the existing vocabulary; new themes must be snake_case and project-specific.
- `topics[]`: prefer the existing vocabulary; new topics OK.
- `storylines[]`: closed whitelist from `story/storylines.json` only. Concepts like `overcriminalization`, `first_amendment`, `permit_regime` are TOPICS.
- `craft.audio_quality`: one of `clean | low_quality | multiple_speakers`.
- `tone.mood`: one of `serious | reflective | celebratory | tense | frustrated | hopeful | resigned | playful | grim | analytical`. For utility/non-emotional clips, default to `analytical` or `reflective`.
- `tone.energy`: one of `low | medium | high`.
- `tone.formality`: one of `casual | conversational | formal | legal_register`.

**Pre-resolved speakers:** if `speakers_raw` already maps Whisper guids to a person slug or canonical name, an earlier human-transcript-resolution pass populated it; trust those mappings as authoritative. Use them directly to populate `people_ids[]` and `key_quotes[].speaker`; do not override or re-derive.

**Optional semantic context:** the runner may include a `_context` object on the input record with:
- `asset_semantic_summary`: a compact, machine-generated summary of what’s happening in the clip (from catalog `asset_semantic_summary` on the sibling video/still JSON, or `editorial_catalog.asset_semantic_chunk`).
- `linked_assets_semantic_summaries[]`: the same type of summaries for a few linked co-recordings (A-cam/B-cam, dual audio, audio↔video).
Use this semantic context to improve disambiguation (who/where/what’s on camera) and to write better `analysis.summary_*`, but do **not** copy it verbatim as if it were transcript text. If semantic context conflicts with transcript text, prefer transcript text for anything spoken; use semantic context for visuals/camera/action/setting.

## Story moments (target for `moment_ids[]` scoring)

| moment_id | act | title | summary |
|---|---|---|---|
| `mom_006` | 1 | **Labor Day Record** | Michelino runs 2:50:50; new Grand Teton FKT. |
| `mom_007` | 1 | **Controversy Erupts** | Old Climber's Trail use questioned; FKT.com rejects record; press hit-pieces mount. |

## Your film storylines whitelist

Define these in `story/storylines.json`; the sample ships four. Closed whitelist: `lawsuit`, `ms_fkt`, `pardon`, `trial`.

- `lawsuit`: USA v. Sunseri criminal case (charging, defense, motions, trial, appeals, pardon)
- `ms_fkt`: Michelino Sunseri's athletic career and competitive running profile, including the 2024 Grand Teton FKT, training, other races, and any footage where he is the central athletic subject
- `pardon`: November 2025 presidential pardon and aftermath
- `trial`: May 2025 trial proceedings specifically (subset of lawsuit)

## People registry: top 3 by mention count

Use these slugs verbatim in `people_ids[]`. The full registry (3 people) is at `people/people.json`; for anyone not in this list, add the canonical name to `_unmatched_people[]`.

- `p_alex_rienzie`: Alex Rienzie
- `p_connor_burkesmith`: Connor Burkesmith
- `p_michelino_sunseri`: Michelino Sunseri

## Organizations registry: top 1 by mention count

Use these slugs verbatim in `org_ids[]`. The full registry (1 organizations) is at `organizations/orgs.json`; for anything not in this list, add to `_unmatched_orgs[]`.

- `o_the_north_face`: The North Face

## Places registry: top 10 by mention count

Use these slugs verbatim in `place_ids[]`. The full registry (10 places) is at `places/places.json`; for places not in this list, add the canonical name to `_unmatched_places[]`.

- `pl_grand_teton`: Grand Teton
- `pl_jackson_wy`: Jackson, Wyoming
- `pl_wyoming`: Wyoming
- `pl_grand_teton_national_park`: Grand Teton National Park
- `pl_lupine_meadows`: Lupine Meadows
- `pl_garnet_canyon`: Garnet Canyon
- `pl_teton_range`: Teton Range
- `pl_owen_spalding_route`: Owen Spalding Route
- `pl_old_climber_s_trail`: Old Climber's Trail
- `pl_united_states`: United States

## Theme vocabulary: 6 themes from moments

Prefer reusing these. New themes allowed but must be snake_case, project-specific (e.g., `father_son_dynamic`, not `family_dynamics`), and evocative enough that an editor would actually filter on them.

- `alpine_risk`
- `athletic_peak_performance`
- `twelve_year_record_falls`
- `public_shaming_cycle`
- `trail_ethics_debate`
- `anonymous_tip_origin`

## Topic vocabulary: top 4 from press articles

Use these for `topics[]`. New topics allowed if needed; prefer existing.

- `switchback_cutting` (1x)
- `trail_erosion` (1x)
- `outdoor_ethics` (1x)
- `fastestknowntime` (1x)

## Output schema (transcript record)

Add/update these blocks on the transcript record. Don't modify `segments`, `full_text`, `manifest`, `speakers`, `speakers_raw`, `asset_id`, `schema_version`. Don't output `craft.shot_kind`, `craft.framing`, `craft.usability`, or `craft.circle_take`; shot kind comes from semantic video review. Do not output a `relations` object (removed from the transcript schema). Put `subject_of_interview` at the **top level** (immediately before `people_ids`), not inside `analysis`.

```json
{
  "subject_of_interview": "p_* | null",
  "people_ids": ["p_*", ...],
  "org_ids": ["o_*", ...],
  "place_ids": ["pl_*", ...],
  "moment_ids": ["mom_001", ...],
  "analysis": {
    "summary_one_line": "...",
    "summary_paragraph": "...",
    "topics": ["..."],
    "themes": ["..."],
    "tone": {"mood": "...", "energy": "...", "formality": "..."},
    "key_quotes": [{
      "start_sec": float, "end_sec": float,
      "speaker": "p_* | null",
      "speaker_label": "Speaker 2 | null",   // fallback when speaker is unmapped
      "text": "...", "why": "..."
    }],
    "key_moments": [{"start_sec": float, "end_sec": float, "description": "..."}],
    "storylines": ["..."]
  },
  "craft": {
    "audio_quality": "clean|low_quality|multiple_speakers"
  },
  "_unmatched_people": [{"name": "...", "context": "..."}],
  "_unmatched_orgs":   [{"name": "...", "context": "..."}],
  "_unmatched_places": ["..."]
}
```

The runner stamps `analysis.analyzed_at`, `analysis.prompt_sha256`, and `analysis.analyzer` at commit time; don't populate them; any value provided will be overwritten.

## Confidence rules

- For `key_quotes`: only include quotes an editor might actually pull. Don't dump the whole transcript.
- For `key_quotes[].speaker`: set the `p_*` slug only when you can confidently map a Whisper `speaker_raw` guid to a person via context. When you can't, leave `speaker: null` and set `speaker_label` to the human-readable label from `speakers_raw` (e.g., `"Speaker 2"`); a downstream pass resolves these.
- For `subject_of_interview`: only set when this is clearly an interview (one primary subject doing most of the talking, asked questions). For verite footage with mixed speakers, leave `null`.
- For `moment_ids`: only include moments with relevance >= 0.6. Don't over-tag.
- For `_unmatched_places`: ONLY include places explicitly named in the transcript text or in `path_metadata` (shoot_label, scene_name, sub_location, etc.). Do NOT infer probable venues from external knowledge; e.g., don't add "Big Sky Resort" just because The Rut race usually happens there.
- For `_unmatched_people` / `_unmatched_orgs`: each entry is an object. `name` is required; `context` should be one short clause that helps a later merge pass disambiguate (e.g., `"interviewee says 'we drove to Helen's place'; Helen Johnson, Connor's girlfriend"`).
- For sparse utility clips (slates, camera checks, off-camera cues with no story content), default `tone.mood` to `analytical` rather than `playful`.
