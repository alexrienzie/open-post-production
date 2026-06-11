# `documents/press/`

The press corpus: one JSON per article under `articles/` keyed by `article_id`
(sha256 of the canonical URL, first 32 hex). Each record carries fetch + metadata
blocks (title, byline, publish date, word count), an extracted `content` block,
our LLM `analysis` (one-line + paragraph summaries, topics, named entities, tone,
storyline tags), and entity ids (`people_ids` / `org_ids` / `place_ids`).
Production also carried per-comment and per-social-post records in sibling folders.

This repo ships a **sample slice**: one article record with `content.text` and
`analysis.pull_quotes` **redacted** - article text is the publisher's copyright;
fetch it from the record's `url`. Everything else (metadata facts + our derived
analysis) is the reusable structure.
