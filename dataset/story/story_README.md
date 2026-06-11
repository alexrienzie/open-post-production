# `story/`

`moments.json` is the story-spine registry: an outline of narrative **moments** (`mom_*`) with title, act, one-line summary, themes, characters, time period, and setting. `moment_ids[]` tags on transcripts and documents key into it, and the transcript-analysis prompt context injects its moments + themes as closed vocabulary.

Ships as a **sample slice** (2 of 24 production moments). Define your own outline as your story takes shape, then regenerate the prompt context (`_scripts/transcripts/build_transcript_prompt_context.py`).
