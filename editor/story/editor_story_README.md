# `editor/story`: sidecars + the EP resources

Two layers live here:

1. **The sidecar pipeline** (`_sidecar scripts/`): turns the canonical XML cut into one Act-scoped sidecar JSON for agent-led scene work, plus the QA and validation passes that read it. Schema: [`sidecars/sidecars_README.md`](sidecars/sidecars_README.md). Workflow + script table: [`../editor_README.md`](../editor_README.md). Your generated sidecars, beats manifests, resolver indexes, and HTML review pages land under `sidecars/` as you cut. A complete sample (XML → sidecar → 2-col HTML, built from the repo's sample assets) ships; see [`sidecars/sidecars_README.md`](sidecars/sidecars_README.md) § "The shipped example".

2. **The EP resources** (`_resources/`): the story-structure reference library the agent uses to critique the cut:
   - `macro_structure/`: 33 story frameworks (Story Circle, McKee, Field, Aristotle, Save the Cat, documentary-specific structures, …), each as structured JSON with stages, diagnostic questions, and an empty `project_relevance` slot; fill it with how each lens maps to *your* film as your story takes shape.
   - `micro_structure/`: 11 craft-principle families (comic craft, documentary honesty, episodic pacing, …) at the scene/moment level.
   - `manifest.json`: the registry of all entries with status + notes; load programmatically via [`resources.py`](resources.py) (Pydantic models).
   - Schemas + genealogy: `_resources/macro_structure/macro_structure_README.md` and `micro_structure/micro_structure_README.md`.

   The intended use is diagnostic, not generative: pick 3–5 frameworks whose lens fits the question ("does Act II escalate?", "where does the inner journey decouple from the outer?"), have the agent score the current cut against their stages using the sidecar + transcripts as evidence, and treat disagreements between frameworks as the interesting finding. Regenerate the library with `_sidecar scripts/_build_story_filters.py` after editing.

**Beats vs. moments.** Editor cut beats (`b_##` in the sidecar) are *timeline ranges* in the current cut. Corpus narrative tags (`moment_ids[]`, `mom_*`) live dataset-side in your story-spine registry and describe *events in the story*, independent of any cut. Keep them distinct; bridge them only when reporting coverage ("which moments have no representation in the cut?").
