# Editor
*Query the corpus, compile selects into a timeline, cut in your NLE, with the agent as researcher, second set of eyes, and in-house EP.*

## What the agent is for

We're cutting a high-touch feature documentary, and the division of labor is deliberate: **the judgment and the storytelling stay human**; that's the best part of the work. The agent is a helper that makes the human judgment faster and better-informed:

1. **Surface and retrieve footage fast.** Smart queries over the full corpus ("verité of X," "quotes about Y," "b-roll that could bridge these two scenes") answered in seconds from the catalog instead of hours of scrubbing.
2. **A second set of eyes on the cut.** The agent reads the cut through one **sidecar JSON** per act (clip geometry + transcripts + diarized speakers + editorial notes, denormalized into a single file) and an **HTML review view** rendered from it. QA passes over those flag what a tired editor misses: a speaker dominating a scene whose stated purpose doesn't mention them, mid-word cut points, stale rationale, visually jarring transitions.
3. **Critical, like an EP.** The story-framework resources (`story/_resources/`) exist so the agent can push back: diagnose the cut against the framework library and say "your second act evades its own dramatic question" with receipts.

That's our dial setting. Someone else might want a more hands-on agent: full scene assembly by default, or even direct-to-MP4 cuts with no NLE (see the interchange notes in [`xml exports/xml_README.md`](xml%20exports/xml_README.md)). The layers here support the whole spectrum; what they never do is make the call for you.

## The working loop

1. **Query** (`queries/`): composers over `editorial_catalog.sqlite` + the embeddings: keyword FTS over transcripts, SigLIP visual similarity, b-roll filters that join shot quality / faces / OCR / captions. Start at [`queries/queries_README.md`](queries/queries_README.md) and its text-driven query protocol.
2. **Compile** (`xml exports/`): turn the selects into a valid xmeml: a selects reel, a scene assembly, or candidates parked after the scene end for scrub-preview (see the b-roll parking convention below). Builders live in `xml exports/_scripts/` and `xml exports/scene_workspace/`.
3. **Cut in Premiere**: import the XML as a fresh sequence and edit by hand. The craft pass is manual on purpose.

Occasionally we have the agent **recreate a full scene** (next section). Sometimes it's easier to just make the change in Premiere manually. Decide per scene; the tooling doesn't care which way you go.

**The artifacts to know from day one:** the **sidecar JSON** ([`story/sidecars/sample.sidecar.json`](story/sidecars/sample.sidecar.json) is a shipped, working example; schema in [`story/sidecars/sidecars_README.md`](story/sidecars/sidecars_README.md)) is what the agent reads and annotates; the **HTML review view** ([`story/html views/sample_2col.html`](story/html%20views/sample_2col.html)) is the human-readable render of the same file; open it in a browser to read the cut as a story. A third view, the **Markdown story brief** ([`story/html views/sample_story_brief.md`](story/html%20views/sample_story_brief.md), via `build_story_brief.py`), flattens the sidecar into hierarchical Markdown, the simplest surface to hand an LLM for narrative feedback.

## Python vs. agent reasoning

Deterministic transforms go in Python; narrative and content judgments are made in-session by the agent. Workspace policy: see [`../AGENTS.md`](../AGENTS.md), P1 and P3.

## The exported timeline is the canonical cut

The timeline export under `xml exports/` (ours: xmeml v4) is the source of truth for clip geometry: in/out, timeline placement, tracks, file refs. The Premiere `.prproj` is a working surface: open it, cut, `Export → Final Cut Pro XML`. Or skip Premiere for structural changes and edit XML directly; either way the XML is what downstream tooling reads. For LLM-edited XML going back to Premiere: **import as a fresh sequence, never overwrite the `.prproj`**.

xmeml is just *our* interchange format because we cut in Premiere. The same compile-selects-into-a-timeline step works against **FCPXML** (Resolve, Final Cut), **AAF** (Avid), **EDL** (lossy, universal), **OTIO** (NLE-agnostic), or **no NLE at all**: ffmpeg rendering the cut straight to MP4 from the sidecar + catalog. The cut decisions live in the catalog and sidecar, not in any project file, so the NLE is a swappable front-end. Format-by-format notes: [`xml exports/xml_README.md`](xml%20exports/xml_README.md) § "Other interchange targets".

**The sidecar layer carries everything XML can't.** For agent-led scene work we generate one Act-scoped sidecar JSON from the cut: `beats[]` (with nested `scenes[]`) plus a flat `annotations[]` list where each clip carries its editorial fields (`rationale`, `lower_third`, `audio_spine`, `_force_ride`, …) and denormalized context (asset metadata, diarized speakers, transcript text, timing): one self-contained, LLM-ready file. The schema ships at [`story/sidecars/sidecars_README.md`](story/sidecars/sidecars_README.md); the sidecars themselves are generated from *your* cut by `story/_sidecar scripts/`. Identity is a content key (asset_id + source in/out + timeline start + track), never `premiere_object_id`; Premiere renumbers those on every export. Round-trips mostly auto-rebind; `reconcile_sidecar.py` proposes matches for the orphans.

## Scene-sandbox workflow (the agent-recreates-a-scene path)

When the agent rebuilds a scene (swapping b-roll, re-cutting an A-roll spine, repositioning lower thirds), work in a **scene-scoped XML sandbox**, not the live Act XML and not live MCP edits:

1. **Write a Python build script** at `xml exports/scene_workspace/_build_scene_<beat>_<scene>.py` that emits a self-contained xmeml for just that scene (typical shapes: a multi-source assembly, or a single-source A-roll with hand-cut in/outs plus b-roll).
2. **Output** `scene_<beat>_<scene>.v<n>.xml`; validate with `story/_sidecar scripts/validate_xml_structure.py`.
3. **Import** into Premiere: fresh sequence, nothing overwritten.
4. **Review, mark up, send notes back** as in/out changes to the script.
5. **Re-emit.** Each rev is ~3 seconds end-to-end.

**B-roll parking convention:** the agent adds 5–10 short candidate clips on an empty track *after the scene end*, so the editor scrub-previews and drags the keepers into the scene. High-conviction picks go straight into the scene's b-roll track; everything else waits in the lot. This is the default any time the editor hasn't pre-committed to specific b-roll; it offers choices instead of making them.

**Why this beats driving Premiere over MCP for scene work:** no ripple constraints (the sandbox has only its own clips), no state drift (every rev is a clean re-emit, not a mutation chain), the build script doubles as a regression after the next master export, and it's ~10× faster per rev. Live MCP control exists (`../premiere mcp/`) and is useful for introspection and one-off operations, but for iterating a scene the sandbox wins.

See [`xml exports/scene_workspace/scene_workspace_README.md`](xml%20exports/scene_workspace/scene_workspace_README.md) for layout, script conventions, and the track cheat sheet (V1 = A-roll spine, V2 = b-roll, V3 = graphics; A1+A2 = dialogue, A3 = nat sound, A5+ = music/SFX).

## The second set of eyes (QA + EP tooling)

What the agent runs *against* the cut, after every meaningful change:

| Tool | What it catches |
|---|---|
| `story/_sidecar scripts/qa_sidecar.py` | Per-scene speaker breakdown + transcript quotes; auto-flags any significant speaker whose name isn't in the scene's stated `label + purpose`: the "is this scene actually about what we say it's about" check. Severity-ranked (dominant-speaker mismatches vs. minor cutaways). |
| `queries/cut_audit.py` | Read-only cross-reference of xmeml + sidecar + editor notes: missing rationale, source-bound violations, stale-note claims. |
| `sidecar_cut_eval.py` | Cut-boundary checks (mid-word cuts via transcript word timing; visually jarring cuts via SigLIP delta when the index is built). |
| HTML review render | One page per Act from the sidecar (beats as sections, scenes as collapsibles) for reading the cut as a story rather than a timeline. |
| `story/_sidecar scripts/build_story_brief.py` | The same cut as hierarchical Markdown (Act → Beat → Scene → clips with semantic descriptions + speaker-prefixed transcript), the simplest surface to hand an LLM for narrative feedback. |
| `story/_resources/` (33 frameworks + 11 principle families) | The EP layer; each framework ships with an empty `project_relevance` slot for your own application notes. Used to interrogate structure: "which scenes advance the dramatic question," "where does the outer/inner journey decouple." See [`story/editor_story_README.md`](story/editor_story_README.md). |

The contract for all of these: **the script's job is evidence; the agent's job is judgment; the editor's job is the decision.** QA findings come back as flags with proposed fixes; the agent doesn't autonomously move scene content without a standing instruction.

## Agent-led re-cuts of an Act (heavier, occasional)

Two maintenance modes for the sidecar after the cut changes:

**Vanilla refresh**, after any Premiere re-export with no material structural reshape:

```powershell
$env:PYTHONIOENCODING="utf-8"
py "story\_sidecar scripts\refresh_act_sidecar.py" --skip-visual-cut
```

Five phases (~7s): rebuild resolver → re-extract sidecar (editorial fields survive via content-key inheritance) → populate model-summary fields from the catalog → denormalize (speakers, transcripts, timing) → render HTML. Pass `--skip-visual-cut` unless `faiss` is installed (that phase aborts the pipeline rather than soft-failing). Verify the output (`xml_sha256` flipped, annotation count plausible, `with_transcript` high) rather than trusting exit codes; several phases soft-fail silently.

**Transcript-grounded boundary walk**: for re-cuts that materially reshape an Act (scenes moved/merged/gutted; ≥10% of annotations shifted across scene seams). A vanilla refresh is *wrong* here: it reuses the prior sidecar's scene ranges. The runbook, condensed:

1. **Baseline**: pick the last sidecar whose scene `purpose` prose was hand-validated against a real cut, never a recent mechanical refresh.
2. **Deterministic diff**: match baseline annotations to the new cut by `asset_id + source-window overlap` (not exact content key); roll up per-scene survival %. ≥85% survival → boundary nudges; <50% → rebuild that scene from the transcript; 0% → flag `content_removed`.
3. **Spine walk (the actual work)**: dump the new cut's spine in timeline order with diarized `speakers[].p_id` and `transcript_text`, and *read it*. Anchor each scene boundary on a transcript moment, not b-roll geometry. Never reason from `chunk_*` model-summary fields here: primary sources only (P1).
4. **Apply with the immutability rules**: beat/scene names never change (flag `name_mismatch` instead); new scenes land `proposed: true`; gone scenes keep their definition with `timeline_range_frames: null` + `content_removed`; beat seams move only on evidence, in both the manifest and the sidecar.
5. **Refresh, then QA**: run the vanilla refresh, then `qa_sidecar.py`, then read the QA report and adjudicate each finding (real mismatch → propose a seam move; terse or stale `purpose` prose → rewrite the prose; diarization error → upstream transcript fix). Log what you fixed and what you left flagged.

Why this isn't a Python script: scene boundaries are *transcript reading*, not pattern matching on metadata. We tried the script version early on; it's the canonical example of the P3 failure mode. The scripts here are I/O: they emit the spine view and apply the result; the walk itself is the agent reading.

## Layout

```text
editor/
├── editor_README.md            this file
├── editor_GAPS.md              open gaps (ships cleared)
├── project.py                  workspace paths + ProjectConfig
├── pyproject.toml, uv.lock     deps
├── sidecar_cut_eval.py         cut-boundary checks
├── queries/                    read-only editorial queries - START HERE (queries_README.md)
├── xml exports/                xmeml builders + direct-edit scripts (xml_README.md)
│   ├── _scripts/               insert clips, rewrite pathurls, ticks math, validation
│   └── scene_workspace/        per-scene sandbox builders (scene_workspace_README.md)
├── story/
│   ├── _sidecar scripts/       refresh orchestrator, resolver, sidecar make/validate/QA, HTML render
│   ├── sidecars/               sidecars_README.md (schema v2) + the shipped sample sidecar
│   ├── _resources/             33 story frameworks + 11 principle families + manifest (the EP layer)
│   └── editor_story_README.md
└── graphics/                   HTML/CSS → PNG → timeline graphics notes (graphics_README.md)
```

Working artifacts this layer generates as you cut: sidecars + beats manifests, resolver indexes, HTML review pages, and your `.prproj` files under a `premiere projects/` folder. A complete **shipped example** built from the sample assets (scene XML → sidecar → two-column HTML + Markdown story brief) is documented in [`story/sidecars/sidecars_README.md`](story/sidecars/sidecars_README.md) § "The shipped example".

## Sidecar format + inheritance (for the agent-led path)

Schema v2, specified in [`story/sidecars/sidecars_README.md`](story/sidecars/sidecars_README.md). The **lean fields** (rationale, lower_third, audio_spine, `_force_ride`, clip ids) are authoritative: hand- or agent-edited, carried across re-extractions by content-key inheritance. The **denormalized fields** (asset metadata, speakers, transcript text, timing, model-summary `chunk_*`) are re-derived from the catalog on every refresh; never hand-edit those. Spine overrides: `_force_ride: true` demotes a V1 clip from the spine; `audio_spine: true` promotes an A-track clip into it; both survive refreshes.

## Connectivity (LLM ↔ Premiere)

Today's path is the XML round-trip above, plus the patched MCP server for live introspection (`../premiere mcp/premiere_mcp_README.md`). Tighter-coupling options we've scouted but not adopted: UXP-based plugins (durable but slow to build) and existing agentic Premiere frameworks (fast start, less control). `pymiere` is used narrowly for `.prproj`-side Essential Graphics text reads; ExtendScript is deprecated, so treat it as a stopgap. The sidecar reconciliation and XML validation layers are substrate-agnostic (xmeml in / xmeml out), so a connectivity swap doesn't disturb them.

## Setup

`uv sync` from `editor/`. Sidecar scripts are stdlib-only. `queries/` needs `numpy` + `sentence-transformers` (transcript text queries); visual text-similarity additionally pulls `transformers` + `torch` + `Pillow` lazily, with SigLIP weights (~3.5 GB) downloading to `indexes/_cache/hf/` on first use. The `xml exports/_scripts/` helpers need `lxml` only.
