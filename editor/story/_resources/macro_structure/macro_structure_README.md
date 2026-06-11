# macro_structure /: narrative-structure frameworks

This is the **macro layer**: narrative-structure frameworks that answer the question *"where am I in the story?"* (Save the Cat beat 6, Vogler's first threshold, Kishōtenketsu's `shō`, the midpoint of Field's paradigm). Each file is a structured, citable reference for one framework: its stages, its lineage, what it's good for, how it maps onto a cut.

The companion **micro layer** (`editor/story/_resources/micro_structure/`) answers a different question: *"how is the scene HERE failing?"* (debate-bloat, missing visual anchor, lay-pipe over-explained). Macro tells you the slot; micro tells you the craft moves available in that slot. See `editor/story/_resources/micro_structure/micro_structure_README.md`.

**Why "filter":** the same beat (or scene) can be *read through* any framework. Vogler reads a beat as **Crossing the First Threshold**; Kishōtenketsu doesn't read it that way at all (its engine is *change*, not *conflict*, and the same stretch sits in its "shō / development" half). Switching the filter switches the read. The cut doesn't move; the lens does.

Filters live here:

```
editor/story/_resources/macro_structure/
├── README.md                          (this file)
├── snyder_save_the_cat.json           Save the Cat 15-beat ladder (one of the 40)
├── vogler_writers_journey.json        Vogler 12 stages (active overlay)
├── kishotenketsu_four_part.json       East Asian four-part (non-Western counter-lens)
├── … (one per framework - 31 total v1 filters + the legacy reference)
```

Filters are registered in `../manifest.json` under `macro_structure[]`.

## Conceptual hierarchy

Three roles a filter can play in the editor:

| Role | What it is | Where it shows up | Example |
|---|---|---|---|
| **Primary** | The beat structure you anchor the cut on: any framework here, or your own ladder | `project_beats.json` `beats[]` themselves | whichever framework you choose |
| **Overlay** *(Layer 1)* | Per-beat alternate description | `project_beats.json` `beats[i].overlays.{framework_id}` → renders as chip in beat header | Vogler: `vogler_writers_journey.json` |
| **Shape** *(Layer 2)* | Film-level metadata (emotional curve, applicability, etc.) | `project_beats.json` `frameworks.{framework_id}` → renders as SVG sparkline or summary block | Vonnegut shape |
| **Thread** *(Layer 3)* | Cross-cutting investigation/subplot referencing N scenes | `editor/story/threads/{thread_id}.json` → renders as chips on each scene | Scientific Method (an investigation arc) |

> `project_beats.json` (your project-level beat ladder, with per-beat overlays + film-level framework data) and `threads/*.json` are **authored by you, not shipped**; this README and `macro_structure_guide.md` document their shape. The HTML renderer skips them gracefully when absent.

A single framework filter file can describe a framework that's used in any of these roles. The filter itself is the *theory*; the role-specific data (per-beat mapping, per-scene assignment) lives in `project_beats.json` or in `threads/*.json`.

## JSON schema (v1)

Every filter in this directory should follow this shape. Fields are optional unless marked **required**.

```jsonc
{
  // ── Identity ────────────────────────────────────────────────────────────
  "framework_id":   "vogler",                            // required, lowercase_snake
  "title":          "The Writer's Journey",              // required
  "author":         "Christopher Vogler",
  "year":           1992,
  "source":         "Originally a 7-page Disney memo (c. 1985); expanded to The Writer's Journey: Mythic Structure for Writers (1992).",

  // ── Taxonomic placement ────────────────────────────────────────────────
  "tradition":      "mythic",                            // dramatic | mythic | non_western | borrowed | heuristic | hybrid
  "engine":         "external_conflict",                 // external_conflict | change | inquiry | grief | salvation | rhythm
  "act_envelope":   "three_movement",                    // three_act | four_part | five_act | three_movement | seven_part | free
  "resolution":     12,                                  // number of stages this framework slices the spine into

  // ── Lineage (cross-links to other filter ids in this directory) ────────
  "lineage": {
    "predecessors": ["campbell_monomyth", "van_gennep_rites"],
    "descendants":  ["harmon_story_circle"],
    "siblings":     ["propp_morphology"],
    "note":         "Practical screenwriting adaptation of Campbell. Where Campbell is descriptive and anthropological, Vogler is prescriptive and production-ready."
  },

  // ── Stages (or beats, or parts - the framework's primitive unit) ──────
  "stages": [
    {
      "stage_id":   "v01_ordinary_world",   // unique within this file, prefixed with a 2-char framework tag
      "position":   1,                       // 1-indexed
      "movement":   "Departure",             // optional grouping (Departure/Initiation/Return for Vogler)
      "name":       "Ordinary World",
      "function":   "Establish the hero's normal state…",
      "duration_hint": "10%"                 // optional - % of runtime where this typically sits
    },
    /* … 11 more stages … */
  ],

  // ── OPTIONAL: mapping onto your beat ladder ──────────────────────────
  // Shipped filters don't carry this block. If your cut anchors on a beat
  // ladder, add one keyed by your own beat ids: each beat id maps to a
  // stage_id in this file (coarser framework → beats share a stage; finer →
  // some stages get no beat).
  "canonical_mapping_to_beats": {
    "_note":  "Project-specific deviations belong in project_beats.json overlays.{framework_id}.",
    "<your_beat_id>": "v01_ordinary_world",
    /* … one entry per beat in your ladder … */
  },

  // ── Cross-framework equivalence (which stages in OTHER frameworks ─────
  // ── this stage approximates). Keyed by other framework_id. Useful when ─
  // ── rendering side-by-side reads of the same beat. ────────────────────
  "cross_framework_equivalence": {
    "snyder_save_the_cat": {
      "v05_crossing_the_first_threshold": "sn06_break_into_two",
      "v08_the_ordeal":                   "sn10_bad_guys_close_in"
    },
    "campbell_monomyth": {
      "v01_ordinary_world":               "c01_call_to_adventure_world",
      "v05_crossing_the_first_threshold": "c05_crossing_the_first_threshold"
    },
    "kishotenketsu_four_part": {
      "v05_crossing_the_first_threshold": "k02_sho_development"
    }
  },

  // ── Applicability and limits ──────────────────────────────────────────
  "applicability": {
    "best_for":    ["epic", "thriller", "fantasy", "transformation_arcs"],
    "weak_for":    ["ensemble", "documentary_observational", "no_conflict_stories"],
    "limitations": "Imposes a hero-shaped journey on material that may not have one. Documentary cuts that try to force this shape on collective/process subjects often feel artificial."
  },

  // ── project-specific notes (why this matters for YOUR film) ──────────────────
  "project_relevance": null  // ships empty - fill with how this lens maps to YOUR film (strong matches, weak stretches, which scenes it reads truest on)
}
```

## Adding a new filter

1. Pick a `framework_id` (lowercase_snake, e.g. `mosaic_portrait`).
2. Copy an existing filter as a template; `vogler_writers_journey.json` is the canonical reference.
3. Fill in the eight schema blocks above. Stages can range from 3 (Aristotle) to 22 (Truby); make the `stage_id` prefix unique to the framework so cross-references don't collide.
4. Add an entry to `../manifest.json` under `macro_structure[]`. Set `status: "pending"` until the canonical_mapping_to_beats is filled and validated against `project_beats.json`.
5. If the framework is going to actively render in the HTML, decide its layer:
   - **Layer 1 (per-beat overlay):** add `overlays.{framework_id}` values per beat in `project_beats.json` (use the `_apply_{framework}_overlay.py` script pattern). Renders as a chip.
   - **Layer 2 (film-level shape):** add `frameworks.{framework_id}` block at the top of `project_beats.json`. Renders as sparkline/summary.
   - **Layer 3 (cross-cutting thread):** create a new file in `editor/story/threads/{thread_id}.json` that references this framework. Renders as scene chips.
6. Re-render the Act HTMLs to validate.

## Equivalence rules

Cross-framework equivalence is **approximate**, never exact. The poster the user added (`editor/story/_resources/macro_structure/macro_structure_guide.md`) makes this explicit: every framework is the same spine sliced at different resolutions, with different engines. So:

- `cross_framework_equivalence` claims should be **defensible**, not strict identity.
- When in doubt, omit. A missing equivalence is honest; a forced one is misleading.
- The `_note` field in each mapping is where to write the editorial nuance.

Direct equivalences:
- Field's "Plot Point 1" ≈ Snyder's "Break Into Two" ≈ Vogler's "Crossing the First Threshold" ≈ Hauge's "Change of Plans" ≈ ~25% of runtime.
- Snyder's "All Is Lost" ≈ Field's "Plot Point 2" ≈ Vogler's "Ordeal" (or "Resurrection setup") ≈ ~75% of runtime.

Indirect/contested equivalences:
- Freytag's "Climax" (apex of the pyramid, at the structural center) is NOT the same as modern "Climax" (near the end). This is the most common confusion and should be called out in any filter that uses Freytag.
- Kishōtenketsu's "ten" (twist) doesn't have a clean Western analog. Don't force one.

## See also

- `editor/story/_resources/macro_structure/macro_structure_guide.md`: the poster decoder. Read first.
- `editor/story/_resources/manifest.json`: the registry of all active filters (look under `macro_structure[]`).
- `editor/story/_resources/micro_structure/micro_structure_README.md`: the micro layer (editorial principles).
- `editor/story/project_beats.json`: where overlays + frameworks data lives.
- `editor/story/threads/`: Layer 3 thread files (and `threads/threads_README.md` for that schema).
- `editor/story/sidecars/sidecars_README.md`: the Act-scoped sidecar schema.
