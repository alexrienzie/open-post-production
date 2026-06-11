# Dataset
*The source-of-truth layer: per-asset catalog JSON, entity registries, documents, and the scripts that maintain them.*

## Quick orientation

**`MANIFEST.json`** at the root is the machine-readable index of everything (generated). **`dataset_SCHEMA.md`** is the human-readable companion with cross-references and conventions. **`editorial_catalog.sqlite`** lives under the workspace **`indexes/`** folder (sibling of this `dataset/`); it is the denormalized SQL surface for the LLM-driven editor. **`_scripts/rebuild_all.cmd`** rebuilds derived artifacts end-to-end (Windows): org dedup, entity backfill, indexes + MANIFEST, editor DB, and **`STATS.json`**.

Workspace version is tracked in `MANIFEST.json` / `STATS.json` (`generated_at` carries the last full-rebuild timestamp).

## Source of truth: read vs write

> **This folder (`dataset/`) is the source of truth.** Per-asset JSON under `assets/{video,audio,stills,transcripts}/` and the canonical registries (`people/`, `organizations/`, `places/`) are the records that everything downstream derives from.
>
> - **Updating state** → edit records here, then run a rebuild script (see "How to run things" below).
> - **Querying state** → use the **SQLite files under `../indexes/`** instead of scanning the per-asset JSON. The SQLite surface is denormalized for fast joins (assets × people × transcript segments). See `../indexes/indexes_README.md`.
>
> Direct edits to `../indexes/*.sqlite` are **not** the right move; they get lost on the next rebuild, since the SQLite is regenerated from this folder. If a flag or field looks wrong in SQLite, fix the JSON record here and rerun `_scripts/build_editor_db.py` (or `_scripts/rebuild_all.cmd` for the full pass).

## Third-party handoff: underlying data to delete or withhold

Before handing a corpus like this (or anything derived from it) to a third party, treat the catalog as a **map over sensitive masters**, not as an automatic clearance list. Plan explicit deletion, exclusion from sync bundles, or **redaction** of underlying sources and derived JSON where the editorial release does not cover them. The categories below are a working checklist and are **not exhaustive**; add rows as new sensitivities surface.

| Category | What to remove, exclude, or redact | Notes |
| --- | --- | --- |
| **Non-public documents** | Anything beyond the public docket and approved editorial material | Retain **public court filings** only where they are meant to travel; treat everything else as **withhold by default**. |
| **Transcripts** | Full transcript JSON and linked audio/video where segments are sensitive | Off-the-record calls, personal conversations, and similar may require **segment- or file-level redaction** (or withholding of the parent asset), not just trusting downstream story edits. |
| **Off-the-record media** | Camera rolls with non-releasable context | Some rolls mix releasable footage with **off-the-record calls or conversations**; exclude or trim at source before any third-party copy leaves trusted storage. |
| **Potentially more** | Archives, server mirrors, semantic DBs, NLE project extracts, phone dumps | Anything that can **rehydrate** withheld material (e.g. `_archive/`, `../indexes/*.sqlite`, extracted audio, proxy trees keyed to sensitive sources) needs the same policy pass as the primary catalog JSON. |

## Structure

> This repo ships **sample slices** of each domain so the structures are tangible; production counts are noted for scale. Runtime / cold-storage folders (`_archive/`, `_audit/`, `_runs/`, `_review_drafts/`) are created as you work. `MANIFEST.json`, `STATS.json`, and `_prompts/transcript_analysis_prompt.md` ship as **sample builds** generated from the sample data; regenerate with `_scripts/build_indexes.py` + `build_stats.py` and `_scripts/transcripts/build_transcript_prompt_context.py`.

| Path | Purpose |
| --- | --- |
| `assets/` | Per-asset JSONs: `video/{id}.video.json`, `stills/{id}.still.json`, `transcripts/{id}.transcript.json`. Ships a small sample slice; production carried ~5.6K video, 412 audio, 1.2K stills, and 4.4K transcripts cataloged over an 18 TB RAID. |
| `people/people.json` | Canonical person registry: slug IDs (`p_*`), aliases, roles, relationships, name-resolution rules. Sample slice (the filmmakers + the film's subject); production carried ~280 people. |
| `organizations/orgs.json` | Canonical org registry: slug IDs (`o_*`), aliases, types. Sample slice (1 record); production carried ~220 orgs post-dedup. |
| `places/places.json` | Canonical places registry: slug IDs (`pl_*`), `type` taxonomy, `parent_id` containment chains. Sample slice (10 records); production carried ~550. |
| `story/moments.json` | Story-spine registry: narrative moments (`mom_*`) with themes/characters/settings; the vocabulary behind `moment_ids[]` tags. Sample slice (2 of 24 production moments). |
| `documents/case/` | Legal-side corpus: JSONL record index over extracted filing text. Sample slice (one public motion + brief); production carried ~98 public court filings. Anything non-public is **withheld by default**; see the handoff table above and `documents/case/case_README.md`. |
| `documents/press/` | Press catalog: per-article JSON with fetch/metadata blocks, entity ids, and LLM `analysis`. Sample slice (1 article, text redacted; publisher's copyright); production: 245 articles, 178 social posts, 3,906 comments. |
| `_lib/` | Shared Python helpers (classifications, date provenance, link edges). |
| `_scripts/` | Cross-domain utilities, organized by domain. See `_scripts/_scripts_README.md`. |
| `dataset_GAPS.md` | Running log of known gaps. Ships cleared; track your own. |
| `dataset_SCHEMA.md` | Human-readable schema reference with cross-reference graph. |

## Conventions

- **`<placeholder>` strings in code are corpus-specific values.** Classification and date-provenance heuristics (`_lib/asset_classifications.py`, `_lib/timeline_date.py`, `_scripts/extraction/verify_ssd_match.py`, and similar) match against folder, shoot, and subject names from the original corpus; those names ship as `<placeholder>` tokens. The logic around them is the reusable part: an adapting agent should inventory the new corpus's folder tree and substitute its own names (matching is against the lowercased source path). Searching the repo for `<` placeholders is the fastest way to find every site that needs adapting.
- **People IDs**: slug prefix `p_` (`p_alex_rienzie`). Defined in `people/people.json`.
- **Org IDs**: slug prefix `o_` (`o_the_north_face`). Defined in `organizations/orgs.json`.
- **Place IDs**: slug prefix `pl_` (`pl_grand_teton_national_park`). Defined in `places/places.json`.
- **Asset IDs**: Lowercase SHA-256 hex (64 chars), shared namespace across video/audio/stills/transcripts. Filenames use `{asset_id}.{kind}.json` to discriminate at a glance. The preimage is **not** (for large files) a full-file hash: `partial_hash()` is SHA-256 of **(first 1 MiB ‖ last 1 MiB ‖ `filesize_bytes` as big-endian u64)**; files **≤ 2 MiB** hash the **entire** file plus the trailing size suffix. Same function across all media kinds (see `INGEST.md` stage 1).
- **Schema version bumps** require: (a) write an idempotent `_scripts/migrate_<date>.py`, (b) bump the entry in `dataset_SCHEMA.md`, (c) run `_scripts/rebuild_all.cmd` to refresh indexes, `editorial_catalog.sqlite`, and `STATS.json`.

## Capture hardware (for accurate queries)

Catalog records carry a `path_metadata.camera_id` field, but it's a **single string** that can be misleading on its own. Two recurring patterns to know (production figures, kept for scale):

### `camera_id` distribution (video assets)

| `camera_id` | n_assets | What it is |
|---|---|---|
| `sony_c0xxx` / `c1xxx` / `c9xxx` / `c8xxx` | 2,029 / 721 / 503 / 235 | Sony cinema cameras (FX3, FX9, A7S-class): broadcast/interview rigs with shotgun or wired lavs |
| `unknown` | 918 | Not identified during indexing (often archival, screen-recorded, or third-party) |
| `iphone` | 454 | Handheld phone-dump (selfie / POV / casual) |
| `DJI_osmo` | 370 | **Mostly DJI Osmo Action / Osmo Pocket, handheld action cams.** Files `DJI_YYYYMMDDHHmmss_NNNN_D.MP4`. A small subset are drone-shot (see below). |
| `dji` | 212 video + many WAV-only | **DJI Mic 2 wireless lavalier system.** WAV filenames `DJI_NN_YYYYMMDD_HHMMSS.WAV`. Not a camera. |
| `red` | 58 | RED cinema (raw R3D) |
| `gopro` | 57 | GoPro Hero body-mount |
| `canon`, `sony_a7xxx`, `sony_c2xxx`, `sony_c7xxx` | <30 each | Misc / one-offs |

### Identifying drone footage: an example of idiosyncratic cleanup

> Every corpus accumulates quirks like this; the specifics below are ours and **will vary significantly by project**. What transfers is the pattern: when a single metadata field misleads, combine cheap cross-signals (folder labels, model tags, audio flags) into one query instead of re-tagging assets by hand.

A production corpus might have ~150–200 genuinely drone-shot videos (3–4% of the catalog), but `camera_id` couldn't separate them from the Osmo Action handhelds sharing the `DJI_osmo` id. The working filter combined four signals: shoot labels (ours were misspelled "Aeriel…", so the query matches `%erial%`), the travel-b-roll folder name, and the video-model's per-chunk `camera_movement` / `semantic_subject` tags (`drone`, `aerial`, `overhead`). Two corollaries: drone-ness turned out to be a **per-chunk property, not a per-folder one** (the handheld-DJI folder was only ~6% drone), and the `audio_quality.is_windy` flag catches both wind-on-mic *and* rotor noise (useful in the same filter, but don't read it as "this is a drone").

## How to run things

| Goal | Command |
| --- | --- |
| Full rebuild (dedup, backfill, indexes, editorial_catalog.sqlite, STATS) | `_scripts\rebuild_all.cmd` (Windows; double-click) |
| Just refresh MANIFEST | `python _scripts\build_indexes.py` |
| Copy video-model semantics into catalog JSON (`asset_semantic_summary`) | `python _scripts\extract_semantic_summaries_to_catalog.py` |
| Normalize `setting.location` → `place_ids` | `python _scripts\registries\normalize_asset_places.py` |
| Just refresh editorial_catalog.sqlite | `python _scripts\build_editor_db.py` |
| After video-label/SigLIP ingest: semantics → catalog → SQLite | `extract_semantic_summaries_to_catalog.py` then `build_editor_db.py` |
| Drop duplicate model JSON from embeddings DB (keep vectors) | `python _scripts/slim_clip_semantics_text.py --apply` (backup first; run after extract) |
| Just refresh STATS (after MANIFEST exists) | `python _scripts\build_stats.py` |
| MANIFEST + editor DB (no STATS) | `python _scripts\refresh_indexes.py` |
| After new transcripts land: sync `machine_transcript` + embedding flags, then refresh | `python _scripts\sync_has_machine_transcript.py` then `python _scripts\sync_embeddings_flags_from_db.py` then `python _scripts\refresh_indexes.py` and `python _scripts\build_stats.py` |
| Backfill people_ids/org_ids on one domain | `python _scripts\registries\backfill_entity_ids.py --domain transcripts` |
| Re-run audio→video link matching (cluster-aware) | `python _scripts\links\propose_audio_video_links_by_transcript.py --apply --apply-min-score 0.72 --apply-margin 0.12 --cluster-eps 0.02 --top-k 15` |
| Re-run same-kind co-recording matching (B-cam ↔ A-cam, dual recorders) | `python _scripts\links\propose_same_kind_links_by_transcript.py --apply --apply-min-score 0.55` |

All record-mutating scripts use atomic writes (`tmp.write_text` + `os.replace`); kill mid-write leaves originals intact.

## Storage tiers

| Tier | Path | Purpose |
| --- | --- | --- |
| RAID master | `<RAID>\<project>\` | Source-of-truth video, audio, stills |
| Catalog | `assets/` (this repo) | JSON catalogs over the RAID; cheap to query/edit |
| Editor working set | `../indexes/editorial_catalog.sqlite` (workspace sibling) | Denormalized SQL surface for the LLM editor |
| Embeddings + chunk registry | `../indexes/clip_and_still_embeddings.sqlite` (optional) | SigLIP vectors + `chunk_id` registry (text in catalog after extract/slim) |
| Cloud backup | provider of choice | Catalogs, registries, and docs are small files; back them up off-site |
