# `transcripts/`: transcript + speaker pipeline
*Runbook for the LLM transcript-analysis pass and the speaker-resolution workflow these scripts implement.*

Batch analysis uses **`_scripts/transcripts/run_transcript_analysis_via_gemini.py`**. Idempotency and “stale” detection match the runner's **`needs_processing()`** check: a record is up to date only when `analysis.analyzed_at` is set, `analysis.summary_one_line` is non-empty, and **`analysis.prompt_sha256` equals the canonical SHA** of `_prompts/transcript_analysis_prompt.md` (computed with **`canonical_prompt_sha()`** in `build_transcript_prompt_context.py`, which ignores volatile `_Generated …_` lines).

## Prompt-engineering patterns (distilled)

The batch prompts are **generated from the live registries** by `_scripts/transcripts/build_transcript_prompt_context.py`. Regenerate when registries change; never hand-edit. The patterns that made the analysis accurate, learned over several calibration rounds:

- **Two-pass split.** Pass 1 = corrections, speaker attribution, structural markup; pass 2 = editorial scoring (soundbite quality, salience, comedic flags). Each prompt stays small, and scoring can re-run without touching corrections.
- **Closed vocabularies everywhere.** Storylines, moods, audio-quality tiers, and moment IDs are closed lists injected from the registries. Free-form labels drift; closed lists keep output joinable.
- **Confidence-gated application.** Outputs carry a confidence; only high (≥0.85) auto-applies, the mid-band goes to a review journal, low is dropped.
- **Multi-signal ground truth for speakers.** A speaker attribution needs agreement across transcript context, face clusters, and audio role before it earns high confidence. ASR text alone never overrides a face-cluster identity.
- **Never auto-correct ambiguous proper nouns.** A name fix requires a registry match plus close edit distance; ambiguous first names are left alone absent face/audio ground truth.
- **Asymmetric error costs.** For subjective flags (e.g. comedic moments), use strict criteria and deliberately under-tag: false positives erode editor trust faster than false negatives.

## Step 0 (optional): Ground speakers against human-made transcripts

If you have human-made reference transcripts for some assets (we had a set from an early manual pass), use them as ground truth before the LLM pass: set **`segments[].speaker`** to canonical **`p_*`** ids via **`resolve_speakers_from_human_transcripts.py`**, then measure machine-vs-human agreement. Skip this section if your corpus is ASR-only.

1. **Structural report** (fast: coverage + audit presence + per-asset flags):
   ```powershell
   python _scripts/transcripts/report_human_machine_speaker_alignment.py --out-dir _runs/speaker_alignment_<yyyymmdd_hhmm>
   ```
2. **Gold accuracy** vs human timecoded utterances (slower: full manifest slice):
   ```powershell
   python _scripts/transcripts/review_speaker_accuracy.py
   ```
   Or combine: add **`--gold-eval`** to the alignment report (runs the reviewer as a subprocess and merges summary metrics).

Re-run the resolver when reference transcripts or machine segments change; then re-run the reports. The gold-eval habit is the transferable part: any time you have human ground truth for a slice, score the machine output against it before and after each pass.

## Before a corpus refresh

1. Update registries (people, orgs, places, and your story-spine vocabulary) as needed.
2. Regenerate the prompt so vocabulary matches:
   ```powershell
   python _scripts/transcripts/build_transcript_prompt_context.py
   ```
3. Measure backlog and export ID lists:
   ```powershell
   python _scripts/transcripts/report_transcript_analysis_freshness.py --out-dir _runs/transcript_analysis_freshness_<yyyymmdd_hhmm>
   ```
   Outputs:
   - `report.md`: summary
   - `summary.json`: machine-readable counts
   - `stale_all_ids.txt`: full re-run queue
   - `stale_priority_ids.txt`: high-ROI slice (assets with reference transcripts, if you ran Step 0)
   - `priority_all_ids.txt` / `priority_missing_transcript_ids.txt`: reference-linked id lists (empty without Step 0)

## Running Gemini analysis

- Prerequisites: `pip install google-generativeai`, `$env:GEMINI_API_KEY` set (PowerShell). API access needs an account with a payment method, and **new accounts start rate-limited**; see `DESIGN.md` § "Inference cost surface" before kicking off a large batch.
- Full pass (respects heuristic skip for trivial clips):
  ```powershell
  python _scripts/transcripts/run_transcript_analysis_via_gemini.py
  ```
- **High-ROI slice first** (reference-linked stale only; see Step 0):
  ```powershell
  python _scripts/transcripts/run_transcript_analysis_via_gemini.py --only-asset-ids-file _runs/transcript_analysis_freshness_<ts>/stale_priority_ids.txt
  ```
- **Borderline clips** (optional): add `--no-heuristic-skip` so short slates still call the model.

Logs and manifests live under `_runs/<run_id>/` (created at runtime). Progress snapshot: `python _scripts/transcripts/transcript_progress.py`.

## After a pass

1. Re-run the speaker resolver if analysis changed `people_ids` (or you use `--restrict-to-asset-people-ids`).
2. If you have ground truth (Step 0), run **`review_speaker_accuracy.py`** and keep outputs under `_runs/speaker_accuracy_*` for regression tracking.

## Related

- Cleanup pass (ASR text, separate workstream): `find_correction_candidates.py`, `run_transcript_cleanup_skeleton.py`.
