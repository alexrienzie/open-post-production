#!/usr/bin/env python3
"""
Transcript analysis quality / consistency — read-only corpus report.

For every machine transcript:
1. **Prompt freshness** — same buckets as `report_transcript_analysis_freshness.py`
   (`needs_processing` / canonical `prompt_sha256`).
2. **Schema consistency** — run `Validator.validate()` on the merged on-disk analysis
   shape (IDs, enums, key_quotes, deprecated keys, etc.).

Writes:
  report.md, summary.json, validator_failures.jsonl (and optional validator_failures_ids.txt)

Usage:
  python _scripts/transcripts/report_transcript_analysis_consistency.py \\
    --out-dir _runs/transcript_analysis_consistency_20260509_1900
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
TRANSCRIPTS_DIR = ROOT / "assets" / "catalog" / "transcripts"
PROMPT_PATH = ROOT / "_prompts" / "transcript_analysis_prompt.md"

sys.path.insert(0, str(ROOT / "_scripts"))
from build_transcript_prompt_context import canonical_prompt_sha  # noqa: E402
from run_transcript_analysis_skeleton import needs_processing  # noqa: E402
from validate_transcript_analysis import Validator  # noqa: E402


def stale_reason(record: dict, canonical_sha: str) -> str | None:
    if not needs_processing(record, canonical_sha):
        return None
    a = record.get("analysis") or {}
    if a.get("analyzed_at") is None:
        return "stale_missing_analyzed_at"
    if not a.get("summary_one_line"):
        return "stale_empty_summary"
    if a.get("prompt_sha256") != canonical_sha:
        return "stale_prompt_sha"
    return "stale_other"


def analysis_payload_from_record(rec: dict) -> dict:
    """Rebuild the shape the validator expects from a merged transcript."""
    out: dict[str, Any] = {}
    for k in ("people_ids", "org_ids", "place_ids", "beat_ids"):
        if rec.get(k):
            out[k] = rec[k]
    if rec.get("analysis"):
        out["analysis"] = rec["analysis"]
    if rec.get("craft"):
        out["craft"] = rec["craft"]
    for k in (
        "_unmatched_people",
        "_unmatched_orgs",
        "_unmatched_places",
        "_unmatched_locations",
        "_proposed_places",
    ):
        if rec.get(k):
            out[k] = rec[k]
    return out


def has_substantive_analysis(rec: dict) -> bool:
    a = rec.get("analysis") or {}
    return bool(a.get("analyzed_at") or a.get("summary_one_line"))


def main() -> int:
    ap = argparse.ArgumentParser(description="Transcript analysis consistency + freshness report.")
    ap.add_argument(
        "--out-dir",
        type=Path,
        required=True,
        help="Output directory for report.md, summary.json, validator_failures.jsonl",
    )
    args = ap.parse_args()

    if not PROMPT_PATH.exists():
        print(f"Missing prompt: {PROMPT_PATH}", file=sys.stderr)
        return 1

    prompt_text = PROMPT_PATH.read_text(encoding="utf-8")
    canonical_sha = canonical_prompt_sha(prompt_text)
    v = Validator.from_workspace(ROOT)

    freshness_counts: Counter[str] = Counter()
    validated_ok = 0
    validated_fail = 0
    skipped_no_analysis = 0
    unreadable = 0
    validator_fail_rows: list[dict[str, Any]] = []
    fresh_but_invalid = 0

    for p in sorted(TRANSCRIPTS_DIR.glob("*.transcript.json")):
        try:
            rec = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            unreadable += 1
            continue
        aid = rec.get("asset_id") or p.stem.replace(".transcript", "")
        reason = stale_reason(rec, canonical_sha)
        bucket = "fresh" if reason is None else reason
        freshness_counts[bucket] += 1

        if not has_substantive_analysis(rec):
            skipped_no_analysis += 1
            continue

        payload = analysis_payload_from_record(rec)
        r = v.validate(payload, rec)
        if r.ok:
            validated_ok += 1
            if bucket == "fresh" and r.warnings:
                pass  # counted in summary via warning histogram if needed
        else:
            validated_fail += 1
            if bucket == "fresh":
                fresh_but_invalid += 1
            validator_fail_rows.append({
                "asset_id": aid,
                "freshness_bucket": bucket,
                "errors": r.errors,
                "warnings": r.warnings,
            })

    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    ts = dt.datetime.now(dt.timezone.utc).isoformat()

    summary = {
        "generated_at_utc": ts,
        "canonical_prompt_sha256": canonical_sha,
        "prompt_path": str(PROMPT_PATH.relative_to(ROOT)),
        "transcript_files_glob": "*.transcript.json",
        "unreadable_transcript_files": unreadable,
        "freshness_buckets": dict(freshness_counts),
        "with_substantive_analysis_validated": validated_ok + validated_fail,
        "validator_ok": validated_ok,
        "validator_failed": validated_fail,
        "fresh_bucket_but_validator_failed": fresh_but_invalid,
        "skipped_no_analysis_block": skipped_no_analysis,
    }
    (out / "summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    fail_path = out / "validator_failures.jsonl"
    with fail_path.open("w", encoding="utf-8") as f:
        for row in validator_fail_rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    if validator_fail_rows:
        (out / "validator_failures_ids.txt").write_text(
            "\n".join(r["asset_id"] for r in validator_fail_rows) + "\n",
            encoding="utf-8",
        )

    report_lines = [
        "# Transcript analysis — quality / consistency",
        "",
        f"- Generated at (UTC): `{ts}`",
        f"- Canonical `prompt_sha256`: `{canonical_sha}`",
        "",
        "## Prompt freshness (all machine transcripts)",
        "",
        "| bucket | count |",
        "|--------|-------|",
    ]
    for k in sorted(freshness_counts.keys()):
        report_lines.append(f"| {k} | {freshness_counts[k]} |")
    report_lines.extend([
        "",
        "## Validator (merged on-disk analysis)",
        "",
        f"- Records with substantive `analysis` validated: **{validated_ok + validated_fail}**",
        f"- **Validator OK:** {validated_ok}",
        f"- **Validator failed:** {validated_fail}",
        f"- **Fresh (prompt) but validator failed:** {fresh_but_invalid} (should be near zero)",
        f"- Unreadable transcript JSON files: {unreadable}",
        f"- Files without substantive analysis (skipped validation): {skipped_no_analysis}",
        "",
    ])
    if validator_fail_rows:
        report_lines.extend([
            "## Validator failures",
            "",
            f"See `validator_failures.jsonl` ({len(validator_fail_rows)} rows) and `validator_failures_ids.txt`.",
            "",
        ])
    else:
        report_lines.extend([
            "## Validator failures",
            "",
            "None.",
            "",
        ])

    (out / "report.md").write_text("\n".join(report_lines) + "\n", encoding="utf-8")

    print(json.dumps(summary, indent=2))
    print(f"\nWrote: {out / 'report.md'}")
    print(f"Wrote: {out / 'summary.json'}")
    print(f"Wrote: {fail_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
