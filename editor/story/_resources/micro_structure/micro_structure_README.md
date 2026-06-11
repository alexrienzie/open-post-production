# micro_structure /: editorial micro-principles

This is the **micro layer**: tactical principles for fixing what's actually in a scene once you know which beat it occupies. Examples: *Pope-in-the-Pool* (exposition rides on motion), *debate is the shortest beat*, *don't ask the question the answer needs*, *don't lie with the cut*.

The companion **macro layer** (`editor/story/_resources/macro_structure/`) answers a different question: *"where am I in the story?"* (Save the Cat beat 6, Vogler's first threshold, the Midpoint, Kishōtenketsu's `shō`). Macro tells you the slot; micro tells you the craft moves available in that slot. See `editor/story/_resources/macro_structure/macro_structure_README.md`.

A scene can be correctly placed (the right beat at the right position) and still fail. Micro-principles name the common failure modes within any slot.

Files live here:

```
editor/story/_resources/micro_structure/
├── README.md                       (this file)
├── snyder_principles.json          Save-the-Cat / Snyder tradition (story-shape failure modes)
├── … (one per family - see "Planned families" below)
```

Family files are registered in `editor/story/_resources/manifest.json` under `micro_structure[]`.

## What goes in here vs. macro_structure/

| Macro (over there) | Micro (in here) |
|---|---|
| Stages, beats, acts | Individual craft moves |
| "Where am I?" | "What can I do here?" |
| Filter applies to the whole film | Principle applies to a scene or a single cut |
| Cross-references via canonical mapping to the project beat ladder | Cross-references via `see_also[]` (to other principles or to macro filters) |
| File = one framework | File = one family of principles |

## JSON schema

```jsonc
{
  // ── Identity ────────────────────────────────────────────────────────────
  "id":                  "snyder_principles",                  // required, matches manifest.micro_structure[].id
  "display_name":        "Snyder - editorial micro-principles",
  "family":              "snyder",                              // short family id, matches manifest entry
  "tradition_id":        "snyder",                              // who teaches this; e.g. snyder, murch, morris, maysles

  // ── Attribution + framing ──────────────────────────────────────────────
  "attribution": {
    "work":              "Save the Cat! et al.",
    "author":            "Blake Snyder + documentary adaptation",
    "usage_note":        "Companion to the macro layer. Macro says where you are; these principles say how to fix what's there."
  },
  "application_guidance": "Use this as a checklist during a scene review pass…",

  // ── Principles ────────────────────────────────────────────────────────
  "principles": [
    {
      "id":              "pope_in_the_pool",                  // unique within this file
      "label":           "Exposition rides on motion",
      "description":     "Information should be delivered while something visually or emotionally engaging is happening…",
      "documentary_application": "If an interviewee explains a place, show the place under their voice…",
      "review_questions": [
        "If I muted the audio, would I still know what this scene is about visually?",
        "Could any of these talking-head clips become voiceover under b-roll without losing meaning?"
      ],
      "tags":            ["exposition", "interview", "any_beat"],          // optional; for filtering
      "see_also":        ["editing_room_tactical/audio_leads_picture"]     // optional; cross-refs to other families or macro filters
    }
    /* … more principles … */
  ]
}
```

### Required fields

- `id`, `display_name`, `family`, `attribution`, `application_guidance`, `principles[]`
- Each principle: `id`, `label`, `description`, `documentary_application`, `review_questions[]`

### Optional fields

- `tradition_id`: short lineage tag (`snyder`, `murch`, `morris`, `maysles`, etc.); useful for cross-family attribution
- Per principle: `tags[]` for filtering; `see_also[]` for cross-references to other principles or to macro filters (form: `"family_id/principle_id"` or `"macro_structure/framework_id"`)

## Adding a new family

1. Pick a `family` id (lowercase_snake, e.g. `interview_sync`).
2. Copy `snyder_principles.json` as a template.
3. Fill in identity, attribution, and 4–8 principles.
4. Register in `editor/story/_resources/manifest.json` under `micro_structure[]`:
   ```json
   {
     "id":            "interview_sync",
     "title":         "Interview & sync craft",
     "family":        "interview_sync",
     "path":          "micro_structure/interview_sync.json",
     "status":        "active",
     "tradition_id":  "morris"
   }
   ```
5. (Optional) Re-render Act HTMLs to surface the principles. (Render integration is not wired yet; principle data lives in JSON only at this stage.)

## Registered + planned families

| Family | Tradition | Status | Examples |
|---|---|---|---|
| **snyder_principles** | Save the Cat / commercial screenwriting | ✓ active | Pope-in-the-Pool, debate-is-short, lay-pipe, whiff-of-death, final-image-mirrors-opening |
| **documentary_craft** | Maysles / Wiseman / Morris / Oppenheimer | ✓ active | The reveal can never live in the interview; B-roll is the argument; show-don't-tell by behavior; the cut is the argument |
| **editing_room_tactical** | Walter Murch / *In the Blink of an Eye* | ✓ active | Cut on emotion not dialogue; L-cut / J-cut; trim entrance keep exit; audio leads picture; pace by feeling not clock |
| **interview_sync** | Errol Morris / journalism | ✓ active | Don't ask the question the answer needs; wait one beat after the natural ending; one-head fatigue |
| **rhythm_pacing** | Ma / Tarkovsky / Ozu (the micro version of `ma_negative_space`) | ✓ active | Two-beat rule after heavy emotion; inhale-exhale; silence is content; the bridge shot |
| **documentary_honesty** | Reflexive / ethical tradition | ✓ active | Don't lie with the cut; lower-thirds are evidence; translation is a choice; time-of-day must agree; Ken Burns isn't free |
| **unscripted_construction** | Bunim-Murray / Mark Burnett / docuseries | ✓ active | The interview button; characters constructed in the cut; reveal escalator; music as emotional script; the frankenbite line; reaction carries the meaning |
| **episodic_pacing** | Vince Gilligan / David Chase / serial TV | ✓ active | Cold open hook; act-break tease; previously-on as structure; season-arc planting; tag scene; midpoint recommitment |
| **voice_driven_nonfiction** | Ira Glass / *This American Life* / Radiolab / Serial | ✓ active | Action + anecdote alternation; the host turn; music as tense underscore; voice-led vs. tape-led; mid-scene pause; setup-question-reveal; the kicker |
| **short_form_attention** | YouTube / TikTok / MrBeast / Casey Neistat | ✓ active | Hook in first 5 seconds; pattern interrupts every 8–12s; retention-curve audit; title-card placement; visible payoff; the "but wait" signal |
| **comic_craft** | Guest / Morris / McKay + comedy-writing tradition | ✓ active | Rule of three; comic timing; the comic button; tonal whiplash; earnest absurdity (deadpan); the callback; misdirection/rug pull; specific absurd detail; comic relief placement |

## When to use a micro-principle vs. a macro filter

- **Macro filter** (over in `editor/story/_resources/macro_structure/`): asks "is the scene in the right beat?" / "does the film overall trace the right shape?"
- **Micro-principle** (here): asks "given the scene IS in the right beat, is it failing internally?"

Rough heuristic: if you'd answer the question by moving the scene to a different beat, that's a macro question. If you'd answer it by re-cutting the scene where it is, that's a micro question.

## See also

- `editor/story/_resources/macro_structure/macro_structure_README.md`: the macro layer (story-shape frameworks).
- `editor/story/_resources/macro_structure/macro_structure_guide.md`: the poster decoder.
- `editor/story/_resources/manifest.json`: the registry (look under `micro_structure[]`).
- `editor/story/html views/html_views_README.md`: render pipeline (principles render integration is a future hook).
