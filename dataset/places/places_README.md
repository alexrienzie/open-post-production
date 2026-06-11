# `places/`

`places.json` is the places registry: slug ids (`pl_*`), canonical names, aliases, a `type` taxonomy (country / state / town / protected_area / natural_feature / route / trailhead …), and an optional `parent_id` containment chain so queries can roll up geography without a geo lookup (e.g. `pl_garnet_canyon` → `pl_grand_teton_national_park` → `pl_wyoming` → `pl_united_states`).

This repo ships a **sample slice** (10 records with closed parent chains). The full registry in production held ~550 places, curated continuously as new transcripts surfaced new locations; see `dataset_SCHEMA.md` § Registry and timeline design patterns for the design rules (name-resolution, confidence tiers, dedup discipline).
