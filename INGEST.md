# Ingest
*From a camera card to a query-ready catalog: what to run, in what order, and where each artifact lands.*

The single entry point for new content. This is the **runbook**: what to run when, and where each artifact lands. For the *why* (rationale, alternatives, what's load-bearing vs. incidental), see the companion [`DESIGN.md`](DESIGN.md).

The work is **cost-ascending and iterative**: cheap deterministic extraction first, build and review the dataset, and spend on the expensive model passes last, then loop as the corpus grows. The corpus is never "done," only good enough for the cut you're making.

---

## The shape of it

```text
  1. Offload + index        cards -> RAID + backup; a content-hash asset_id per file
  2. Cheap local extraction audio + transcribe, 720p proxies, visual-similarity vectors
  3. Cheap enrichment       shots, faces, OCR, shot + audio quality, FAISS,
                            audio<->video links -- all free + local
  4. Build + iterate        promote transcripts; backfill places / people / orgs and
     the dataset            asset classifications. Human-in-the-loop; this is the loop
  5. Model passes (API)     frame captioning (cheap), then video-feed labeling
  6. Consolidate + rebuild  extract into the catalog; rebuild editorial_catalog.sqlite

Stages 3-5 loop. As the dataset improves you re-run enrichment; you re-clean
periodically as new footage lands.
```

**Entity enrichment is not push-button.** People, organizations, places, and asset classifications all took real human review and prompt iteration to make accurate and relevant; the registries were curated continuously, not generated in one pass. Budget for that review time; it is where corpus quality is won or lost.

---

## Where everything lives

The **per-asset JSON catalog** (`dataset/assets/{video,audio,stills,transcripts}/…`) is the source of truth. Anything that isn't a vector or other binary lands there as a per-asset field:

| In the catalog JSON (source of truth) | In `indexes/` (derived or binary) |
| --- | --- |
| `shots`, `shot_quality`, `ocr_detections`, `dense_captions` | SigLIP clip/still vectors + semantic-chunk registry (`clip_and_still_embeddings.sqlite`) |
| `asset_semantic_summary` (the video-model labels) | Face embeddings (`face_embeddings.sqlite`) |
| `asset_classifications`, `shoot_location`, `linked_assets` | FAISS visual-similarity index (`clip_embeddings.faiss`) |
| `proxy`, `embeddings` (presence flags), and the transcript records | transcript embeddings, audio fingerprints, audio events |

`editorial_catalog.sqlite` is a **derived query projection** over the JSON plus the binary indexes: rebuilt by a single step, never hand-edited. This repo ships a **sample build** (rebuild with `dataset/_scripts/build_editor_db.py` after catalog changes). **Rule of thumb: if it isn't a vector or other binary, it belongs in the catalog JSON;** the SQLite is a read-optimized view on top.

---

## Stages

### 1 · Offload + index: *cheap, deterministic*
Mirror each card to `<RAID>/<project>/<YYYY-MM-DD>_<location>_<subject>/` plus at least one verified backup, then walk the new tree and write a per-asset JSON keyed by a content-hash `asset_id`.

- **Hash.** `asset_id = sha256(head 1 MiB ‖ tail 1 MiB ‖ filesize_be8)`; whole file when ≤ 2 MiB. One function for video, audio, and stills, so the id is stable across re-offloads and survives filename reuse.
- **Output.** `dataset/assets/{video|audio|stills}/{aid}.{kind}.json`.
- **Upstream discipline matters most.** Consistent filename / folder / camera-id / slate conventions upstream save more downstream pain than any phase here; see [`DESIGN.md` § upstream disciplines](DESIGN.md).

### 2 · Cheap local extraction: *free, on-box*
All local, no external API. Safe to run in parallel.

- **Audio + transcribe**: extract WAV, run ASR (we used Whisper Large v3 Turbo) → a staged per-asset transcript JSON.
- **Proxies**: 720p H.264 proxies on the working SSD; catalog `proxy.path` carries a tilde reference resolved through `asset_map.json`. The encode spec is locked: changing it means a coordinated full re-encode, not a one-off.
- **Visual-similarity vectors**: SigLIP keyframe embeddings → `clip_and_still_embeddings.sqlite`. Binary, separate store, no contention with the above.

### 3 · Cheap enrichment: *free, local; do this before the expensive pass*
Independent local runners, each idempotent per asset/frame. Order is flexible: shot detection is a soft prerequisite for OCR and shot quality (both key off `shot_idx`). Most of these write **per-asset fields into the catalog JSON**; the vector/fingerprint layers write their binary index file. This is the point your earlier pipeline missed: it's cheap, and it improves the dataset *before* you spend on the model pass.

| Layer | What it adds | Lands in |
| --- | --- | --- |
| Shots | scene boundaries (`shot_idx`, start/end) | catalog `shots` |
| Shot quality | sharpness / motion / exposure + flags | catalog `shot_quality` |
| OCR | on-screen text, bib hits | catalog `ocr_detections` |
| Faces | embeddings + named-identity tags | `face_embeddings.sqlite` (vectors); identities project into `editorial_catalog` |
| FAISS | visual-similarity ANN index | `clip_embeddings.faiss` (binary) |
| Audio quality | RMS / clipping / silence + flags | catalog (audio records) |
| Audio events | ambient-sound tags (CLAP) | `audio_events.sqlite` |
| Audio↔video links | chromaprint fingerprint match | catalog `linked_assets` |

### 4 · Build + iterate the dataset: *human-in-the-loop*
Promote the staged transcripts to canonical, then backfill and review the entity / place / classification layers. This is the loop: review, re-prompt, re-run until the corpus reads right.

- Promote transcripts → `dataset/assets/transcripts/`; backfill `people_ids` / `org_ids` / `place_ids` (entity slots live on transcripts and documents only).
- Normalize `setting.location` → `place_ids` + `shoot_location`.
- Curate the people / orgs / places registries and asset classifications, the part that actually takes review time.

### 5 · Model passes: *API cost; cheap frames first, pricey video last*
With the cheap layers in place, spend on the inference-heavy work. These are API calls, ordered cheap-to-expensive, and they reward iteration. (API access requires account setup with a payment method, and new accounts start rate-limited; see `DESIGN.md` § "Inference cost surface" for how to throttle and how to get limits raised.)

- **Frame captioning (dense captions)**: a per-shot descriptive caption from a few sampled frames (e.g. 25 / 50 / 75 % of each shot), *not* the full video feed, so it costs a fraction of the video pass. Seed the prompt with the chunk-semantics metadata, plus anything specific you want the model to watch for, to sharpen accuracy. Lands in catalog `dense_captions`.
- **Video-feed labeling**: a per-chunk editorial description (subject, action, setting, camera, key moments) from the actual video. We used Gemini 2.5 Pro via Vertex AI; any capable video model works. Roughly $450 for a ~200-hour catalog. Extracts into catalog `asset_semantic_summary`.
- **Transcript LLM cleanup**: an optional pass to tighten diarization and labeling.

Sequencing here is a judgment call you tune by testing: a cheap frame pass can run first, then the pricier video pass, then maybe another frame round seeded with what the video pass surfaced. The two passes inform each other.

### 6 · Consolidate + rebuild
Extract the model outputs into the catalog, then rebuild the derived projection.

- Extract semantic summaries → catalog; normalize places; sync the `machine_transcript` and `embeddings` presence flags.
- `rebuild_all` → `editorial_catalog.sqlite`, `MANIFEST.json`, `STATS.json`.
- Slim the embeddings DB: the text now lives in the catalog; the vectors and chunk registry stay in the DB.

> **Moving between machines.** If extraction runs on one box and editing on another, copy the workspace between SSDs (close any SQLite writer first). That's logistics, not a pipeline stage; see workspace `README.md`.

## Asset-type-aware enrichment depth

**Principle:** not every asset needs the same ingestion. Different content types have radically different "what matters" surfaces. Running every enrichment layer over every asset is wasteful and produces low-signal noise on layers that don't apply (e.g. CLAP audio events on a dialog-dominated interview).

This is a principle as much as an implementation rule. Today the enrichment layers mostly run over the full corpus uniformly; over time they should accept an `--asset-types` filter and consult this table to scope what they touch. Dense captions was the first layer designed asset-type-aware from day one.

| Asset type | High-value layers | Low-value layers | Notes |
|---|---|---|---|
| **interview** | Transcripts + chromaprint cross-links (camera ↔ lavalier) + faces + people_ids + semantic_chunk subject | Dense captions (1/shot suffices), OCR (lower-thirds only), CLAP (dialog dominates the spectrum) | Selection signal is what the speaker is *saying*, not what's *visible*. Locked-off cameras mean shot count is tiny (110 shots / 44 hr = avg 24 min per shot). |
| **b_roll** | SigLIP visual, dense captions, shots, shot_quality, aesthetic, audio_quality | Transcripts (rarely speech), face index (often empty), CLAP music tags (rarely musical) | Visual nuance carries the meaning. Dense captions earn their compute here. |
| **verite** | Everything: unpredictable content mix | None (catch-all asset_type) | Verite is the dominant editorial surface at production scale (~8,900 shots / ~117 hr in a typical long-form doc corpus). Mix of speech + action + ambient. Every layer pays off somewhere. |
| **third_party** | Editorial notes (from licensing, often pre-existing), OCR for chyrons / lower-thirds | Dense captions less critical (intended editorial use already known from licensing) | Press, podcast, news clips. Usually clear what they're FOR before ingest. |
| **aerial / drone** (shoot_label match) | SigLIP visual, framing labels (camera_movement from the video labels) | Transcripts, OCR, face index | Often no speech, no on-screen text, no people. |
| **timelapse** | Shot boundary only (no semantic value) | Everything else | Cheap to skip entirely; most layers produce noise on time-compressed content. |
| **archival** | OCR (titles / cards), basic semantics | Dense captions secondary | Historical footage; OCR catches dates / location titles. |

### How layers currently respect (or don't respect) this principle

| Layer | Asset-type-aware? | Notes |
|---|---|---|
| faces | Implicitly (only runs where faces detected) | Skips empty-face assets automatically |
| shots | No | Runs on all video; cheap enough not to scope |
| OCR | Implicitly (skips frames with no text) | Could explicitly skip interviews where chyrons aren't expected |
| shot_quality | No | All-shots run; cheap |
| audio_quality | No (all-assets) | Cheap enough; outputs useful for interview + verite alike |
| FAISS | No (all SigLIP vectors) | Index over everything; query-time filtering is the right place |
| audio_events (CLAP) | No (all assets) | Wasted compute on dialog-dominated interviews. Future: filter to `asset_type IN ('verite','b_roll','aerial')` would halve the run. |
| audio_fingerprint | No (all WAVs) | All-pairs match is correct here; cross-modal links can span any types |
| dense_captions | **Yes: design baked-in** | Verite/b_roll/aerial/archival get 3-frames-per-shot; third_party gets midpoint only; interview gets 1/shot; timelapse skipped |

### When to revisit / fold in

When a layer's compute starts to dominate ingest wall-clock (CLAP was ours), retrofit asset-type scoping. The pattern is: add `--asset-types` flag, default to "all" (current behavior), let operator narrow when needed.

The bigger architectural question this opens: **future layers should be designed asset-type-aware from inception** rather than retrofitted. Dense captions was the first; subsequent layers should follow the pattern.

---

## Triggers

Match the change to the stage; the projection rebuild is the common tail.

| What changed | Re-run |
| --- | --- |
| New shoot offloaded | all six stages (stage 3's layers in any order) |
| Re-transcribed an asset | stage 2 (that asset) → stage 4 promote → rebuild |
| New proxies only | stage 3 shot detection → OCR + shot quality → rebuild |
| New WAVs only | stage 3 audio quality / events / fingerprint → rebuild |
| New video-model batch | stage 5 → stage 6 extract + rebuild |
| Re-tuned a layer's thresholds | re-run that layer → rebuild |
| Schema change | rebuild |

Run the projection rebuild (`editorial_catalog.sqlite`) after anything that mutates catalog JSON or a binary index.

---

## Cross-cutting conventions

- **Dry-run / smoke first.** A `--dry-run`, then a small `--limit` smoke pass, before any bulk or destructive run. It catches path / spec / idempotency problems before they touch thousands of assets.
- **Atomicity.** Record-mutating scripts write a temp file then `os.replace`; a kill mid-run leaves originals intact. Sandboxed-mount agents need the safe-write pattern (see `README.md`).
- **Look assets up by `asset_id`, never filename**: camera filenames (`C0050.MP4`) repeat across cards. The id is the content hash above.
- **Entity slots.** `people_ids` / `org_ids` / `place_ids` / `beat_ids` live on transcripts and documents only.
- **Tilde paths.** Resolve catalog `proxy.path` via `dataset/_scripts/workspace_paths.py::resolve_proxy_path()` or `derivative media/_index/asset_map.json`.

---

## Where the operator detail lives

This file says *what* and *in what order*. The per-layer commands, flags, and hardware notes live with the scripts: the inference-heavy stages (2, 3, 5) in `dataset/_scripts/_scripts_README.md`, the consolidation step in `dataset/dataset_README.md` and `dataset/_scripts/rebuild_all.cmd`. The pipeline is OS-agnostic; what matters is running the inference-heavy stages on a capable machine.
