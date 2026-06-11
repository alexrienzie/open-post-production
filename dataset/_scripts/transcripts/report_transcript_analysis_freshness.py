#!/usr/bin/env python3
"""
Transcript analysis freshness — read-only report.

Compares each machine transcript's analysis.prompt_sha256 to the *canonical*
SHA of `_prompts/transcript_analysis_prompt.md` (same rule as
`build_transcript_prompt_context.canonical_prompt_sha`: volatile
"_Generated ..._" lines excluded).

Buckets match `run_transcript_analysis_skeleton.needs_processing`:
  - fresh: analyzed_at set, summary_one_line non-empty, prompt_sha256 == canonical
  - stale_missing_analyzed_at
  - stale_empty_summary
  - stale_prompt_sha (summary present but SHA mismatch)

Optional **human-linked** slice: asset_ids that appear in
`human_transcripts/clip_segments_manifest.jsonl` (human clip coverage).

Writes optional run directory (like `audit_catalog_freshness`):
  report.md, summary.json, stale_all_ids.txt, stale_human_linked_ids.txt, human_linked_all_ids.txt

Usage:
  python _scripts/transcripts/report_transcript_analysis_freshness.py
  python _scripts/transcripts/report_transcript_analysis_freshness.py --out-dir _runs/transcript_analysis_freshness_20260508_1200
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
CLIP_MANIFEST = ROOT / "assets" / "catalog" / "human_transcripts" / "clip_segments_manifest.jsonl"

sys.path.insert(0, str(ROOT / "_scripts"))
from build_transcript_prompt_context import canonical_prompt_sha  # noqa: E402
from run_transcript_analysis_skeleton import needs_processing  # noqa: E402


def load_human_linked_asset_ids() -> set[str]:
    if not CLIP_MANIFEST.exists():
        return set()
    out: set[str] = set()
    with CLIP_MANIFEST.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                o = json.loads(line)
            except json.JSONDecodeError:
                continue
            aid = o.get("asset_id")
            if isinstance(aid, str) and aid:
                out.add(aid)
    return out


def stale_reason(record: dict, canonical_sha: str) -> str | None:
    """Return None if fresh; otherwise a short reason key."""
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


def scan_transcripts(
    canonical_sha: str,
) -> tuple[list[dict[str, Any]], Counter[str]]:
    """Returns (per-file rows, reason_counts including 'fresh')."""
    rows: list[dict[str, Any]] = []
    counts: Counter[str] = Counter()
    if not TRANSCRIPTS_DIR.exists():
        return rows, counts
    for p in sorted(TRANSCRIPTS_DIR.glob("*.transcript.json")):
        try:
            rec = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            counts["unreadable"] += 1
            continue
        aid = rec.get("asset_id") or p.stem.replace(".transcript", "")
        reason = stale_reason(rec, canonical_sha)
        if reason is None:
            counts["fresh"] += 1
            bucket = "fresh"
        else:
            counts[reason] += 1
            bucket = reason
        a = rec.get("analysis") or {}
        rows.append({
            "asset_id": aid,
            "path": str(p.relative_to(ROOT)),
            "bucket": bucket,
            "prompt_sha256": a.get("prompt_sha256"),
            "analyzer": a.get("analyzer"),
        })
    return rows, counts


def main() -> int:
    ap = argparse.ArgumentParser(description="Report transcript analysis prompt freshness.")
    ap.add_argument(
        "--out-dir",
        type=Path,
        default=None,
        help="Write report.md, summary.json, and id lists here.",
    )
    ap.add_argument(
        "--human-linked-only",
        action="store_true",
        help="Only print / write stats for assets in clip_segments_manifest.jsonl.",
    )
    args = ap.parse_args()

    if not PROMPT_PATH.exists():
        print(f"Missing prompt: {PROMPT_PATH}", file=sys.stderr)
        return 1
    prompt_text = PROMPT_PATH.read_text(encoding="utf-8")
    canonical_sha = canonical_prompt_sha(prompt_text)

    human_linked = load_human_linked_asset_ids()
    rows, counts_all = scan_transcripts(canonical_sha)
    total = len(rows)
    ids_on_disk = {r["asset_id"] for r in rows}

    stale_ids = [r["asset_id"] for r in rows if r["bucket"] != "fresh"]
    human_linked_stale = sorted(set(stale_ids) & human_linked)
    human_linked_fresh = sorted({r["asset_id"] for r in rows if r["bucket"] == "fresh"} & human_linked)
    human_linked_missing_file = sorted(human_linked - ids_on_disk)

    sha_hist: Counter[str | None] = Counter()
    for r in rows:
        sha_hist[r["prompt_sha256"]] += 1

    counts_print = counts_all
    if args.human_linked_only:
        counts_print = Counter(r["bucket"] for r in rows if r["asset_id"] in human_linked)

    # Console summary
    print(f"canonical_prompt_sha256: {canonical_sha}")
    print(f"prompt_path: {PROMPT_PATH.relative_to(ROOT)}")
    print(f"machine_transcript_files: {total}")
    print(f"human_linked_asset_ids (clip manifest): {len(human_linked)}")
    print()
    print("buckets (all transcripts):" if not args.human_linked_only else "buckets (human-linked only):")
    for k in sorted(counts_print.keys()):
        print(f"  {k}: {counts_print[k]}")
    print()
    print(f"stale_total: {len(stale_ids)}")
    print(f"stale_in_human_linked (human-linked): {len(human_linked_stale)}")
    print(f"fresh_in_human_linked: {len(human_linked_fresh)}")
    print(f"human_linked_without_transcript_file: {len(human_linked_missing_file)}")
    print()
    print("top stored prompt_sha256 (all files):")
    for sha, n in sha_hist.most_common(6):
        label = sha or "(null)"
        mark = "  <-- canonical" if sha == canonical_sha else ""
        print(f"  {n:5d}  {label}{mark}")

    if args.out_dir:
        out = Path(args.out_dir)
        out.mkdir(parents=True, exist_ok=True)
        summary = {
            "generated_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
            "canonical_prompt_sha256": canonical_sha,
            "prompt_path": str(PROMPT_PATH.relative_to(ROOT)),
            "totals": {
                "machine_transcript_files": total,
                "human_linked_asset_ids": len(human_linked),
                "stale_all": len(stale_ids),
                "stale_human_linked": len(human_linked_stale),
                "fresh_human_linked": len(human_linked_fresh),
                "human_linked_missing_transcript_file": len(human_linked_missing_file),
            },
            "buckets_all": dict(counts_all),
            "buckets_human_linked_only": dict(Counter(r["bucket"] for r in rows if r["asset_id"] in human_linked)),
            "top_prompt_shas": [{"sha": s, "count": n} for s, n in sha_hist.most_common(20)],
        }
        (out / "summary.json").write_text(
            json.dumps(summary, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        (out / "stale_all_ids.txt").write_text("\n".join(sorted(stale_ids)) + ("\n" if stale_ids else ""), encoding="utf-8")
        (out / "stale_human_linked_ids.txt").write_text("\n".join(human_linked_stale) + ("\n" if human_linked_stale else ""), encoding="utf-8")
        (out / "human_linked_all_ids.txt").write_text("\n".join(sorted(human_linked)) + ("\n" if human_linked else ""), encoding="utf-8")
        if human_linked_missing_file:
            (out / "human_linked_missing_transcript_ids.txt").write_text(
                "\n".join(human_linked_missing_file) + "\n", encoding="utf-8"
            )

        all_counts = counts_all
        report_lines = [
            "# Transcript analysis freshness",
            "",
            f"- Generated at (UTC): `{summary['generated_at_utc']}`",
            f"- Canonical `prompt_sha256` (volatile lines stripped): `{canonical_sha}`",
            f"- Machine transcript files: **{total}**",
            f"- Human-linked assets (clip manifest): **{len(human_linked)}**",
            "",
            "## Buckets (all machine transcripts)",
            "",
            "| bucket | count |",
            "|--------|-------|",
        ]
        for k in sorted(all_counts.keys()):
            report_lines.append(f"| {k} | {all_counts[k]} |")
        report_lines.extend([
            "",
            "## Human-linked slice",
            "",
            f"- Stale (need re-run): **{len(human_linked_stale)}** — see `stale_human_linked_ids.txt`",
            f"- Fresh at canonical SHA: **{len(human_linked_fresh)}**",
            f"- Manifest asset_ids with no `*.transcript.json`: **{len(human_linked_missing_file)}**",
            "",
            "## Re-run command (example)",
            "",
            "After `build_transcript_prompt_context.py` if registries changed:",
            "",
            "```powershell",
            f'python _scripts/transcripts/run_transcript_analysis_via_gemini.py --only-asset-ids-file "{(out / "stale_human_linked_ids.txt").as_posix()}"',
            "```",
            "",
            "Force LLM on borderline clips (optional): add `--no-heuristic-skip`.",
            "",
        ])
        (out / "report.md").write_text("\n".join(report_lines) + "\n", encoding="utf-8")
        print()
        print(f"Wrote: {out / 'report.md'}")
        print(f"Wrote: {out / 'summary.json'}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
