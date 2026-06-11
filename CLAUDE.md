# CLAUDE.md: agent entry point
*Claude-flavored onboarding: read order, the routing map, and the per-asset rules.*

Open-source post-production stack: a media asset catalog built from open tools,
plus the editorial tooling layered on top. **Start here:** [README.md](README.md).

> **Policy precedes work.** [AGENTS.md](AGENTS.md) holds the portable,
> model-agnostic agent policy; this file is the Claude-flavored pointer to it.
> Two policies bind every session:
> - **P1: do judgment work yourself; batch the bulk.** Make editorial calls
>   (scene boundaries, labels, shot choice) in-session from transcripts + diarized
>   `speakers[].p_id`. Reserve external API models for corpus-scale passes the
>   subscription can't cover.
> - **P2: name fields for the data, not the model.** Provenance lives in the
>   field's value, not its name; for identity use `speakers[].p_id`, never a
>   model's `semantic_subject` (it hallucinates names).

## Read order (new session)

1. [README.md](README.md): what this is, layout, conventions
2. [INGEST.md](INGEST.md): what to do when new content lands (cards → catalog → indexes)
3. [DESIGN.md](DESIGN.md): rationale + alternatives per phase
4. [editor/editor_README.md](editor/editor_README.md): the cut + sidecar layer
5. [dataset/dataset_README.md](dataset/dataset_README.md): catalog source of truth, rebuild
6. [indexes/indexes_README.md](indexes/indexes_README.md): SQLite query surface

## Routing map: reach for the right layer first

| Question type | First stop |
|---|---|
| **"Find me clips / quotes / b-roll about X"** | [`editor/queries/queries_README.md`](editor/queries/queries_README.md) § Text-driven query protocol: parse → route → diversify → confirm before building |
| Resolve scene state, beats, annotations | `editor/story/sidecars/` (Act sidecar + `refresh_act_sidecar.py`) |
| Build / re-cut a single scene from scratch | `editor/xml exports/scene_workspace/` (`_build_scene_*.py` pattern) |
| Splice a scene into an Act / ripple-shift | `editor/xml exports/_scripts/` |
| Catalog ingest, rebuild, schema | `dataset/_scripts/` (per INGEST.md stages) |
| Frame / pproTick math, pathurl encoding | `editor/xml exports/_scripts/_pproticks.py` |

Output is not Premiere-only: see README § "How it works" (*It is portable*) for the
ffmpeg direct-render (no NLE), FCPXML / AAF / EDL, and OTIO paths.

## Per-asset rule (always)

Look up assets by `asset_id` (sha256 content hash), **never** by filename:
camera-card filenames (`C0050.MP4`) repeat across shoot days. For speaker identity
use transcript `speakers[].p_id`, **never** a model's `semantic_subject` (it
hallucinates names).

## Write vs read

| Update | Location |
| --- | --- |
| Catalog records | `dataset/assets/` then a targeted rebuild |
| Per-clip editorial notes | `dataset/assets/editor_notes/{asset_id}_editor_notes.json` |
| Workflow learnings | Nearest `*_README.md` |
| Open gaps | `dataset/*_GAPS.md`, `editor/*_GAPS.md`, `indexes/*_GAPS.md` |

Do **not** edit `indexes/*.sqlite` by hand; they are regenerated from dataset JSON.

## Conventions

- **NTSC 23.976** (= 24000/1001 fps); `pproTicks = frame * 10_594_584_000` exactly.
- `asset_id` = content hash (head 1 MiB ‖ tail 1 MiB ‖ filesize), not filename.
- Catalog JSON is the source of truth; SQLite/FAISS indexes are derived and rebuilt.
