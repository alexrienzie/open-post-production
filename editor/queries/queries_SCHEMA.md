# editor/queries: schema doc for multi-layer composition

The catalog now exposes ~9 composable enrichment layers (in addition to the catalog
+ transcript core). This doc lists every editorially-meaningful signal an
LLM-driven query composer can pivot or filter on. **This is the menu** the
`compose.py` helpers draw from.

Conventions:
- Every layer keys on `asset_id` (sha256). Some also key on `shot_idx`,
  `frame_time_sec`, `chunk_id`, `seg_idx`, or `p_id`.
- All boolean flags are stored as `INTEGER` (0/1).
- Time is in seconds unless suffixed (`_frames`, `_ms`).
- Source of truth is per-asset catalog JSON under `dataset/assets/{video,audio,stills}/`.
  Tables in `indexes/editorial_catalog.sqlite` are projections, rebuilt by
  `dataset/_scripts/build_editor_db.py`.

---

## Catalog core (always available)

### `asset`: one row per source media file

| Column | Type | Editorial meaning |
|---|---|---|
| `asset_id` | TEXT PK | sha256 of the source file |
| `record_kind` | TEXT | `video` / `audio` / `still` |
| `source_path` | TEXT | original camera-card path (not proxy) |
| `filename` | TEXT | basename only |
| `duration_sec` | REAL | from ffprobe |
| `width` / `height` | INTEGER | source dims (not proxy) |
| `shoot_date` | TEXT | YYYY-MM-DD |
| `shoot_label` | TEXT | shoot-folder slug (`2025-7-9_<shoot name>`) |
| `camera_id` | TEXT | for filtering by camera body |
| `bucket` | TEXT | `in_house_other` / `in_house_priority_ht` / `third_party` |
| `asset_type` | TEXT | `verite` / `b_roll` / `interview` / `timelapse` / `archival` / `third_party` |
| `semantic_location` | TEXT | Gemini headline location string |
| `semantic_subject` | TEXT | Gemini headline subject string |
| `semantic_editorial_notes` | TEXT | Gemini headline editor-facing notes |
| `has_machine_transcript` | INTEGER | 1 if `assets/transcripts/<aid>.transcript.json` exists |
| `has_audio_extract` | INTEGER | 1 if `audio_extract` block present |
| `people_ids_json` / `org_ids_json` / `moment_ids_json` / `place_ids_json` | TEXT | JSON arrays |

### `segment`: WhisperKit transcript segments

Per `(asset_id, seg_idx)`. Columns: `start_sec`, `end_sec`, `speaker_id`, `speaker_p_id`, `text`.
**FTS5 index:** `segment_fts(text, asset_id UNINDEXED, seg_idx UNINDEXED)`.

### `person_appearance`: denormalized person × segment

For "who spoke when". Per `(p_id, asset_id, seg_idx)` with `start_sec`, `end_sec`, `text`.

### `asset_semantic_chunk`: per-chunk Gemini fields

Per `chunk_id`. Headline columns: `setting_location`, `camera_movement`, `camera_shot_size`, `editorial_notes`, `action`, `subject`, `audio_character`, `emotional_tone`.

### `asset_semantic_key_moment`: Gemini-flagged in-out proposals

Per `(chunk_id, moment_idx)`. `timestamp_sec` + `description`.

### `asset_place`: content-inferred place links

Per `(asset_id, pl_id)` with `source` + `confidence` + `matched_phrase`.

---

## Enrichment layers (the new composable surface)

### `frame_face`: named face detections (consent-gated)

Per `(asset_id, frame_idx)`. Only named identities (`p_id` resolved against `dataset/people/people.json`); unnamed clusters stay in the canonical `face_embeddings.sqlite` for debugging.

| Column | Editorial meaning |
|---|---|
| `asset_id`, `frame_time_sec`, `chunk_id`, `frame_idx` | location of the detection |
| `p_id` | which person (joins to `dataset/people/people.json`) |
| `cluster_id` | which HDBSCAN cluster this face came from |
| `det_score` | InsightFace detection confidence |
| `bbox_json` | 4-point polygon, abs pixels |
| `identified_via` | how the name was assigned (auto-suggest / manual / cluster-leader) |

**Compose pivot:** "person on camera near time T". Join with `shot` to scope to a shot.

### `shot`: per-asset shot boundaries (PySceneDetect)

Per `(asset_id, shot_idx)`. Columns: `start_sec`, `end_sec`, `duration_sec`.

**Compose pivot:** time-scoping for any per-frame layer.

### `shot_quality`: per-shot editorial-usability metrics

Per `(asset_id, shot_idx)`. Columns:

| Column | Meaning |
|---|---|
| `sharpness_score` | Laplacian variance; higher = sharper |
| `motion_score` | frame-diff between sampled frames; higher = more camera/subject motion |
| `exposure_mean` | mean luminance 0-255 |
| `clipping_ratio` | fraction of pixels at 0 or 255 |
| `is_blurry` / `is_in_focus` | derived (`sharpness_score` ≷ 100) |
| `is_dark` / `is_blown` | derived (exposure_mean threshold + clipping) |
| `is_setup_or_teardown` | derived (first/last short shot heuristic) |
| `aesthetic_score` / `is_aesthetic` | NIMA NIMA aesthetic score ≥ 4.05 = "publishable" |

**Compose pivot:** quality gate on any per-shot result (`is_in_focus AND NOT is_setup_or_teardown` is the canonical "usable B-roll" filter).

### `frame_text`: OCR detections (filtered)

Per-frame OCR rows that survived the QA filter (Gemini Flash flagged hallucinations dropped, <3 alnum filtered, Cyrillic dropped). Columns: `asset_id`, `record_kind`, `shot_idx`, `frame_time_sec`, `bbox_json`, `text`, `confidence`, `ocr_engine` (`rapidocr` / `apple_vision`).

**Compose pivot:** find shots where on-screen text contains a phrase ("Jenny Lake", a brand name, a vehicle plate).

### `bib_hit`: numeric-bib projection of `frame_text`

Per-frame bib-number rows where text matches `^\d{2,4}$`. Columns: `asset_id`, `shot_idx`, `frame_time_sec`, `bib_number`, `confidence`, `ocr_engine`, `p_id` (nullable; filled later from bib→athlete map).

**Compose pivot:** "find all visible appearances of bib N" or "all frames where athlete P's bib is visible".

### `shot_text` (view): per-shot OCR rollup

`GROUP_CONCAT(DISTINCT text)` per `(asset_id, shot_idx)`. Useful for shot-level text searches without exploding into per-frame rows.

### `audio_quality`: per-asset DSP metrics

Per `asset_id` (videos use `record_kind='video'` for their extracted audio; audio assets use `record_kind='audio'`). Columns: `duration_sec`, `rms_dbfs`, `peak_dbfs`, `clipping_ratio`, `silence_ratio`, `dc_offset`, `low_freq_ratio`, plus derived flags `is_silent` / `is_quiet` / `is_clippy` / `is_windy` / `is_usable`.

**Compose pivot:** audio gate on any soundbite or interview result (`is_usable AND NOT is_windy` is canonical).

### `audio_event`: timed CLAP audio events

Per-window `(asset_id, window_start_sec, window_end_sec)`. Columns: `tag` (laughter, applause, footsteps, wind, music, ...), `theme` (voice / movement / environment / outdoor / vehicle / music / media_artifact / race_event), `score`, `rank_in_win`, `engine` (`laion_clap` / `ms_clap`).

**Compose pivot:** find moments containing a specific sound ("laughter near time T" / "footsteps without dialogue" / "applause during X").

### `dense_caption`: per-frame VLM captions

Per `(asset_id, frame_time_sec)`. Columns: `chunk_id`, `shot_idx`, `sample_pos` (`25%` / `50%` / `75%` / `midpoint`), `caption_text` (flat string for FTS), `caption_json` (structured), `model_engine` (`gemini_flash` etc.), `prompt_variant` (`meta` / `no-meta`).

**Compose pivot:** "find shots whose VLM caption mentions X"; complements OCR (text in the frame) and transcript (text being spoken) with text describing the visual content.

### `still_aesthetic`: per-still NIMA score

Per `asset_id` (only for `record_kind='still'`). Columns: `aesthetic_score`, `is_aesthetic`, `image_ext`.

**Compose pivot:** rank stills by aesthetic for publication-grade selection.

---

## Transcript enrichment: key-quote scoring (per-asset JSON, not in `editorial_catalog`)

Lives in `dataset/assets/transcripts/<aid>.transcript.json` under the `analysis` block (not yet projected to a SQL table; `compose.py` walks JSONs directly). ~4.5K transcripts in the corpus carry these fields.

### `analysis.key_quotes[]`: scored individual quotes

| Field path | Meaning |
|---|---|
| `text` | the quote (verbatim transcript span) |
| `speaker` | who said it (`p_id` string, not nested) |
| `start_sec` / `end_sec` | timing on the asset's own timeline |
| `soundbite_quality.score` | **INTEGER 1-5** (LLM rating; median 4 across corpus) |
| `soundbite_quality.reasons` | LLM rationale array (`thinking_aloud` / `excessive_fillers` / `clean_delivery` / etc.) |
| `soundbite_quality.trim_suggestion` | optional `{start_sec, end_sec, reason}` for a tighter cut |
| `soundbite_quality.delivery_notes` | LLM observations on cadence / clarity |
| `story_function` | `setup` / `catalyst` / `theme_statement` / `character_reveal` / `payoff` / etc. |
| `comedic` | dict (see below); **comedy lives on quotes, not moments** |
| `comedic.is_comedic` | string `'True'` / `'False'` / `'None'` (legacy LLM output; treat as boolean) |
| `comedic.type` | `outrageous_claim` / `setup_punchline` / `irony` / `banter` / `self_deprecation` / `reaction` / `one_liner` / `contrast` / `outrageous_claim_plus_reaction` (other types may appear) |
| `comedic.confidence` | 0-1 (sometimes returned as string) |
| `comedic.notes` | LLM rationale |
| `linked_moment_idx` | index into the same transcript's `analysis.key_moments[]` for the parent moment |

**Corpus stats:** ~13K total quotes across ~4.5K transcripts; ~2.3K flagged `is_comedic = True` (`outrageous_claim` 882 / `setup_punchline` 506 / `irony` 213 most common).

### `analysis.key_moments[]`: scene-level beats with pre-computed on-camera subjects

| Field path | Meaning |
|---|---|
| `label` | one-line summary of the moment |
| `summary` | longer prose |
| `categories[]` | `story` / `emotional_peak` / `character_revealing` / etc. |
| `start_sec` / `end_sec` | moment timing |
| `salience` | 0-1 LLM-rated importance |
| `key_quote_idxs[]` | quotes inside this moment (indices into `analysis.key_quotes[]`) |
| `moment_ids[]` | links to `dataset/story/moments.json` (`mom_NNN`) for editorial spine |
| `subjects_on_camera[]` | **PRE-COMPUTED `p_id` array**: who's visible in the moment |

**Corpus stats:** ~7.6K total moments; ~3.8K (51%) have `subjects_on_camera` populated. When populated, this is cheaper than a `frame_face` SQL lookup.

**Compose pivot:** top soundbites by score + filter by speaker / topic / story_function / asset_type / place; comedic quotes by `comedic.confidence` + `type`; use `subjects_on_camera` on the linked moment for fast face-presence check.

---

## Cross-modal joins that matter

These are the join patterns `compose.py` exposes as named helpers:

1. **Person × time**: `frame_face` × `shot` → "Michelino on camera during this shot"
2. **Person × speech × usable audio**: `key_quotes` × `frame_face` × `audio_quality` → "strongest soundbites where the speaker is on camera and audio is clean"
3. **Place × visual quality**: `asset_place` × `shot` × `shot_quality` → "in-focus, not-setup b-roll at Jenny Lake"
4. **Caption × OCR × transcript**: `dense_caption` × `frame_text` × `segment` → "shots where VLM says X, text on screen says Y, someone is saying Z"
5. **Bib × person × in-focus**: `bib_hit` × `frame_face` × `shot_quality` → "bib N visible AND athlete P on camera AND in focus"
6. **Comedy × on-camera × audio**: enrichment `moments.comedy` × `frame_face` × `audio_quality` → "funny moments where the laughter is on camera and audio captures it"

---

## Cardinality reference

| Layer | Row count |
|---|---|
| `asset` | ~7.2K |
| `segment` | ~280K |
| `person_appearance` | ~129K |
| `asset_semantic_chunk` | ~6.7K |
| `asset_semantic_key_moment` | ~21K |
| `asset_place` | ~22K |
| `frame_face` | ~45K |
| `shot` | ~12K |
| `shot_quality` | ~12K |
| `frame_text` | ~54K |
| `bib_hit` | ~1.8K |
| `audio_quality` | ~4.7K |
| `audio_event` | ~140K |
| `dense_caption` | ~26K |
| `still_aesthetic` | 729 |

---

## What's NOT in this schema (deliberate)

- **Sub-frame ML internals**: face embeddings vectors, SigLIP frame vectors, MiniLM windows. Use the existing single-layer helpers (`find_visually_similar*`, `find_similar_transcript_windows`) when you need vector similarity. The composer can call those and merge with SQL filters.
- **xmeml / sidecar state**: that's an editor-side concern; queries surface candidates and the editor places them via `editor/sidecar_cut_eval.py` + xmeml or Premiere MCP.
- **Pricing / cost**: not editorial.
