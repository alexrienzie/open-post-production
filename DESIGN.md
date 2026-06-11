# Design: rationale and alternatives
*Why each phase is built the way it is, the alternatives we weighed, and the choices most likely to age out.*

Companion to [`INGEST.md`](INGEST.md). INGEST.md says what to run when; this doc says why we built it this way, what alternatives we looked at, and which assumptions are likely to age out first. It follows INGEST.md's six cost-ascending stages.

Audience: someone evaluating this pipeline as a template for their own documentary corpus, or maintaining it later when the original choices need re-justification.

> The high-level design overview now lives in the README's "How it works" section. This doc is the per-phase rationale and the decisions most likely to age out.

---

## Cross-cutting design choices

Read this table first. It covers the core design choices the per-phase tables build on.

| Decision | Choice | Rationale | Alternatives | Assumption / decay |
| --- | --- | --- | --- | --- |
| Catalog storage | Per-asset JSON files, one record per source file | git-diffable, no lock contention, mutable from many scripts in parallel, easy to inspect with `cat` | Single SQLite source-of-truth (lock contention, opaque diffs), single mega-JSON (rewrite cost), graph DB (overkill at this scale) | Under 100K assets total; the filesystem handles many small files well. Likely to creak at 1M-plus assets where FS overhead starts to matter. |
| Query surface | SQLite rebuilt from the JSON catalog | Single file, no server, replicable, fast joins, can be regenerated from scratch in under 30 minutes of unattended runtime | Postgres + JSONB (server overhead, sync complexity), DuckDB (newer, faster on analytics, viable swap-in), Neo4j (graph queries marginal here) | Corpus fits in memory during build. |
| Asset identity | `sha256(head 1 MiB ‖ tail 1 MiB ‖ filesize_be8)`; whole file when 2 MiB or smaller | Stable across copies and renames. Fast: only 2 MiB read on a 50 GB R3D. Collision risk is negligible because media containers have entropic headers and footers. | Full SHA-256 (slow on big files), xxhash (faster but less universal tooling), BLAKE3 (faster, less ubiquitous), path+mtime (breaks on copy), UUID (no content stability) | Head, tail, and size together carry enough entropy for media files. Holds for MP4, MOV, R3D, WAV, JPEG. |
| Mutation discipline | Atomic write: `tmp.write_text` then `os.replace` | Kill mid-write leaves the original intact | Direct write (data loss on crash), git commit per change (binary bloat, slow) | Filesystem supports atomic rename. Broken on sandboxed-mount agents (Cowork class); see `README.md` § "Working with the workspace" for the per-agent-mode taxonomy and workaround. |
| Heavy vs. light machine | Run the inference-heavy phases (ASR, encode, vision) on the most capable machine you have | What matters is compute, not OS: heavy phases want a strong GPU or Apple Silicon and enough RAM, while catalog work and editing are light and run anywhere. Runtime figures in this doc are measured on a 64 GB M4 Max; on a light laptop the same passes take much longer (see the hardware-variation table below). | One machine does it all if it's strong enough, or split across a fast inference box and a separate editing box. All-cloud works at recurring cost. | Nothing in the architecture requires a particular OS or machine count. |
| Schema versioning | Integer per domain (`video.v6`, `audio.v4`, `transcripts.v5`) | Migrations are explicit, and coverage of any migration can be computed from the version field | No versioning (silent drift), date-only (loses ordering), semver per domain (overkill for a monorepo schema) | Migrations are write-once and replayable. |

---

## Cost-ordered pipeline (in hindsight)

The same logic applies to ordering, not just scoping: run cheap, deterministic signals before expensive, probabilistic ones, and use the cheap ones to aim the expensive one. We'd sequence it: (1) free metadata (hash, transcribe, proxies, audio/quality layers); (2) shot detection; (3) a sampled-frame image pass through an LLM (a few frames per shot, e.g. at 25/50/75%, like the `dense_caption` layer); (4) only then the video-model pass, parsed at the shot level with a prompt refined by what (1)-(3) surfaced. We actually ran the video pass early, before shots existed, so it parsed fixed time-chunks and couldn't be targeted. And whatever the step, smoke-test on a few assets before the bulk run. The expensive pass is the one most worth deferring until the cheap context exists. The frame-caption pass is itself an LLM API call (sampled frames, not the video feed), so it's far cheaper than the video pass; you can seed its prompt with the chunk semantics, or with anything specific you need the model to watch for, and run it again after the video pass lands. The two passes inform each other; treat the order as something you tune by testing, not a fixed sequence.

---

## Portability: what generalizes vs. what's our setup

The pipeline architecture (per-asset JSON catalog, SQLite indexes, embeddings DB, NLE interchange format) is OS-agnostic and NLE-agnostic. The specific tools we use are choices driven by the hardware on hand and operator expertise, not by the design.

Our setup as deployed: a 64 GB M4 Max runs the inference-heavy phases (ASR, encode, vision), and a lighter machine handles catalog work and editing. We use Adobe Premiere Pro (which is cross-platform, Mac and Windows) with xmeml v4 as the NLE interchange format. The heavy-vs-light split is what matters; the specific OS on each side doesn't.

What's OS-locked, what's swappable, per layer:

| Layer | Our choice (Mac/Windows) | Alternatives on other OS or other stacks |
| --- | --- | --- |
| Operating system overall | Mac and Windows split | Linux works for every phase except the NLE itself. Premiere is Mac/Windows only; DaVinci Resolve runs on Linux. Pipeline scripts are plain Python, cmd, or bash. |
| ASR engine | MacWhisper Large v3 Turbo (Mac-only GUI plus CLI) | `whisper.cpp` (cross-platform; Metal, CUDA, Vulkan, or CPU), `faster-whisper` (Python plus CTranslate2, NVIDIA-favored), `insanely-fast-whisper` (HuggingFace stack), OpenAI `whisper` CLI (universal but slow). All produce text plus word-level timestamps. A thin adapter is needed to land in the v5 transcript schema. |
| H.264 encoder | `h264_videotoolbox` (Apple HW) | `h264_nvenc` (NVIDIA), `h264_amf` (AMD), `h264_qsv` (Intel Quick Sync), `libx264` (CPU, universal). The pipeline cares about the output spec (720p, ~3 Mbps VBR, 1-second keyframes, BT.709), not the encoder backend. `CMD_HASH` differs per encoder, so switching after the library is built means re-encoding everything. |
| R3D decode | REDline CLI (Mac/Windows) | DaVinci Resolve (Mac/Windows/Linux; licensed RED decode). No native Linux REDline. R3D footage is rare enough that the workflow can target whichever box has the decoder. |
| Vision-encoder accelerator | MPS (Apple Silicon) | CUDA (NVIDIA), ROCm (AMD), CPU (slow but functional). PyTorch device string is the only change. |
| Cloud video APIs | Vertex AI plus AI Studio | Already OS-agnostic, HTTPS client only. Could be swapped for any equivalent video-understanding API (OpenAI when video lands at parity, future entrants). |
| NLE interchange format (editor side) | xmeml v4 (Premiere) | FCPXML (DaVinci Resolve, Final Cut Pro X), AAF (Avid Media Composer), EDL (almost any NLE, lossy), OTIO (NLE-agnostic; best as an interchange or sandbox layer rather than a Premiere substrate). The catalog-to-cut integration in `editor/xml exports/_scripts/` is the boundary. Rebuilding it for a different NLE is trial-and-error against that format's quirks (where it's forgiving vs. strict, what survives round-trip), not a structural redesign. |
| NLE itself | Premiere Pro (Windows) | DaVinci Resolve (Mac/Windows/Linux), Final Cut Pro (Mac), Avid Media Composer (Mac/Windows), or no NLE at all. Cut decisions live in the catalog plus sidecars (NLE-agnostic), so you can also compile the cut straight to MP4 with ffmpeg (the archived `editor/_archive/render_to_mp4/` machinery is a starting point) and skip the timeline app. Only the interchange scripts are NLE-specific. |

Porting effort ranked by relative cost (qualitative, since AI coding agents now do most of the adaptation):

1. Different NLE. Rewrite `editor/xml exports/_scripts/` against the target's interchange format (FCPXML, AAF, EDL, etc.). Largest job. These scripts encode hard-won knowledge of where one specific NLE's parser is permissive vs. brittle. Expect trial-and-error per format.
2. Different ASR. Small adapter from your Whisper variant of choice to the v5 transcript schema.
3. Different encoder. Change one ffmpeg invocation and re-derive `CMD_HASH`. Trivial code-wise, but commits you to re-encoding the proxy library on the swap.
4. Different vision accelerator (CUDA or ROCm). Change one PyTorch device string.
5. Cloud-API swap (video labeling). Replace one client and adapt to the new response schema.

The catalog, indexes, and embeddings layer is fully portable. Almost all of the work above lives at the edges of the pipeline (ingest tooling and NLE handoff).

---

## Inference cost surface: external LLM vs. local inference vs. no inference

The pipeline mixes three flavors of compute. Knowing which phase uses which tells you what hardware you actually need and what your recurring bill looks like.

| Flavor | What it means | Cost model | Machine constraint |
| --- | --- | --- | --- |
| External LLM API | Cloud-hosted model called over HTTPS | Per-token recurring cost | Internet plus credentials; no local compute requirement. See the account-setup note below. |
| Local inference | Open-weight model running on your machine (Whisper, SigLIP) | Free per call; one-time hardware cost | Speed scales with GPU or Apple Silicon; CPU fallback always works |
| Plain code | Python plus ffmpeg plus SQLite; no ML inference at all | Free | Runs anywhere |

**Account setup is a real step, not a footnote.** Every external API here (Gemini via AI Studio or Vertex, and any alternative) requires an account with a **payment method on file** before production batches run. Two practical gotchas:

- **New accounts ship rate-limited.** Fresh API keys land on the lowest quota tier (requests-per-minute and per-day caps well below batch scale). Throttle your runners accordingly (our scripts take worker-count flags for exactly this reason) and expect the first batch to run slower than the math says.
- **Higher tiers take deliberate effort.** Limits rise with billing history, but you can accelerate: enable billing early (even before you need volume), make a small spend so the account has history, and file the quota-increase request as soon as you know your batch size rather than mid-run. On Google, Vertex AI and AI Studio carry **separate quotas** on the same project; having both configured doubled our effective throughput and gave us a failover when one side throttled.

Per-phase breakdown:

| Step | Inference type | Model / engine | Hardware needed | Per-asset cost (our deployment) |
| --- | --- | --- | --- | --- |
| Offload | None | n/a | Any | Free |
| Index + hash | None | n/a | Any (CPU-only) | Free |
| WAV + transcribe | Local | Whisper Large v3 Turbo | Apple Silicon (MPS), NVIDIA (CUDA), AMD (ROCm), or CPU | Free; about 15-25× realtime on M4 Max |
| Promote + entity backfill | None | Whole-word string match against registries | Any | Free |
| Proxies | None | ffmpeg plus HW encoder (or `libx264`) | Any with ffmpeg | Free; about 4 clips/min on M4 Max with VideoToolbox |
| Video labeling | External | Gemini 2.5 Pro (Vertex AI primary, AI Studio fallback) | Any with internet | About $2 per hour of media (~$453 for 200 hr) |
| SigLIP vectoring | Local | SigLIP-So400m | MPS, CUDA, ROCm, or CPU | Free; about 80 fps on M4 Max |
| Workspace copy | None | n/a | Any | Free |
| Consolidation | None | Python catalog mutations plus SQLite rebuild | Any | Free |
| Cross-modal linking | None | chromaprint or audfprint DSP | Any | Free |
| Faces | Local | InsightFace buffalo_l (SCRFD-10G + ArcFace R100) | CoreML on Apple Silicon; ONNX runtime cross-platform | Free; about 1.5 hr wall-clock to detect + embed faces across the catalog on M4 Max |
| Shots | None | PySceneDetect ContentDetector (HSV histogram) | Any | Free; about 25 min on M4 Max |
| OCR | Local | RapidOCR (PP-OCRv4 ONNX) + Apple Vision (`VNRecognizeTextRequest`) | RapidOCR cross-platform; Apple Vision Mac-only | Free; about 30 min on M4 Max for the 26K shot-sampled frames |
| Shot quality | None | OpenCV Laplacian + frame-diff + luma stats | Any | Free; about 7 min on M4 Max with 4 workers |
| Audio quality | None | ffmpeg `astats` filter | Any | Free; about 8 min on M4 Max |
| FAISS | None | FAISS `IndexHNSWFlat` | Any (CPU); GPU optional | Free; about 3.5 sec to build over 116K vectors |

Adjacent passes outside core ingest that use external LLMs and live in `dataset/_scripts/`:

- Case-file enrichment (Gemini 2.5 Pro). Court-filing PDFs: OCR plus enrichment for `people_ids`, `org_ids`, `place_ids`, `analysis`. Few cents to few dollars per document batch.
- Press article analysis (Gemini Pro, completed). `analysis.storylines`, sentiment, named entities.
- Transcript analysis refresh. Corpus-wide LLM pass over transcripts; budget TBD, scope tied to story and beat review.
- Orgs registry dedup (Opus-assisted pass historically). Proposed merges, human-reviewed.

Heuristic-not-LLM passes that look like LLM work but aren't:

- Entity backfill (`backfill_entity_ids.py`). Whole-word string match against people and orgs registries. Deterministic, high precision when registries are complete.
- Same-kind and audio-to-video co-recording linkers (`propose_*_links_by_transcript.py`). Transcript-overlap scoring with thresholds; no model in the loop.
- Place normalization (`normalize_asset_places.py`). String-to-registry-slug mapping.

### Local-inference hardware variation

Reference numbers are observed on the M4 Max baseline. Non-Mac figures are order-of-magnitude estimates from public benchmarks; treat them as planning, not commitments.

| Machine | Whisper Large v3 Turbo (realtime multiple) | SigLIP-So400m (frames/sec) | H.264 720p encode (clips/min/SSD) |
| --- | --- | --- | --- |
| M4 Max, 64 GB (our reference) | 15-25× | ~80 fps | ~4 (VideoToolbox) |
| NVIDIA 4090, 24 GB VRAM | ~50-80× (faster-whisper) | ~200-400 fps | ~5-8 (NVENC) |
| NVIDIA 3060, 12 GB VRAM | ~30-50× | ~80-150 fps | ~3-5 (NVENC) |
| Apple M1 / M2, 16-32 GB | ~5-10× | ~30-50 fps | ~4 (VideoToolbox; same per-SSD ceiling) |
| Modern x86 CPU only | ~0.5-2× | ~5-10 fps | ~1-2 (`libx264`) |

Memory floors for concurrent ASR plus vectoring plus a Premiere working set:

- Whisper Large v3: about 3 GB working RAM plus 626 MB model weights
- SigLIP-So400m: about 2-3 GB VRAM (or unified memory) in fp16
- Practical floors: 8 GB machine means serial only and slow; 16 GB means comfortable serial; 32 GB or more means parallel inference plus Premiere editing without thrash

### External-LLM cost calibration (video labeling, observed)

- Gemini 2.5 Pro: $453 actual for the 200-hour catalog vs. $525 estimated. Failure rate about 7% during overload; failures are not billed, only successful generates.
- Tokens per second of media: about 300 (vision 258, audio 32, plus about 10 prompt overhead).
- Long chunks (38-55 min) hit the over-200K token tier individually at $1-2.50 each.
- Gemini 2.5 Flash: about 10× cheaper but disqualified during pilot for timestamp hallucination (see the video-labeling table below).
- Recurring cost is bounded by corpus growth, not by re-runs (the DB caches `label_status=done` rows).

### Alternatives by layer and machine class

> The LLM and open-weights landscape shifts on a months-not-years cadence. Specific models named below will be partly stale within a few months. Recheck before adopting any of these for a new corpus.

#### ASR: Whisper (local inference)

| | |
| --- | --- |
| What we run | MacWhisper Large v3 Turbo, MPS-accelerated, Mac-only GUI plus CLI |
| Local alternatives, any OS | `whisper.cpp` (Metal, CUDA, Vulkan, or CPU), `faster-whisper` (Python plus CTranslate2, NVIDIA-favored), OpenAI `whisper` CLI (universal but slow), `insanely-fast-whisper` (HuggingFace stack), `distil-whisper` (faster, weaker on long-form) |
| External alternatives | Deepgram, AssemblyAI, OpenAI Whisper API, Gladia, Speechmatics, Gemini audio mode |
| Why local | Free per call. No data egress on production-recorder audio. MPS makes it fast enough on the Mac. The MacWhisper GUI is useful for spot-checks even when scripts drive the CLI. |
| When external makes sense | No GPU on hand, one-off jobs, or you want diarization baked in without doing the setup |

#### Video labeling (external LLM)

| | |
| --- | --- |
| What we run | Gemini 2.5 Pro via Vertex AI (primary) and AI Studio (fallback) |
| External alternatives | Twelve Labs Pegasus (video-native, separate billing), OpenAI video (when it lands at parity), Claude video (when it lands at parity), open-weight video models hosted on Together or Replicate |
| Local alternatives | LLaVA-NeXT-Video, MiniCPM-V 2.6, Qwen2-VL, InternVL, VideoLLaMA. Quality lagged Gemini Pro at decision time and the gap is closing. Hardware floor is roughly 24 GB VRAM for the larger checkpoints, 12-16 GB for the smaller ones. |
| Why this choice | Gemini Pro joined audio and video in a single call, held timestamps under hallucination pressure where Flash failed, and accepts strict Pydantic output schemas. The per-corpus cost ($453 for 200 hours) was acceptable for a one-time pass. |
| When local makes sense | Material that can't leave your network, or a corpus that grows fast enough that recurring API spend matters. Expect a quality gap on `key_moments` timestamping until the open-weight models catch up. |

#### Vision-encoder embeddings (local inference)

| | |
| --- | --- |
| What we run | SigLIP-So400m on Apple Silicon MPS |
| Local alternatives, any GPU | CLIP ViT-L/14 (established, smaller, slightly weaker), OpenCLIP ViT-bigG (bigger, slower, marginally better), SigLIP-2 (newer, likely better), DINOv2 (stronger visual features but no text-shared space, which would break text-to-image search) |
| External alternatives | Voyage embeddings, Cohere multimodal embeddings, Jina embeddings v3 (text plus image bilingual) |
| Why local | Free. Vectors fit comfortably in SQLite (about 460 MB for the full catalog at 1152-dim float32). MPS is fast enough on the Mac. Shared text-vision space opens the door to text-to-image search later without re-embedding. |
| When external makes sense | Very large corpora where managed vector storage plus ANN indexing is worth the recurring cost over rolling your own SQLite plus FAISS |

### Decay note

External LLM pricing has been falling roughly an order of magnitude per year on the frontier, so the per-hour Gemini cost is likely to drop further. Local model quality is rising fast. Running a Gemini-Pro-equivalent video model locally on a high-end Apple Silicon or NVIDIA workstation card looks plausible within a couple of years. The mix in the table above will probably shift toward local inference over time, especially for groups that don't want production-recorder audio leaving their network. Worth re-checking the video-labeling and embedding choices once a year.

---

## Why this instead of a product

Most of what these tools do breaks into three layers: perception (transcribe, tag, search), integration glue (a searchable index, an NLE handoff), and editorial judgment. The first is now commodity open source; the second a coding agent assembles for you; the third stays with the director and editor regardless of tooling. Owning the first two yourself is what this repo is about.

What you'd otherwise rent, and the local substitute:

| Capability | Paid option (rough cost) | Local OSS substitute | Marginal cost |
|---|---|---|---|
| Transcribe + diarize | Eddie / Simon Says (metered) | Whisper / WhisperX | $0 |
| Visual + face search | Jumper ($169–599 one-time), Twelve Labs ($2.52/hr to index) | SigLIP + FAISS + InsightFace | $0 |
| Multimodal index of the corpus | Twelve Labs (cloud, metered; hundreds of dollars to index this corpus once, then per query) | SigLIP / CLAP + SQLite | $0 |
| Shot / quality / OCR / audio tags | bundled in the above | PySceneDetect, RapidOCR, CLAP | $0 |
| Semantic clip labels | bundled in the above | a frontier model, one pass | ~$2/hr once, or $0 with a local VLM |
| Cut handoff | Jumper MCP, Eddie export | xmeml / FCPXML / AAF / OTIO, or ffmpeg direct render | $0 |

There are good commercial tools here: **Eddie AI** (A/B-roll logging and rough cuts), **Jumper** (local footage search with an MCP bridge to agents), **Twelve Labs** (a hosted multimodal video-understanding API), and others. They're polished. The difference is ownership more than capability: they package the perception layer as a managed service, while this repo keeps it as data you hold and extend. If a subscription fits your workflow, use it. This is the build-it-yourself path for teams who'd rather own the catalog and the tooling around it.

**You aren't even locked to an NLE.** The cut lives in the catalog and sidecars, not in a project file. Render it to finished MP4 with ffmpeg and skip the timeline app, or hand it to Resolve, Avid, or Final Cut via FCPXML, AAF, or OTIO. The NLE is a swappable front-end, not the system of record. (See `editor/editor_README.md`, "Output paths and interchange.")

**The hardware trend points the same way.** Local machines are about to run far larger models than they do today. NVIDIA's RTX Spark (announced May 31, 2026; ships October) claims up to 128 GB of unified memory and 120-billion-parameter models locally on a laptop, with Claude Code running natively. These are launch-day specs without independent benchmarks yet, so treat them as direction rather than a guarantee. The direction is that the local-versus-rented math keeps tilting toward local.

**What you gain by building your own:** you own the data, so every adjacent tool (graphics, subtitles, sound spotting, grade prep) is a small build rather than another subscription, and nothing is tied to a vendor's roadmap. **What it costs:** setup is real work, though bounded. With a frontier coding agent (Claude Code, Cursor, Codex), standing this up and adapting it to a similarly sized corpus is on the order of a few days to a week. The agent stays trustworthy because it reasons over a diarized, provenance-tagged catalog rather than raw footage. The trade is convenience for control.

---

## Upstream media-organization disciplines (retrospective)

> Honest framing: this pipeline was retrofitted onto a documentary corpus that had already been organized via normal human-readable folder discipline (which was helpful) and partial filename conventions (which were mixed). Half the AI-pipeline work below exists to **compensate for inconsistent upstream organization**: chromaprint to recover audio↔video links that filename conventions could have asserted, place normalization to reconcile shoot_label drift, transcript entity backfill to recover names that consistent slating would have made unambiguous. **If you're starting a documentary corpus from day one**, the principles below would save the downstream AI tooling a lot of work.
>
> The retrospective is also a useful self-audit for any project mid-stream: most points can still be partly applied to new content going forward, even if the back-catalog stays as-is.

### What to lock in BEFORE day-one shooting

1. **Date in every camera filename.** Set cameras to embed `YYYYMMDD` or `YYMMDD` in the recorded filename. Critical because cameras with simple counters (Sony C-clip naming, GoPro `GH010370.MP4`) collide across shoot days, forcing downstream tools to rely on folder context for time grounding. we caught this halfway through production; earlier shoots have ambiguous filenames that downstream had to disambiguate via `path_metadata.shoot_date` extracted from folder structure. Workspaces where every filename starts with a date are dramatically cheaper to ingest.

2. **Verify camera date settings before every shoot.** Handhelds (GoPro, DJI Osmo Action/Pocket, action cams) **routinely have wrong system clocks**: battery pulls, time-zone resets, factory defaults. we hit this on several GoPro shoots where the recorded timestamps were months off. Downstream the `ffprobe` `creation_time` becomes unreliable as a sort/group key. Fix: run a date-check macro at shoot start that visually confirms each camera's date display matches a phone clock.

3. **Folder-naming as a hard schema, not a convention.** The convention used here is `YYYY-MM-DD_<location>_<subject>` and it's good when followed. The places downstream pipelines hit friction are where it isn't:
   - **Asset types mixed in one folder.** `2025-8-22_Jane` contains an interview shot, B-roll cutaways, AND verite from the same day. Downstream `asset_classifications.type` was inferred via folder-path heuristics + spot-checks; accuracy would be 100% if folders were already segregated. **Rule**: never mix `interviews / verite / b_roll` in the same shoot folder. Create subfolders (`5. Interview/`, `4. B-roll/`, etc. is the pattern that worked for us; apply it everywhere).
   - **Multiple cameras in one shoot.** If the day had a Sony A-cam, a GoPro B-cam, and a DJI drone, give each its own subfolder. We mostly did this (`REDSW/`, `7. Alex DJI/`) but not always.
   - **Audio in its own subfolder.** we consistently used `Audio/` for production-recorder files and that works well; chromaprint linker keys off this convention. Make it universal: any non-camera-onboard audio file lives in an `Audio/` subfolder.

4. **Third-party media ingested deliberately, not haphazardly via the NLE.** Premiere has its own clip-import flow that lands third-party material (news clips, podcast screencaps, archival footage) wherever a sequence happened to be active. Downstream this fragmented the third-party set across many folders, with metadata (date, license terms, intended editorial use) buried in Premiere project metadata rather than in filesystem-accessible form. **Better pattern**: a dedicated `12. Third Party/` (or similar) folder with subfolders per source, each containing a small `_README.md` capturing license terms + intended editorial use + provenance link. our `12. News + Other Clips/` folder is closer to this ideal; other third-party material is scattered.

### Additional disciplines that earn their cost

5. **Camera ID + slate discipline.** Each camera body should have a stable camera-ID setting (Sony lets you set the C-prefix; DJI gives you a settings menu) and that ID should be locked across the production. We mostly did this (`sony_c0xxx`, `sony_c8xxx`, `sony_c9xxx`, `DJI_osmo`) but has cases where camera_id is `unknown` because the setting wasn't applied. **Pair with**: a clap-slate at the start of every take when both onboard mic and lavalier are rolling. Chromaprint handles audio↔video sync without a slate, but a deterministic slate moment makes it trivial to verify the linker's output and gives editors a reliable sync point even when the lavalier signal is bad.

6. **Per-shoot README at offload time.** A 5-minute text file in each shoot folder capturing: subjects (with consistent name spelling; Gemini hallucinated "Maura Shuttleworth" / "Jane Moss" / "micalino sanseri" partly because no upstream document anchored the canonical spelling for that shoot), location, intent / what was supposed to be captured, anything unusual (e.g. "first take was staged; subsequent takes are real verite"). Becomes invaluable when an AI agent or new editor revisits the folder months later.

7. **Consistent audio-recorder filename conventions.** ours (which the chromaprint apply step keys off):
   - DJI Mic 2 lavalier: `DJI_NN_YYYYMMDD_HHMMSS.WAV` (great: date + time + sequence)
   - RED `.RDC` bundle audio: `B###_C###_YYYYMMDD…_A01_001.wav` (great)
   - Tentacle Track: `MMDD_<run-name>_NNNNa.wav` (OK: date is implicit, run name explicit)
   - Zoom: `ZOOMNNNN_LR.wav` (date-less; relies on folder context)
   - Sony PCM: `C####.wav` or `C#### Audio.mp3` (date-less; relies on folder context)

   The first two are the easiest for downstream auto-classification. If you're picking new recorders, lean toward devices that timestamp the filename.

8. **Backup discipline locked in from day one.** we follow 3-2-1 (RAID master + active SSD + transport SSD + cloud cold). The principle worth stating: **never reformat a camera card until at least two independent backups have verified the entire transfer**. we had a near-miss where a card was about to be reformatted before the off-site backup had completed; locking in a "two checkmarks before format" rule eliminates that class of risk.

9. **People-registry maintenance as a first-class workstream.** The single source of truth for "who is this person" should be a curated registry with **canonical_name + aliases[] + role + notes**, populated continuously rather than reconstructed in post. we built ours retroactively at `dataset/people/people.json` (291 entries); upstream this would be: as new subjects appear on camera, add them to the registry within a few shoot days, with at least one alias (nickname, formal name, common misspelling) to anchor cross-references.

### What this retrospective is NOT saying

It's not saying organize-then-shoot is the only viable approach. our mid-stream pivot to AI-driven post still produced a queryable, editable corpus; the enrichment layers + chromaprint + place normalization + entity backfill collectively bridge the gap. The retrospective is for two audiences:

- **Future filmmakers** reading this repo as a template: bake in the disciplines above and your downstream pipeline (whether this one or some 2027-vintage replacement) will be cheaper to deploy.
- **Mid-stream operators** of any documentary: apply what's applicable to new content going forward, even if the back-catalog stays as-is. Most of these disciplines have non-zero migration value (e.g. retroactively segregating mixed-asset-type folders during a content audit).

---

## Stage 1 · Offload + index

### Offload

| Decision | Choice | Rationale | Alternatives | Assumption / decay |
| --- | --- | --- | --- | --- |
| Storage tiers | RAID master plus SSD active plus SSD transport plus cloud cold backup | 3-2-1 backup discipline; tiers map to use frequency and recovery cost | Cloud-only (recurring cost, bandwidth at restore), single drive (data loss risk), LTO tape (operator overhead, slow restore) | Under 100 TB corpus, physical transport feasible, RAID rebuild time tolerable |
| Folder naming | `YYYY-MM-DD_<location>_<subject>` | Sortable, human-readable, parseable for downstream classifiers | Shot day numbers (opaque), camera card names (collision risk across days), shoot codenames (only the operator can decode them) | Operator commits to a naming standard at offload time and applies it consistently |

---

### Index + hash

| Decision | Choice | Rationale | Alternatives | Assumption / decay |
| --- | --- | --- | --- | --- |
| Hash algorithm | SHA-256 over partial content plus size | See cross-cutting table | Full SHA-256 (slow), xxhash (faster, less ubiquitous), BLAKE3 (faster, less established) | BLAKE3 likely becomes the better choice in 2-3 years |
| Hash window size | 1 MiB head plus 1 MiB tail plus 8B big-endian size | Empirically zero collisions across about 7K media files; balances speed and entropy | 64 KiB windows (collision risk on similar-headered files from the same camera), full file (slow on big sources) | Media containers carry entropic data at both ends |
| Small-file branch | Hash whole file when 2 MiB or smaller | Two 1 MiB reads of a 1.5 MiB file would overlap and produce a malformed hash | Always partial (broken on small files), always full (slow on big files) | 2 MiB threshold matches the window size |
| Record format | One JSON per asset, kind in the filename (`{aid}.{kind}.json`) | Git-friendly, parallel-mutable, kind visible in `ls`, disambiguates if directories ever merge | One JSON per shoot (lock contention), single mega-JSON (rewrite cost), SQLite per shoot (opacity) | Under 100K assets |
| Per-domain schema versions | `video.v6`, `audio.v4`, `stills.v3`, `transcripts.v5` | Migrations evolve domains independently | Single workspace-wide version (couples unrelated migrations), no versioning (silent drift) | Migrations are write-once and replayable |

---

## Stage 2 · Cheap local extraction

### WAV extract + transcribe

| Decision | Choice | Rationale | Alternatives | Assumption / decay |
| --- | --- | --- | --- | --- |
| Extractor | ffmpeg | Universal, scriptable, handles every container including R3D-derived MP4 | CoreAudio (Mac-only), sox (smaller container support), gstreamer (heavier setup) | ffmpeg in PATH. Willingness to debug codec edge cases (limited-range YUV, missing audio stream, etc.) |
| WAV target | 16-bit PCM, 16 kHz, mono | MacWhisper's native input; smallest size that doesn't lose ASR accuracy on speech | 48 kHz (4× size, no ASR gain), Opus (lossy ahead of ASR), MP3 (lossy) | Decay risk: if ASR moves to richer-audio models that benefit from full-band, 16 kHz becomes a bottleneck |
| ASR engine | MacWhisper Large v3 Turbo (`whisperkit:openai_whisper-large-v3-v20240930_626MB`) | Apple Silicon MPS acceleration. Free. GUI for spot-checks. About 15-25× realtime on long-form. | OpenAI Whisper CLI (slower, no MPS), WhisperX (better diarization, more setup), Deepgram / AssemblyAI (recurring cost, API dependency), Distil-Whisper (faster, about 2% WER worse on long-form), Gemini audio (recurring cost) | Apple Silicon Mac. English. Doc-style audio (interviews, voiceover). High decay risk: Whisper v4 or successor likely within 18 months. |
| Schema | v5 transcript: words plus segments plus speaker placeholders plus analysis slot | Word-level enables tight cuts. Segments enable readable display. Speakers ready for diarization upgrade. Analysis slot allows downstream LLM passes without schema migration. | VTT or SRT (lossy), raw Whisper JSON (no analysis slot, no speaker placeholders) | Editor needs word-level timing |
| Landing zone | `derivative media/_transcript staging/` (NOT canonical) | Mac script doesn't touch the canonical catalog. Promotion plus entity backfill is a separate operator-side step. | Mac writes canonical directly (couples machines, no spot-check window), shared mount (impractical with current SSD setup) | Two-machine workflow. Mounted file sharing too slow for full passes. |

---

### Make proxies

| Decision | Choice | Rationale | Alternatives | Assumption / decay |
| --- | --- | --- | --- | --- |
| Resolution | 720p max long edge | Enough for shot recognition and edit decisions. About 3 GB/hr. Scrubs fast. | 1080p (3× disk, no edit gain on doc content), 540p (too low for matchback in tight cuts), ProRes Proxy (huge, about 30 GB/hr) | Edit decisions made from proxy; conform to source for final master |
| Encoder | `h264_videotoolbox` (Apple HW) | About 4 clips/min/SSD on M1 and later. Free. No GPU contention with SigLIP MPS. | libx264 (3-5× slower on CPU), HEVC (older Premiere compat issues), AV1 (encode time, decoder support gaps) | Apple Silicon Mac. Willing to accept HW rate-control quirks (slightly worse rate control vs. libx264 at the same bitrate). |
| Bitrate | VBR `-b:v 3000k -maxrate 4000k -bufsize 8000k` | Enough for clean motion at 720p. Keeps total proxy library under 1 TB for 200 hours. | 5 Mbps (more disk, marginal gain on doc), 1.5 Mbps (banding on gradients, breaks aerials) | Doc-style content. High-motion shots (sport, action) may show artifacts. |
| Keyframe cadence | 1 second (`-force_key_frames "expr:gte(t,n_forced*1)" -g 240`) | Premiere scrubs per keyframe. 1 second is the sweet spot for J-cut precision without ballooning size. | 0.5 sec (2× size for marginal scrub gain), 2 sec (visible scrub jitter), source-keyframe-only (unpredictable per source) | Editor uses keyboard-driven scrubbing where keyframe placement matters |
| R3D path | REDline, then ProRes 422 Proxy 720p, then H.264 (matches main `CMD_HASH`) | ffmpeg can't decode R3D. REDline is the official path. ProRes intermediate preserves BT.709 color. Final H.264 is byte-spec-equivalent to the rest of the catalog. | DaVinci Resolve render (no headless CLI), native RED SDK integration (engineering cost), shell out to RED's HTTP service (doesn't exist on Mac) | REDCINE-X PRO installed (free download). Willing to pay the two-stage cost on long R3Ds. |
| Idempotency key | `CMD_HASH` of the full ffmpeg command, stamped on every proxy | Spec change means re-encode all. Preserves contract with the existing roughly 3,800 proxies. | Modification time (breaks on file move), filename only (encoding params not tracked) | Locked spec is genuinely locked. Changes are coordinated re-encodes, not side effects. |

---

### SigLIP vectoring

| Decision | Choice | Rationale | Alternatives | Assumption / decay |
| --- | --- | --- | --- | --- |
| Model | SigLIP-So400m | Vision-only signature in a shared text-vision space (enables future text-to-image search). Free local inference on MPS. Strong on natural-scene content. | CLIP ViT-L/14 (768d, established, marginally weaker), OpenCLIP ViT-bigG (about 1% better, 5× slower), DINOv2 (stronger visual features but no text-shared space, blocks text-to-image search later), Voyage or Cohere (API cost plus lock-in), SigLIP-2 (released 2025; obvious upgrade candidate) | 1152d storage acceptable (about 460 MB for about 104K frames). High decay risk: SigLIP-2 likely better. Switching cost is a full re-embed (about 1.5 h on M4 Max) plus 460 MB discard. |
| Keyframe cadence | 1 frame per 7 seconds | Catches major shot changes (doc shots are typically 3-8 seconds). 1 frame per shot is enough for "more like this" similarity. About 104K frames for 200 hr keeps the DB under 500 MB. | 1 fps (about 15× the frames, about 6 GB DB, throughput drop), PySceneDetect-driven, first plus middle plus last per chunk (cheap but misses internal content) | Strong assumption: doc-style 3-8 second shots. Music-video or fast-cut content gets vector smearing where one frame spans multiple shots. PySceneDetect-driven sampling is the principled fix. |
| Keyframe resolution | 512 px long edge JPEG (`-vf scale=512:-1`) | SigLIP ingests at 384×384 anyway. 512 leaves room for crop. Smaller JPEGs mean faster I/O. Original 4K JPEGs were the disk-IO bottleneck (about 200 GB, about 20 fps embed throughput). After patch: about 0.3 GB, about 80 fps. | Native proxy resolution (1280×720; about 30 GB; 60-80 fps), 384 exactly (brittle if the model changes input size) | SigLIP-So400m input size 384 stable in the current release |
| Storage format | 1152-dim float32 in SQLite BLOB, L2-normalised, with a sibling FAISS HNSW index for sub-millisecond ANN query | Cosine similarity equals dot product. SQLite is the durable source of truth; FAISS is the query accelerator (built from the SQLite blob in about 3.5 sec). | pgvector or Chroma or Qdrant (still overkill at 116K vectors), float16 (half storage, about 0.5% accuracy loss; worth doing if storage grows), int8 quantization (more loss, faster) | Storage cost dominated by Gemini text, not embeddings. Decay: float16 plus an IVF-based FAISS variant becomes worth doing past about 1M vectors; we're 10× under that today. |
| Encoding | `struct.pack('<1152f', *vec)` little-endian | Compact (4 bytes per dim), reproducible, language-agnostic readout | Pickle (Python-only), JSON (10× size), Protobuf (overkill for a flat vector) | Reader knows the dimension at decode time |

---

## Stage 3 · Cheap enrichment

The enrichment layers share one pattern. **The non-binary results land as per-asset fields in the catalog JSON** (`shots`, `shot_quality`, `ocr_detections`, `dense_captions`); the catalog is the source of truth, same as everywhere else. Only the genuinely binary layers keep a standalone file under `indexes/`: face embeddings, the FAISS index, audio fingerprints, and audio events. `editorial_catalog.sqlite` projects from the catalog JSON plus those binary stores. Each layer is independently re-runnable, per-asset (or per-frame) idempotent, and read-only from the upstream catalog and embeddings.

| Decision | Choice | Rationale | Alternatives | Assumption / decay |
| --- | --- | --- | --- | --- |
| Layer isolation | Non-binary results write per-asset catalog fields (`shots`, `ocr_detections`, `shot_quality`, `dense_captions`); only binary layers keep a standalone file (`face_embeddings.sqlite`, the FAISS index, `audio_fingerprints.sqlite`, `audio_events.sqlite`) | The catalog stays the single source of truth; a bad run on one layer doesn't corrupt the others, and each rebuilds independently. | Per-layer SQLite stores for everything (extra files, duplicate truth against the catalog), tables added directly to `clip_and_still_embeddings.sqlite` (couples vector storage to enrichment lifecycle) | Binary data that can't live in JSON stays in its own file; everything else is a catalog field. |
| Projection model | `dataset/_scripts/build_editor_db.py` reads the catalog-JSON enrichment fields and ATTACHes the binary stores, then drops + repopulates the per-layer tables in `editorial_catalog.sqlite` | One rebuild step composes everything for the editor; the catalog JSON stays the source of truth. | Materialised views (SQLite has no first-class materialised view), live ATTACH at query time (operator surface gets confusing fast) | Editor queries pay the join cost once at rebuild time, not per query. |
| Layer ordering | Shots is a soft prerequisite for OCR + shot_quality (both join on `shot_idx`). The other layers are independent. | Lets a single bad shot-detector run be re-fixed without re-OCRing 26K frames. | Hard pipeline dependency (forces rebuild cascades), no ordering hint (operator surprise) | Per-layer READMEs document the soft deps. |

### Faces

| Decision | Choice | Rationale | Alternatives | Assumption / decay |
| --- | --- | --- | --- | --- |
| Detector + embedder | InsightFace buffalo_l (SCRFD-10G + ArcFace R100) | Strong recall on non-frontal faces (athlete footage has lots of partial profiles), 512-d L2-normalised embeddings, CoreML-accelerated on M4 Max. | RetinaFace (older, slower), MediaPipe FaceMesh (alignment-focused, not recognition), DeepFace wrappers (more abstraction layers, less control), FaceNet (older embedder), DLIB (CPU-only, slower) | Apple Silicon. Doc-style footage. High decay risk: a successor face model with similar weight (~250 MB) likely within a year. Re-embed cost is small relative to the index value. |
| Clustering | HDBSCAN over the 512-d embeddings | Density-based, doesn't require a target cluster count, surfaces "noise" detections naturally (sunglasses, motion blur). | K-means (need to pick K), DBSCAN (sensitive to eps choice), graph-based (Chinese-whisper) (more tuning), Agglomerative (no noise label) | Single-threaded MST construction is slow at 70K detections (~45 min wall-clock on M4 Max). Worth re-running only when new shoots add a lot of new face content. |
| Privacy posture | Named identities only projected into the editor surface (posture #2) | The canonical store keeps every detection + cluster; the editor surface only shows clusters with a labeled `p_id`. Lets editors pick people by name without exposing unconsented bystanders. | Project everything (privacy surface bleeds into editorial workflow), project nothing (loses the editorial value entirely) | Adopt deliberately per project. Could be revisited per deliverable. |
| Sibling contamination workaround | Manual person-registry split (e.g. parent + adult sibling as distinct `p_id`s) | HDBSCAN merges biological siblings ~50% of the time. Splitting at the registry level lets the editor disambiguate. | Force k clusters per family (operator overhead), accept the merge (editorial wrong-name risk) | Caught during labeling; document splits in your people registry. |

### Shots

| Decision | Choice | Rationale | Alternatives | Assumption / decay |
| --- | --- | --- | --- | --- |
| Detector | PySceneDetect ContentDetector (HSV histogram delta) | Mature, single-threaded but fast enough (~25 min on M4 Max), well-understood thresholds. | TransNetV2 (neural; more accurate on hard cuts but heavier setup), AdaptiveDetector (PySceneDetect alternative; threshold-of-thresholds tuning), absolute-pixel-diff (broken on luminance changes) | Doc-style cuts dominate (hard cuts + dissolves). Music-video / heavy-effect content might miss soft transitions. |
| Threshold | 27 (default, validated against an operator-reviewed sample) | Threshold experiment ran across {21, 24, 27, 30, 33}; 27 hit the right balance on doc footage. Lower thresholds fragmented long interview takes; higher missed legitimate cuts. | Higher (27-30) for music-video content, lower (15-20) for fast-cut commercial content | Calibration is content-class-specific. Holds for this corpus. |
| `min_scene_len` | 15 frames | Filters out flash-frame micro-shots (camera shutter pulses, in-camera transitions). Still leaves some 0.0-0.5 sec fragments at asset boundaries; the shot-quality layer catches those via `is_setup_or_teardown`. | 30 frames (loses legit short cutaways), 0 (drowns in noise) | 23.976 fps NTSC. ~625 ms floor. |
| Single-take asset handling | Synthetic shot row covering `[0, duration_sec]` for assets where PySceneDetect returns empty | PySceneDetect returns `[]` for single-take assets rather than one synthetic scene, so the runner writes a synthetic row covering the whole asset. | Treat empty as missing data (downstream joins miss the asset), per-asset opt-out (operator overhead) | Long interviews + locked-off B-roll are common; one-shot-per-asset is a frequent case. |

### OCR

| Decision | Choice | Rationale | Alternatives | Assumption / decay |
| --- | --- | --- | --- | --- |
| Dual engine | RapidOCR (PP-OCRv4) + Apple Vision, merged into a single `ocr_detection` table with `ocr_engine` column | A stratified pilot (30 frames) showed neither engine dominated: RapidOCR caught small + curved text (bibs, signs), Apple Vision caught lower-thirds + clean serif chyrons. Running both and merging beats picking one. | Single engine (loses recall in the other's blind spot), Tesseract (worst recall in pilot), PaddleOCR full (heavier deps, marginal gain over RapidOCR) | Pilot validated both engines pull their weight. Decay: a future single engine that subsumes both would simplify (PaddleOCR v5? Apple's next Vision release?). |
| Projection filter | `text` must contain ≥3 alphanumeric chars before projection into `frame_text` | Apple Vision hallucinates short isolated tokens (`==`, lone Cyrillic) on noisy regions. RapidOCR has similar noise floor. Filter keeps the raw detections full-fidelity but cleans the editor surface. A cheap LLM QA pass on top (a small model judging each detection plausible vs. suspicious, a few dollars over ~70K rows) caught a further ~13% of noise the character heuristic missed; the hallucinations are dual-engine (both OCR engines emit confident pseudo-words on noisy regions), so the filter applies to both. The QA-verdict pattern generalizes to any enrichment layer. | Stricter (≥5 chars; loses legitimate 3-letter signage), looser (more noise in editor queries), per-engine confidence gate only (still leaks hallucinations) | Filter is at projection time; raw detections are retained so the threshold can be re-tuned without re-OCR. |
| Sampling strategy | Shot-aware: 1 frame at shot midpoint + 2 extras at 25/75% for shots ≥10s + every 10s for shots ≥60s | ~26K frames total vs. ~100K if we'd used the SigLIP 1/7s cadence. Same coverage at ~10× lower compute because most shots have stable on-screen text. | Fixed cadence (overspends on long interviews where text is static), per-frame (overkill for B-roll) | Doc content: lower-thirds change at chyron boundaries, signs are static within a shot. |
| Bib subset | Numeric-pattern regex (`\d{2,4}`) over `ocr_detection` → separate `bib_hit` table | Cheap derived projection. Race-day footage is sparse in this corpus, but the regex is free; bibs join cleanly to a future `bib → p_id` map. | Separate OCR pass for bibs (wasted compute), in-line filtering (loses the general OCR data) | Bib → athlete map is deferred; the regex projection sits ready. |

### Shot quality

| Decision | Choice | Rationale | Alternatives | Assumption / decay |
| --- | --- | --- | --- | --- |
| Sharpness metric | Variance of Laplacian, averaged over up to 4 frames per shot at insetted quartiles (10%, 35%, 65%, 90%) | Cheap, well-understood, robust to content type. Insetted quartiles avoid shot-boundary motion blur. | LAP4 / LAP8 (marginal precision gain, not worth the compute), DCT-based (more sensitive to JPEG artifacts), neural blur detection (heavy, opaque) | Doc B-roll. Threshold (<100) calibrated against a 30-shot manual review. |
| Setup/teardown heuristic | `(shot_idx == MIN OR shot_idx == MAX) AND duration_sec ≤ 5` | Catches camera-up / hand-out-of-frame moments + PySceneDetect micro-fragments at asset edges. Pure visual heuristic. | Audio-aware (more precise but couples to the audio-quality layer), longer duration window (more false positives on legit cutaways), no heuristic (editor scrubs through noise) | Operator-flagged followup: an audio-presence check at first/last 2 sec would tighten precision. |
| Threshold persistence | Raw metrics (`sharpness_score`, `motion_score`, `exposure_mean`, `clipping_ratio`) stored alongside derived flags | Re-derive flags via SQL `UPDATE` if thresholds drift wrong for a specific shoot. No re-decode needed. | Flags only (lose tunability), thresholds in DB config (over-engineering for a small set of constants) | Thresholds are constants at the top of the runner. |

### Audio quality

| Decision | Choice | Rationale | Alternatives | Assumption / decay |
| --- | --- | --- | --- | --- |
| DSP engine | ffmpeg `astats` filter | Cross-platform, stable, single-pass over the WAV. Already in the pipeline (transcription). | librosa (Python; ~5× slower, more deps), sox stats (less ergonomic on per-asset batch), neural audio classifiers (overkill for this layer's goals) | WAVs already extracted upstream. astats output is line-parseable. |
| Per-asset granularity | One row per asset (not per shot) | Audio characteristics are usually whole-take properties (room tone, mic placement, lavalier vs. shotgun). Per-shot would multiply rows ~10× for marginal information. | Per-shot (overkill), per-segment (would need transcript segment alignment) | Doc footage: a few minutes of monologue or ambient is the natural unit. |
| Drone-aware metric set | Standard set (RMS, peak, clipping, silence ratio) + wind/handling flags | Originally over-classified DJI camera_id as "drone" (DJI_osmo is mostly handheld Action / Pocket). Hardware-takeaway section in `dataset/dataset_README.md` documents the cross-signal pattern (shoot_label + category_name + Gemini camera_movement) for real drone identification. | Single "is_drone" flag from camera_id (wrong; later corrected via cross-signal review), audio-only drone-blade frequency detection (interesting but lossy in mixed environments) | Hardware taxonomy is documented at the dataset level so other layers can reuse it. |

### FAISS visual similarity

| Decision | Choice | Rationale | Alternatives | Assumption / decay |
| --- | --- | --- | --- | --- |
| Index type | `IndexHNSWFlat` (M=32, efConstruction=200, METRIC_INNER_PRODUCT) | Graph-based ANN, sub-millisecond query at 116K vectors. No IVF buckets to retrain when corpus grows. At this scale HNSW is overkill for accuracy and ideal for latency. | `IndexFlatIP` (exact, still milliseconds at 116K; smaller file but slower query), `IndexIVFPQ` (compressed, useful past ~1M vectors; quality loss not worth it here) | Under 1M vectors. Past that, switch to IVFPQ or PQ-compressed HNSW. |
| Inner product metric | IP on L2-normalised vectors = cosine | SigLIP outputs come L2-normalised already. IP avoids a per-query normalisation that L2 metric would add. | L2 metric (adds per-query norm cost), explicit cosine (FAISS doesn't have a native cosine metric) | SigLIP normalisation discipline holds upstream. |
| Position-to-meta map | Sidecar `clip_embeddings.faiss.meta.json` (36 MB) carrying `embedding_pk`, `chunk_id`, `parent_asset_id`, timestamps per FAISS position | One JSON lookup per result, no SQL join needed at query time. Keeps FAISS as a query accelerator, not a primary store. | JOIN back to SQLite per query (more latency), embed metadata in FAISS (FAISS doesn't support it natively) | Metadata fits in RAM. Past ~1M vectors this becomes ~300 MB JSON; fine, but worth re-evaluating shape. |
| Rebuild strategy | Full rebuild from `clip_embeddings` blob every time | ~3.5 sec at 33K vec/s. Simpler than HNSW's "add new vectors without retrain" path, and the path isn't well-supported for incremental grow on HNSW anyway. | Incremental add (HNSW supports it but recall drifts), persistent ANN service (overkill for a single-machine workflow) | Build cost is trivial relative to upstream embedding cost (~1.5 hr SigLIP for the same set). |

### Audio events (CLAP)

| Decision | Choice | Rationale | Alternatives | Assumption / decay |
| --- | --- | --- | --- | --- |
| Dual engine | LAION-CLAP + MS-CLAP-2023, merged with engine-tagged rows | A stratified pilot (30 clips) showed only 40% top-1 cross-agreement: LAION catches rare specific events (drone propeller, music, panting), MS catches confident common categories (applause+breathing on race finishes, indoor+kitchen on home interviews). Persisting both with `engine` column lets editors query for OR (recall) or AND (precision). | Single engine (loses ~60% of unique signal); PaSST or BEATs (better on AudioSet-style classification, more complex pipeline) | Pilot validated both engines pull weight. Successor model (CLAP v3, AudioFlamingo) likely within a year, at which point may collapse to single engine. |
| Vocabulary | ~50 film-specific flat English tags across 8 themes (outdoor, race_event, movement, voice, vehicle, music, environment, media_artifact) | CLAP scores any English phrase against audio at query time, so vocab can grow without schema change. Themes give grouping for report output + editorial filters. | Open-vocab (no constraints; loses operator interpretability); fixed AudioSet 527 ontology (~10× larger, lots of irrelevant categories) | Vocab needs periodic revisit as new shoot types appear. |
| Vocab cleanup (post-pilot) | Removed 3 over-firing tags: `race announcer over PA system` (67% MS hit rate as default), `podcast intro music` (57%), `phone or video call voice` (interview false positives) | Pilot data made the bias obvious. Documented in `_audio_events.py::VOCAB` next to the removed lines. | Keep all tags + per-tag baseline subtraction (more sophisticated but harder to explain); ignore the bias (poisons editor queries) | Bias detected at pilot scale. New tags may also have bias; re-pilot when vocab grows substantially. |
| Per-engine threshold | LAION 0.18 (p75), MS 0.30 (p75 post-cleanup) | Calibrated from pilot score distributions: keep top-quartile hits per engine. LAION runs lower + narrower band; MS runs higher + wider; same percentile filter, different absolute thresholds. | Single global threshold (wrong-shaped distributions); ROC-curve tuning (needs labeled positives, not available at this scale) | Thresholds are constants in the runner. Re-tune on the next pilot if a new engine joins or vocab shifts significantly. |
| Shot-aware sampling | Per-shot midpoint (≥2 sec) + 15-sec interior windows on shots ≥30 sec | Mirrors OCR strategy. ~26K windows vs ~100K naive fixed-stride; same coverage at ~4× lower compute. Skip `is_silent` assets entirely (885 saved). | Fixed 7-sec stride (over-samples long static interviews); single midpoint per asset (sparse like Gemini, defeats the timed-events purpose) | Holds for doc B-roll. Music-video or fast-cut content would need denser sampling inside shots. |
| Per-asset granularity (audio_event vs audio_quality) | `audio_event` is per-window (~10 sec); `audio_quality` is per-asset | Different question: "what does this sound like, when?" vs "is this audio usable, overall?". Different time horizons. Different consumers. Keep separate canonical stores. | Single combined `audio_*` table (couples lifecycles; muddies projection); per-frame audio events (overkill, CLAP doesn't have sub-second granularity anyway) | Holds. |

### Audio fingerprint (chromaprint cross-modal linker)

| Decision | Choice | Rationale | Alternatives | Assumption / decay |
| --- | --- | --- | --- | --- |
| Engine | chromaprint via `fpcalc` (Homebrew); `pyacoustid` Python wrapper | Industry standard for audio fingerprinting (powers MusicBrainz / AcoustID). Mature, fast (~30 fp/sec on M4 Max), handles compression / sample-rate differences. Output: 32-bit hash per ~128ms frame at ~7.8 Hz. | audfprint (Dan Ellis; less battle-tested on compressed audio); panako (overkill; designed for time-stretched / pitch-shifted matching, which production audio doesn't need); whisper-based audio embeddings (semantic not acoustic; wrong tool) | Chromaprint is genre-agnostic; if a project picks up heavy music-cue work, panako's pitch-invariant matching might be worth re-evaluating. |
| Matching | Bit-Hamming similarity via vectorized 8-bit popcount LUT (numpy 1.x has no `bitwise_count`); sliding window across the longer fingerprint | Standard chromaprint comparison. `sim = 1 - mean_popcount/32` in [0..1]; ≥0.7 = aligned same source. Brute-force slide is O(n*m) but at this corpus's scale (~60-200 hashes per fp) takes <1ms per pair. | Inverted-hash index on top byte (AcoustID's production approach; needed past ~50K assets); exact-hash equality (faster but sparse; misses near-matches) | Holds under ~50K asset count. Past that, build the inverted index. |
| Two-stage prefilter | (1) Path-overlap heuristic (`shoot_label` exact / substring + `shoot_date` + `camera_id` family) thresholded at 0.30; (2) 16-frame quick equality probe thresholded at 0.30 | Cuts 1.5M raw pairs to 15K compared, of which 14.9K are quick-skipped (98% filter rate) before any full chromaprint match. Total wall-clock: <1 min for 4.6K fingerprints × pairwise. | Brute-force all pairs (~hours of work for no recall gain); shoot-label-only prefilter (loses cross-day backups); transcript-based prefilter (couples to a different pipeline) | Path overlap is reliable when operator folder discipline holds (offload naming). Across-shoot legitimate matches are rare in this corpus; revisit if it stops holding. |
| Apply semantics | Mirror PC-side typed-relation convention: `linked_assets.audio[].link_kind = audio_video_reverse` (video side), `linked_assets.video[].link_kind = audio_video_transcript` (audio side), both with `established_by: chromaprint_pairwise_match` + scores + offset_frames | Reuses the existing slot the editor already queries: zero learning curve, immediate editorial value. New `established_by` value distinguishes chromaprint contributions from snippet/transcript-derived links for query-time filtering and rollback. | New `audio_source` / `audio_overlap` typed `link_kind` (per an older design note); flat `linked_*_asset_ids[]` slot (turns out that's a different / older convention; the typed-relation pattern won) | Editor consumer hasn't demanded typed kinds yet. `combined_score` is preserved per link so editors can derive `audio_source` vs `audio_overlap` at query time. Schema-add when demand arrives. |
| Overlap with existing pipelines | 89% of chromaprint's high-confidence proposals were missed by the existing PC-side transcript + filename-snippet linkers | Different brands' filenames (GoPro / DJI), RED-bundle cross-asset audio, Tentacle Track production-bag, none of which carry filename / transcript signal that the prior linkers depend on. Chromaprint is genuinely additive, not redundant. | n/a | Holds: chromaprint catches a different class of pair. The 10% overlap is a useful cross-validation signal. |

---

## Stage 4 · Build + iterate the dataset

### Promote transcripts + entity backfill

| Decision | Choice | Rationale | Alternatives | Assumption / decay |
| --- | --- | --- | --- | --- |
| Entity backfill | Whole-word string match against people, orgs, and places registries | Cheap, deterministic, high precision on roster-known names | LLM-based extraction (recurring cost, lower precision on rare names), embedding match (semantic but more false positives), CRF or NER (training overhead, harder to debug) | Registries are reasonably complete; rare or new names need manual roster updates |
| Where entity slots live | On transcripts and documents only (not video, audio, or still rows) | Entities derive from text content, not bare video. Avoided duplication and the sync problem when the same asset's transcript got new tags. | On every record (redundant and sync-prone), only on a separate `entity_tags` table (loses locality with the transcript text) | Transcripts cover the relevant text source for entity tagging |

---

## Stage 5 · Expensive model passes

### Video-feed labeling (we used Gemini 2.5 Pro)

| Decision | Choice | Rationale | Alternatives | Assumption / decay |
| --- | --- | --- | --- | --- |
| Model | Gemini 2.5 Pro | Timestamps stay grounded (Flash hallucinated frame numbers in pilot, for example `key_moments` at 3,058s on a 2,880s clip). Audio plus video joint processing. 1-hour upload limit per request. Pydantic-enforced JSON schema. | Gemini 2.5 Flash (timestamp hallucination disqualified), GPT-4o (no native video at decision time), Claude (no video API at decision time), Twelve Labs Pegasus (vision-only, separate billing), open-source video LLMs (about 5× worse on doc benchmarks at decision time) | Cloud API acceptable. About $2/hr catalog cost tolerable. High decay risk: successor model likely better and possibly cheaper. |
| Provider | Vertex AI primary plus AI Studio fallback | Independent quotas on the same Google Cloud project. Failover when one throttles. | Vertex only (single quota), AI Studio only (1K generate per day cap on Tier 1), direct partner billing (no fallback path) | Both APIs return identical results on identical input |
| Chunking | Split clips over 55 min into about 50-min segments via ffmpeg `-c copy -f segment` | Stays under the 1-hour API limit. Stream-copy is free (no re-encode). Audio sync preserved at segment boundaries. | Always single (fails on long interviews), aggressive splitting (more API calls means more cost and more 429s), segment-by-keyframe (more complex, no clear gain) | Stream-copy preserves sync. Long-take interviews are the main over-55-min case. |
| Unit of analysis (retrospective) | Time-chunk: each Gemini pass summarizes a ~chunk-length window (`asset_semantic_chunk`, `chunk_idx`/`start_sec`/`end_sec`). | It was the natural unit given the 1-hour upload limit and 1 fps sampling, and it required no upstream dependency. | **Shot-level** (one summary per detected shot): what we'd do in hindsight. A shot-aligned `setting`/`camera`/`subject` maps 1:1 onto the editorial unit (a clip's shot), avoids a single chunk summary spanning a hard cut, and would have made the downstream sidecar fields shot-precise rather than window-averaged. | **We'd have done shot-level video parsing, but the shot-split analysis (PySceneDetect shot detection) didn't exist yet when the video pass ran**, so there were no shot boundaries to key on. With shot detection now in place, a future re-run (or successor-model pass) should parse at the shot level. Note the *separate* per-shot dense image pass (`dense_caption`, 3 frames/shot) already operates shot-aligned; the gap is specifically the video/semantic pass still being chunk-keyed. |
| Frame sampling | 1 fps (Gemini's native rate) | Matches the model's internal sampling. Finer doesn't help on doc-style content. | Higher fps (recurring cost, marginal gain on talking heads), scene-detected (loses `key_moments` timestamp precision) | Gap: action sports and fast-cut content are undersampled at 1 fps. A shot-aware pass can tag high-motion regions for finer sampling. |
| Metadata injection | None. Gemini doesn't see the catalog (people_ids, transcript topics, etc.). | Independent signal. Avoids parrot-hallucination on low-confidence catalog entries. Useful for join-time confidence checks. | Inject everything (overfits to existing metadata errors), inject high-confidence only (where to draw the line?) | Independent signal more useful than maximum recall |
| Schema enforcement | Pydantic `response_schema` with closed enums on `time_of_day`, `weather`, `shot_size`, `movement`, `perspective` | Structured output guaranteed parseable. Closed enums for fields with finite values. | Free-form output plus post-parse (brittle), JSON Schema (less ergonomic than Pydantic in Python) | Closed enums are guidance. Model follows them about 95% on Pro; downstream normalises the rest. |
| Concurrency | 8 workers default, never exceed 14 | 8 sits below Vertex per-region RPM cap with margin. 14 and above triggers cascading 429s that aren't billed but waste wall clock. | 1 (too slow for production batch), 4 (underutilizes quota), 20 (wasted retries) | Per-project RPM cap. Quotas could change. |

---

## Stage 6 · Consolidate + rebuild

### Workspace consolidation

| Decision | Choice | Rationale | Alternatives | Assumption / decay |
| --- | --- | --- | --- | --- |
| Rebuild strategy | Monolithic `rebuild_all.cmd` (6 numbered steps plus STEP 0 bindfs cleanup) | Idempotent, deterministic, easy to audit (each step's output is JSON committed to git) | Incremental (Make or Bazel dep graph; complex setup, brittle), reactive file-watch (fragile), single-shot Python (loses step granularity for partial failure) | Rebuild fits in under 30 minutes of unattended runtime. Corpus fits in memory during build. |
| Step granularity | 6 steps plus STEP 0 | Each step is rerunnable in isolation. Clear failure surface (which step exited non-zero). | Single mega-script (no resume), per-file scripts (orchestration overhead) | Steps map cleanly to natural data lifecycle stages |
| Slim strategy | Drop dup Gemini JSON from embeddings DB after copy to catalog | Embeddings DB stays vector-only. Catalog stays text-only. No dup truth. | Keep dup (no clear source of truth), only in DB (catalog can't be diffed in git) | Catalog is the editorial source of truth. Embeddings DB is the query accelerator. |
| Bindfs cleanup | STEP 0 deletes `.fuse_hidden*` shadow files in `indexes/` | Cowork bindfs mount leaves shadows that break SQLite readers | Re-mount (operator-level, not scriptable), ignore (intermittent SQLite errors) | Operator may run through Cowork. Cheap to always do. |

---

### Copying the workspace between machines

| Decision | Choice | Rationale | Alternatives | Assumption / decay |
| --- | --- | --- | --- | --- |
| Sync mode | Full copy (Explorer, robocopy, SanDisk transport) | Simpler than incremental. Corruption easier to debug. No merge state to reconcile. | rsync incremental (faster but partial-state bugs across platforms), Syncthing continuous (no manual handoff but always-on, conflict resolution gets hairy), git-LFS (binary bloat, not for this scale) | Workspace fits on a transport SSD. Copy window under 24 h. Decay risk: painful beyond about 5 TB. |
| Pre-copy hygiene | Close Premiere plus SQLite writers; checkpoint WAL (`PRAGMA wal_checkpoint(TRUNCATE)`) | Avoids orphan `-wal` and `-shm` pairs on the destination. SQLite recovers from clean copies. | Hot copy (orphaned WAL files break readers), forced kill (data loss in WAL) | Operator can afford a short close window |

---

## Decisions we know will need revisiting

Decay-flagged from above, ranked by likely impact and switching cost:

1. ASR engine. MacWhisper Large v3 Turbo. Whisper v4 or successor likely within 18 months. Re-transcribe cost is about 90 minutes of compute per 25 hours of audio. Catalog schema is forward-compatible.
2. Gemini 2.5 Pro (video labeling). Successor model likely. Cost calibration ($2/hr) is baked into the budget; a successor may be cheaper or pricier. Schema is independent of model version.
3. SigLIP-So400m. SigLIP-2 or a later open-source vision encoder likely better. Full re-embed is about 1.5 hours on M4 Max plus 460 MB discard. Worth tracking benchmarks every 6 months.
4. 7-second keyframe cadence (SigLIP). Assumes doc-style 3-8 second shots. PySceneDetect-driven sampling is the principled fix for fast-cut content.
5. Per-asset JSON catalog (cross-cutting). Fine at under 100K assets. Painful at 1M and above. Migration path: SQLite-as-source-of-truth with JSON export for git diffs.
6. Full-copy workspace sync. Fine at under 5 TB workspace. Painful beyond. Migration path: rsync incremental with content-addressed dedupe.
7. Atomic-write assumption (cross-cutting). Already broken on sandboxed-mount agents (Cowork class). Native-FS modes (direct local, CLI agents like Claude Code, Codex, Aider, IDE agents like Cursor) are unaffected. Per-mode guidance in `README.md` § "Working with the workspace". Safe-write pattern is the universal fallback if the mode mix gets messy.
8. Face detector + embedder. InsightFace buffalo_l. Like Whisper / SigLIP, a successor checkpoint is likely within a year. Re-embed cost is moderate (~1.5 hr on M4 Max) plus an HDBSCAN re-cluster (~45 min, single-threaded MST). Labels survive the re-embed if cluster centroids stay close; otherwise the labeling pass repeats.
9. OCR dual-engine merge. RapidOCR + Apple Vision. If a single engine subsumes both (PaddleOCR v5 in cross-platform mode, or a future Apple Vision release that handles small bibs as well as it handles lower-thirds), drop the dual pass. Re-OCR cost is ~30 min on M4 Max (small).
10. FAISS index type. `IndexHNSWFlat` is right under 1M vectors. Past that, IVFPQ or PQ-compressed HNSW becomes worth the complexity. SigLIP keyframe count grows linearly with new shoots; revisit when the catalog crosses ~500K keyframes.
11. CLAP dual engine. LAION-CLAP + MS-CLAP. Same successor-model risk as Whisper / SigLIP / face. The pilot pattern (run both, eyeball, drop the over-firing tags) is reusable when a new engine joins; the vocab is the durable artifact.
12. Chromaprint match-pair fan-out. Brute-force prefiltered match works at ~5K assets. Past ~50K assets, build the inverted top-byte index AcoustID uses. Path-overlap prefilter strength assumes operator folder discipline holds.

---

*Organized to follow INGEST.md's six cost-ascending stages; the per-decision tables keep their original phase letters as cross-reference labels.*
