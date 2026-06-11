#!/usr/bin/env python3
"""
Human ↔ machine speaker alignment report (clip-manifest slice).

Answers for assets in human_transcripts/clip_segments_manifest.jsonl:

1) **Structural / refreshed** — Do machine segments use canonical `p_*` in
   `segments[].speaker` (what `review_speaker_accuracy.py` expects)? Is there a
   speaker resolution audit (inline or `_audit/transcript_provenance/` sidecar)?

2) **Gold accuracy** (optional) — Run `review_speaker_accuracy.py` in a
   subprocess and merge its aggregate accuracy into the report (human timecoded
   utterances vs majority machine speaker in window).

This is the recommended **step-one** QA gate before transcript analysis refresh.

Usage:
  python _scripts/transcripts/report_human_machine_speaker_alignment.py
  python _scripts/transcripts/report_human_machine_speaker_alignment.py --out-dir _runs/speaker_alignment_20260508
  python _scripts/transcripts/report_human_machine_speaker_alignment.py --out-dir _runs/... --gold-eval
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import subprocess
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
TRANSCRIPTS_DIR = ROOT / "assets" / "catalog" / "transcripts"
CLIP_MANIFEST = ROOT / "assets" / "catalog" / "human_transcripts" / "clip_segments_manifest.jsonl"

from transcript_provenance import get_speaker_resolution_audit  # noqa: E402


def _pid_speaker(s: str | None) -> bool:
    return isinstance(s, str) and s.startswith("p_") and len(s) > 2


def manifest_rows_by_asset() -> dict[str, list[dict[str, Any]]]:
    by_aid: dict[str, list[dict[str, Any]]] = defaultdict(list)
    if not CLIP_MANIFEST.exists():
        return {}
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
                by_aid[aid].append(o)
    return by_aid


def segment_stats(transcript: dict) -> dict[str, Any]:
    segs = transcript.get("segments") or []
    total = len(segs)
    pid = missing = non_pid = 0
    for s in segs:
        sp = s.get("speaker")
        if sp is None or sp == "":
            missing += 1
        elif _pid_speaker(sp):
            pid += 1
        else:
            non_pid += 1
    denom = total if total else 1
    return {
        "segments_total": total,
        "segments_speaker_pid": pid,
        "segments_speaker_missing": missing,
        "segments_speaker_non_pid": non_pid,
        "pct_pid": round(pid / denom, 6),
    }


def audit_meta(transcript: dict, *, asset_id: str) -> dict[str, Any]:
    aud = get_speaker_resolution_audit(transcript, asset_id=asset_id)
    if not isinstance(aud, dict):
        return {"has_speaker_resolution_audit": False}
    src = aud.get("source") or {}
    return {
        "has_speaker_resolution_audit": True,
        "audit_run_id": aud.get("run_id"),
        "audit_resolved_at": aud.get("resolved_at"),
        "audit_roster_id": src.get("roster_id"),
        "audit_method": aud.get("method"),
    }


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Report speaker tag alignment for human-linked machine transcripts.",
    )
    ap.add_argument(
        "--out-dir",
        type=Path,
        default=None,
        help="Write report.md, summary.json, per_asset.json",
    )
    ap.add_argument(
        "--gold-eval",
        action="store_true",
        help="Run review_speaker_accuracy.py and merge aggregate metrics (slower).",
    )
    ap.add_argument(
        "--pid-threshold-warn",
        type=float,
        default=0.85,
        help="Below this pct_pid, structural_flag uses low_pid_coverage.",
    )
    args = ap.parse_args()

    by_aid = manifest_rows_by_asset()
    per_asset: list[dict[str, Any]] = []

    totals = Counter()
    for asset_id in sorted(by_aid.keys()):
        rows = by_aid[asset_id]
        roster_ids = sorted({r.get("roster_id") for r in rows if r.get("roster_id")})
        tp = TRANSCRIPTS_DIR / f"{asset_id}.transcript.json"
        entry: dict[str, Any] = {
            "asset_id": asset_id,
            "manifest_row_count": len(rows),
            "roster_ids": roster_ids,
            "missing_transcript": not tp.exists(),
        }
        if not tp.exists():
            totals["assets_missing_transcript"] += 1
            entry["structural_flag"] = "missing_transcript"
            totals["flag_missing_transcript"] += 1
            per_asset.append(entry)
            continue
        try:
            transcript = json.loads(tp.read_text(encoding="utf-8"))
        except Exception:
            totals["assets_transcript_read_error"] += 1
            entry["structural_flag"] = "read_error"
            totals["flag_read_error"] += 1
            per_asset.append(entry)
            continue

        st = segment_stats(transcript)
        am = audit_meta(transcript, asset_id=asset_id)
        entry.update(st)
        entry.update(am)
        pct = float(st.get("pct_pid") or 0.0)
        thr = float(args.pid_threshold_warn)
        if st["segments_total"] == 0:
            entry["structural_flag"] = "no_segments"
        elif pct < thr:
            entry["structural_flag"] = "low_pid_coverage"
        elif pct < 0.98:
            # Partial resolution — still worth re-running resolve or inspecting.
            entry["structural_flag"] = (
                "moderate_pid_no_audit"
                if not am["has_speaker_resolution_audit"]
                else "moderate_pid_with_audit"
            )
        elif not am["has_speaker_resolution_audit"]:
            entry["structural_flag"] = "no_audit_high_pid"
        else:
            entry["structural_flag"] = "ok"
        totals[f"flag_{entry['structural_flag']}"] += 1
        totals["assets_with_transcript"] += 1
        per_asset.append(entry)

    summary = {
        "generated_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "clip_manifest": str(CLIP_MANIFEST.relative_to(ROOT)),
        "unique_asset_ids_in_manifest": len(by_aid),
        "manifest_row_count": sum(len(v) for v in by_aid.values()),
        "totals": dict(totals),
        "structural_flags": {
            k.replace("flag_", ""): v
            for k, v in totals.items()
            if k.startswith("flag_")
        },
        "gold_eval": None,
    }

    # Console
    print("Human-linked speaker alignment (structural)")
    print(f"  unique assets (manifest): {len(by_aid)}")
    print(f"  manifest rows: {summary['manifest_row_count']}")
    print(f"  assets with transcript file: {totals.get('assets_with_transcript', 0)}")
    print(f"  missing transcript file: {totals.get('assets_missing_transcript', 0)}")
    print("  structural_flag counts:")
    for k, v in sorted(summary["structural_flags"].items()):
        print(f"    {k}: {v}")

    gold: dict[str, Any] | None = None
    if args.gold_eval:
        reviewer = ROOT / "_scripts" / "review_speaker_accuracy.py"
        if not reviewer.exists():
            print("ERROR: review_speaker_accuracy.py not found", file=sys.stderr)
            return 1
        proc = subprocess.run(
            [sys.executable, str(reviewer)],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        if proc.returncode != 0:
            print(proc.stderr or proc.stdout, file=sys.stderr)
            return proc.returncode or 1
        try:
            gold = json.loads(proc.stdout)
        except json.JSONDecodeError:
            print("ERROR: could not parse review_speaker_accuracy stdout as JSON", file=sys.stderr)
            print(proc.stdout[:2000], file=sys.stderr)
            return 1
        summary["gold_eval"] = {
            "run_id": gold.get("run_id"),
            "accuracy": gold.get("accuracy"),
            "weighted_accuracy_by_pred_support_sec": gold.get("weighted_accuracy_by_pred_support_sec"),
            "utterances_scored": gold.get("utterances_scored"),
            "utterances_correct": gold.get("utterances_correct"),
            "utterances_incorrect": gold.get("utterances_incorrect"),
            "out_json": gold.get("out_json"),
            "samples": gold.get("samples"),
        }
        print()
        print("Gold eval (review_speaker_accuracy)")
        print(f"  run_id: {gold.get('run_id')}")
        print(f"  utterance accuracy: {gold.get('accuracy')}")
        print(f"  weighted (by pred overlap sec): {gold.get('weighted_accuracy_by_pred_support_sec')}")
        print(f"  scored / correct / incorrect: {gold.get('utterances_scored')} / "
              f"{gold.get('utterances_correct')} / {gold.get('utterances_incorrect')}")
        print(f"  detail: {gold.get('out_json')}")

    if args.out_dir:
        out = Path(args.out_dir)
        out.mkdir(parents=True, exist_ok=True)
        (out / "summary.json").write_text(
            json.dumps(summary, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        (out / "per_asset.json").write_text(
            json.dumps(per_asset, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        lines = [
            "# Human ↔ machine speaker alignment",
            "",
            f"- Generated at (UTC): `{summary['generated_at_utc']}`",
            f"- Manifest slice: `{CLIP_MANIFEST.relative_to(ROOT)}`",
            f"- Unique assets: **{len(by_aid)}** (manifest rows: **{summary['manifest_row_count']}**)",
            "",
            "## Structural (refreshed tags)",
            "",
            "For each machine transcript, `segments[].speaker` should be canonical `p_*` "
            "(see `resolve_speakers_from_human_transcripts.py`). "
            "Speaker resolution audit lives in `_audit/transcript_provenance/<asset_id>.json` (pointer `speaker_provenance` on transcript), or legacy inline.",
            "",
            "| structural_flag | meaning |",
            "|-----------------|---------|",
            "| `ok` | ≥98% segments have `p_*` **and** resolution audit present (sidecar or inline) |",
            "| `moderate_pid_with_audit` | 85–98% segments have `p_*`, audit present (partial mapping) |",
            "| `moderate_pid_no_audit` | 85–98% segments have `p_*`, no audit |",
            "| `no_audit_high_pid` | ≥98% `p_*` but no audit (unusual) |",
            "| `low_pid_coverage` | &lt;85% segments with `p_*` (default threshold; override `--pid-threshold-warn`) |",
            "| `missing_transcript` | no `*.transcript.json` for manifest asset_id |",
            "| `no_segments` | empty segments array |",
            "| `read_error` | transcript JSON failed to load |",
            "",
            "### Counts",
            "",
        ]
        for k, v in sorted(summary["structural_flags"].items()):
            lines.append(f"- **{k}**: {v}")
        lines.extend([
            "",
            "## Gold accuracy (human timecoded labels)",
            "",
        ])
        if summary["gold_eval"]:
            ge = summary["gold_eval"]
            lines.extend([
                f"- From `review_speaker_accuracy.py` run **`{ge.get('run_id')}`**",
                f"- Utterance accuracy: **{ge.get('accuracy')}**",
                f"- Weighted by machine overlap support: **{ge.get('weighted_accuracy_by_pred_support_sec')}**",
                f"- Scored utterances: **{ge.get('utterances_scored')}** (correct **{ge.get('utterances_correct')}**, incorrect **{ge.get('utterances_incorrect')}**)",
                f"- Full JSON: `{ge.get('out_json')}`",
                "",
            ])
        else:
            lines.extend([
                "Not run in this invocation. To measure:",
                "",
                "```powershell",
                "python _scripts/transcripts/review_speaker_accuracy.py",
                "# or re-run this report with: --gold-eval",
                "```",
                "",
            ])
        lines.extend([
            "## Files",
            "",
            "- `per_asset.json` — segment counts, audit metadata, structural_flag per asset",
            "- `summary.json` — aggregates + optional gold_eval",
            "",
        ])
        (out / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
        print()
        print(f"Wrote: {out / 'report.md'}")
        print(f"Wrote: {out / 'summary.json'}")
        print(f"Wrote: {out / 'per_asset.json'}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
