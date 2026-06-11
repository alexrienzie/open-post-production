# `documents/case/`

The legal-side document corpus: per-record JSONL index (`records/ecf_master_index.jsonl`)
over extracted filing text (`pdfs_text/ecf_text/{record_id}.txt`). Records carry
`ecf_no`, `filed_date`, `filing_party`, `doc_type`, and the docket title; entity ids
(`people_ids` / `org_ids` / `place_ids`) attach at the document level, and an LLM
`analysis` block can be added per record (see `dataset_SCHEMA.md`).

This repo ships a **sample slice**: ECF 33 (the defense motion to dismiss, filed
2025-04-08, before the pre-trial motion hearing) plus its supporting brief (33-1),
both public court record. The production corpus held ~98 public court filings; anything non-public is
**withheld by default** (see `dataset_README.md` § Third-party handoff).
