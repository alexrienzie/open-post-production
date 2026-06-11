# `sidecars/`: the cut, annotated (schema v2)

One scoped sidecar JSON + a resolver index bridge NLE xmeml exports to LLM-facing editorial metadata. The sidecar is the single self-contained file an agent reads to reason about a stretch of the cut: structure (`beats[]` → `scenes[]`), plus one annotation per clip carrying both the editorial intent and the denormalized evidence (asset metadata, diarized speakers, transcript text, timing).

**The scoping unit is yours to choose.** We scope sidecars to an *Act* (`--act-id` on the pipeline scripts) because a feature Act is the largest stretch one editorial session reasons about, but "act" is just a label. Point the same pipeline at a whole short film, a single scene, or a selects reel: one xmeml in, one sidecar out, beats partitioning whatever range the XML covers.

## Why this layer exists

xmeml is lossy in two ways that matter for LLM-assisted editing:

1. **NLE ids renumber on every export.** `clipitem-NNNN` / `file-NNN` are reassigned each `Export → Final Cut Pro XML`. Confirmed empirically: two consecutive exports of the same Act produced completely disjoint id ranges. So annotation identity is a **content key** (`asset_id + source_in + source_out + timeline_start + track`), and the resolver index (`_resolver/<unit>_clip_index.json`) maps content keys to the *current* export's clipitem ids.
2. **Editorial knowledge has no xmeml home.** Why a clip is in the cut, lower-third text, spine overrides: none of it round-trips. The sidecar carries it.

## The shape

```jsonc
{
  "act_id": "sample", "label": "Sample scene",
  "timeline_range_frames": [0, 372], "frame_rate": "24000/1001",
  "xml_source": "…/scene_sample.v1.xml", "xml_sha256": "80adab64…",   // provenance
  "beats": [
    { "id": "b_01", "label": "Sample beat", "timeline_range_frames": [0, 372],
      "scenes": [
        { "id": "b_01_s01_warmup_and_moose", "label": "Warm-up and moose",
          "timeline_range_frames": [0, 192], "purpose": "Open on the athlete in routine…" }
      ] }
  ],
  "annotations": [
    { "key": "<content key>", "clip_id": "c0000", "beat": "b_01", "scene": "b_01_s01_warmup_and_moose",
      // ── lean fields (authoritative; hand- or agent-edited; survive re-extraction) ──
      "rationale": null, "lower_third": null, "location_title": null, "date_tracker": null,
      "audio_spine": false, "_force_ride": false,
      // ── denormalized fields (derived; regenerated every refresh - never hand-edit) ──
      "asset": { "filename": "C8962.MP4", "...": "catalog metadata + classifications" },
      "speakers": [ { "p_id": "p_michelino_sunseri", "...": "diarized identity" } ],
      "transcript_text": "…", "chunk_subject": "…", "chunk_action": "…", "timing": { "...": "multiple time refs" } }
  ]
}
```

- **Lean fields are authoritative.** Carried across re-extractions by content-key inheritance: `rationale`, `lower_third`, `location_title`, `date_tracker`, the spine flags, and the stable `clip_id` (`c####` video / `a####` audio / `o####` overlays; ids are continuous across beats).
- **Denormalized fields are derived** from the catalog DB, dataset transcripts, and the sidecar's own `scenes[]`; `refresh_act_sidecar.py` regenerates them on every run.
- **Spine overrides:** `_force_ride: true` demotes a V1 clip from the narrative spine; `audio_spine: true` promotes an A-track clip into it. Both survive refreshes.
- **Beat/scene membership** is by `timeline_start_frames` falling inside the range, after `-1`-sentinel resolution (see `editor/xml exports/xml_README.md` for the sentinel rules; skipping sentinel clips silently corrupts spine derivation).

## Lifecycle

```text
xmeml export
  └─ refresh_act_sidecar.py --act-id <unit> --xml <path>     (orchestrator, ~seconds)
       1. build_resolver.py        XML → content_key → clipitem id
       2. make_act_sidecar.py      XML + <unit>_beats_manifest.json → sidecar (lean fields inherited)
       3. populate model fields    from catalog semantic summaries
       4. denormalize              catalog + transcripts + speakers + timing
       5. render HTML              one review page per unit
  └─ build_story_brief.py          sidecar → hierarchical Markdown brief (the LLM-review view)
  └─ qa_sidecar.py <unit>          per-scene speaker/transcript QA (run after every refresh)
```

Edit `beats[]` / `scenes[]` directly in the sidecar (or the beats manifest for beat seams), re-run the refresh, and annotations re-assign to the new ranges. Validate with `validate_sidecar.py`.

## The shipped example

Built entirely from the repo's sample assets; regenerate it yourself from scratch:

| Artifact | Path | Made by |
|---|---|---|
| Scene XML (3 clips, V1 + linked stereo A1) | `editor/xml exports/scene_workspace/scene_sample.v1.xml` | `_build_scene_sample.py` (same folder) |
| Beats manifest | `sample_beats_manifest.json` (this folder) | hand-authored |
| Sidecar | `sample.sidecar.json` (this folder) | `refresh_act_sidecar.py --act-id sample --xml "xml exports\scene_workspace\scene_sample.v1.xml" --no-archive --skip-visual-cut --skip-cut-eval --skip-render` |
| Resolver index | `_resolver/sample_clip_index.json` | (same run) |
| Two-column HTML review | `editor/story/html views/sample_2col.html` | `render_sidecar_html_2col.py "story\sidecars\sample.sidecar.json" --out "story\html views\sample_2col.html"` |
| Markdown story brief (for LLM review) | `editor/story/html views/sample_story_brief.md` | `build_story_brief.py "story\sidecars\sample.sidecar.json" --out "story\html views\sample_story_brief.md"` |

The example shows the full loop: the banter clip's annotation arrives with its transcript text and diarized `p_id` attached; the two `scenes[]` entries were added by hand to the sidecar and a second refresh re-assigned every annotation. The XML's `pathurl`s are absolute; re-run the builder to point them at your checkout before importing into an NLE.
