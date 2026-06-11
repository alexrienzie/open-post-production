#!/usr/bin/env python3
"""
Substantive quality / cross-record consistency for merged transcript analysis.

This is **not** a second LLM judge pass. It applies **deterministic** checks and
**corpus-level statistics** that surface likely editorial inconsistencies:

1. **Timecodes** — `key_quotes` / `key_moments` windows vs `playback_duration_sec`
2. **subject_of_interview** — must appear in `people_ids` when set
3. **Registry mentions** — flagged when a tagged `people_id` / `org_id` has no
   obvious `canonical_name` / `aliases` substring in `full_text` (case-insensitive).
   Expect false positives when the speaker is only referred to via pronouns/nicknames
   not in the registry entry.
4. **Duplicate one-line summaries** — exact match (normalized whitespace)
5. **Pass / analyzer lineage** — counts by `analysis.analyzer` prefix
6. **Cross-pass field distributions** — by analyzer bucket: `tone.mood`,
   `craft.audio_quality`, mean `key_quotes` / `themes` counts (drift detector)

Usage:
  python _scripts/transcripts/report_transcript_analysis_substantive.py \\
    --out-dir _runs/transcript_analysis_substantive_20260509_2000
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
TRANSCRIPTS_DIR = ROOT / "assets" / "catalog" / "transcripts"
PEOPLE_PATH = ROOT / "people" / "people.json"
ORGS_PATH = ROOT / "organizations" / "orgs.json"


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _norm_ws(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())


def _analyzer_bucket(analyzer: str | None) -> str:
    if not analyzer:
        return "(none)"
    s = str(analyzer)
    if "heuristic-skip" in s:
        return "heuristic-skip"
    if "gemini-2.5-pro" in s:
        return "gemini-2.5-pro"
    if "gemini-3" in s or "gemini-3.1" in s:
        return "gemini-3.x"
    if "transcript-batch" in s:
        return "other-batch"
    return "other"


def _mention_strings_for_person(p: dict) -> list[str]:
    out: list[str] = []
    cn = p.get("canonical_name")
    if isinstance(cn, str) and cn.strip():
        out.append(cn.strip())
    for a in p.get("aliases") or []:
        if isinstance(a, str) and len(a.strip()) >= 2:
            out.append(a.strip())
    # de-dupe preserving order
    seen: set[str] = set()
    uniq: list[str] = []
    for x in out:
        k = x.lower()
        if k not in seen:
            seen.add(k)
            uniq.append(x)
    return uniq


def _mention_strings_for_org(o: dict) -> list[str]:
    return _mention_strings_for_person(o)  # same shape


def _text_has_any(haystack: str, needles: list[str]) -> bool:
    h = haystack.lower()
    for n in needles:
        if len(n) >= 2 and n.lower() in h:
            return True
    return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-dir", type=Path, required=True)
    args = ap.parse_args()

    people_raw = _load_json(PEOPLE_PATH)
    people_by_id = {p["id"]: p for p in (people_raw.get("people") or []) if p.get("id")}
    orgs_raw = _load_json(ORGS_PATH)
    orgs_by_id = {o["id"]: o for o in (orgs_raw.get("organizations") or []) if o.get("id")}

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    rows_flags: list[dict[str, Any]] = []
    summary_counts: Counter[str] = Counter()
    analyzer_counter: Counter[str] = Counter()
    summary_dupes: Counter[str] = Counter()

    # Per analyzer bucket: collect metrics
    bucket_key_quotes: dict[str, list[int]] = defaultdict(list)
    bucket_themes_len: dict[str, list[int]] = defaultdict(list)
    bucket_mood: dict[str, Counter[str]] = defaultdict(Counter)
    bucket_audio: dict[str, Counter[str]] = defaultdict(Counter)

    unreadable = 0
    no_analysis = 0

    for p in sorted(TRANSCRIPTS_DIR.glob("*.transcript.json")):
        try:
            rec = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            unreadable += 1
            continue
        aid = rec.get("asset_id") or p.stem.replace(".transcript", "")
        a = rec.get("analysis") or {}
        if not a.get("analyzed_at"):
            no_analysis += 1
            continue

        full_text = str(rec.get("full_text") or "")
        dur = float(rec.get("playback_duration_sec") or 0.0)
        analyzer = a.get("analyzer")
        bucket = _analyzer_bucket(analyzer)
        analyzer_counter[bucket] += 1

        sol = a.get("summary_one_line")
        if isinstance(sol, str) and sol.strip():
            summary_dupes[_norm_ws(sol)] += 1

        kq = a.get("key_quotes") if isinstance(a.get("key_quotes"), list) else []
        km = a.get("key_moments") if isinstance(a.get("key_moments"), list) else []
        themes = a.get("themes") if isinstance(a.get("themes"), list) else []
        bucket_key_quotes[bucket].append(len(kq))
        bucket_themes_len[bucket].append(len(themes))

        tone = a.get("tone") if isinstance(a.get("tone"), dict) else {}
        mood = tone.get("mood")
        if isinstance(mood, str) and mood:
            bucket_mood[bucket][mood] += 1
        craft = rec.get("craft") if isinstance(rec.get("craft"), dict) else {}
        aq = craft.get("audio_quality")
        if isinstance(aq, str) and aq:
            bucket_audio[bucket][aq] += 1

        flags: list[str] = []

        soi = rec.get("subject_of_interview")
        if soi is None and isinstance(a, dict):
            soi = a.get("subject_of_interview")
        pids = list(rec.get("people_ids") or [])
        if soi and soi not in pids:
            flags.append("subject_of_interview_not_in_people_ids")

        if dur > 0:
            for i, q in enumerate(kq):
                if not isinstance(q, dict):
                    continue
                for key in ("start_sec", "end_sec"):
                    v = q.get(key)
                    if isinstance(v, (int, float)) and (v < -0.5 or v > dur + 0.5):
                        flags.append(f"key_quotes[{i}].{key}_out_of_duration")
            for i, m in enumerate(km):
                if not isinstance(m, dict):
                    continue
                for key in ("start_sec", "end_sec"):
                    v = m.get(key)
                    if isinstance(v, (int, float)) and (v < -0.5 or v > dur + 0.5):
                        flags.append(f"key_moments[{i}].{key}_out_of_duration")

        if full_text.strip():
            for pid in pids:
                pr = people_by_id.get(pid)
                if not pr:
                    continue
                needles = _mention_strings_for_person(pr)
                if needles and not _text_has_any(full_text, needles):
                    flags.append(f"people_id_no_literal_mention:{pid}")
            for oid in rec.get("org_ids") or []:
                org = orgs_by_id.get(oid)
                if not org:
                    continue
                needles = _mention_strings_for_org(org)
                if needles and not _text_has_any(full_text, needles):
                    flags.append(f"org_id_no_literal_mention:{oid}")

        for f in flags:
            summary_counts[f] += 1
        if flags:
            rows_flags.append({"asset_id": aid, "analyzer_bucket": bucket, "flags": flags})

    dupe_clusters = {s: n for s, n in summary_dupes.items() if n > 1}
    dupe_cluster_count = len(dupe_clusters)
    dupe_record_estimate = sum(n for n in dupe_clusters.values())

    def _stats(xs: list[int]) -> dict[str, float | int]:
        if not xs:
            return {"n": 0}
        return {
            "n": len(xs),
            "mean": round(statistics.mean(xs), 3),
            "median": round(statistics.median(xs), 3),
        }

    bucket_stats: dict[str, Any] = {}
    for b in sorted(bucket_key_quotes.keys()):
        bucket_stats[b] = {
            "records": analyzer_counter.get(b, 0),
            "key_quotes_count": _stats(bucket_key_quotes[b]),
            "themes_count": _stats(bucket_themes_len[b]),
            "tone_mood": dict(bucket_mood[b].most_common(12)),
            "craft_audio_quality": dict(bucket_audio[b].most_common(8)),
        }

    ts = dt.datetime.now(dt.timezone.utc).isoformat()
    summary = {
        "generated_at_utc": ts,
        "transcript_files_unreadable": unreadable,
        "records_without_analysis_analyzed_at": no_analysis,
        "records_with_analysis": sum(analyzer_counter.values()),
        "analyzer_bucket_counts": dict(analyzer_counter),
        "flag_counts_corpus": dict(summary_counts),
        "records_with_any_substantive_flag": len(rows_flags),
        "duplicate_one_line_summary_strings": dupe_cluster_count,
        "transcripts_sharing_duplicate_summary": dupe_record_estimate,
        "bucket_field_distributions": bucket_stats,
    }

    (out_dir / "summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    with (out_dir / "flagged_assets.jsonl").open("w", encoding="utf-8") as f:
        for row in rows_flags:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    if rows_flags:
        (out_dir / "flagged_asset_ids.txt").write_text(
            "\n".join(r["asset_id"] for r in rows_flags) + "\n",
            encoding="utf-8",
        )

    # Top duplicate summaries (editorial sameness / copy-paste)
    top_dupes = sorted(dupe_clusters.items(), key=lambda x: -x[1])[:25]

    report = [
        "# Transcript analysis — substantive consistency (heuristic)",
        "",
        f"- Generated at (UTC): `{ts}`",
        "- **Not** an LLM quality judge; uses timecodes, ID lists, registry strings, and distributions.",
        "",
        "## Analyzer lineage",
        "",
        "| bucket | records |",
        "|--------|---------|",
    ]
    for b, n in sorted(analyzer_counter.items(), key=lambda x: -x[1]):
        report.append(f"| {b} | {n} |")
    report.extend([
        "",
        "## Corpus flags (counts)",
        "",
        "| flag / pattern | transcripts |",
        "|------------------|---------------|",
    ])
    for k, n in sorted(summary_counts.items(), key=lambda x: -x[1])[:40]:
        report.append(f"| {k} | {n} |")
    if len(summary_counts) > 40:
        report.append(f"| … | ({len(summary_counts) - 40} more in summary.json) |")
    report.extend([
        "",
        "## Duplicate `summary_one_line` (exact, whitespace-normalized)",
        "",
        f"- **{dupe_cluster_count}** distinct summary strings shared by **{dupe_record_estimate}** transcripts.",
        "",
    ])
    for s, n in top_dupes:
        excerpt = (s[:120] + "…") if len(s) > 120 else s
        report.append(f"- **{n}×** — {excerpt}")
    report.extend([
        "",
        "## Cross-pass distributions (drift check)",
        "",
        "Compare `key_quotes` / `themes` means and `tone.mood` mix across analyzer buckets.",
        "Full table: `summary.json` → `bucket_field_distributions`.",
        "",
    ])
    report.extend([
        "",
        "## Flagged records",
        "",
        f"**{len(rows_flags)}** assets with ≥1 flag — see `flagged_assets.jsonl`.",
        "",
        "### Interpretation",
        "",
        "- **`people_id_no_literal_mention` / `org_id_*`:** often pronouns, ASR errors, or "
        "registry aliases that do not appear verbatim — triage, not automatic delete.",
        "- **Timecode flags:** usually model or duration drift; fix or re-run analysis.",
        "",
    ])
    (out_dir / "report.md").write_text("\n".join(report) + "\n", encoding="utf-8")

    print(json.dumps(summary, indent=2))
    print(f"\nWrote: {out_dir / 'report.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
