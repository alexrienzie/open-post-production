# `_scripts/`
*Cross-domain utilities for the dataset layer: rebuild, registries, transcripts, links, audits.*

> **Heads-up:** many pipeline steps here execute immediately when invoked: no `--help`, no dry-run flag. Read the script's docstring before running it, and prefer the documented `cmd` wrappers. Also: `<placeholder>` strings throughout these scripts mark corpus-specific folder / shoot / subject names; substitute your own (see `dataset_README.md` § Conventions).

Prefer **`rebuild_all.cmd`** for a full refresh (orgs dedup -> entity backfill -> indexes/MANIFEST -> `editorial_catalog.sqlite` -> `STATS.json`).

## Primary entry points

| Script | Role |
|--------|------|
| `rebuild_all.cmd` | Full Windows rebuild (five steps). |
| `clean_fuse_hidden.cmd` | Delete `.fuse_hidden*` shadow files from `indexes/` (bindfs/FUSE cleanup; runs as STEP 0 of `rebuild_all.cmd`). |
| `refresh_indexes.py` | `build_indexes.py` + `build_editor_db.py` only (no STATS; faster). |
| `build_indexes.py` | `MANIFEST.json`. |
| `build_editor_db.py` | `../indexes/editorial_catalog.sqlite`: projects catalog JSON + binary stores into the query surface. |
| `build_stats.py` | Root `STATS.json` (run after `build_indexes.py`). |
| `sync_has_machine_transcript.py` | Set `machine_transcript` on video/audio from transcript file presence (run after new `*.transcript.json` land). |
| `sync_embeddings_flags_from_db.py` | Set `embeddings.{semantic, vector}` from `clip_and_still_embeddings.sqlite` (all catalog kinds). |
| `extract_semantic_summaries_to_catalog.py` | Copy video-model JSON from embeddings DB -> catalog `asset_semantic_summary` (before slim). |
| `slim_clip_semantics_text.py` | NULL the model-text columns in the embeddings DB after extract (vectors kept). |
| `registries/normalize_asset_places.py` | Match `setting.location` + transcript -> `place_ids[]`. |
| `transcripts/embed_transcript_rolling_windows.py` | `../indexes/transcript_rolling_embeddings.sqlite` (optional; local `sentence-transformers`). |
| `workspace_paths.py` | Shared path resolution (workspace root, `resolve_proxy_path`). Imported by most scripts here. |

## Registry & entity maintenance

`registries/build_orgs_registry.py`, `registries/dedup_orgs.py`, `registries/backfill_entity_ids.py`, `registries/scan_people.py`, `registries/build_locations_registry.py`, `registries/curate_locations_registry.py`, `registries/apply_location_hierarchy.py`, `registries/enrich_location_parents.py`, `registries/place_matcher.py`, `registries/print_locations_tree.py`, `registries/reconcile_unmatched_people_registry.py`, `registries/propose_entity_promotions.py` / `registries/apply_entity_promotion_decisions.py` / `registries/build_exact_entity_match_decisions.py`, `registries/export_fuzzy_unmatched_people_csv.py`.

## Human-reference ground truth (optional)

If you have human-made reference transcripts for a slice of the corpus, three scripts implement the gold-eval loop described in [`transcripts/transcripts_README.md`](transcripts/transcripts_README.md): `transcripts/resolve_speakers_from_human_transcripts.py` (set `segments[].speaker` from the references), `transcripts/report_human_machine_speaker_alignment.py` (coverage report), and `transcripts/review_speaker_accuracy.py` (machine-vs-human accuracy). `transcripts/backfill_no_diar_speakers_from_human_clips.py` covers assets where diarization came up empty.

**Human <-> machine / linkage helpers:** `human_link_components.py` (shared manifest/link graph), `links/build_link_edge_tiers.py`, `links/audit_catalog_link_symmetry.py` (reverse-link audit; optional `--fix-reverse-audio`), `links/export_link_review_queue.py`.

## Transcripts & speakers (machine)

Runbook: [`transcripts/transcripts_README.md`](transcripts/transcripts_README.md) (LLM analysis pass + speaker resolution).

`transcripts/backfill_speakers_rollup_from_segments.py`, `transcripts/backfill_no_diar_speakers_from_human_clips.py`, `transcripts/propagate_speakers_via_linked_transcripts.py`, `transcripts/reserve_transcript_slots.py`, `transcripts/transcript_progress.py`, `transcripts/transcript_provenance.py`, `transcripts/build_transcript_prompt_context.py`, `transcripts/add_speaker_confidence.py`, `transcripts/resolve_speakers_from_human_transcripts.py`, `transcripts/build_speaker_review_pack.py`, `transcripts/review_speaker_accuracy.py`, `transcripts/sync_transcript_dates_from_assets.py`.

**Cross-modal links:** `links/audit_audio_video_transcript_links.py`, `links/propose_audio_video_links_by_transcript.py` (cross-kind audio->video), `links/propose_same_kind_links_by_transcript.py` (video<->video, audio<->audio co-recording pairs), `links/rank_audio_video_link_candidates.py`, `links/scan_audio_video_folder_snippet_matches.py`, `links/apply_folder_av_snippet_and_transcript.py`.

## Audits, QA & reports

`reports/audit_catalog_freshness.py`, `reports/comprehensive_audit.py`, `reports/verify_catalog_primary_timeline_canonical.py`, `transcripts/report_transcript_analysis_*.py` (freshness, consistency, substantive), `reports/count_assets_by_source_folder.py`, `transcripts/validate_transcript_analysis.py`, `transcripts/validate_transcript_cleanup.py`, `transcripts/find_correction_candidates.py`, `reports/filter_raid_inventory.py`, `catalog/backfill_asset_bucket3.py`.

## LLM / batch runners

**Primary:** `transcripts/run_transcript_analysis_via_gemini.py` (Gemini API batch analysis over transcripts).

Supporting: `prep_for_transcript_analysis.cmd` (pre-flight), `transcripts/build_transcript_prompt_context.py` (generates the prompts from live registries), `transcripts/restamp_prompt_sha.py`, `transcripts/run_transcript_cleanup_skeleton.py`, `transcripts/run_cleanup_deterministic.py`, `transcripts/extract_podcast_relevance_windows.py`.

## Misc

`backup_workspace.cmd` / `.ps1` (tar.gz the workspace), `duckdb_query.py`, `semantic_catalog.py`, `apply_sqlite_perf_indexes.py`, `append_editor_note.py`, `ffmpeg_timeline_first_n_copy.py`, `backfill_*` (dates, audio-extract metadata, timecode), `catalog/index_missing_assets_with_locations.py`.

Domain-specific ingestion under `documents/<domain>/scripts/` is intentionally colocated with each corpus.

## Inference-heavy runners (transcribe / proxies / labeling / enrichment)

These want a capable machine (GPU or Apple Silicon); everything else in `_scripts/` runs anywhere. We ran them on an M4 Max; the platform notes below reflect that, but each tool has cross-platform equivalents (see `DESIGN.md` § Portability).

### WAV extract + transcribe

| Script | Step | Output |
|---|---|---|
| `extraction/verify_ssd_match.py` | catalog ↔ SSD reconcile | `verify_ssd_match_report.json` (this dir) |
| `extraction/extract_audio.py` | WAV extraction | `derivative media/<shoot>/<stem>.wav` |
| `extraction/extract_audio_from_proxy.py` | R3D: AAC pulled from H.264 proxy | same per-shoot layout |
| `extraction/transcribe_all.py` | MacWhisper CLI orchestrator | MacWhisper SQLite |
| `extraction/build_transcripts.py` | MacWhisper SQLite → per-asset v5 JSON | `derivative media/_transcript staging/<aid>.transcript.json` (staging; promote to canonical in stage 4) |

### Proxies

| Script | Step | Output |
|---|---|---|
| `extraction/make_proxies.py` | H.264 proxy encode (`CMD_HASH` `db669c79afc9b0d3…`) | `derivative media/<shoot>/<filename>` (`.MP4`/`.MOV` filename preserved) |
| `extraction/transcode_r3d.py` | R3D: REDline → ProRes 422 Proxy → H.264 | `derivative media/<shoot>/.../<stem>.mp4` (`.R3D` → `.mp4` lowercase) |

### Video labeling + SigLIP vectoring

Under [`production_run/`](production_run/):

| Script | Step | Notes |
|---|---|---|
| `label_videos_vertex.py` | Vertex AI Gemini 2.5 Pro (default backend) | reads `indexes/clip_and_still_embeddings.sqlite`; resolves proxies via asset_map |
| `label_videos_aistudio.py` | AI Studio Gemini (fallback / cleanup) | same DB + asset_map flow |
| `siglip_embed_keyframes.py` | SigLIP-So400m on M4 Max MPS | keyframes staged at `derivative media/_siglip keyframes/` |
| `production_run/build_delta_manifest.py` | delta manifest builder | filters by `resolve_proxy_via_asset_map()` |
| `production_run/build_stills_run.py` | helper: stills coverage walk + DB load | reads `dataset/assets/stills/` |

### Helper module

[`_paths.py`](_paths.py): single source of truth for filesystem locations + per-asset path resolvers. Every script in this directory imports from it; update one constant here and every runner picks up the change. Public surface:

- `WORKSPACE_ROOT`, `DATASET_ROOT`, `INDEXES_DIR`, `DERIVATIVE_MEDIA`, `RUNS_DIR`: root paths
- `VIDEO_CATALOG`, `AUDIO_CATALOG`, `STILLS_CATALOG`, `TRANSCRIPT_CATALOG`: catalog dirs
- `EMBEDDINGS_DB`: `clip_and_still_embeddings.sqlite`
- `ASSET_MAP`, `TRANSCRIPT_STAGING`, `PROXY_CHUNKS_DIR`, `SIGLIP_KEYFRAMES_DIR`
- `proxy_output_path(record)` / `wav_output_path(record)`: per-asset derivation from `source_path`
- `workspace_tilde(path)`: render abs path as `~/<relative>` for catalog writes
- `resolve_proxy_via_asset_map(asset_id, kind)`: look up an existing proxy via `derivative media/_index/asset_map.json`
- `transcript_staging_path(asset_id)`: flat staging path for new transcripts
- `iter_catalog_jsons(catalog_dir, suffix)`: exFAT-safe glob; skips macOS AppleDouble (`._*`) sidecars
- `open_sqlite_ro(path)`: open a sqlite in `?mode=ro`, fall back to rw if the workspace SSD's exFAT can't write the `-shm` file

### Maintenance script

[`extraction/rebuild_asset_map.py`](extraction/rebuild_asset_map.py): walks `dataset/assets/` and `derivative media/`, rebuilds `derivative media/_index/asset_map.json`. Default behavior **merges** rediscovered entries into the existing map (preserves history); `--replace` does a clean rebuild. Run after any ingest that produces new proxies / WAVs / stills.

### Editorial enrichment layers

Each lives in its own subdir. Non-binary outputs land as per-asset catalog-JSON fields; binary layers (face embeddings, FAISS, fingerprints, audio events) keep a store under `indexes/`. Everything is projected into `editorial_catalog.sqlite` by [`build_editor_db.py`](build_editor_db.py).

| Subdir | What it produces |
|---|---|
| `faces/` | Per-frame face detections + clustering + name labels → `editorial_catalog.sqlite::frame_face` |
| `shots/` | Per-asset shot boundaries via PySceneDetect → `shot` table |
| `ocr/` | Per-frame OCR text (RapidOCR + Apple Vision) → `frame_text`, `bib_hit`, `shot_text` view |
| `quality/` | Per-shot sharpness / motion / exposure / setup-flag (`quality/build_shot_quality.py`) + NIMA aesthetic 1-10 score (`quality/build_aesthetic.py`, recalibrated to corpus p85 = 4.05) → `shot_quality` |
| `audio_quality/` | Per-asset RMS / peak / clipping / silence / wind flags over WAV extracts → `audio_quality` |
| `faiss_index/` | HNSW visual-similarity index over 116K SigLIP keyframe vectors → `indexes/clip_embeddings.faiss` + meta |
| `audio_events/` | Timed audio event tags via dual-engine CLAP (LAION-CLAP + MS-CLAP), shot-aware sampling, vocab-cleaned twice (pilot + post-QA). 139,841 events across 3,578 assets in `audio_event` table. |
| `audio_fingerprint/` | Cross-modal audio↔video linker via chromaprint. Three phases: per-WAV uint32 hashes → path-prefiltered pairwise match → catalog `linked_assets` write with `established_by: chromaprint_pairwise_match`. 85 chromaprint-novel links applied (90% of high-conf proposals were missed by the existing PC-side snippet/transcript pipelines). |

### Standalone dataset-side primitives

Helpers that don't have their own subdir / canonical store; they're called from editor or other scripts:

| Module | What it does |
|---|---|
| [`siglip_cut_delta.py`](siglip_cut_delta.py) | `SigLIPCutIndex` + `compute_cut_delta(asset_out, ts_out, asset_in, ts_in)`: cosine distance between nearest SigLIP keyframes on each side of a cut. Loads `indexes/clip_embeddings.faiss` once + bisect-indexes the meta map for O(log N) lookups. Pair with editor's `sidecar_cut_eval.py` to surface visually jarring cuts alongside mid-word flags. |

### Cross-layer QA (`qa/` subdir)

Sanity-check runners that surface inconsistencies between enrichment layers. Outputs land at `dataset/_runs/qa/<ts>/`. Run any time after a layer rebuilds.

| Runner | Cost | What it checks |
|---|---|---|
| [`qa/run_layer_qa.py`](qa/run_layer_qa.py) `all` | Free (pure SQL) | Cross-layer coverage gaps, shots ↔ Gemini key_moment alignment, CLAP ↔ audio_quality contradictions, CLAP music/crowd vs interview-type assets, chromaprint applied_link shoot-context sanity, face cluster sizes / outliers, OCR pseudo-word noise candidates |
| [`qa/llm_qa.py`](qa/llm_qa.py) `all` | ~$1.30 with Gemini 2.5 Pro across the apply-ready set | OCR ↔ Gemini scene consistency, face cluster ↔ Gemini subject sanity (catches contamination), chromaprint apply ↔ Gemini scene plausibility. **Important:** prompts must inject project-specific hardware + people context (DJI Mic 2 ≠ drone, RED .RDC audio = production source, nickname ↔ canonical name maps for your cast); without it generic LLM intuition systematically generates 20-30% false-positive flags. See chromaprint prompt in `qa/llm_qa.py` for the working pattern. |

### Frozen archives (read-only baselines)

Stored under `derivative media/` so they survive workspace copies. Treated as read-only references: future pipelines may diff against or revert from them, but should not modify them in place.

| Path | What | Source / when |
|---|---|---|
| `derivative media/_original whisper transcripts/` | **4,470** v5 transcript JSONs exactly as they came off the transcription pass, before any later catalog-side edits. Baseline for LLM-direct-edit passes against the canonical transcripts in `dataset/assets/transcripts/`. | See `_README.md` inside the dir for provenance + diff vs canonical. |

## Dependencies (workspace-level)

Shared deps across the inference runners.

### Python environment

A Python 3.9+ environment with: `torch` 2.8.0, `torchvision` 0.23.0, `torchaudio` 2.8.0, `transformers` 4.57.6, `onnxruntime` 1.19.2, `numpy` 1.26.4, `opencv-python`, `pillow`, `scikit-learn`, `hdbscan`, `einops`, `timm`, `faiss-cpu` 1.13.0.

Added during the enrichment-layer push:
- `pyiqa` 0.1.15+ (NIMA aesthetic, shot-quality extension)
- `laion-clap`, `msclap`, `librosa` 0.10.2 (CLAP audio events)
- `pyacoustid` (chromaprint Python bindings)
- `pydantic` (video-label response schemas), `soundfile` (audio events), `pillow_heif` (HEIC stills), `fastembed` (podcast windows), `rapidfuzz` (people scan)
- `google-genai` (Gemini 2.5 SDK: video labeling, QA passes, dense captions)

### System binaries (Homebrew)

| Binary | Used by | Notes |
|---|---|---|
| `/opt/homebrew/bin/ffmpeg` | every enrichment layer that decodes media (faces, shots, OCR, quality, audio_quality, audio_events, audio_fingerprint, NIMA, dense_captions) | Apple Silicon Homebrew; current version handles all your proxy + WAV formats. |
| `/opt/homebrew/bin/ffprobe` | duration probes (audio_quality, dense_captions) | Shipped alongside ffmpeg. |
| `/opt/homebrew/bin/fpcalc` (chromaprint) | audio_fingerprint | `brew install chromaprint`. Provides the audio fingerprint CLI. |
| MacWhisper Large v3 Turbo | transcription | GUI app + CLI; model bundled in app. |
| REDline CLI (REDCINE-X PRO) | `.R3D` proxy transcode | Free download from RED. |

### Model checkpoints (cached to disk after first use)

| Model | Where cached | Size | Used by | First-run cost |
|---|---|---:|---|---|
| InsightFace `buffalo_l` (SCRFD + ArcFace R100) | `~/.insightface/models/` | ~250 MB | faces | one-time download |
| SigLIP-So400m (`google/siglip-so400m-patch14-384`) | `~/.cache/huggingface/hub/` | ~3.5 GB | SigLIP keyframe vectoring, FAISS `query-image`, dense_captions (via editor's SigLIPEncoder) | one-time |
| LAION-CLAP `630k-best.pt` | `~/.cache/huggingface/hub/` + LAION-CLAP cache | ~2.3 GB | audio_events | one-time |
| MS-CLAP-2023 | `~/.cache/huggingface/hub/` | ~340 MB | audio_events | one-time |
| Roberta-base + WaveLM (CLAP deps) | `~/.cache/huggingface/hub/` | ~1.5 GB combined | audio_events deps | one-time |
| NIMA-InceptionV2 (pyiqa) | `~/.cache/torch/hub/pyiqa/` | ~208 MB | shot-quality aesthetic | one-time |
| Florence-2-large | `~/.cache/huggingface/hub/` | ~1.5 GB | dense_captions (pilot only; not production winner) | one-time |
| MiniCPM-V 2.6 | `~/.cache/huggingface/hub/` | ~16 GB | dense_captions (pilot only) | one-time, slowest |
| All ffmpeg-bundled audio codecs | system | - | universal | - |

### External APIs + credentials

| Service | Credential | Used by |
|---|---|---|
| Google AI Studio (Gemini 2.5 Flash + Pro) | `GEMINI_API_KEY` (exported in `~/.zshrc`) | video-labeling AI-Studio fallback, QA passes (chromaprint LLM QA, OCR LLM verdict, face-vs-Gemini-names), dense_captions if Gemini engine selected |
| Google Cloud Vertex AI (Gemini 2.5 Pro) | Service account JSON via `GOOGLE_APPLICATION_CREDENTIALS` + `gcloud auth application-default login` | video labeling: production-run/label_videos_vertex.py |
| Google Cloud Storage | same Vertex SA (gs://your-gcs-bucket-*) | video-label uploads |

No other external services. Everything else is local inference or pure SQL.

## Layout convention

Derivative media on the workspace SSD mirrors the catalog `source_path` tree:

| Input (catalog `source_path`) | Output |
|---|---|
| `<RAID>\<project>\2024-9-15_<location>\C9059.MP4` | `derivative media/2024-9-15_<location>/C9059.MP4` (proxy), `…/C9059.wav` (WAV) |
| `<RAID>\<project>\<camera-folder>\DJI_20250910…D.MP4` | `derivative media/<camera-folder>/DJI_20250910…D.MP4` (proxy), `…D.wav` (WAV) |
| `<RAID>\<project>\2025-5-20_<location>\Red\<bundle>.RDC\…001.R3D` | `derivative media/2025-5-20_<location>/Red/<bundle>.RDC/…001.mp4` (proxy), `…001.wav` (WAV) |

Catalog `proxy.path` and `audio_extract.path` writes use the tilde form `~/derivative media/<shoot>/<filename>` (workspace-root tilde), consistent with the convention noted in [`CLAUDE.md`](../../CLAUDE.md) § "Proxy paths (the workspace)"; resolution flows through `workspace_paths.py::resolve_proxy_path()`.

**Transcripts stage flat** at `derivative media/_transcript staging/<aid>.transcript.json` (see INGEST.md stage 2). The operator promotes them into the canonical `dataset/assets/transcripts/` and runs entity backfill (stage 4).

## Post-run housekeeping

After an extraction run lands new proxies / WAVs, refresh `derivative media/_index/asset_map.json` so the model passes and enrichment layers can resolve the new files:

```bash
python3 dataset/_scripts/extraction/rebuild_asset_map.py
```

The default is **merge**: re-discovered entries are updated, existing entries that didn't resurface from disk are preserved (so historical references survive). Pass `--replace` for a full filesystem-only rebuild. `--dry-run` previews counts.

Production-run scripts (`production_run/*.py`) target `indexes/clip_and_still_embeddings.sqlite`. **Schema has evolved** since the original Gemini + SigLIP pass; validate the `semantic_chunks` / `clip_embeddings` table definitions match each script's assumptions before re-running. Those runners were last invoked during the original ingest; later schema migrations (e.g. the embeddings-DB slim) may have shifted column names.

## First full ingest: wall-time + learnings

The first end-to-end run of the full ingest pipeline was a **3-clip third-party news** delta-ingest (~7 min of source video). Real numbers + the gotchas the test surfaced:

| Step | Wall-time |
|---|---|
| Index + hash 14 candidates (`/tmp/ingest_missing.py`, one-off) | 30 s |
| WAV extract (direct ffmpeg, RAID source) | 30 s for 3 |
| Proxy encode (direct ffmpeg) | 25 s for 3 |
| `extraction/transcribe_all.py` (incl. one cold-start retry) | ~13 min |
| `gemini-describe --workers 3` (3.5 Pro) | 2 min • $0.18 |
| `extract_semantic_summaries_to_catalog.py` | 5 s |
| SigLIP `extract-keyframes` + `embed-keyframes` | 10 s |
| editorial_catalog.sqlite rebuild (gate before enrichment-layer reads) | 1 min |
| Enrichment: shots, shot_quality, NIMA aesthetic, audio_quality, audio_events, audio_fingerprint, faces, FAISS | ~3 min combined |
| editorial_catalog.sqlite rebuild (so OCR + dense_captions see new shots) | 1 min |
| Enrichment: OCR + dense_captions | ~1.5 min |
| editorial_catalog.sqlite rebuild (final) | 1 min |

**End-to-end:** ~25 min for 3 short third-party clips. For a typical shoot day (1–3 hr footage), project ~30–60 min total; transcription and the three editor-DB rebuilds dominate.

### Process improvements landed from this run

| Problem | Fix |
|---|---|
| MacWhisper's `originalFilename` = WAV basename (not asset_id), so `extraction/build_transcripts.py`'s 64-char filter silently skipped 3 new sessions | `extraction/transcribe_all.py` now symlinks the WAV at `/tmp/<asset_id>.wav` before invoking `mw transcribe`. MacWhisper stores `<asset_id>` as `originalFilename`; the filter passes. |
| Cold-start `mw transcribe` timed out on the first 86 s clip (60 s timeout floor) | Bumped the floor to 120 s in `transcribe_one()`. |
| macOS exFAT AppleDouble (`._*`) sidecars in catalog dirs broke `json.loads()` on 12+ globbers | All catalog-glob loops now skip `._*` names. Helper at `_paths.py::iter_catalog_jsons()`. |
| Cross-layer reads via `?mode=ro` failed with "disk I/O error" on the workspace SSD's exFAT volume | Helper at `_paths.py::open_sqlite_ro()` probes with a sqlite_master query, falls back to rw if the ro path can't write its `-shm`. Applied to `audio_events/build_audio_events.py` and `quality/build_aesthetic.py`. |
| `asset_map.json` had no automated rebuild path; required a manual JSON injection for new ingests | New `extraction/rebuild_asset_map.py` walks the filesystem and merges with the existing map. See Post-run housekeeping above. |

### Dependency ordering (the non-obvious bits)

The enrichment layers and the editor DB form a 3-stage cycle, **not** a single batch:

1. **Build shots first** (depends on proxy + catalog only).
2. **Rebuild editor DB**. This is what makes `ec.shot` visible to the next phase.
3. **Run OCR + dense_captions + shot_quality + NIMA** (all read `ec.shot`).
4. **Rebuild editor DB** again to fold the new rows in.
5. **Run faiss_index `build`** to include the new SigLIP vectors.
6. **Run faces video pass** (reads SigLIP keyframes, writes face_embeddings.sqlite).
7. **Final editor DB rebuild** to project everything to `editorial_catalog.sqlite`.

Faces only appear in `editorial_catalog.frame_face` after a separate `cluster + label` pass (because that table only includes **named** detections). Cluster + label is best batched across multiple shoots.

### Pre-ingest discipline: mirror new content to a backup SSD first

The standard runners (`extraction/extract_audio.py`, `extraction/make_proxies.py`) read source files via `extraction/verify_ssd_match.py`, which scans the **backup SSD tree** (the mirror volumes), not the RAID master. This is intentional: the RAID is the master, the SSDs are the working mirrors, and extraction runs against the mirrors so a RAID disconnect doesn't strand mid-pipeline jobs.

**If a file is only on the master RAID, it's a state to fix, not a script gap.** Copy the folder to at least one backup SSD before ingest, then re-run `extraction/verify_ssd_match.py` and the runners pick it up normally.

A delta-ingest hit this because new clips landed straight on the RAID without a backup-SSD copy. A direct-ffmpeg one-off (a `/tmp` script) worked as a one-time bypass, but the lasting fix is "mirror first."

Optional follow-up (not yet built): a `verify_ssd_match.py --check-master` mode that walks the master volume too and reports any catalog asset whose source file exists on the master but **not** on any backup SSD: a pre-ingest gap report so the mirror step doesn't get forgotten.

### Other limitations not yet fixed

- The editor DB rebuilds in step 2/4/7 of the dependency cycle above could be wrapped in a single `ingest_orchestrator.py` that takes a list of new asset_ids and walks the cycle automatically.

## Moved out

Editor-facing query scripts live in `editor/queries/` (read-only consumers of indexes/dataset, not dataset writers): `beat_coverage_report.py`, `beat_select_ranker.py`. See `editor/queries/queries_README.md` for the dataset-vs-queries boundary.
