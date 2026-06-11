# editor/queries

Read-only queries over `indexes/*.sqlite` and `dataset/` that answer editorial questions ("strongest soundbites where Michelino is on camera", "find b-roll at Jenny Lake in focus and not setup", "visually similar chunks to this shot"). **Contracted to never write back to the catalog.**

> **A simple starting point, deliberately.** This query surface is what got *us* through a feature cut: plain SQL joins, FTS5 keyword search, off-the-shelf embedding similarity. It has been more than enough to be genuinely useful, but there's nothing sacred about it; better rankers, hybrid retrieval, rerankers, query expansion, and an LLM front-end that picks composers by intent are all natural next steps. Because you own the catalog and the code, the search/retrieval logic is infinitely customizable; iterate on it the way you'd iterate on a cut.

## Boundary

| Concern | Home |
|---|---|
| Dataset writes (ingest, normalize, backfill, dedup, apply, migrate, build_indexes) | `dataset/_scripts/` |
| Dataset self-audits (`audit_*`, `report_transcript_analysis_*`, `count_assets_*`) | `dataset/_scripts/` |
| Editorial-decision queries over indexes + transcripts | `editor/queries/` (this directory) |
| Premiere XML round-trip, sidecar pipeline | `editor/story/_sidecar scripts/` |
| Cut-boundary self-eval (mid-word + SigLIP visual delta) | `editor/sidecar_cut_eval.py` |

**The test:** if the output is consumed by the data pipeline ("is the catalog stale?"), it stays dataset-side. If the output is consumed by the editor or LLM editorial agent ("rank shots for this beat"), it belongs here.

If a query script ever needs to write back to the catalog (persist computed scores, mutate `linked_assets`, etc.), it is **not a query**; it's a backfill, and it belongs in `dataset/_scripts/`.

## Search types: by signal modality

The catalog indexes five **independent signal modalities**. A given editorial question maps to one or more of them, and each modality demands a different *kind* of logic (keyword vs. semantic vs. threshold vs. deterministic filter). Start here to decide *which modality answers the question*; the [Text-driven query protocol](#text-driven-query-protocol-agent-operating-procedure) below is the procedure for driving the chosen one.

> **Iterating.** This map organizes what exists today. As we add capabilities (audio-embedding search, an LLM-front composer, etc.) they slot in under the matching modality. If a question doesn't fit any row cleanly, flag it.

**Quick index (question phrasing → modality):**

| If the editor asks about… | Modality | Section |
|---|---|---|
| what someone *said* / a quote / a topic discussed | **Spoken** | [§ Spoken-word search](#1-spoken-word-search-transcripts) |
| what's *on screen* / a look / a shot / visible text | **Visual** | [§ Visual search](#2-visual-search-frames--shots) |
| how something *sounds* / nat sound / silence / wind | **Audio (non-speech)** | [§ Audio search](#3-audio-non-speech-search) |
| *when* / *what type* / *who* / *where* (facts) | **Structured & temporal** | [§ Structured & temporal search](#4-structured--temporal-search) |
| any combination of the above ("on-camera soundbite where audio is clean") | **Composite** | [§ Composite search](#5-composite-search-multi-layer-composers) |

---

### 1. Spoken-word search (transcripts)

**Signal:** what people say: diarized, word-timed transcripts. Tables: `segment`, `segment_fts`, `person_appearance`; enrichment `analysis.key_quotes` / `key_moments` in `dataset/assets/transcripts/*.json`.

| Logic required | Function | When |
|---|---|---|
| **Keyword / boolean** (exact words) | `search_transcript_fts(query)` | You know the literal wording. Fast, exact. Misses paraphrase. |
| **Semantic** (meaning, paraphrase-robust) | `find_similar_transcript_windows(text)` | Topic/theme, or the editor's recall is approximate. **Default for paraphrased requests.** MiniLM rolling-window. |
| **Editorial-value ranked** | `find_quotes_about_topic(topic)` | Want quote-scored soundbite scores layered on keyword hits. |
| **Speaker identity** | `person_appearance` WHERE `p_id=` ; or `speaker_p_id` on segments | Always use diarized `p_id`, **never** Gemini `semantic_subject` (hallucinates names). |

**Combine with:** date / asset_type SQL filters (pre or post); diversify by `asset_id` / `speaker_p_id`.
**Blind spots:** non-verbal relevance is invisible (a silent sticker shot scores ~0); semantic top-k has a recall ceiling, so run 3–5 seed variants; key-quote coverage is partial (a 0-hit may mean "not enriched," not "no match"; see protocol § 6).

### 2. Visual search (frames & shots)

**Signal:** what's on screen. SigLIP frame embeddings (`clip_and_still_embeddings.sqlite` + `_cache/clip_chunk_means.npy`), VLM `dense_caption`, `shot_quality`, `frame_text` (OCR), `bib_hit`, `frame_face`.

| Logic required | Function | When |
|---|---|---|
| **"Looks like this description"** | `find_visually_similar_by_text(text)` | SigLIP text→image. Cross-modal cosine is low (~0.2) but rankings are meaningful. Finds shots the catalog never classified. |
| **"Looks like this shot"** | `find_visually_similar(asset_id= / chunk_id=)` | SigLIP image→image. Cosine 0.85+ = twin shot; expand a known hero shot into a pool. |
| **"VLM described it as X"** | `find_dense_caption_matches(caption_query)` | Literal `LIKE` against caption text, not semantic; pick distinctive nouns. |
| **Quality gates** | `shot_quality` flags | in_focus, not setup/teardown, aesthetic_score (NIMA), sharpness. |

**Combine with:** `exclude_people` (frame_face), `audio_state` (audio_quality), `exclude_visible_text` (frame_text), `exclude_bibs`, place/date: all exposed through [`find_broll_v2`](#find-b-roll-v2-the-rich-b-roll-picker).
**Blind spots:** SigLIP knows nothing about spoken words, person identities, or audio (see [§ What SigLIP knows nothing about](#what-siglip-knows-nothing-about)); `dense_caption` is `LIKE`, not semantic; keyframe cadence is ~7 s so sub-shot moments can be missed.

### 3. Audio (non-speech) search

**Signal:** sound character independent of words. `audio_event` (tag/theme/score), `audio_quality` (is_silent / is_usable / is_clippy / is_windy / rms_dbfs).

| Logic required | Function | When |
|---|---|---|
| **Sound-event tag match** | SQL on `audio_event.tag` / `.theme` | "find applause / wind / a whistle." No composer yet; query directly. |
| **Usability threshold** | `audio_quality` flags, or `find_broll_v2(audio_state=...)` | Gate b-roll to clean ambient (`usable`), pure silence (`silent`), or drop windy/clippy. |

**Combine with:** visual b-roll (clean ambient under a landscape shot); spoken search (is the soundbite's audio actually usable; that's what `find_soundbites_with_face` gates on).
**Blind spots:** event-tag coverage is partial; **no "sounds like this" embedding search for audio exists yet** (gap: analogue of SigLIP for audio). Until then, non-speech audio is tag/threshold only, not similarity.

### 4. Structured & temporal search

**Signal:** facts about assets: `shoot_date`, `primary_timeline_date`, `asset_type`, `shoot_label`, `asset_place`, people/org/place id lists, `record_kind`.

| Logic required | Function | When |
|---|---|---|
| **Deterministic filter** | `asset_allowlist(...)` or direct SQL | Restrict any candidate pool by date / type / place / person. Exact, instant. |
| **Story-time resolution** | your story-spine registry (chronology anchors + moments), `asset.primary_timeline_date` | Resolve "after the FKT" / "the fall" → date range. **Reflect interpretation back** (protocol § 3). Use `primary_timeline_date` for *story* time, `shoot_date` for *when filmed*. |

**Combine with:** every modality, as a pre-filter (shrink the candidate set first) or post-filter (narrow results).
**Blind spots:** `shoot_label` naming is inconsistent (a single label can pack two interview subjects); **filename collisions are real: always key on `asset_id` (sha256), never filename.**

### 5. Composite search (multi-layer composers)

**Signal:** joins across the four modalities above. These are the multi-layer composers; pick the one whose joins match the intent rather than re-joining by hand.

| Composer | Joins | Modalities |
|---|---|---|
| `find_soundbites_with_face` | scored quotes × `frame_face` × `audio_quality` | Spoken + Visual + Audio |
| `find_broll_v2` | `shot_quality` × `dense_caption` × `frame_face` × `audio_quality` × `frame_text` × `bib_hit` × SigLIP | Visual + Audio + (optional) Structured |
| `find_funny_moments_on_camera` | scored comedic quotes × on-camera × `audio_quality` | Spoken + Visual + Audio |
| `find_bib_appearances` | `bib_hit` × `shot_quality` × `frame_face` | Visual + Structured |
| `find_dense_caption_matches` | `dense_caption` × `shot_quality` × `frame_face` | Visual |
| `find_quotes_about_topic` | `segment_fts` × key-quote overlap | Spoken |
| `find_broll_with_quality` | `asset_place` × `shot` × `shot_quality` | Visual + Structured |

Full parameter menus: [§ Multi-layer composition](#multi-layer-composition) and [§ Find b-roll v2](#find-b-roll-v2-the-rich-b-roll-picker). All return JSON-serializable dicts and accept `limit`.
**Blind spots:** the transcript-side composers don't yet expose a `diversify_by_*` knob (b-roll-side does), so diversify in post-processing (protocol § 4); negative-filter (`exclude_topics=`) is not a first-class param yet. Both are open follow-ups.

---

## Text-driven query protocol (agent operating procedure)

When an editor (or LLM agent on the editor's behalf) asks a natural-language question about the corpus, route through this procedure rather than reaching for raw SQL or the first composer that looks close. The protocol is the result of failure modes observed in actual editorial sessions. Each step exists because skipping it produced wrong/biased/over-narrow results downstream.

### 1. Parse intent before querying

Before any query call, resolve these dimensions from the request. If any are ambiguous, **stop and ask**; don't guess:

| Dimension | What to extract | Failure mode if skipped |
|---|---|---|
| **Asset-type filter** | podcast → `asset_type='third_party'`; interview → `'interview'`; verite / "on the day" → `'verite'`; b-roll / cutaway / establishing → `'b_roll'`; if unspecified, ask whether to constrain | Searching across all types returns spurious cross-type matches; e.g., a podcast quote where you wanted live verite |
| **Speaker / person named** | Resolve to canonical `p_id` via `dataset/people/people.json`. **If 0 matches OR >1 plausible match, exit and ask.** Surface known aliases (Frank/Franc, Mike/Mikey/Michelino) so the editor can disambiguate | Picking the wrong person silently (esp. with shared first names: "Mike" might map to your subject vs. another interviewee with the same nickname) |
| **Date / time reference** | See § "Date intent by asset type" below; handling depends on asset type | The "2025 vs 2024" mistake: confidently returning irrelevant results from the wrong year |
| **Specific quote (recall) vs. topic (theme)** | Quote-recall → `find_soundbites_with_face` first. Topic → `find_similar_transcript_windows` (semantic) first. *Don't* default to FTS keyword search for paraphrased requests | Keyword-guessing roulette: needing to try 6 phrasings before one matches |
| **Negative filters** | "non-criminal", "excluding court process", "not from one source" → capture as exclusion list to apply *after* retrieval | Burying contrasting voices under thematically-similar but unwanted noise |

### 2. Route by intent

```
Interview asked for specifically
    → subject_of_interview lookup:
        1. `analysis.subjects` in transcript JSON (if present)
        2. shoot_label pattern: '%<name>% Interview%' (or 'Interviews' for multi-subject shoots)
        3. dominant-speaker heuristic in `person_appearance` GROUP BY p_id
    → If no match in any: EXIT and flag. Don't fall back to face/transcript search.

Interview not specified, person mentioned (general "find clips of X talking about Y")
    → parallel:
        - face presence:    `frame_face` WHERE p_id = ...
        - speaker presence: `person_appearance` WHERE p_id = ...
    → these may surface different windows of the same asset (or different assets);
      report both, marked clearly

Specific quote in mind (editor knows the wording roughly)
    → `find_soundbites_with_face(p_id=..., topic_contains=..., min_soundbite_score=3)`
    → If 0 hits, FIRST diagnose:
        - Has transcript enrichment been run on this asset?
          (check `analysis.key_quotes` in the transcript JSON; missing/empty means
           "not enriched", not "no matches")
        - Is the filter too narrow? (drop topic_contains, lower min_score)
    → THEN fall back to `find_quotes_about_topic(...)` filtered by speaker

Topic / paraphrased theme (no specific wording in mind)
    → `find_similar_transcript_windows(seed_text, top_k=80)`
    → MiniLM semantic - robust to phrasing variance. Default for paraphrased requests.
    → Cross-reference with `find_quotes_about_topic` only if you want quote-scored
      ranking on top of semantic recall.

B-roll / visual vibe
    → `find_broll_v2(...)`  for catalog-classified candidates
    → `find_visually_similar_by_text("...")`  for SigLIP-semantic candidates outside
                                              the catalog's classification net
    → `find_visually_similar(asset_id=<known_good>)`  to expand a known hero shot
    → Combine all three for a deep pool; dedupe by asset_id.
```

### 3. Date intent by asset type

The user's date reference means something different per asset type. Handle each accordingly:

| Asset type | What "date" means | Action |
|---|---|---|
| **verite, third_party** | The year/month of the actual event matters editorially (the story is anchored to real-world dates). Vague refs ("the fall", "after the FKT", "around the court date") must resolve to a date range before querying. | Consult `editor/story/project_beats.json::chronology_anchors` in your full workspace (not shipped in the sample repo; production holds ~14 canonical events with `date_approx` + `beat`). Map the reference → date range. **Reflect the interpretation back** ("you mean after the FKT was set on 2024-09-02, so 9/3/2024 onward?") before querying. If still ambiguous after the lookup, ask. |
| **b_roll** | Year usually doesn't matter; **seasonal/visual consistency** does, and only for *outdoor Jackson + East Coast* footage where leaf color, snow, foliage betray a wrong season. For LA / indoor / studio footage, ignore. | Derive season from `shoot_date` (month 4-5=spring, 6-8=summer, 9-11=fall, 12-3=winter). Cross-reference `place_ids_json`: if the cutaway lands in a scene set in Jackson fall 2024, only surface b-roll from Jackson autumn (any year) unless the editor explicitly opens it up. |
| **interview** | Usually irrelevant; interviews are retrospective. The *only* exception: "frame of mind" intent ("Michelino *before* attempting the FKT" vs. "after"), which requires interview date relative to the event being reflected on. | Default: no date filter on interviews. Only apply one if the editor explicitly invokes frame-of-mind. When they do, compare interview `shoot_date` to the event date from `chronology_anchors`. |

`asset.primary_timeline_date` is the underused field for resolving story-time vs. shoot-time mismatches (e.g., footage that depicts an earlier event should carry that event's date). Prefer it over `shoot_date` when the query is about *story time*; use `shoot_date` when the query is about *when it was filmed*.

### 4. Diversify before reporting

**Hard default for any multi-source query: cap hits at ≤ 2-3 per asset_id before ranking the top N.** Without this, whichever speaker happens to talk most about the topic dominates the pool, hiding shorter contrasting voices. Observed example: the FKT-ethics semantic search returned 10 of the top 20 hits from a single Alex Rienzie podcast: semantically accurate but editorially over-weighted.

When the editor's intent is explicitly "a range of voices" / "different perspectives" / "broader coverage", strengthen further:
- cap to 1-2 per asset
- cap to 1-2 per speaker (`speaker_p_id`)
- consider hard-excluding the dominant source if it's about to bias the result set

`find_broll_v2` already exposes `diversify_by_shoot=True`. The transcript-side composers (`find_similar_transcript_windows`, `find_quotes_about_topic`, `find_soundbites_with_face`) currently do *not*; agents must diversify in post-processing until those composers expose the knob.

### 5. Confirm before building

When a search lands on a **single high-value hit** (one quote that exactly matches the editor's recall, one b-roll shot that perfectly fits), don't immediately treat it as a build target. Instead:

1. Pull ±15 seconds of context around the hit (surrounding `segment` rows for transcript; adjacent shots for visual).
2. Reflect the quote + asset + date back to the editor with the context.
3. Wait for an explicit "yes, that's the one" before proceeding to scene-workspace build, sidecar update, or any downstream action.

Costs ~30 seconds of read time. Catches the common failure: the editor's recall is pointing at a similar moment in a different asset, or at the same speaker on a different day, or at a paraphrase that drifted from the actual wording.

### 6. Diagnose 0-hit results

When any composer returns 0, *do not silently fall back*. First diagnose the reason; they're different failure modes with different right responses:

| Diagnosis | Action |
|---|---|
| Asset not enriched | Fall back to FTS / semantic search, AND surface the enrichment gap |
| Filter too narrow (`min_score`, `topic_contains`, etc.) | Widen the filter, re-run |
| Asset doesn't exist in catalog | Check asset_id resolution; check date / shoot_label spelling |
| Vocabulary mismatch (topic field uses different word than query) | Switch from `find_quotes_about_topic` to `find_similar_transcript_windows` |

## Layout

```text
editor/queries/
├── queries_README.md         # this file
├── queries_SCHEMA.md         # full schema doc - every queryable signal across all layers
├── __init__.py               # public API re-exports (single-layer + compose)
├── retrieval.py              # thin facade: imports from store/encoder/filters/transcript/cli
├── store.py                  # SigLIP mean-vector cache (load + persist + invalidate)
├── encoder.py                # SigLIP text/image encoders (lazy, CPU default)
├── filters.py                # asset_allowlist() + search_broll() (basic, asset-level)
├── transcript.py             # FTS5 keyword + MiniLM rolling-window text search
├── visual.py                 # SigLIP visual similarity (chunk/asset/text)
├── compose.py                # multi-layer editorial composers
├── source_window.py          # heuristic clean source-in/out window picker
├── cli.py                    # argparse subcommand dispatcher
├── beat_coverage_report.py   # moments × transcripts coverage report (markdown)
├── beat_select_ranker.py     # top-N shot ranker per beat (LLM-assisted)
└── cut_audit.py              # read-only diagnosis of xmeml + sidecar + editor_notes
```

`cut_audit.py` is CLI-shaped (not re-exported via `__init__.py`); it joins the xmeml against asset_map / catalog / editor_notes / sidecar and reports per-scene issues. Status mix declared in its module docstring: EARNED checks (source-bound, notes-missing, rationale-missing, the join layer itself), FRAGILE checks (regex-on-prose avoid/stale detection), SPECULATIVE checks (scene visual-hole / V3-gap). Read the docstring before extending.

## Public API

```python
from editor.queries import (
    # Single-layer SQL helpers
    search_broll,                    # by place_id and/or location_like (basic)
    search_transcript_fts,           # FTS5 keyword over segment text

    # Single-layer vector helpers
    find_visually_similar,           # by chunk_id or asset_id (SigLIP image space)
    find_visually_similar_by_text,   # text query → top-K chunks (SigLIP text encoder)
    find_similar_transcript_windows, # text query → top-K transcript windows (MiniLM)

    # Multi-layer composition
    find_soundbites_with_face,       # scored quotes × frame_face × audio_quality
    find_broll_with_quality,         # asset_place × shot × shot_quality
    find_broll_v2,                   # rich b-roll: dense_caption + face + audio + ocr + bib + similarity
    find_dense_caption_matches,      # VLM caption × shot_quality × frame_face
    find_funny_moments_on_camera,    # scored comedic quotes × subjects_on_camera × audio_quality
    find_bib_appearances,            # bib_hit × shot_quality × frame_face
    find_quotes_about_topic,         # segment_fts × key-quote overlap

    # Filtering helper
    asset_allowlist,                 # restrict candidates by bucket/date/place/person

    # Cache / encoder
    load_chunk_mean_store,           # warm or build the .npy cache
    SigLIPEncoder,                   # text/image encoder, lazy load
)
```

## Multi-layer composition

`compose.py` exposes 7 named composers that join 2-5 layers each. Read **[queries_SCHEMA.md](queries_SCHEMA.md)** for the menu of signals each can pivot or filter on.

| Function | Pivots on | Joins to | Returns |
|---|---|---|---|
| `find_soundbites_with_face(p_id, ...)` | `analysis.key_quotes` (score 1-5) | `frame_face` + `audio_quality` | top-rated quotes where speaker is on camera and audio is clean |
| `find_broll_with_quality(place_id= / location_like=, ...)` | `asset_place` × `shot` | `shot_quality` | b-roll at a place, filtered by in_focus / not-setup, ranked sharpness |
| **`find_broll_v2(...)`** | shot pool | `shot_quality` + `dense_caption` + `frame_face` + `audio_quality` + `frame_text` + `bib_hit` + SigLIP optional | **rich b-roll picker: see "Find b-roll v2" section below** |
| `find_dense_caption_matches(caption_query, ...)` | `dense_caption.caption_text LIKE` | `shot_quality` + optional `frame_face` | shots whose VLM caption mentions a phrase |
| `find_funny_moments_on_camera(p_id=, comedy_type=, ...)` | `analysis.key_quotes[].comedic` | `key_moments.subjects_on_camera` (or `frame_face` fallback) + `audio_quality` | comedic quotes where someone (optionally specific person) is on camera |
| `find_bib_appearances(bib_number= / p_id=, ...)` | `bib_hit` | `shot_quality` + `frame_face` | bib visible in usable shots, optional athlete-on-camera filter |
| `find_quotes_about_topic(topic, ...)` | `segment_fts MATCH` | `key_quotes` overlap by time | FTS5 hits with quote score / story_function attached when available |

All return lists of plain dicts (JSON-serializable for LLM hand-off). All accept `limit` (default 30).

**Example: soundbites.**

```python
from editor.queries import find_soundbites_with_face
hits = find_soundbites_with_face(
    p_id="p_michelino_sunseri",
    min_soundbite_score=4,           # 1-5 scale, default 4
    topic_contains="pardon",
    story_function="character_reveal",
    require_audio_usable=True,
    require_not_windy=True,
    limit=20,
)
# → [{asset_id, start_sec, end_sec, text, soundbite_score, ...}, ...]
```

**v2 (deferred):** thin LLM-front layer that takes a natural-language editorial intent and picks a composer + parameters.

## Find b-roll v2: the rich b-roll picker

`find_broll_v2` composes 5+ enrichment layers into one helper. Choose which knobs you want for each query:

| Knob | Default | Reads |
|---|---|---|
| `place_id=` / `location_like=` | None / None | `asset_place` / `asset.semantic_location` |
| `caption_contains="trail at golden hour"` | None | `dense_caption.caption_text LIKE` |
| `subject_contains="moving car"` | None | `asset.semantic_subject LIKE` |
| `similar_to_text="ranger station exterior"` | None | SigLIP text↔image (top-K candidates pre-filter) |
| `similar_to_asset_id="<sha256>"` | None | SigLIP image↔image (top-K candidates pre-filter) |
| `camera_movement="gimbal"` or `["gimbal","dolly","drone"]` | None | `asset_semantic_chunk.camera_movement`, **shot-aligned** by chunk time-overlap. Corpus values: handheld/static/mixed/pan/gimbal/drone/dolly/push_in/pull_out/whip/tilt. Refiner only (not a sole anchor). |
| `exclude_people=True` | False | `frame_face` (drop shots where any face appears) |
| `require_people_p_ids=["p_michelino_sunseri", ...]` | None | `frame_face` (must contain these people) |
| `audio_state="silent" / "usable" / "any"` | `"any"` | `audio_quality.is_silent` / `is_usable` |
| `exclude_visible_text=True` | False | `frame_text` (drop shots with on-screen text) |
| `exclude_bibs=True` | False | `bib_hit` (drop shots with race bibs visible) |
| `require_in_focus=True` | True | `shot_quality.is_in_focus` |
| `exclude_setup_or_teardown=True` | True | `shot_quality.is_setup_or_teardown` |
| `min_aesthetic_score=4.0` | None | `shot_quality.aesthetic_score` (NIMA, ~4.05 = corpus p85) |
| `min_duration_sec=2.0` | `2.0` | `shot.duration_sec` |
| `rank_by="aesthetic" / "sharpness" / "duration" / "blended"` | `"blended"` | combination metric |
| `diversify_by_shoot=True` | False | spread top-N across distinct shoots |
| `asset_type="b_roll"` | `"b_roll"` | pass `None` for all asset types |
| `limit=30` | `30` | max results |

Returns ranked list of dicts: `{asset_id, shot_idx, start_sec, end_sec, duration_sec, sharpness_score, motion_score, aesthetic_score, is_aesthetic, camera_movement, dense_caption_text, on_camera_p_ids, audio_state, has_visible_text, has_bib, place_ids, semantic_subject, ..., blended_score}`.

> **`camera_movement` + `subject_contains`.** Both were gaps that forced raw SQL, surfaced while hunting a "gimbal driving through the Tetons" b-roll ride. The one-call query is now: `find_broll_v2(location_like="Teton", subject_contains="moving", camera_movement=["gimbal","dolly"], min_duration_sec=40, rank_by="duration", asset_type=None)`. **Note for "one continuous clip":** `min_duration_sec` is **shot-level** (an uninterrupted shot ≥N s), which is the right signal for a continuous ride; a long *asset* may contain internal cuts.

**Example: establishing landscape, no people, usable ambient audio, golden hour.**

```python
from editor.queries import find_broll_v2
hits = find_broll_v2(
    location_like="Teton",
    caption_contains="mountain",
    exclude_people=True,
    audio_state="usable",
    require_in_focus=True,
    min_aesthetic_score=4.0,
    rank_by="aesthetic",
    diversify_by_shoot=True,
    limit=10,
)
```

**Example: visually-similar b-roll to a reference hero shot, in focus, not setup.**

```python
hits = find_broll_v2(
    similar_to_asset_id="0eaa22b868f0...",
    require_in_focus=True,
    exclude_setup_or_teardown=True,
    limit=20,
)
```

## Cross-Act usage / dedup

Before pulling b-roll / stills for a new scene, check what a prior Act export already used so you don't duplicate a clip across acts (real failure mode: Act I had already burned 5/7 Bryce archival photos for his "1983 Record Holder" backstory). `usage.py` parses the committed Act exports (top-level `editor/xml exports/*.xml`, excludes `_archive/`), resolves every `<pathurl>` → `asset_id`, and reports what's used + where.

```python
from editor.queries import used_assets, is_used, filter_unused, annotate_usage

used = used_assets()                      # {asset_id: [{act, clip_name, src_in_sec, src_out_sec}, ...]}
is_used("0a545eff75...", used)            # bool
filter_unused([a1, a2, a3], used)         # subset NOT used in any committed act
annotate_usage(broll_hits, used)          # adds a `used_in: [act, ...]` field to each composer dict
```

The intended workflow: run a composer (`find_broll_v2`, etc.) → `annotate_usage(results)` → eyeball/skip anything with a non-empty `used_in`. CLI:

```powershell
py editor\queries\usage.py                          # summary: per-act clip/asset counts + shared count
py editor\queries\usage.py --check <aid> [<aid>..]  # is each used? where? (10-char prefix OK)
py editor\queries\usage.py --list --act "act I"     # dump used asset_ids (optionally one act)
py editor\queries\usage.py --unresolved             # pathurls that didn't resolve (data-quality)
py editor\queries\usage.py --scene ...              # also include scene_workspace/ WIP XMLs
```

Resolution backbone: pathurl → derivative-media-relative path → `asset_id` via the `derivative media/_index/asset_map.json` reverse index, with a catalog `source_path` tail-match fallback. **Two gotchas baked in:** (1) asset_map stores the *source* relative_path (e.g. `…CANON.MXF`) while the Act XML references the transcoded *proxy* (`…CANON.mp4`), so the index is keyed by both full path and extension-stripped stem. (2) Genuinely-unresolved pathurls are expected and correct: post-production media that isn't a corpus asset (Epidemic Sound SFX, headline/FOIA graphics, court screenshots) has no `asset_id` to dedup against.

## CLI

```powershell
py editor\queries\retrieval.py broll --place pl_jenny_lake_ranger_station
py editor\queries\retrieval.py search-transcript --query "search and rescue"
py editor\queries\retrieval.py similar-chunk --asset-id <asset> --top-k 25
py editor\queries\retrieval.py similar-text  --text "Jenny Lake ranger station building exterior" --top-k 25
py editor\queries\retrieval.py similar-transcript --text "ranger station trailhead"

# Optional filters that compose onto similar-* :
#   --bucket broll  --asset-type b_roll
#   --shoot-date-from 2024-06-01 --shoot-date-to 2024-09-30
#   --place pl_jenny_lake_ranger_station
#   --exclude-asset <asset_id>  (repeatable)
#   --camera-movement static  --shot-size WS

py editor\queries\retrieval.py build-cache    # one-shot: rebuild clip_chunk_means cache

# Cut audit (read-only): xmeml + sidecar + editor_notes diagnosis
py editor\queries\cut_audit.py "editor\xml exports\<file>.xml"
py editor\queries\cut_audit.py "editor\xml exports\<file>.xml" --scene <scene_id>
py editor\queries\cut_audit.py "editor\xml exports\<file>.xml" --json audit.json
```

The 7 multi-layer composers are Python-only for now (no CLI wrappers). The deferred v2 LLM-front would dispatch to them by intent rather than name.

## SigLIP: how it works in this stack

**SigLIP** (Sigmoid Loss for Language Image Pre-training) is Google's 2023 contrastive vision-language model: same idea as CLIP (image encoder + text encoder land in a shared embedding space) but with a sigmoid loss that scales better to large batches and gives more robust retrieval. The model used here is `google/siglip-so400m-patch14-384`.

| Detail | Value |
|---|---|
| Model weights | `google/siglip-so400m-patch14-384` (~3.5 GB) |
| Cache | `indexes/_cache/hf/` (set via `HF_HOME` at import; see `encoder.py`) |
| Input image | 384×384 RGB |
| Output | **1152-d float32 vector, L2-normalized** |
| Text encoder | same 1152-d space → text↔image cosine "just works" |
| First call | ~5 s to load weights from disk after the initial download |
| Per-query encode | ~50 ms on CPU |

### How this stack stores + queries SigLIP

```
proxies (mp4)
  │ extract one keyframe per gemini chunk (~7 s cadence)
  ▼
SigLIP image encoder
  │ 1152-d vector per frame, L2-normalized
  ▼
indexes/clip_and_still_embeddings.sqlite::clip_embeddings
  │   ← ~116K raw frame vectors (binary blobs) + semantic_chunks registry
  │
  ├─► mean-pool per chunk_id  →  indexes/_cache/clip_chunk_means.npy (~5.6K mean vectors, mmap'd)
  │                              ← used by find_visually_similar*
  │
  └─► HNSW index            →  indexes/clip_embeddings.faiss (567 MB)
                              ← used by SigLIPCutIndex
                              ← sub-second top-K
```

At query time:
- **Image↔image:** `find_visually_similar(asset_id=X)` averages all chunks of X, dots against chunk-mean store
- **Text↔image:** `find_visually_similar_by_text("ranger station at golden hour")` → SigLIP text encoder → 1152-d → dot against chunk-mean store
- **Cut delta:** `SigLIPCutIndex.compute_cut_delta(asset_out, ts_out, asset_in, ts_in)` → reconstruct two frame vectors from FAISS → cosine

### Score interpretation (calibrated against this corpus)

| Cosine | What it means |
|---|---|
| ~0.40 | baseline noise floor |
| ~0.55 | "useful match" threshold for retrieval |
| ~0.85+ | "twin shot": same scene / same framing |
| ~0.95+ | within-take or identical SigLIP keyframe |

### What SigLIP knows nothing about

- People identities (use `frame_face` / `face_embeddings.sqlite`)
- Spoken words
- Audio events (use `audio_event`)
- Temporal sequence within a take
- Camera-specific calibration drift (proxy compression + rolling shutter drag scores)

## Cache layer

Per-chunk SigLIP mean vectors are computed once and persisted to `indexes/_cache/clip_chunk_means.npy` + `clip_chunk_ids.npy` (parallel chunk_id array). The cache is invalidated when `clip_and_still_embeddings.sqlite` mtime is newer than the `.npy`. First build reads all 116k frame vectors (~535 MB) and writes ~26 MB of mean vectors. Subsequent loads `mmap` the `.npy` and finish in <100 ms.

Rebuild explicitly if you suspect drift:

```powershell
py editor\queries\retrieval.py build-cache --force
```

## External dependencies (read-only)

```text
indexes/editorial_catalog.sqlite              denormalized catalog + 9 enrichment tables (the join surface)
indexes/clip_and_still_embeddings.sqlite      SigLIP frame vectors + semantic_chunks registry
indexes/transcript_rolling_embeddings.sqlite  MiniLM transcript windows (binary embeddings)
indexes/clip_embeddings.faiss + .meta.json    HNSW over SigLIP keyframes (sub-second top-K)
indexes/_cache/clip_chunk_means.npy           per-chunk mean-pooled SigLIP (built on demand)
indexes/_cache/clip_chunk_ids.npy             parallel chunk_id array (mmap'd alongside means)
indexes/_cache/hf/                            HuggingFace cache for SigLIP weights (~3.5 GB)
(story-spine registry, e.g. moments.json - define your own)   story spine (mom_*)
dataset/assets/transcripts/*.transcript.json  enriched transcripts (analysis.key_quotes + key_moments + WhisperKit segments)
dataset/assets/editor_notes/                  per-asset editorial findings (wobble warnings, "use src 25-31s", tags)
dataset/people/people.json                    p_id ↔ canonical_name registry
```

## Schema dependencies (read columns)

| Table | Columns read |
|---|---|
| `editorial_catalog.asset` | `asset_id, filename, source_path, duration_sec, semantic_subject, semantic_location, shoot_date, shoot_label, bucket, asset_type, record_kind, semantic_editorial_notes, has_machine_transcript` |
| `editorial_catalog.asset_place` | `asset_id, pl_id, source, confidence, matched_phrase` |
| `editorial_catalog.asset_semantic_chunk` | `asset_id, editorial_notes, action, setting_location, camera_movement, camera_shot_size` |
| `editorial_catalog.segment` | `asset_id, seg_idx, start_sec, end_sec, text, speaker_p_id` |
| `editorial_catalog.segment_fts` | FTS5 over segment text |
| `editorial_catalog.person_appearance` | `p_id, asset_id, seg_idx, start_sec, end_sec, text` |
| `editorial_catalog.frame_face` | `asset_id, frame_time_sec, p_id, cluster_id, det_score, bbox_json` |
| `editorial_catalog.shot` | `asset_id, shot_idx, start_sec, end_sec, duration_sec` |
| `editorial_catalog.shot_quality` | `asset_id, shot_idx, sharpness_score, motion_score, is_blurry, is_in_focus, is_setup_or_teardown, is_dark, is_blown, aesthetic_score, is_aesthetic` |
| `editorial_catalog.frame_text` | `asset_id, shot_idx, frame_time_sec, text, confidence, ocr_engine, bbox_json` |
| `editorial_catalog.bib_hit` | `asset_id, shot_idx, frame_time_sec, bib_number, confidence, ocr_engine, p_id` |
| `editorial_catalog.audio_event` | `asset_id, window_start_sec, window_end_sec, tag, theme, score, engine` |
| `editorial_catalog.audio_quality` | `asset_id, record_kind, rms_dbfs, peak_dbfs, is_silent, is_usable, is_clippy, is_windy` |
| `editorial_catalog.dense_caption` | `asset_id, chunk_id, shot_idx, frame_time_sec, sample_pos, caption_text, model_engine, prompt_variant` |
| `editorial_catalog.still_aesthetic` | `asset_id, aesthetic_score, is_aesthetic, image_ext` |
| `clip_and_still_embeddings.clip_embeddings` | `chunk_id, vector_dim, vector_blob` |
| `clip_and_still_embeddings.semantic_chunks` | `chunk_id, parent_asset_id, chunk_start_sec, chunk_end_sec, chunk_idx` |
| `transcript_rolling_embeddings.embedding_run` | `run_id, model_name` |
| `transcript_rolling_embeddings.transcript_window_embedding` | `run_id, asset_id, window_anchor_ms, window_start_sec, window_end_sec, text_preview, embedding_dim, vector_blob` |

Schema drift will surface as a runtime error in `store.load_chunk_mean_store()` or as empty results from the SQL queries. There is no schema-pinning mechanism today; that's a follow-up.

## Conventions

- Vectors stored as `float32` little-endian BLOBs, `len(blob) == 4 * vector_dim`.
- SigLIP frame vectors are L2-normalized at ingest. Per-chunk mean vectors are *not* normalized (mean of unit vectors is not unit); the query path normalizes inside `_cosine`.
- All similarity functions return lists of plain `dict` so they're trivially JSON-serializable for handing to an LLM.
- `score` is cosine similarity in [-1, 1]; for SigLIP-vs-SigLIP queries, useful matches start at ~0.55 and "twin shot" sits ~0.85+ (see SigLIP section).

## Editor notes (per-asset editorial knowledge)

`dataset/assets/editor_notes/{asset_id}_editor_notes.json` holds per-asset editorial findings the user has surfaced about specific clips ("wobble at start," "obscured by branches," "good middle window at src 25-31s"). Mirrors the per-asset transcript pattern.

`find_visually_similar()` and `find_visually_similar_by_text()` automatically include `editor_notes` and `editor_tags` in each result dict when the file exists. The LLM editor sees these alongside the SigLIP score so it can avoid picks the human has already vetoed, or steer toward known good windows.

Schema and the controlled tag vocabulary: `dataset/assets/editor_notes/_schema.md`.

When the user gives editorial feedback during a session, append a cleaned-up, deduplicated note to the relevant `*_editor_notes.json` before ending. This is what "captures the learnings" means for editorial knowledge specifically.

## Known data quirks

- **Multiple shoot days share camera-card filenames.** Querying by `filename = 'C0050.MP4'` returns up to 4 different assets from different shoots. Always lookup and store by `asset_id` (sha256).
- **Catalog `source_path` points to original camera-card paths** (e.g. `<RAID>\<project>\...`), not proxy paths. For Premiere pathurls, resolve via `derivative media/_index/asset_map.json` `entries[asset_id].video_video_proxy.relative_path`.
- **Catalog `width`/`height` are source dimensions**, not proxy dimensions. Proxies are 1280×720 H.264; sources are typically 3840×2160. For xmeml writes use 1280×720.
- **Transcript-enrichment fields live under `analysis.{key_quotes,key_moments}`** in transcript JSONs, not at top-level. See `queries_SCHEMA.md` for the exact field paths.

## Gaps

Track query-surface gaps in `editor/editor_GAPS.md` (ships cleared) as you extend the composers.
