# `xml exports/`: XML-direct editing notes

The NLE's timeline export is the canonical cut: for us, xmeml v4 from Premiere. Direct-editing it (script-emitting new clipitems, ripple-shifting, replacing tracks) is supported, but Premiere has more invariants than the spec hints at. This file captures what we learned the hard way so future sessions don't relearn it one wrong-import at a time.

> **Update this README as you find new invariants.** Several bugs in this codebase had already been characterized in this file before they bit downstream consumers. The discipline is: whenever you discover an XML-shape or sentinel rule that another script gets wrong, capture both the rule AND the downstream-consumer effect here, in the same pass as your fix. Look for the existing invariants table and the "-1 sentinel cheat sheet"; extend them rather than re-discovering in a year.

For the helper script + CLI, see `_scripts/_scripts_README.md`. For the plan JSON format, also `_scripts/_scripts_README.md`.

For **single-scene iteration** (re-cutting one scene without touching the rest of the Act), see `scene_workspace/scene_workspace_README.md`, the preferred path for that workflow.

## Output naming convention

Both act-level XMLs and scene-sandbox XMLs follow a consistent pattern so a sorted directory listing reads like a history.

**Act-level XMLs** (`editor/xml exports/`):

```
project_act<ROMAN>_<proxies|original>_<date>[_<contextual_modifiers>].xml
```

| Token | Examples |
|---|---|
| `<ROMAN>` | `I`, `II`, `III` |
| `<proxies\|original>` | `proxies` for 1280×720 mirrored-tree builds; `original` if you ever round-trip to source 4K/8K |
| `<date>` | `20260522` (just-date) or `20260526T1614` (datetime, when multiple builds per day) |
| `<contextual_modifiers>` | Optional. The reason this build exists: `criminal_v2`, `pre_color_pass`, `b06_recut`, etc. |

Examples:
- `project_act II_proxies_20260522.xml`: clean Mac re-export, ingested
- `project_act II_proxies_20260526T1614_criminal_v2.xml`: act-level XML with the criminal scene swapped in from a scene sandbox

**Scene-sandbox XMLs** (`editor/xml exports/scene_workspace/`):

```
act<ACT#>_b<NN>_s<NN>_<brief descriptor>.xml      (canonical: scene_workspace_README.md)
```

| Token | Examples |
|---|---|
| `a<ACT#>` | `a2` (Act II), numeral, not Roman |
| `b<NN>` | Beat ID without internal underscore (`b06`, not `b_06`) |
| `s<NN>` | Scene ID likewise (`s01`, not `s_01`) |
| `<brief descriptor>` | short, human-readable (spaces OK), the cut's *state / change / next step*: `before interviews`, `criminal recut`, `broll options`, `crescendo A`. Replaces a rigid `_v<N>`; add `_v2` only to disambiguate same-descriptor iterations. |

Examples:
- `a2_b06_s01_before interviews.xml`: descriptor names the cut's state
- `a2_b06_s01_broll options.xml`: same scene, different working question
- `scene_sample.v1.xml`: the shipped example (see `scene_workspace/`)

The b/scene numbers correspond to the sidecar IDs (`b_06_s05` ↔ `b06_s05`; sidecar uses internal underscores, filenames don't).

`replace_scene_in_act.py` parses the act-level pattern (`<prefix>_<proxies|original>_<date>`) to build its default output filename when `--output` is omitted; honor the convention upstream and the auto-output stays predictable.

## Ingesting a fresh Mac Premiere export

A raw FCP7 export from the editor's machine is **not directly usable** on the workspace. It carries source pathurls from that machine (`/Volumes/<source-drive>/<project>/...`), source-resolution dimensions on every `<file>` def (often 4K/8K), and frequently has unused leftover material past the editorial cutoff. `_scripts/ingest_premiere_export.py` performs the one-pass conversion:

```powershell
# Long export with leftover material past the editorial cutoff:
py "editor\xml exports\_scripts\ingest_premiere_export.py" `
    --xml "editor\xml exports\project_act I_premiere export_<ts>.xml" `
    --truncate-at-frame <N>

# Already-ingested re-export (just fixes sequence format):
py "editor\xml exports\_scripts\ingest_premiere_export.py" `
    --xml "editor\xml exports\project_act II_premiere export_<ts>.xml"
```

What it does:

1. **Pathurl rewrite.** Every `<file>` pathurl matched to `editorial_catalog.sqlite` by tail-of-`<project root>/<...>`, then re-emitted as the the workspace proxy path. Picks the right proxy_kind per extension (`video_video_proxy` for .mp4/.mov/.r3d, `audio_audio_proxy` for .wav/.m4a/.mp3, `still_still_proxy` for .jpg/.heic/.png).
2. **Placeholder creation.** For pathurls not in the catalog (SFX library, occasional missing camera-card file) and for catalogued assets whose proxy of the expected kind is absent: writes a minimal-valid placeholder media file at the **mirrored** path under `derivative media/<project-root tail>`. Audio uses ffmpeg-generated 1-sec silence, video a 1-frame black .mp4, stills are a 1×1 JPG/PNG. The director can later use Premiere's *Link Media* against the RAID, where the matching folder structure makes relinking a one-click affair.
3. **Frame-size sync.** Sequence `<format>` and `MZ.Sequence.PreviewFrameSize*` set to 1280×720. Per-`<file>` video samplecharacteristics set by **ffprobing the actual proxy file**: landscape proxies become 1280×720, phone portrait proxies stay 720×1280, still proxies use their native (downscaled) dimensions. Synthetic clipitems (Graphic, Black Video; they have no pathurl) are intentionally left at the original sequence size.
4. **Truncate (optional).** With `--truncate-at-frame N`: drops clipitems with `start >= N` across every V/A track, drops transitionitems crossing N, sets sequence `<duration>` to N.
5. **Dangling link cleanup (always).** Strips every `<link>` whose `<linkclipref>` points at a clipitem id that doesn't exist in the sequence. Premiere refuses to import xmeml with any dangling linkclipref. Catches both (a) refs that became dangling because truncation dropped their target and (b) pre-existing source dangles from earlier Premiere-side deletions that didn't clean their links (one of our source exports already carried 30).

Output is a new timestamped xmeml alongside the source (never overwrites). A `_plans/<output>_report.json` lists every rewrite, every placeholder, the dim updates, and the truncation stats.

Pre-existing source XML quirks to expect (these survive ingest unchanged):

- `clipitem` with `<in>-29</in>` on `Nested Sequence` clips, a Premiere artifact for nested-sequence pre-roll. `validate_xml_structure.py` flags it because its rule is "only -1 is a legal sentinel," but the source export emits this for nested sequences. Safe.
- `Graphic`/`Black Video` clipitems still declared at source resolution: synthetic generators, no pathurl, will rescale at the 1280×720 sequence rate.

NTFS-illegal characters in mirrored placeholder paths: Mac/HFS allows `:` in directory names (e.g. an `Animations: Generative` folder), but NTFS does not. The ingest script sanitizes `:`, `*`, `?`, `"`, `<`, `>`, `|` to `_` in the placeholder path. The pathurl in the XML uses the sanitized name. Director relinking on the RAID should still work; Premiere's *Link Media* matches by filename, and the folder name is recognizable (e.g. `Animations_ Generative` on the workspace ↔ `Animations: Generative` on the RAID).

## XML invariants (must satisfy or Premiere misbehaves)

| Invariant | Symptom if violated | Where enforced |
|---|---|---|
| Every video clipitem needs a `<filter>` Basic Motion block | Clip flickers between displayed and black during scrub/playback | `insert_video_clips.py:_build_basic_motion_filter()` |
| Clipitem-level `<pixelaspectratio>` + `<anamorphic>` required | Same flicker, plus possible aspect issues | `_build_clipitem()` |
| `<file>` first occurrence carries full body; subsequent occurrences are stub `<file id="..."/>` | Duplicate file def errors, or orphan refs | `_build_clipitem()` reuses via `existing_files` dict |
| Pathurls use Windows-style `file://localhost/E%3a/...` (lowercase `%3a`, `%20` for space) | Clips show offline (red banner) | `_pproticks.py:windows_path_to_pathurl()` |
| Pathurls percent-encode **ONLY** spaces + the drive colon; every other char is literal (`,` `!` `#` `(` `)` `+` literal; `&` → `&amp;` via XML escaping). **Do not `urllib.parse.quote`.** | Over-encoding (`,`→`%2C`, `&`→`%26`, `!`→`%21`) → Premiere reports **MISSING MEDIA** (its pathurl decoder treats those percent-codes literally) | `_pproticks.py:windows_path_to_pathurl()` |
| Audio `<track>` needs `currentExplodedTrackIndex="0"` + `totalExplodedTrackCount="1"` attributes (alongside `premiereTrackType`) | Media imports into the project bin but clips are **NOT placed on the timeline**: track shows empty ("loaded but not in timeline"). A hard-won invariant: a selects reel built with only `premiereTrackType` dropped every audio clip on import; v4 (which carried all three attrs) was fine. | scene_workspace builders: set all three on every audio `<track>` |
| `pproTicksIn = in_frame × 10_594_584_000` at NTSC 23.976 (exact integer) | Sync drift, +2/-2 speed badges | `_pproticks.py:ticks_for_frame()` |
| Timeline-frames span MUST equal source-frames span (or you get a speed change) | Premiere shows +N or -N speed badge on the clip | `insert_video_clips.py` auto-snaps when mismatch ≤ 2 frames |
| `start = -1` or `end = -1` is a **transition boundary sentinel**, not a "through-edit pair" marker | Audio cut mid-sentence / extended past source / silence | `ripple_shift_after()` skips sentinels but shifts the non-sentinel edge |
| `<transitionitem>` elements must shift with surrounding clipitems on ripple | Crossfade stays put while clipitems shift → exact above symptom | `_all_transitionitems()` + ripple loop |
| Output sequence `<format>` should match the actual proxy resolution (1280×720), NOT the original 4K finishing format | 2/3 black bars around every clip | Plan `sequence_format` field |
| `<sequence><duration>` must extend by ripple delta | Last clips appear truncated | `ripple_shift_after()` updates duration |
| `<link><linkclipref>X</linkclipref></link>` must reference an existing clipitem id | Import fails outright. Pre-existing dangling refs from Premiere-side deletions also kill a fresh import. | `ingest_premiere_export.py::prune_dangling_links()` (unconditional safety pass) |
| XML declaration must use **double quotes** (`<?xml version="1.0" encoding="UTF-8"?>`) | Silent "File Import Failure" dialog with empty Error Message. lxml's `xml_declaration=True` emits single quotes and trips this. | `ingest_premiere_export.py` + `insert_video_clips.py` both prepend the literal double-quoted decl manually instead of trusting lxml. |
| Premiere xmeml uses **mixed** empty-tag conventions: REFERENCE STUBS (`<file id="X"/>`, `<sequence id="X"/>`) MUST be self-closing; EMPTY CONTENT ELEMENTS (`<description></description>`, `<scene>`, `<lut>`, `<lognote>`, etc.) MUST be open/close. | Same silent "File Import Failure" with empty Error Message. Both halves of the rule have to hold simultaneously; forcing everything to one form breaks the other. Cataloging self-closing tags across known-good Premiere exports turned up only two types: `<file id=...>` and `<sequence id=...>`. The common attribute is `id`. | Walk every element; if `.text is None and len(el) == 0 and "id" not in el.attrib`, set `.text = ""` (forces open/close). The `id`-attr exclusion preserves reference stubs as self-closing. Verified by byte-comparing no-op round-trips of known-good exports: diff is 1 byte (trailing newline). |

## Premiere's `-1` sentinel cheat sheet

A clipitem with `<start>-1</start>` AND `<end>N</end>` (or vice versa) is NOT "off-timeline." It means the clipitem's edge meets a `<transitionitem>` immediately before/after it on the same track. Premiere computes the implicit edge from the transition's actual position.

When you ripple-shift, you MUST shift the transitionitem too. Otherwise the implicit clipitem edge stays at the stale transition position while the explicit edge shifts, producing a clipitem with a mismatched declared duration and source duration.

We learned this when a 4-frame audio crossfade at A1 frames 8166–8170 stayed put after a +60 frame ripple, leaving clipitem-1358 with timeline span 8166→8329 (163 frames) but source span 103 frames. Premiere played the source for 103 frames then went silent for 60 frames, exactly matching the "cut off / extended" feedback.

### Multicam clipitems and the V-track export gap

Premiere's FCP7 XML export is lossy for **Multi-Camera Source Sequence** (MCSS) clips. The structure that survives:

- The A-track placements on the timeline → emitted as `<clipitem>` with a `<sequence id="...">` *inline definition* (the multicam source) and `<sourcetrack><mediatype>audio</mediatype></sourcetrack>`.
- The multicam source sequence's internal V tracks → present inside that inline `<sequence>` definition, containing each camera angle's `<file>` defs.

What gets dropped:

- The **V-track timeline placements** of the multicam clip. Even when the .prproj shows video for the multicam clip in Premiere's preview, the exported XML often contains **only A-track refs** with no V-track counterpart.

Symptoms downstream:

- "Missing video in such-and-such window": V tracks empty in the timeline range, but A1/A2 carry multicam audio refs (`name="C9854.MP4Multicam"` etc., no `<file>` child, a `<sequence>` child).
- Inspecting `<sourcetrack>` confirms only audio refs exist on the top sequence.

Detection: any top-level clipitem whose `<file>` child is absent but `<sequence>` child is present is a multicam ref. Cross-check `<sourcetrack><mediatype>`: if all such refs in a window are `audio`, you have the export gap.

Remediation (in Premiere, before re-exporting):
1. **Flatten** the multicam clip: right-click on the timeline clip → `Multi-Camera → Flatten`. The active angle becomes a regular V+A clip and exports normally.
2. Or replace the multicam with the chosen underlying camera-angle clip from the project bin.

This isn't an xmeml-edit issue we can fix in this codebase; the data Premiere didn't write is gone. Document the multicam refs you find so the editor knows which timeline ranges need flattening.

**Sidecar-side handling.** The data Premiere refuses to write to V-tracks is gone, but everything else in the multicam structure IS present in the XML; Premiere reads it because the nested `<sequence>` definition lives inline inside the clipitem on first occurrence. The sidecar pipeline now does the same. Two-pass design in `make_beat_sidecar.py::_extract_all_clipitems`:

| Pass | Builds | Notes |
|---|---|---|
| 1 | `file_meta: dict[file_id, {asset_id, pathurl, name}]` | Walks every `<file>` with `<pathurl>`. Same first-occurrence-vs-stub rule as before. |
| 1b | `sequence_defs: dict[seq_id, etree._Element]` | Walks every `<sequence>` with a body. Mirrors the file pattern: `<sequence id="sequence-28"/>` stub-refs (every multicam ref after the first) resolve to the canonical body via this map. **Without this pass, only the first reference per multicam source resolves; the others fall through to no-asset_id.** |
| 2 | `clipitems` (annotations) | For each clipitem on every track: if it has `<file>`, look up `file_meta`. If it has `<sequence>` instead, call `_resolve_multicam_file_meta` (audio asset_id) and `_resolve_multicam_v_angles` (linked V-track angles from the multicam source). |

Resolved fields per multicam audio annotation:

- `asset_id`: the underlying audio file's asset_id (typically the master ZOOM mix). Source is `<sequence>/media/audio/track[trackindex]/clipitem/file` with `trackindex` from the outer clipitem's `<sourcetrack>`.
- `is_multicam_ref: true`: flag for downstream consumers.
- `multicam_v_angles: [{asset_id, file_id, pathurl, name}, ...]`: every V-track angle inside the multicam source. Visible in Premiere when the editor double-clicks the audio clip; surfaced in HTML under the audio row as `linked V angles (N): C9854.MP4 · DJI_…` chips.

**Stereo-pair duplication.** Premiere splits stereo audio into one clipitem per channel (A1 = L, A2 = R), so every stereo multicam audio produces TWO annotations with identical content keys except for track. The sidecar preserves both (the XML truth); `render_sidecar_html.py::_dedupe_stereo_pairs` collapses adjacent odd/even A-track pairs (A1+A2, A3+A4, …) into single display rows at render time. The kept row shows a `_stereo_track_pair` marker so the HTML can label the badge `A1+2` and the dropped twin's content (filename, transcript) is identical anyway.

**Caveat on multicam timing.** The OUTER clipitem's `<in>`/`<out>` are source frames within the multicam SEQUENCE's timeline, not the underlying file's timeline. We pass them through unchanged. Works correctly when the multicam audio starts at multicam-time-0 == file-time-0 (typical master-audio multicam). If a camera has a sync offset, the transcript-window mapping (`segments_overlap`) will pick up the wrong segments. Fix is to map the outer source frames through the inner clipitem's `<start>`/`<in>` to get true file frames; deferred until we see an off-window transcript.

**Rule of thumb for new consumers:** if your code does anything with clipitems and only handles `<file>` refs, you have a multicam bug waiting. Whenever you walk `<clipitem>`s, branch on `ci.find("file") is not None vs ci.find("sequence") is not None`. For the sequence branch, resolve stubs via a `sequence_defs` map (build once, like `file_meta`). Validate against an export known to contain multicam clips.

**HTML rendering conventions for multicam V angles.** `render_sidecar_html.py::_render_track_strip` adds virtual `mc1`, `mc2`, …, `mcN` rows between V1 and the V-A divider: one per unique multicam V-angle asset across the beat. Each row's boxes show timeline ranges where that angle is *available via the multicam source* (dashed border, italic label, lighter color = "available, not placed"). Sorted by total coverage descending so the most-used angle sits closest to V1. The same data is exposed under each multicam audio row as `linked V angles (N): name1 · name2 · …` chips, hover-titled with the asset_id. If the editor wants to actually use one of those angles, the workflow is: drag from the bin onto V1 in Premiere, then re-export. Until then the visual signal is "V coverage IS available throughout, just not placed yet."

Flattening multicam in Premiere remains a valid alternative if you want simpler XMLs, but with this resolver, you no longer have to.

### Downstream consumers must resolve `-1` sentinels, not skip them

The sentinel rule above is about direct XML editing. Any **downstream tool** that derives state from xmeml has to translate the sentinel into a real frame number before using it; skipping these clipitems silently corrupts the derived state.

Example bug we hit: `_detect_audio_spines` in `story/_sidecar scripts/make_beat_sidecar.py` built its V1 coverage list by excluding V1 clipitems whose `timeline_end_frames` was `None` (which it is for clips with `<end>-1</end>`). Result: audio clips sitting under those V1-with-sentinel-end clips appeared V1-uncovered and got `audio_spine: true`. Act I had **145 falsely-spined audio annotations** out of 1,123; Act III had 44 out of 555. The sidecar HTML view then misrepresented the spine, which is the editorial source-of-truth for downstream LLM cuts.

When you write any consumer that filters clipitems by `timeline_end_frames is not None` (or similar nullability check), STOP: first resolve the `-1` sentinel by reading the adjacent `<transitionitem>` end, or by computing `timeline_start_frames + (source_out_frames - source_in_frames)`. We patched around it with a stopgap script that re-derived spine flags on the existing sidecar; the real fix belongs in `_detect_audio_spines`.

| Consumer | Status | Fix |
|---|---|---|
| `make_beat_sidecar.py::_detect_audio_spines` | Known buggy (skips -1-end V1 clips) | Resolve `-1` end via duration math or transitionitem lookup |
| Other future consumers | Audit when adding | If you filter by null end/start, check for `-1` first |

## Pre-flight checklist (run before any ripple)

Before writing an output xmeml, dump a structural diff of the source XML inside the affected window. The script does this; the checklist for a HUMAN review pass:

1. **Clipitems straddling the ripple point**: Anything with `start < ripple_frame < end`. Script warns; verify they should not move.
2. **Through-edit / transition sentinels**: Count clipitems with `start=-1` or `end=-1` AND any `<transitionitem>` whose start is `>= ripple_frame`. The script shifts these correctly now; verify the counts add up.
3. **Files added**: How many new `<file>` defs will be emitted, and how many slots already exist (stub reuse). New file defs need pathurls verified against `derivative media/_index/asset_map.json`.
4. **Sequence format mismatch**: If sequence is 4K and proxies are 720p, set `sequence_format: {width:1280, height:720}` in the plan.
5. **Sidecar drift**: the act sidecar's `xml_source` field should match the xml you're editing. If not, run `refresh_act_sidecar.py --xml <new>` first OR accept that sidecar annotations may not align with the cut.

The script's dry-run mode (`--dry-run`) emits everything except the write; use it before every real run.

## Safe-write pattern (FUSE-mounted workspaces)

Some agent-harness environments mount the workspace through a FUSE layer (e.g. bindfs) that can silently truncate larger writes. The script writes through `/tmp` first, then `dd conv=fsync`, then re-reads and SHA-256 verifies, with up to 3 retries. See `_scripts/insert_video_clips.py:_atomic_safe_write()`.

On a normal local checkout this is overkill, but it's harmless to keep.

## Other interchange targets (if you don't cut in Premiere)

xmeml v4 is *our* interchange because we cut in Premiere; nothing upstream of this folder cares. The catalog-to-cut boundary is the builder scripts in `_scripts/` and `scene_workspace/`; retargeting them to another format is a focused rewrite of the emitters, not a redesign:

| Target | When | What to know |
|---|---|---|
| **FCPXML** | DaVinci Resolve, Final Cut Pro X | The closest analogue to what we do here: same shape of work (clip refs + timeline placement + per-format quirks to learn by trial). Resolve also runs on Linux, which Premiere doesn't. |
| **AAF** | Avid Media Composer | Heavier spec; expect more trial-and-error on round-trip fidelity. |
| **EDL** | Almost anything | Lossy (single track, no effects) but near-universal; fine for selects reels and conform handoffs. |
| **OTIO** | NLE-agnostic pipelines, programmatic scratch timelines | We evaluated OpenTimelineIO as a full xmeml replacement and passed: Premiere's OTIO support is beta-only, and the FCP adapter dropped transitions, Basic Motion, and tick precision. As a *hub* format between other tools, or for building timelines in code, it's the right choice. |
| **ffmpeg direct render** | No NLE at all | The sidecar + catalog carry enough (asset paths, in/outs, track layout) to compile a cut straight to MP4; we used a content-addressed segment cache + concat pipeline for beat previews. For a team that doesn't want to buy or learn an NLE, this is a viable *primary* output, and the natural fit for a more hands-on agent. |

Whatever the target, the structure of the work is the same as this folder documents for xmeml: learn the format's invariants (our "XML invariants" section above is the xmeml instance of that), find where its parser is permissive vs. brittle, and encode that as validation the way `validate_xml_structure.py` does here.

## Lessons that aren't xmeml-specific

- **Multiple shoot days share camera-card filenames.** C0050.MP4 on shoot 2025-08-16 is the Ranger Station sign; on 2025-08-14 it's a runner; on 2025-08-17 it's a guy driving a truck. Always lookup by asset_id (sha256), never by filename, when picking clips.
- **Catalog `width`/`height` are SOURCE resolution, not proxy resolution.** For 4K cameras downscaled to 720p proxies, the catalog says 3840×2160 but the proxy file is 1280×720. New `<file>` defs in inserted clipitems must use proxy dimensions (script hardcodes 1280×720) or Premiere can't render.
- **`source_path` in the catalog points to original camera-card paths (`<RAID>\<project>\...`), not proxy paths.** Use `derivative media/_index/asset_map.json` `entries[asset_id].video_video_proxy.relative_path` for the proxy under `derivative media/`.

## Process: capture editorial learnings as you go

When the user gives feedback on a specific clip ("C0150 is shaky at the start"), don't just remember it for the conversation. Append it to that asset's `dataset/assets/editor_notes/{asset_id}_editor_notes.json` so:

1. Future query results for the asset surface the warning automatically (`editor/queries/visual.py` enriches results with `editor_notes`)
2. The next session (including a fresh LLM) sees the editorial signal alongside the SigLIP rank
3. The knowledge isn't trapped in a conversation that will be compacted away

Schema: `dataset/assets/editor_notes/_schema.md`.
