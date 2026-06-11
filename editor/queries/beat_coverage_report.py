"""
Layer 1: Beat-coverage report (fast, no LLM).

Scans analyzed transcript records (assets/transcripts/*.transcript.json) and
aggregates moment coverage across the story spine (story/moments.json).

Outputs an editor-readable markdown report to:
  _review_drafts/beat_coverage_<ts>.md

Optional: also emits a CSV summary alongside the markdown.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional


# editor/queries/<this> -> editor/queries -> editor -> open-post-stack
WORKSPACE = Path(__file__).resolve().parent.parent.parent
DATASET_ROOT = WORKSPACE / "dataset"
MOMENTS_PATH = DATASET_ROOT / "story" / "moments.json"
TRANSCRIPTS_DIR = DATASET_ROOT / "assets" / "catalog" / "transcripts"
REVIEW_DRAFTS_DIR = DATASET_ROOT / "_review_drafts"
ROOT = DATASET_ROOT  # back-compat for any internal references


def _utc_ts_slug(now: Optional[dt.datetime] = None) -> str:
    now = now or dt.datetime.now(dt.timezone.utc)
    return now.strftime("%Y%m%dT%H%M%SZ")


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _safe_get(d: dict[str, Any], *keys: str, default: Any = None) -> Any:
    cur: Any = d
    for k in keys:
        if not isinstance(cur, dict) or k not in cur:
            return default
        cur = cur[k]
    return cur


@dataclass(frozen=True)
class Moment:
    moment_id: str
    title: str
    act: Optional[int]


@dataclass(frozen=True)
class Candidate:
    asset_id: str
    record_path: str
    playback_duration_sec: float
    shot_kind: str
    subject_of_interview: Optional[str]
    key_quotes_count: int
    summary_one_line: Optional[str]

    @property
    def duration_min(self) -> float:
        return float(self.playback_duration_sec or 0.0) / 60.0

    @property
    def is_signal_rich(self) -> bool:
        return bool(self.subject_of_interview and self.key_quotes_count >= 2)

    @property
    def signal_score(self) -> float:
        # Simple monotonic signal: prioritize quote density, then longer runtime.
        return (self.key_quotes_count * 100.0) + self.duration_min


def load_moments(moments_path: Path) -> list[Moment]:
    raw = _read_json(moments_path)
    outline = raw.get("moments_outline") or []
    moments: list[Moment] = []
    for b in outline:
        if not isinstance(b, dict):
            continue
        moment_id = str(b.get("moment_id") or "").strip()
        if not moment_id:
            continue
        moments.append(
            Moment(
                moment_id=moment_id,
                title=str(b.get("title") or "").strip() or "(untitled)",
                act=b.get("act") if isinstance(b.get("act"), int) else None,
            )
        )
    return moments


def iter_transcript_paths(transcripts_dir: Path) -> Iterable[Path]:
    # Most files follow: <asset_id>.transcript.json
    yield from transcripts_dir.glob("*.transcript.json")


def parse_candidate(record: dict[str, Any], path: Path) -> Candidate:
    asset_id = str(record.get("asset_id") or path.name.split(".")[0])
    playback_duration_sec = float(record.get("playback_duration_sec") or 0.0)

    craft = record.get("craft") if isinstance(record.get("craft"), dict) else {}
    shot_kind = str((craft or {}).get("shot_kind") or "unknown")

    analysis = record.get("analysis") if isinstance(record.get("analysis"), dict) else {}
    subject_of_interview = record.get("subject_of_interview")
    if subject_of_interview is None:
        subject_of_interview = analysis.get("subject_of_interview")
    if subject_of_interview is not None:
        subject_of_interview = str(subject_of_interview).strip() or None

    key_quotes = analysis.get("key_quotes") if isinstance(analysis.get("key_quotes"), list) else []
    key_quotes_count = len(key_quotes)
    summary_one_line = analysis.get("summary_one_line")
    if summary_one_line is not None:
        summary_one_line = str(summary_one_line).strip() or None

    return Candidate(
        asset_id=asset_id,
        record_path=str(path.as_posix()),
        playback_duration_sec=playback_duration_sec,
        shot_kind=shot_kind,
        subject_of_interview=subject_of_interview,
        key_quotes_count=key_quotes_count,
        summary_one_line=summary_one_line,
    )


def write_markdown_report(
    out_path: Path,
    moments: list[Moment],
    per_moment_records: dict[str, list[Candidate]],
    shot_kind_counts: dict[str, Counter[str]],
    total_duration_sec: dict[str, float],
    gap_min_records: int,
) -> None:
    lines: list[str] = []
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    lines.append(f"# Beat coverage report\n")
    lines.append(f"- Generated at (UTC): **{now}**\n")
    lines.append(f"- Moments: **{len(moments)}**\n")
    lines.append(f"- Source moments: `{MOMENTS_PATH.as_posix()}`\n")
    lines.append(f"- Source records: `{TRANSCRIPTS_DIR.as_posix()}`\n")
    lines.append("\n---\n")
    lines.append("## Executive gaps\n")

    gap_rows: list[str] = []
    for moment in moments:
        recs = per_moment_records.get(moment.moment_id, [])
        total = len(recs)
        strong = sum(1 for r in recs if r.is_signal_rich)
        if total < gap_min_records or strong == 0:
            gap_rows.append(
                f"- **{moment.moment_id} — {moment.title}**: "
                f"{total} tagged, {strong} signal-rich (subject + ≥2 quotes)\n"
            )
    if gap_rows:
        lines.extend([*gap_rows, "\n"])
    else:
        lines.append("- (none)\n\n")

    lines.append("---\n")
    lines.append("## Per-moment breakdown\n")

    for moment in moments:
        moment_id = moment.moment_id
        recs = per_moment_records.get(moment_id, [])
        total = len(recs)
        dur_min = (total_duration_sec.get(moment_id, 0.0) or 0.0) / 60.0
        shots = shot_kind_counts.get(moment_id, Counter())
        strong = sum(1 for r in recs if r.is_signal_rich)
        gap_flag = "GAP" if (total < gap_min_records or strong == 0) else ""

        act = f"Act {moment.act}" if moment.act is not None else "Act ?"
        lines.append(f"\n### {moment_id} — {moment.title} ({act}) {gap_flag}\n")
        lines.append(f"- Total records tagged: **{total}**\n")
        lines.append(f"- Total tagged duration: **{dur_min:.1f} min**\n")
        lines.append(
            f"- Editorial signal (`subject_of_interview` + ≥2 `key_quotes`): **{strong}**\n"
        )

        # Shot kind breakdown
        if shots:
            s_parts = ", ".join(f"{k}: {v}" for k, v in shots.most_common())
        else:
            s_parts = "(none)"
        lines.append(f"- craft.shot_kind: {s_parts}\n")

        # Top 10 by signal
        top = [r for r in recs if r.is_signal_rich]
        top.sort(key=lambda r: r.signal_score, reverse=True)
        top = top[:10]

        lines.append(
            "\n**Top 10 records by signal** (`subject_of_interview` + ≥2 `key_quotes`)\n"
        )
        if not top:
            lines.append("- (none)\n")
        else:
            for i, r in enumerate(top, start=1):
                summary = r.summary_one_line or "(no summary)"
                lines.append(
                    f"- {i}. **{r.asset_id}** "
                    f"({r.duration_min:.1f}m, {r.shot_kind}, quotes={r.key_quotes_count}) — {summary}\n"
                )
                lines.append(f"  - path: `{r.record_path}`\n")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("".join(lines), encoding="utf-8")


def write_csv_summary(
    out_path: Path,
    moments: list[Moment],
    per_moment_records: dict[str, list[Candidate]],
    shot_kind_counts: dict[str, Counter[str]],
    total_duration_sec: dict[str, float],
    gap_min_records: int,
) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(
            [
                "moment_id",
                "title",
                "act",
                "tagged_records_total",
                "tagged_duration_min",
                "signal_rich_count",
                "gap_flag",
                "shot_kind_breakdown",
            ]
        )
        for moment in moments:
            moment_id = moment.moment_id
            recs = per_moment_records.get(moment_id, [])
            total = len(recs)
            dur_min = (total_duration_sec.get(moment_id, 0.0) or 0.0) / 60.0
            shots = shot_kind_counts.get(moment_id, Counter())
            strong = sum(1 for r in recs if r.is_signal_rich)
            gap_flag = (total < gap_min_records) or (strong == 0)
            w.writerow(
                [
                    moment_id,
                    moment.title,
                    moment.act if moment.act is not None else "",
                    total,
                    f"{dur_min:.2f}",
                    strong,
                    "GAP" if gap_flag else "",
                    "; ".join(f"{k}:{v}" for k, v in shots.most_common()),
                ]
            )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--moments", default=str(MOMENTS_PATH), help="Path to story/moments.json")
    ap.add_argument("--transcripts-dir", default=str(TRANSCRIPTS_DIR), help="Directory of *.transcript.json records")
    ap.add_argument("--out-dir", default=str(REVIEW_DRAFTS_DIR), help="Output directory")
    ap.add_argument("--gap-min-records", type=int, default=5, help="Gap flag if < N total tagged records")
    ap.add_argument("--emit-csv", action="store_true", help="Also write a CSV summary next to the markdown")
    ap.add_argument("--limit", type=int, default=0, help="If >0, only scan first N transcript files (debug)")
    args = ap.parse_args()

    moments_path = Path(args.moments)
    transcripts_dir = Path(args.transcripts_dir)
    out_dir = Path(args.out_dir)

    moments = load_moments(moments_path)
    moments_by_id = {m.moment_id: m for m in moments}

    per_moment_records: dict[str, list[Candidate]] = defaultdict(list)
    shot_kind_counts: dict[str, Counter[str]] = defaultdict(Counter)
    total_duration_sec: dict[str, float] = defaultdict(float)

    paths = list(iter_transcript_paths(transcripts_dir))
    if args.limit and args.limit > 0:
        paths = paths[: args.limit]

    for p in paths:
        try:
            record = _read_json(p)
        except Exception:
            continue

        moment_ids = record.get("moment_ids") if isinstance(record.get("moment_ids"), list) else []
        if not moment_ids:
            continue

        cand = parse_candidate(record, p)
        for moment_id in moment_ids:
            moment_id = str(moment_id)
            if moment_id not in moments_by_id:
                continue
            per_moment_records[moment_id].append(cand)
            shot_kind_counts[moment_id][cand.shot_kind] += 1
            total_duration_sec[moment_id] += float(cand.playback_duration_sec or 0.0)

    ts = _utc_ts_slug()
    md_path = out_dir / f"beat_coverage_{ts}.md"
    csv_path = out_dir / f"beat_coverage_{ts}.csv"

    write_markdown_report(
        out_path=md_path,
        moments=moments,
        per_moment_records=per_moment_records,
        shot_kind_counts=shot_kind_counts,
        total_duration_sec=total_duration_sec,
        gap_min_records=int(args.gap_min_records),
    )
    if args.emit_csv:
        write_csv_summary(
            out_path=csv_path,
            moments=moments,
            per_moment_records=per_moment_records,
            shot_kind_counts=shot_kind_counts,
            total_duration_sec=total_duration_sec,
            gap_min_records=int(args.gap_min_records),
        )

    print(str(md_path))
    if args.emit_csv:
        print(str(csv_path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

