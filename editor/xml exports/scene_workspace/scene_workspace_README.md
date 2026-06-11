# `scene_workspace/`: single-scene XML sandbox

Drop-in sandbox for iterating on one scene at a time without touching the live Act XML or risking Premiere-side state drift. Each scene gets:

- A **Python build script** (`_build_scene_<beat>_<scene>.py`) that emits the scene's xmeml from scratch
- One or more **versioned XML outputs** (`scene_<beat>_<scene>.v<n>.xml`), importable into Premiere as fresh sequences

## Why this exists

For single-scene editorial work (re-cutting A-roll moments, swapping b-roll, repositioning lower thirds), this is **the preferred path over MCP-direct edits**. See `editor_README.md` § Scene-sandbox workflow for the full rationale.

Short version:

- No ripple constraints (each scene XML has only its own clips)
- No state drift: every rev is a deterministic re-emit from the script
- Iteration is fast (~3s per rev) and reproducible (re-runnable from the script)
- The script doubles as a regression check when the master XML re-exports

## Layout

```
scene_workspace/                       (builders are written per scene as you work; one shipped example below)
├── scene_workspace_README.md          this file
├── _build_scene_sample.py             SHIPPED example: 3 sample-asset clips, V1 + linked stereo A1
├── scene_sample.v1.xml                its output (feeds the sidecar example - see story/sidecars/sidecars_README.md)
├── _build_scene_<beat>_<scene>.py     one build script per scene (e.g. a multi-source assembly,
│                                      or a single-source A-roll with hand-cut in/outs + b-roll)
├── scene_<beat>_<scene>.v1.xml        output of that script
└── scene_<beat>_<scene>.v2.xml        next rev (after Premiere import + notes + re-emit)
```

File naming (scene_workspace), recommended convention:

```
act<ACT#>_b<NN>_s<NN>_<brief descriptor>.xml
```

- `a<ACT#>`: act number, e.g. `a2` for Act II (not `act II`).
- `b<NN>_s<NN>`: beat/scene, no internal underscore (`b06_s01`).
- `<brief descriptor>`: short, human-readable (spaces OK), describing the cut's **state / change / next step** so it's identifiable at a glance, e.g. `before interviews`, `criminal recut`, `broll options`, `crescendo A`. This **replaces** a rigid `_v<N>` suffix; the descriptor is the differentiator (use `_v2` etc. only to disambiguate same-descriptor iterations).
- Example: `a2_b06_s01_before interviews.xml`.
- The build scripts keep `_build_scene_<beat>_<scene>.py` (Python modules can't start with a number).

## Authoring a new scene

1. **Copy the shipped example** (`_build_scene_sample.py`) and grow it toward the shape you need:
   - multi-source assembly: splice multiple existing clips from master + inject new ones
   - single-source re-cut: one A-roll source cut into multiple moments + master B-tracks copied verbatim
2. **Edit the top-level constants** (the lists of cuts, the source asset IDs, the sync offsets, etc.); everything else should be reusable.
3. **Run the script**: outputs its versioned XML alongside.
4. **Validate**:
   ```powershell
   py "story\_sidecar scripts\validate_xml_structure.py" "xml exports\scene_workspace\scene_sample.v1.xml"
   ```
   Look for `valid: True`. The "file declared N times" warning is informational (counts each `<file>` reference, including stubs).
5. **Import** in Premiere: `File → Import` → pick the XML. Lands as a fresh sequence.

## Conventions

- **One source = one in/out range** on the build-script side. If the editor wants to re-cut, edit the `*_CUTS` constants and re-emit.
- **Verbatim master copies** for B-tracks (lower thirds, phone-call audio, V3 b-roll, nat sound): deep-copy from the master XML so identity stays consistent.
- **B-roll parking lot.** Park 5–10 short candidate b-roll clips on V2 (or another empty track) after the scene end, so the editor can scrub-preview and drag chosen ones into the scene. Standard pattern when the editor hasn't pre-committed to specific b-roll. Skipped for high-conviction picks.
- **Don't commit edits made in Premiere.** The build script is the source of truth. If the editor's manual changes need to be preserved, update the build script and re-emit.
- **Stills: bake the scale, don't rely on "Scale to Frame Size."** Premiere's per-clip *Scale to Frame Size* toggle has **no representation in FCP7 XML**; it's dropped on every export/import, so the editor has to re-mark photos every round-trip. Instead, when a builder emits a still at non-1280×720 dimensions, compute a baked Motion>Scale via `insert_video_clips.fit_to_frame_scale(w, h, mode="fill"|"fit")` and pass it to `_build_basic_motion_filter(scale)`. `100` = source pixels 1:1; `fill` = cover (crops edges, default), `fit` = contain (letterbox/pillarbox). A baked scale **survives** the round-trip. Video proxies stay at `scale=100` (they're already 1280×720). Expose it as a `STILL_SCALE_MODE` constant in your builder so re-cuts keep the choice.

## Track conventions

Documentary editing convention; what each track is "for." Holds for both scene sandboxes and the live Act XML. The rule is **stack upward from V1**, not from a center line: V1 is the bottom of the video stack and the spine; higher tracks appear visually on top.

| Track | Conventional use | Notes |
|---|---|---|
| **V1** | A-roll spine: interview, hero shots, the continuous story-bearing layer | Should always be filled if anything is playing. If V1 has a gap mid-narrative, the audience sees black unless V2 covers. |
| **V2** | B-roll cutaways + overlays: hides jump cuts, illustrates the speaker's words | The main non-spine layer. In short scenes, can pull double duty as the opener/transition layer too. |
| **V3** | Lower thirds, name plates, location titles | Keep graphics separate from b-roll so toggling visibility or re-cutting one doesn't affect the other. |
| **V4** | Title cards, chapter headers, transitions | Occasional full-screen text/graphics. |
| **V5+** | Stretch: PIP, watermark, logo bug, full-screen graphics | Rarely used in documentary. |
| **A1+A2** | Dialogue spine, stereo. Interview / VO audio. | Premiere auto-splits stereo into A1=L, A2=R for stereo sources; treat as a linked pair. |
| **A3** | Nat sound: production audio from b-roll, location sound paired with V1 | E.g., a drone nat-sound bed under an interview clip. |
| **A4** | Secondary dialogue: phone calls, archival audio, off-camera voices | E.g., a phone-call recording mixed with the in-room conversation on A1. |
| **A5+A6** | Music beds (stereo pair, or split: A5=cue, A6=alt) | |
| **A7+** | SFX: whooshes, transitions, foley | |

Core principle: **dialogue at the top of the audio stack, music/SFX at the bottom**, nat sound and secondary audio between. Final mix passes are easier when you can mute whole categories at once.

A subtle one for documentary specifically: some shops informally split V2 into two lanes: explicit-illustration b-roll just above V1, more atmospheric/vibe b-roll on V3 (when V3 isn't carrying graphics). Overkill for most scenes; useful to know when a scene has a lot of both.

## XML invariants

All scene XMLs must satisfy the same invariants as the Act-level exports (see `../xml_README.md`):

- NTSC 23.976 timebase, `pproTicks = frame × 10_594_584_000`
- Pathurls use Windows-style `file://localhost/E%3a/...` (lowercase `%3a`, `%20` for spaces)
- Sequence `<format>` at 1280×720 to match proxy resolution
- Every video clipitem carries a `<filter>` Basic Motion block + `<pixelaspectratio>` + `<anamorphic>`
- `<file>` first occurrence carries full body; subsequent ones are `<file id="..."/>` stubs
- XML declaration uses double quotes (`<?xml version="1.0" encoding="UTF-8"?>`)
- Reference stubs (`<file id="X"/>`, `<sequence id="X"/>`) are self-closing; empty content elements (`<description></description>`) are open/close
- No dangling `<linkclipref>` (every reference must point at a clipitem inside the same XML)

The validator at `story/_sidecar scripts/validate_xml_structure.py` checks most of these.

## Cross-references

- `../xml_README.md`: the full xmeml-invariant cheat sheet + ingest workflow
- `../_scripts/_scripts_README.md`: the act-level direct-edit helpers (`insert_video_clips.py`, etc.)
- `../../editor_README.md` § Scene-sandbox workflow: when to reach for this pattern vs MCP-direct edits vs Premiere-direct edits
