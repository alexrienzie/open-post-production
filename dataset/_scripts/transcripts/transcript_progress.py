"""
Snapshot of in-flight transcript analysis (or cleanup) pass.

Reads the most-recently-touched _runs/transcript_*_<ts>/ dir, tails its
manifest + log.jsonl + errors.jsonl, cross-checks against on-disk transcript
records, and prints a tight progress report: completed, throughput, ETA,
recent rate-limit signals, error summary.

Usage:
    python _scripts/transcripts/transcript_progress.py
    python _scripts/transcripts/transcript_progress.py --pass-name transcript_analysis_20260504_2052
    python _scripts/transcripts/transcript_progress.py --kind cleanup     # snapshot a cleanup run instead

Intended to be cheap to run — should complete in well under a second even
across 3,878 records.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TRANSCRIPTS = ROOT / "assets/transcripts"
RUNS_DIR = ROOT / "_runs"


def find_active_run(kind: str, explicit: str | None = None) -> Path | None:
    if explicit:
        p = RUNS_DIR / explicit
        return p if p.exists() else None
    candidates = [d for d in RUNS_DIR.glob(f"transcript_{kind}_*") if d.is_dir()]
    if not candidates:
        return None
    return max(candidates, key=lambda d: d.stat().st_mtime)


def parse_log(log_path: Path) -> tuple[list[dict], list[dict]]:
    """Returns (ok_entries, fail_entries)."""
    ok, fail = [], []
    if not log_path.exists():
        return ok, fail
    with log_path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except Exception:
                continue
            (ok if entry.get("ok") else fail).append(entry)
    return ok, fail


def parse_errors(errors_path: Path) -> list[dict]:
    if not errors_path.exists():
        return []
    out: list[dict] = []
    with errors_path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                pass
    return out


def count_done_on_disk(prompt_sha: str | None) -> tuple[int, int]:
    """Returns (total_files, matching_sha_count) — sanity-check vs log totals."""
    files = list(TRANSCRIPTS.glob("*.json"))
    total = len(files)
    if not prompt_sha:
        return total, 0
    matched = 0
    for p in files:
        try:
            rec = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        a = rec.get("analysis") or {}
        if a.get("prompt_sha256") == prompt_sha and a.get("analyzed_at"):
            matched += 1
    return total, matched


def fmt_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        return f"{seconds/60:.1f} min"
    return f"{seconds/3600:.1f} h"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pass-name", default=None)
    ap.add_argument("--kind", default="analysis", choices=["analysis", "cleanup"])
    ap.add_argument("--no-disk-check", action="store_true",
                    help="Skip the on-disk transcript count cross-check (faster).")
    args = ap.parse_args()

    run_dir = find_active_run(args.kind, args.pass_name)
    if run_dir is None:
        print(f"No active transcript_{args.kind}_* run found in {RUNS_DIR}", file=sys.stderr)
        return 1

    manifest_path = run_dir / "manifest.json"
    log_path = run_dir / "log.jsonl"
    errors_path = run_dir / "errors.jsonl"

    if not manifest_path.exists():
        print(f"No manifest.json in {run_dir}", file=sys.stderr)
        return 1

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    ok_entries, fail_entries = parse_log(log_path)
    errors = parse_errors(errors_path)

    started_iso = manifest.get("started_at")
    started_at = dt.datetime.fromisoformat(started_iso.replace("Z", "+00:00")) if started_iso else None
    now = dt.datetime.now(dt.timezone.utc)
    elapsed = (now - started_at).total_seconds() if started_at else 0

    durations_ms = [e.get("duration_ms") for e in ok_entries if isinstance(e.get("duration_ms"), int)]
    last_n = durations_ms[-50:] if durations_ms else []

    total_committed = len(ok_entries)
    total_failed = len(fail_entries)

    # Rate-limit gap detection: look at gaps between consecutive log file mtimes
    # (we don't have per-entry timestamps in the current log shape, so we use
    # a heuristic: a single duration_ms > 5 min is a strong signal we waited
    # through call_with_resume backoff).
    rate_limit_signals = sum(1 for d in durations_ms if d > 300_000)

    avg_ms_recent = statistics.mean(last_n) if last_n else 0
    median_ms_recent = statistics.median(last_n) if last_n else 0
    p90_ms_recent = sorted(last_n)[int(len(last_n) * 0.9)] if len(last_n) >= 10 else (max(last_n) if last_n else 0)

    print(f"Run: {run_dir.name}")
    print(f"Started: {started_iso}  ({fmt_duration(elapsed)} ago)")
    print(f"Model: {manifest.get('model')}  via {manifest.get('model_invocation', '?')}")
    print(f"Prompt sha: {(manifest.get('prompt_sha256') or '')[:12]}...")
    print()
    print(f"Committed (ok):  {total_committed}")
    print(f"Failed:          {total_failed}")
    if total_committed:
        print(f"Throughput:      {total_committed / max(elapsed, 1) * 3600:.1f} records/hr"
              f" ({fmt_duration(elapsed / total_committed)}/record overall)")
    if last_n:
        print(f"Recent (last {len(last_n)}):  "
              f"avg {avg_ms_recent/1000:.1f}s  "
              f"median {median_ms_recent/1000:.1f}s  "
              f"p90 {p90_ms_recent/1000:.1f}s")
    if rate_limit_signals:
        print(f"[!] Rate-limit-shaped delays (single record > 5 min): {rate_limit_signals}")

    # On-disk cross-check
    if not args.no_disk_check:
        total_files, matched = count_done_on_disk(manifest.get("prompt_sha256"))
        remaining = max(0, total_files - matched)
        print()
        print(f"On-disk: {matched} of {total_files} transcripts have analysis with current prompt sha")
        print(f"Remaining: {remaining}")
        if last_n and remaining:
            eta_sec = remaining * (avg_ms_recent / 1000)
            print(f"ETA (pure compute, no rate-limit sleeps): {fmt_duration(eta_sec)}")

    # Error summary
    if errors:
        print()
        print(f"Errors ({len(errors)} total). Last 5:")
        for e in errors[-5:]:
            aid = (e.get("asset_id") or "?")[:16]
            err = (e.get("error") or "?")[:120]
            print(f"  {aid}  {err}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
