"""
Re-stamp `analysis.prompt_sha256` on all committed transcript records to the
current prompt's sha. Use when the prompt file has been modified in a way that
doesn't change semantic meaning (linter formatting, whitespace, line endings)
but does change the sha — which would otherwise cause the analysis runner's
idempotency check to mark every record as needing re-processing.

Run from PowerShell, NOT from inside Cowork's bash sandbox:

    cd <workspace root>
    python _scripts\\restamp_prompt_sha.py --dry-run        # preview counts
    python _scripts\\restamp_prompt_sha.py                  # apply

Idempotent: records that already have the current sha are skipped.

Safety: records WITHOUT `analyzed_at` (i.e., never processed) are not touched.
We only re-stamp records that already have a successful analysis.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TRANSCRIPTS = ROOT / "assets/transcripts"
PROMPT_PATH = ROOT / "_prompts/transcript_analysis_prompt.md"


def atomic_write_json(path: Path, data: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="Scan and report counts but don't mutate files.")
    args = ap.parse_args()

    if not PROMPT_PATH.exists():
        print(f"ERROR: prompt not found at {PROMPT_PATH}", file=sys.stderr)
        return 1

    prompt_text = PROMPT_PATH.read_text(encoding="utf-8")
    current_sha = hashlib.sha256(prompt_text.encode("utf-8")).hexdigest()
    print(f"[prompt] current sha: {current_sha}")

    files = sorted(TRANSCRIPTS.glob("*.json"))
    print(f"[scan] {len(files)} transcript files")

    counts = {
        "already_current": 0,
        "restamped": 0,
        "no_analysis_skipped": 0,
        "no_old_sha_skipped": 0,
        "read_errors": 0,
    }
    old_sha_examples: dict[str, int] = {}

    for p in files:
        try:
            rec = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            counts["read_errors"] += 1
            continue

        a = rec.get("analysis") or {}
        analyzed_at = a.get("analyzed_at")
        old_sha = a.get("prompt_sha256")

        if not analyzed_at:
            counts["no_analysis_skipped"] += 1
            continue
        if not old_sha:
            counts["no_old_sha_skipped"] += 1
            continue
        if old_sha == current_sha:
            counts["already_current"] += 1
            continue

        old_sha_examples[old_sha] = old_sha_examples.get(old_sha, 0) + 1
        if not args.dry_run:
            a["prompt_sha256"] = current_sha
            rec["analysis"] = a
            atomic_write_json(p, rec)
        counts["restamped"] += 1

    print("\n=== RESTAMP SUMMARY ===")
    for k, v in counts.items():
        print(f"  {k:24s} {v}")
    if old_sha_examples:
        print("\n[detail] old shas seen (count):")
        for sha, n in sorted(old_sha_examples.items(), key=lambda kv: -kv[1]):
            print(f"  {sha[:16]}...  ×{n}")
    if args.dry_run:
        print("\n[dry-run] no files modified.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
