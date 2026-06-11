# AGENTS.md: portable agent policy (model-agnostic)
*Three standing rules for any AI agent in this repo, plus how to bootstrap your own per-workflow notes.*

Governance for any AI agent working in this repo, independent of vendor or model.
`CLAUDE.md` is the Claude-flavored instance; other agents (Cursor, Codex, Aider, a
future model) should read this and adapt before substantive work.

Three standing policies bind every session.

**P1: do judgment work yourself; batch the bulk.** Make editorial and judgment
calls (scene boundaries, labels, "is this the right shot", rationale) in-session,
reading transcripts and diarized `speakers[].p_id` the way a human editor would.
Reserve external API models for corpus-scale passes the subscription can't
practically cover (e.g. labeling thousands of assets at ingest). Test: dozens of
items each needing their own read → you do it; thousands of items uniformly → batch it.

**P2: name fields for the data, not the model.** A schema field name describes
*what the data is*; provenance (which model, which run) is a value *inside* the
field. So re-running or swapping a model never forces a rename, and an in-session
agent can populate the same field a batch pass would. For identity, always use
transcript `speakers[].p_id`, never a model's `subject`/`semantic_subject`; those
hallucinate names.

**P3: Python for the deterministic, you for the judgment.** Reach for Python (or any script) when the transform is deterministic and verifiable by re-running: bulk path rewrites, frame math, schema validation, structural surgery over thousands of nodes. Do the work in-session yourself when the output is a judgment about narrative or content and each item needs its own read. The usual shape is hybrid: a little Python to extract fields, your reasoning over the extract, a little Python to apply the result. The failure mode this guards against is reaching for Python by habit when the task is actually editorial.

## Bootstrap: read first, then write your own working notes

Those three are the only fixed rules. Everything else (how ingest runs, how to query
the corpus, how exports round-trip to an NLE) is an operating procedure that lives
in the layer READMEs, not here. Before doing substantive work:

1. **Read the READMEs for the area you're touching.** Start with [README.md](README.md),
   then: ingest → [INGEST.md](INGEST.md) + [DESIGN.md](DESIGN.md);
   catalog → [dataset/dataset_README.md](dataset/dataset_README.md); finding footage →
   [editor/queries/queries_README.md](editor/queries/queries_README.md); the cut →
   [editor/editor_README.md](editor/editor_README.md) +
   [editor/xml exports/xml_README.md](editor/xml%20exports/xml_README.md); query
   surface → [indexes/indexes_README.md](indexes/indexes_README.md).
2. **Distill them into your own persisted notes, one per workflow you'll repeat.**
   An ingest checklist, the editorial-query protocol (parse → route → diversify →
   confirm), the xml export invariants, the sidecar-refresh steps. Save them wherever
   your agent keeps durable memory so you don't re-derive them each session. The
   READMEs are the source procedures; your notes are your adaptation of them.
3. **Map all of the above onto your own setup** (your model tier, your tool names,
   your memory mechanism), then start.

## Workspace access modes and write safety

> 2026-Q2 snapshot. Coding-agent tooling shifts on a months-not-years cadence: model
> providers, sandbox architectures, and IDE integrations all move quickly. Specifics
> here will probably be partly outdated within a few months. The high-level
> distinction (sandboxed-mount FS vs. native FS access) should hold longer.

How a given agent reaches the workspace determines whether you need write-safety care:

| Interaction mode | Examples | Workspace access | Write safety |
| --- | --- | --- | --- |
| Direct local execution | PowerShell, cmd, double-clicked `.cmd`, Premiere, ad-hoc `python` runs | Native NTFS | Full. Atomic writes work as the scripts assume. |
| CLI coding agents (native FS) | Claude Code, Codex CLI, Aider, plain SSH session with editor | Native NTFS via the agent's local file tools | Full. Same as direct local. |
| IDE-integrated agents | Cursor, Continue, GitHub Copilot, JetBrains AI, VS Code AI features | Native NTFS via the editor | Full. Same as direct local. |
| Sandboxed desktop agents | Claude desktop in Cowork mode; any agent that mounts the workspace into a Linux sandbox via bindfs, virtiofs, or FUSE | Bindfs (or similar) mount of the Windows path | Caveat below. Writes over about 5 KB can silently truncate, and large file copies stall. |
| Hosted or cloud agents | Anything running in a remote container with the workspace pulled in | Depends on shipping method. `git pull` is effectively native (inside the container); rsync mount or sshfs is the bindfs class. | Depends on access path |

Sandboxed-agent caveat (the Cowork class). When the agent runs inside a Linux sandbox
and the workspace is exposed through a bindfs, virtiofs, or FUSE-class mount, two
failure modes have been observed:

- Writes over about 5 KB to the workspace root can silently truncate. The Edit/Write
  tool reports success but the file on disk is cut off mid-content.
- Large file copies (over about 100 MB) stall at roughly 2-4 MB/s and may time out
  before completion.

Workaround: stage writes in the sandbox's own `/tmp`, `dd ... conv=fsync` to the
mount in chunks, then verify with `tail` or `wc -c`. Reference implementation:
`editor/xml exports/_scripts/insert_video_clips.py::_atomic_safe_write()`. Any script
that mutates catalog JSON from a sandboxed agent should use this pattern. The
`reference_bindfs_write_workarounds` memory file has more detail if available.

Reads are unaffected in every mode. Exploration, queries, and SQLite reads behave the
same regardless of how the agent reaches the workspace.

## Learnings hygiene (read before ending a session)

Knowledge left only in chat does not survive context compaction or a new session.
Before ending substantive work, capture durable parts:

| Type of learning | Where it goes |
| --- | --- |
| Code patterns + **why** | Inline comments in the relevant `.py` |
| Workflow / process | Nearest `*_README.md` (e.g. `xml_README.md`, `queries_README.md`) |
| Per-asset editorial findings | `dataset/assets/editor_notes/{asset_id}_editor_notes.json` |
| Hard-won invariants | Inline + one line in the relevant README "lessons" section |

**Skip:** ephemeral state, in-progress tasks, things derivable from reading code or
`*_GAPS.md`.
