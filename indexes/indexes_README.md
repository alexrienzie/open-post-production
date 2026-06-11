# indexes/

Machine-generated derived stores (sibling of `dataset/`). Two kinds of file here:

1. **`editorial_catalog.sqlite`**: the **join surface**. Denormalized projection of catalog JSON + transcripts + the enrichment layers below. Built by `dataset/_scripts/build_editor_db.py` (typically via `rebuild_all.cmd` or `refresh_indexes.py`). Safe to delete and rebuild any time.

2. **Embedding / binary stores** (face vectors, SigLIP vectors, FAISS index, etc.): canonical for the binary signal they carry, because catalog JSON isn't a sensible home for high-dimensional float32 vectors at scale. Built by per-pipeline ML runners under `dataset/_scripts/`.

> **Read here, write to `dataset/`.** Catalog JSON under `../dataset/assets/{video,audio,stills}/` is the source of truth for all editorial signal (segments, shots, OCR, quality flags, captions, etc.). The SQLite files here are derived, generated from per-asset JSON for fast joinable queries. **Never write to `editorial_catalog.sqlite` by hand**: manual edits get lost on the next rebuild and break the assumption that `dataset/` is authoritative.

---

## Rebuild commands

```powershell
# Full editorial_catalog rebuild from catalog JSON state
python ..\dataset\_scripts\build_editor_db.py

# Same, but as part of the wider rebuild that also refreshes MANIFEST
python ..\dataset\_scripts\refresh_indexes.py

# Full rebuild including dedup, entity backfill (5 steps; idempotent)
..\dataset\_scripts\rebuild_all.cmd
```

---

## Current state

This repo ships **sample builds** of five stores (sliced to the sample assets) so the shapes are tangible; the rest are generated or rebuilt locally. Production figures are noted for scale.

| File | Ships as | Built by | Role |
|---|---|---|---|
| `editorial_catalog.sqlite` | sample build | `dataset/_scripts/build_editor_db.py` | **The join surface.** Catalog + transcripts + all enrichment layers projected into one queryable DB (production: ~400 MB, 25 tables). Rebuild after catalog JSON changes. |
| `clip_and_still_embeddings.sqlite` | sample build | SigLIP pipeline (`dataset/_scripts/production_run/`) | SigLIP frame/still vectors + the `chunk_id` registry (production: ~116K vectors / ~600 MB). |
| `transcript_rolling_embeddings.sqlite` | sample build | `dataset/_scripts/transcripts/embed_transcript_rolling_windows.py` | MiniLM-L6-v2 384-d rolling windows, 90s / 45s overlap (production: ~28K windows). |
| `audio_events.sqlite` | sample build | `dataset/_scripts/audio_events/build_audio_events.py` | Timed CLAP audio-event tags (production: ~140K events). Projects to `editorial_catalog.sqlite::audio_event`. |
| `audio_fingerprints.sqlite` | sample build | `dataset/_scripts/audio_fingerprint/build_audio_fingerprint.py` | Chromaprint blobs for video↔audio asset linking (camera-sync ↔ field-recorder). |
| `face_embeddings.sqlite` | **not shipped**: face vectors + cluster labels are personal data; schema documented below | `dataset/_scripts/faces/build_face_index.py` | Canonical face detections + HDBSCAN clusters (production: ~95K detections / ~445 MB). Named subset projects to `frame_face`. |
| `clip_embeddings.faiss` + `.meta.json` | **not shipped**: rebuilds in seconds from the clip DB | `dataset/_scripts/faiss_index/build_faiss.py build` | HNSW visual-similarity index over the SigLIP keyframes (~3.5s to build at 116K vectors). |

## `editorial_catalog.sqlite`: schema overview

**Built by:** `dataset/_scripts/build_editor_db.py` (typically via `rebuild_all.cmd` / `refresh_indexes.py`).

**What it is:** Denormalized **catalog** surface: assets (video/audio/still), transcript **segments**, speakers, people/org links, events, press rows, semantic child tables, **plus the enrichment layers projected from catalog JSON**.

**Core tables** (row counts are production-scale, for reference):

| Table | Rows | Purpose |
| --- | --- | --- |
| `asset` | ~7.2K | One row per source media file. Headline semantics: `semantic_location`, `semantic_subject`, `semantic_editorial_notes`. |
| `asset_semantic_chunk` | ~6.7K | Per-chunk video-model fields (setting, camera, audio, editorial notes, …). |
| `asset_semantic_key_moment` | ~21K | Timestamped `key_moments[]` for in-out proposals. |
| `asset_place` | ~22K | `pl_*` links from `normalize_asset_places.py`. |
| `segment` | ~280K | WhisperKit transcript segments. |
| `person_appearance` | ~129K | Denormalized person × segment lines. |
| `segment_fts` | ~280K | FTS5 full-text index over `segment.text` (for keyword search). |
| `v_asset_with_transcript` | (view) | `segment_count`, `speaker_count` per asset. |
| `v_asset_enriched` | (view) | Per-asset semantic chunk / key-moment / place counts + `place_ids` CSV. |

**Enrichment-layer tables (projected from catalog JSON via `load_<layer>()` in build_editor_db.py):**

| Table | Rows | Source on catalog JSON | Built by (inference runner) |
| --- | --- | --- | --- |
| `frame_face` | ~45K | `face_embeddings.sqlite` (binary; not in catalog JSON) | `faces/build_face_index.py` |
| `shot` | ~12K | `video.json["shots"]["items"]` | `shots/build_shots.py` |
| `frame_text` | ~54K | `video.json["ocr_detections"]["items"]` + `still.json[...]` (filtered) | `ocr/build_ocr.py` |
| `bib_hit` | ~1.8K | `video.json["bib_hits"]["items"]` + `still.json[...]` | `ocr/build_ocr.py` (derive-bibs pass) |
| `shot_text` | (view) | derived from `frame_text` | (built into build_editor_db.py) |
| `shot_quality` | ~12K | `video.json["shot_quality"]["items"]` | `quality/build_shot_quality.py` (sharpness/motion/exposure/clipping); `quality/build_aesthetic.py` (NIMA aesthetic_score) |
| `audio_quality` | ~4.7K | `video.json["audio_extract"]["audio_quality"]["metrics"]` (videos) + `audio.json["audio_quality"]["metrics"]` (audios) | `audio_quality/build_audio_quality.py` |
| `audio_event` | ~140K | `audio_events.sqlite` (binary; not in catalog JSON) | `audio_events/build_audio_events.py` |
| `dense_caption` | ~26K | `video.json["dense_captions"]["items"]` | `dense_captions/build_dense_captions.py` |
| `still_aesthetic` | ~730 | `still.json["still_aesthetic"]["metrics"]` | `quality/build_still_aesthetic.py` |

**Idempotency / re-running.** Each enrichment layer carries a `processed_at` timestamp (and per-frame trackers for layers that work per-frame) inside the catalog JSON. Re-running the builder picks up only un-processed assets / frames. To force a full re-run on an asset, hand-delete the `<layer>` key from its catalog JSON.

**Typical uses:** timeline-aware queries, `segment_fts` keyword search (FTS5), b-roll SQL on `asset_semantic_chunk + shot_quality` (usable-only filter), cross-modal joins (`frame_face × frame_text × shot` for "people on screen when text X appears"), LLM editor joins. Agent helpers: `editor/queries/retrieval.py` (`broll`, `search-transcript`, `similar-chunk`, `similar-transcript`). Rebuild FTS after catalog edits: `python dataset/_scripts/apply_sqlite_perf_indexes.py --catalog-only`.

**B-roll triage query**, illustrating how the enrichment layers compose:

```sql
SELECT s.asset_id, s.shot_idx, s.start_sec, s.duration_sec, a.semantic_subject
FROM shot s
JOIN asset a ON s.asset_id = a.asset_id
JOIN shot_quality sq ON sq.asset_id = s.asset_id AND sq.shot_idx = s.shot_idx
WHERE (a.semantic_location LIKE '%Jenny Lake%'
       OR EXISTS (SELECT 1 FROM frame_text ft
                  WHERE ft.asset_id = s.asset_id AND ft.shot_idx = s.shot_idx
                    AND ft.text LIKE '%Jenny Lake%'))
  AND sq.is_in_focus = 1
  AND sq.is_setup_or_teardown = 0
  AND sq.is_blown = 0
  AND s.duration_sec >= 2.0
ORDER BY s.duration_sec DESC, sq.sharpness_score DESC LIMIT 20;
```

---

## `clip_and_still_embeddings.sqlite`

**Built by:** the SigLIP + chunk-registry pipeline (optional; **not** from `rebuild_all.cmd`). **Ships as a sample build** covering the sample assets.

**Tables after `slim_clip_semantics_text.py`:**

1. **`clip_embeddings`** (~116K rows): SigLIP `vector_blob` every ~7s; join via `chunk_id`.
2. **`still_embeddings`** (~1K rows): one vector per still.
3. **`semantic_chunks` / `semantic_stills`**: registry rows (`chunk_id`, `parent_asset_id`, timing, ingest metadata).

**Join keys:** `parent_asset_id` / `asset_id` ↔ catalog; `chunk_id` ↔ `clip_embeddings`.

**Typical uses:** vector source for the FAISS index, embedding-flag sync, chunk registry for vector joins. **Not** for editorial text; use catalog JSON / `editorial_catalog`.

---

## `transcript_rolling_embeddings.sqlite`

**Built by:** `dataset/_scripts/transcripts/embed_transcript_rolling_windows.py` (optional; local `sentence-transformers`). **Ships as a sample build.**

**Tables:** `embedding_run`, `transcript_window_embedding` (384-d MiniLM, 90s / 45s overlap).

---

## `face_embeddings.sqlite`

**Built by:** `dataset/_scripts/faces/build_face_index.py`. **Not shipped**: face vectors and cluster labels are personal data; the schema is documented so you can build your own.

Canonical store for every face detection (production: ~95K rows) + HDBSCAN cluster assignments. The named subset (faces resolved to a `p_id` in `dataset/people/people.json`) projects to `editorial_catalog.sqlite::frame_face` honoring the consent posture chosen during face-index setup (named identities only on the editorial query surface). Unnamed detections remain in this file for clustering / debugging.

---

## `audio_events.sqlite`

**Built by:** `dataset/_scripts/audio_events/build_audio_events.py`. **Ships as a sample build.**

Canonical store for timed CLAP audio-event tags. Projects fully to `editorial_catalog.sqlite::audio_event`. Kept as a separate canonical store because the per-event metadata (window timing + score + engine) lives well in SQLite and there's no need to embed it into per-asset JSON.

---

## `audio_fingerprints.sqlite`

**Built by:** `dataset/_scripts/audio_fingerprint/build_audio_fingerprint.py`. **Ships as a sample build.**

Per-asset chromaprint fingerprints (binary blobs) used to propose video↔audio asset links (e.g., camera-sync video matching a field-recorder audio). The `link_proposal` table holds candidate matches. Not currently projected to `editorial_catalog`.

---

## `clip_embeddings.faiss` (visual similarity index)

**Built by:** `dataset/_scripts/faiss_index/build_faiss.py build` (~3.5s for 116K vectors). **Not shipped**: rebuild it locally from the clip DB.

HNSW (`IndexHNSWFlat`, `M=32`, `efConstruction=200`, inner-product metric) over every SigLIP keyframe vector. Built from `clip_and_still_embeddings.sqlite::clip_embeddings`. Sub-second top-K queries via standard FAISS Python API. Companion `clip_embeddings.faiss.meta.json` maps each index position back to `(embedding_pk, chunk_id, frame_idx, timestamp_sec, parent_asset_id, abs_time_sec)`.

**Query-time caveat.** Within a long single take, the top neighbors are dominated by other frames of the same asset (0.98+ cosine throughout). For cross-shoot similarity, exclude the source asset_id before re-ranking.

---

## Underused / latent capability

Ideas the data already supports but we never wired end-to-end; good first projects on top of the stack:

- **Dense vision pass** on long interview chunks (frame-level captions at 1 fps): heavy compute; we deferred it.
- **SigLIP-delta cut evaluation** for the editor sidecar (the `siglip_cut_delta.py` primitive exists; editor wiring doesn't).
- **Transcript reranker**: hybrid keyword + embedding rank with a cross-encoder on top.
- **Places taxonomy QA**: alias + override review against the places registry.
- **xmeml emitter straight from the sidecar** (skip the NLE round-trip for simple cuts).

## Known follow-ups

The following secondary readers / writers were **not** updated when the per-pipeline SQLite stores were retired. They'll fail loudly (`FileNotFoundError`) when next run and need to be migrated to catalog JSON:

- `dataset/_scripts/qa/run_layer_qa.py`: pure-SQL consistency checks
- `dataset/_scripts/reports/comprehensive_audit.py`: reads dense_captions

The two critical writers (`quality/build_aesthetic.py` for NIMA aesthetic scores and `qa/ocr_verdict_pass.py` for Gemini-Flash QA verdicts) **were** migrated in the same pass.

---

## SQLite WAL mode

If you see **`*.sqlite-wal`** and **`*.sqlite-shm`**, back up or copy all pieces together, or checkpoint with `PRAGMA wal_checkpoint(TRUNCATE)` while nothing else holds the file open.

## Junk files on virtiofs / Cursor mounts

**`.fuse_hidden*`**: not created by our scripts on purpose. They appear when a file under this folder is **deleted or replaced while still open** (typical when rebuilding `*.sqlite` on a FUSE-mounted workspace). Safe to delete if no DB build is running. Ignored via the repo `.gitignore`.

**`*.bak` / `*.bak_*`**: manual or agent backups from editing docs here; safe to delete.
