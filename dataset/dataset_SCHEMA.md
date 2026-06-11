# Schema reference

Single source of truth for what shape data takes across the project: the schema index + cross-reference. Field-level shapes are visible in the records themselves and in `MANIFEST.json`.

For programmatic consumers, prefer `MANIFEST.json` at the root; it has the same info in a queryable shape.

The workspace carries a date-coded version anchoring a coherent snapshot of records; per-domain integer schema versions track migration discipline.

## Domain schemas

Every domain carries an integer `schema_version` on each record, bumped per-domain by migration scripts (see Conventions below). Approximate production record counts are shown for scale; this repo ships sample slices; see each folder's README.

| Domain | Records (production scale) | Storage shape |
| --- | --- | --- |
| `assets/video/` | ~5.6K | per-asset JSON: `{id}.video.json` |
| `assets/audio/` | ~400 | per-asset JSON: `{id}.audio.json` |
| `assets/stills/` | ~1.2K | per-asset JSON: `{id}.still.json` |
| `assets/transcripts/` | ~4.4K | per-asset JSON: `{id}.transcript.json` |
| `people/people.json` | ~280 (ships 3) | single JSON document |
| `organizations/orgs.json` | ~220 (ships 1) | single JSON document |
| `places/places.json` | ~550 (ships 10) | single JSON document |
| `story/moments.json` | 24 moments (ships 2) | single JSON document |
| `documents/case/` | ~100 public filings (ships 2) | JSONL index + extracted text |
| `documents/press/articles/` | ~250 (ships 1, text redacted) | per-article JSON |
| `documents/press/comments/` (not shipped) | ~3.9K | per-comment JSON |
| `documents/press/social_posts/` (not shipped) | ~180 | per-post JSON |

## Clip and still embeddings (`clip_and_still_embeddings.sqlite`)

**SQLite sidecar** under the workspace **`indexes/`** directory. It is **not** built by `rebuild_all.cmd`; it is merged in when present (this repo ships a **sample build** covering the sample assets). After slim, it holds **SigLIP vectors** and a **chunk registry** only; editorial Gemini text is in catalog `asset_semantic_summary`.

| Layer | Tables | What it is |
| ----- | ------ | ----------- |
| **Chunk registry** | `semantic_chunks`, `semantic_stills` | Rows tie `chunk_id` → `parent_asset_id` / `asset_id` and ingest metadata. After slim, **no** `response_json`; text lives in catalog. |
| **Embeddings (visual)** | `clip_embeddings`, `still_embeddings` | **Float32 vectors** (e.g. SigLIP 1152-d) in `vector_blob`. Join via `chunk_id` → `semantic_chunks`. |

**Other binary stores under `indexes/`** follow the same pattern (canonical for the binary signal they carry; everything non-binary lives in catalog JSON): `transcript_rolling_embeddings.sqlite` (windowed text embeddings), `audio_events.sqlite` (CLAP tags), `audio_fingerprints.sqlite` (chromaprint hashes + link proposals), `face_embeddings.sqlite` (face vectors + clusters; not shipped, since cluster labels are personal data), and the FAISS visual-similarity index (rebuildable from the SigLIP vectors). Sample builds of the first three ship alongside the clip/still DB; see `../indexes/indexes_README.md` for full schemas.

**Why both:** Semantics answer “what is happening in words?” Embeddings answer “what looks or feels like this clip?” without storing a paragraph. They complement each other; neither replaces the other.

**`embeddings` on catalog JSON** is a short **`semantic` / `vector` presence** tally only (see cross-cutting table) for **video, audio, and stills** (same meaning across those kinds; compact keys save bytes). Transcript JSON omits this block; use the sibling **video / audio / still** record for the same `asset_id`. It does **not** store vector components or URIs: vectors remain in `clip_and_still_embeddings.sqlite` (`vector_blob`); text in catalog / `editorial_catalog`.

**Duplicate JSON on disk:** If the same Gemini output was also exported as loose `.json` files (e.g. next to proxies), that is **redundant** with `response_json` / `response_raw` in the DB once you treat the DB as canonical, **after** you verify backups and retention policy. Do **not** delete: per-record **`analysis`** on **transcripts** or **press** JSON (different pipeline), `_runs/*/log.jsonl` (operational logs, not full semantics), or anything still your only copy.


## Filename conventions

Asset records: `{asset_id}.{kind}.json` where `{kind}` is `video | audio | still | transcript`. `asset_id` is SHA-256 hex (64 chars), produced by indexer `partial_hash()`: **`sha256`(first 1 MiB ‖ last 1 MiB ‖ `filesize_bytes` as 8-byte big-endian)**, reading the whole file instead of head/tail when **size ≤ 2 MiB**. Video, audio, and stills share this same function (see `INGEST.md` stage 1). The kind discriminator means you can tell at a glance what kind of record you're looking at, and disambiguates if directories ever get merged.

Document records keep `{id}.json` since each lives in its own directory.


## ID conventions across domains


| ID type              | Format                          | Example                        | Stable across             | Used in                                                  |
| -------------------- | ------------------------------- | ------------------------------ | ------------------------- | -------------------------------------------------------- |
| `asset_id`           | sha256 hex (64 chars); preimage = `partial_hash()` (1 MiB head/tail + BE size; full file if ≤ 2 MiB) | `4cdbebbe...`                  | renames, copies, machines | video, audio, stills, transcripts                        |
| `p_<slug>`           | `p`_ + lowercase + underscores  | `p_alex_rienzie`               | renames                   | people, all `people_ids[]` cross-refs                    |
| `o_<slug>`           | `o`_ + lowercase + underscores  | `o_the_north_face`             | renames                   | orgs, all `org_ids[]` cross-refs                         |
| `pl_<slug>`          | `pl`_ + lowercase + underscores | `pl_grand_teton_national_park` | renames                   | places registry; `place_ids[]` cross-refs                |
| `article_id`         | sha256(canonical_url)[:32]      | `00200b9727...`                | URL canonicalization      | documents/press/articles                                 |
| `comment_id`         | sha256[:32]                     | `000e31b6...`                  | re-ingestion              | documents/press/comments                                 |
| Case-record IDs      | record-level keys per stream    | varies                         | -                         | documents/case/records                                   |


## Cross-cutting fields on every record

All asset and document records carry:


| Field                                        | Purpose                                                                                                                                                                       |
| -------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `schema_version: int`                        | Per-domain schema marker                                                                                                                                                      |
| `record_kind: str`                           | Top-level discriminator (`video`, `audio`, `article`, `text_message`, …). On **catalog** video / audio / still JSON, this is the only kind field (legacy `media_type` removed). |
| `primary_timeline_date: ISO 8601 date`       | Single canonical date for timeline filtering, hoisted from per-domain source fields. Format `YYYY-MM-DD`. Some sparse coverage (a few hundred records lack any date signal). |
| `date_source: str \| null` (catalog **video / audio / still**) | Provenance of `primary_timeline_date`: `source_path` (folder / `path_metadata.shoot_date` from layout), `camera_metadata` (ffprobe `creation_time` or stills EXIF `date_taken`, including when `shoot_date_source` is `ffprobe_creation_time`), `filesystem_metadata` (calendar day matches file `mtime` and does not match those stronger signals). `null` when `primary_timeline_date` is missing or source is ambiguous. See `_lib/timeline_date.py`. |
| `people_ids: [p_*]`                          | FK to people.json. **Not** on catalog video / audio / still; use the sibling **transcript** row. Elsewhere: heuristic-populated; confidence low.                               |
| `org_ids: [o_*]`                             | FK to orgs.json. **Not** on catalog video / audio / still; use **transcript** / press / events. Heuristic elsewhere; confidence low.                                         |
| `moment_ids: []`                             | **Transcripts** and other editorial records: narrative moment tags (`mom_*`), keyed to `story/moments.json` (ships a 2-moment sample; grow your own). **Not** on catalog video / audio / still. |
| `embeddings` flags | **Reconciliation flags** vs optional `clip_and_still_embeddings.sqlite`: `{ semantic, vector }` on **video, audio, still**. `semantic` = chunk/still registry row present; `vector` = SigLIP row present. Maintained by `_scripts/sync_embeddings_flags_from_db.py`. |


Asset-specific cross-cutting:

- `shoot_location: { place: str \| null, source: str \| null }` (video, audio, still): **Where the media was captured** (`place`: `pl_*` or free text; `source`: provenance such as `path`, `gps`, `manual`). Both start `null` on fresh rows. Written **immediately before** `location` in JSON key order. Normalized by `_scripts/registries/backfill_shoot_location_catalog_assets.py` and set on new rows in `_scripts/catalog/index_missing_assets_with_locations.py`.
- `location: object \| null` (video, audio, still): GPS / ISO6709-style payload from ffprobe tags or stills EXIF when present; not the same as editorial shoot place (`shoot_location`).
- `machine_transcript: bool` (video, audio): derived from existence of `assets/transcripts/{asset_id}.transcript.json` (maintained by `_scripts/sync_has_machine_transcript.py`)
- `human_transcript: bool` (video, audio): `true` when a human-made transcript exists for the asset (vs. ASR-only).
- `asset_classifications: { bucket: str, type: str }` (video, audio, still). **bucket**: same three values as legacy `asset_bucket` (`third_party` \| `in_house_priority_ht` \| `in_house_other`), from top-level source path (+ a priority flag for assets with human-made transcripts). **type**: path-derived editorial tag, `b_roll` \| `interview` \| `timelapse` \| `archival` \| `third_party` \| `verite` \| `court_recordings` (first matching rule in `_lib/asset_classifications.py`). Maintained by `_scripts/catalog/backfill_asset_bucket3.py` and set on new rows in `_scripts/catalog/index_missing_assets_with_locations.py`. Legacy `asset_bucket` is **not** written on video/audio after backfill. Stills also carry `asset_classifications` (same bucket + type) while retaining `asset_bucket`.
- `asset_bucket: str` (stills only): `third_party` \| `in_house_priority_ht` \| `in_house_other`; maintained by `_scripts/catalog/backfill_asset_bucket3.py` (duplicates `asset_classifications.bucket` on stills).
- `linked_assets: { video: [], audio: [], stills: [] }`: typed cross-refs on **all** catalog media rows (`video`, `audio`, `still`). Each array holds **edge objects** (not bare ids):

  ```json
  {
    "target_asset_id": "<64-char id>",
    "link_kind": "audio_video_transcript | audio_video_reverse | same_kind_video | same_kind_audio | still_to_video",
    "established_by": "<script name or migrate step>",
    "confidence": 0.0,
    "symmetric": true
  }
  ```

  Only `target_asset_id` and `link_kind` are required; `confidence` / `symmetric` are optional. **Bucket** = target modality (`video` / `audio` / `stills`), not source.

  | link_kind | Typical source record | Target bucket | Meaning |
  |-----------|------------------------|---------------|---------|
  | `audio_video_transcript` | audio | `video` | Primary A-cam (or chosen) video for this audio (transcript match). |
  | `audio_video_reverse` | video | `audio` | Audio row(s) whose primary video is this clip (mirror / review list). |
  | `same_kind_video` | video | `video` | Co-recording: another camera same scene (symmetric pair). |
  | `same_kind_audio` | audio | `audio` | Co-recording: another recorder same speech (symmetric pair). |
  | `still_to_video` | still | `video` | Frame / parent video link. |

  Maintained by `propose_audio_video_links_by_transcript.py` (`--apply`, `--sync-reverse-audio-links`), `propose_same_kind_links_by_transcript.py` (`--apply`).

  **Legacy (removed after migration):** `linked_video_asset_id`, `linked_audio_asset_ids[]`, `linked_video_asset_ids[]`.

## Cross-reference graph

```
people/people.json (p_*)              organizations/orgs.json (o_*)
   ↑                                       ↑
   │ referenced via people_ids[]           │ referenced via org_ids[]
   ↓                                       ↓
   ├── assets/video/{id}.video.json           .linked_assets{ video,audio,stills }  (no people/org/beat slots)
   ├── assets/audio/{id}.audio.json           .linked_assets{ … }  (no people/org/beat slots)
   ├── assets/stills/{id}.still.json          .linked_assets{ … }  (no people/org/beat slots)
   ├── assets/transcripts/{id}.transcript.json
   │     .people_ids[]   .org_ids[]   .speakers[].p_id   .moment_ids[]
   ├── documents/case/...                              .people_ids[] (TBD pass)
   ├── documents/press/articles/{id}.json              .people_ids[]   .org_ids[]   .moment_ids[]   .analysis.storylines[]
   ├── documents/press/comments/{id}.json              .people_ids[]   .org_ids[]   .parent.{kind,id}
   └── documents/press/social_posts/{id}.json          .people_ids[]   .org_ids[]   .moment_ids[]
```

## Population status (heuristic backfill, production figures)


| Catalog      | Records | with people_ids                                | with org_ids     |
| ------------ | ------- | ---------------------------------------------- | ---------------- |
| video        | ~5.6K   | 0 (covered via transcripts)                    | 0 (no body text) |
| transcripts  | ~3.9K   | ~2.2K                                          | ~2.2K           |
| articles     | ~250    | all                                            | 245              |
| comments     | ~3.9K   | ~2K                                         | ~2,000           |


Heuristic pass = whole-word match against canonical_name + aliases (skipping ambiguous first-name tokens). Confidence: low; a context-aware LLM pass raises it.

## Registry and timeline design patterns

The parts of the registry design that took real iteration:

- **Name-resolution rules for ambiguous names.** The people registry carries a `name_resolution_rules[]` array mapping ambiguous tokens to a default resolution plus contextual exceptions (a bare first name resolves to the film's main subject of that name unless context says otherwise). Entity backfill **skips ambiguous first-name tokens entirely** unless a rule covers them: a false-positive tag costs far more than a missed one.
- **Confidence tiers on registry entries and backfilled tags.** `high` = corroborated by multiple sources or court records; `medium` = 3+ independent mentions (or a reviewed merge); `low` = single mention or inference. Downstream consumers filter on the tier; audits start from `low`.
- **Registry meta-version vs. record schema version.** Registries track both the record `schema_version` (field shape) and a registry meta-version (curation state, bumped on dedup / merge passes). A dedup is a curation event, not a schema change.
- **Dedup keeps an explicit merge map.** Each registry dedup records winner-slug ← merged-slugs (aliases absorbed), so backfilled IDs migrate deterministically instead of being re-derived.
- **Place hierarchy via `parent_id`.** Places chain upward (trailhead → park → state → country), so place queries can roll up containment without a geo lookup.
- **Date precision on timeline events.** Events carry `date_precision` (`day | month | approx | unknown`) plus deterministic anchors for approximate dates ("early <month>" → the 1st, "late" → the 20th, "summer" → July 1); imprecise dates sort stably without overstating precision.

## Conventions

- All slugs: lowercase, alphanumeric + underscores only.
- All timestamps: ISO 8601 with timezone.
- All hashes: lowercase hex, no separators.
- All `schema_version` fields are integers, monotonically increasing per domain.
- Schema bumps require: (a) write an idempotent migration script in `_scripts/`, (b) bump the entry in this file, (c) run `build_indexes.py` then `build_stats.py` (both invoked from `rebuild_all.cmd`) so `MANIFEST.json` and `STATS.json` stay aligned.
- All record-mutating scripts use atomic writes.

## Top-level layout (current)

```
dataset/                          (under the workspace root)
├── MANIFEST.json                 machine-readable index of all catalogs/registries/indexes
├── dataset_SCHEMA.md             this file
├── STATS.json                    rolled-up stats (`build_stats.py`, after MANIFEST)
├── dataset_GAPS.md               running gaps log
│
├── assets/                       canonical per-asset JSON (video/, stills/, transcripts/; audio/ in production)
├── people/                       canonical person registry (p_*)
├── organizations/                canonical org registry (o_*)
├── places/                       canonical places registry (pl_*)
├── documents/                    case (+ press, texts in production; colocated `scripts/` per domain)
│
├── _lib/                         shared Python modules for scripts
├── _prompts/                     generated LLM prompt contexts (created at runtime)
├── _runs/, _archive/, _audit/, _review_drafts/   runtime + cold storage (created as you work)
└── _scripts/                     cross-domain utilities, organized by domain - see `_scripts/_scripts_README.md`
    ├── rebuild_all.cmd           dedup → backfill → build_indexes → build_editor_db → build_stats
    ├── refresh_indexes.py        build_indexes + build_editor_db (no STATS)
    ├── build_indexes.py          MANIFEST.json
    ├── build_editor_db.py        ../indexes/editorial_catalog.sqlite (workspace sibling of dataset/)
    └── build_stats.py            STATS.json (requires MANIFEST)
```

Full script inventory (migrations, audits, LLM runners) lives in **`_scripts/_scripts_README.md`** so this file stays a schema reference, not a file manifest.
