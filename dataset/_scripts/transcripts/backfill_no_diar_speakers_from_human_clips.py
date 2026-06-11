#!/usr/bin/env python3
"""
Backfill segment-level `p_*` speakers when Whisper diarization is missing, using
human transcript clip timecodes + labels (same roster as resolve_speakers_from_human_transcripts).

When **every** machine segment has empty/null `speaker_raw` and `speakers_raw` is empty,
we cannot map diarization GUIDs. Instead, for each segment we pick the **human
utterance** (resolved to a single `p_*`) with the largest **time overlap** in
seconds, with margin/ratio gates (conservative).

Also:
- Normalizes `speakers_raw` from `[]` → `{}` when empty.
- Sets `diarized` to **false** when we stamp any segment from this pass
  (ASR was not diarized; prior true was metadata drift).
- Rebuilds `speakers[]` as **synthetic** rollup rows (`speaker_id`: `human_clip:{p_id}`).
- Unions **segment-assigned** and **human-clip-resolved** `p_id` into `people_ids` (including both
  candidates from slash/composite labels), even when overlap gates assign **no** segments — so
  downstream analysis still sees who appears in the human transcript.
- Writes `speaker_resolution_audit` (sidecar under `_audit/transcript_provenance/`, pointer on transcript;
  prior run preserved under `prior_speaker_resolution_audit` when present).

Does **not** overwrite segments that already have a `p_*` in `speaker`.

Usage:
  python _scripts/transcripts/backfill_no_diar_speakers_from_human_clips.py --dry-run
  python _scripts/transcripts/backfill_no_diar_speakers_from_human_clips.py
  python _scripts/transcripts/backfill_no_diar_speakers_from_human_clips.py --only-asset-ids-file ids.txt
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
TRANSCRIPTS_DIR = ROOT / "assets" / "catalog" / "transcripts"
HUMAN_DIR = ROOT / "assets" / "catalog" / "human_transcripts"
CLIP_MANIFEST = HUMAN_DIR / "clip_segments_manifest.jsonl"
AUDIT_DIR = ROOT / "_audit"

sys.path.insert(0, str(ROOT / "_scripts"))
from resolve_speakers_from_human_transcripts import (  # noqa: E402
    PeopleIndex,
    build_people_index,
    norm,
    overlap,
    parse_human_clip_utterances,
    resolve_label_to_pid,
)
from transcript_provenance import (  # noqa: E402
    get_speaker_resolution_audit,
    set_resolution_audit_on_transcript,
)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def now_run_id() -> str:
    return datetime.now(timezone.utc).strftime("no_diar_human_speakers_%Y%m%dT%H%M%SZ")


def atomic_write_json(path: Path, data: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def append_jsonl(path: Path, rec: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _is_pid(s: Any) -> bool:
    return isinstance(s, str) and s.startswith("p_") and len(s) > 2


def build_pid_terms(root: Path) -> dict[str, list[str]]:
    """Lowercase search substrings per p_* (canonical + aliases), longest first."""
    people = json.loads((root / "people" / "people.json").read_text(encoding="utf-8"))
    out: dict[str, list[str]] = {}
    for p in people.get("people") or []:
        pid = p.get("id")
        if not pid:
            continue
        terms: list[str] = []
        for t in [p.get("canonical_name", "")] + list(p.get("aliases") or []):
            tn = norm(t)
            if len(tn) >= 2:
                terms.append(tn)
        terms = sorted(set(terms), key=len, reverse=True)
        if terms:
            out[pid] = terms
    return out


def score_pid_in_text(pid: str, text_lc: str, pid_terms: dict[str, list[str]]) -> float:
    """Rough substring score for disambiguating two-speaker slash labels."""
    score = 0.0
    for term in pid_terms.get(pid) or []:
        if term and term in text_lc:
            score += float(len(term))
    return score


def speakers_raw_is_empty(rec: dict) -> bool:
    sr = rec.get("speakers_raw")
    if sr is None:
        return True
    if isinstance(sr, dict):
        return len(sr) == 0
    if isinstance(sr, list):
        return len(sr) == 0
    return True


def all_segments_lack_diarization_raw(segments: list[dict]) -> bool:
    for s in segments or []:
        v = s.get("speaker_raw")
        if v:
            return False
    return True


def is_no_diar_transcript(rec: dict) -> bool:
    if not speakers_raw_is_empty(rec):
        return False
    return all_segments_lack_diarization_raw(rec.get("segments") or [])


def pick_manifest_row(rows: list[dict], *, fps: float, people_index: PeopleIndex) -> tuple[dict, str]:
    """Pick one manifest row per asset when multiple clips point at same asset_id."""
    if not rows:
        raise ValueError("empty rows")
    if len(rows) == 1:
        return rows[0], "single_clip"
    best = rows[0]
    best_score = -1
    for m in rows:
        seg_rel = m.get("segment_path")
        if not seg_rel:
            continue
        cp = HUMAN_DIR / seg_rel
        if not cp.exists():
            continue
        try:
            clip = json.loads(cp.read_text(encoding="utf-8"))
        except Exception:
            continue
        utts = parse_human_clip_utterances(clip.get("human_clip_text", ""), fps=fps)
        n = 0
        for u in utts:
            pid, _ = resolve_label_to_pid(u.get("speaker_label", ""), people_index)
            if pid:
                n += 1
        score = len(utts) * 1000 + n
        if score > best_score:
            best_score = score
            best = m
    return best, "longest_clip_heuristic"


def classify_human_utts(
    human_utts: list[dict],
    people_index: PeopleIndex,
    *,
    min_human_utt_dur_sec: float,
) -> tuple[list[dict], list[dict]]:
    """Split human clip utterances into single-speaker and dual-candidate (slash) lists."""
    resolved_utts: list[dict] = []
    dual_utts: list[dict] = []
    for u in human_utts or []:
        dur = float(u["end_sec"]) - float(u["start_sec"])
        if dur < min_human_utt_dur_sec:
            continue
        label = u.get("speaker_label", "")
        pid, ev = resolve_label_to_pid(label, people_index)
        if pid:
            resolved_utts.append(
                {
                    "start_sec": float(u["start_sec"]),
                    "end_sec": float(u["end_sec"]),
                    "p_id": pid,
                    "label": label,
                    "kind": "single",
                }
            )
            continue
        if (
            ev.get("reason") == "composite_ambiguous"
            and isinstance(ev.get("candidate_pids"), list)
            and len(ev["candidate_pids"]) == 2
        ):
            a, b = ev["candidate_pids"][0], ev["candidate_pids"][1]
            dual_utts.append(
                {
                    "start_sec": float(u["start_sec"]),
                    "end_sec": float(u["end_sec"]),
                    "p_a": a,
                    "p_b": b,
                    "label": label,
                    "kind": "dual",
                }
            )
    return resolved_utts, dual_utts


def global_pids_from_classified(resolved_utts: list[dict], dual_utts: list[dict]) -> set[str]:
    """All registry slugs implied by the human clip (singles + both ends of slash pairs)."""
    out: set[str] = set()
    for u in resolved_utts:
        if _is_pid(u.get("p_id")):
            out.add(u["p_id"])
    for u in dual_utts:
        if _is_pid(u.get("p_a")):
            out.add(u["p_a"])
        if _is_pid(u.get("p_b")):
            out.add(u["p_b"])
    return out


def assign_segments_from_human_overlaps(
    segments: list[dict],
    resolved_utts: list[dict],
    dual_utts: list[dict],
    human_utts_total: int,
    pid_terms: dict[str, list[str]],
    *,
    tolerance_sec: float,
    min_overlap_sec: float,
    min_best_minus_second_sec: float,
    min_best_to_second_ratio: float,
    min_seg_dur_sec: float,
) -> tuple[int, dict[str, Any]]:
    """
    Returns (segments_updated_count, stats dict with overlap diagnostics).
    """
    stats = {
        "human_utts_total": human_utts_total,
        "human_utts_resolved_single": len(resolved_utts),
        "human_utts_dual_pair": len(dual_utts),
        "segments_considered": 0,
        "segments_skipped_short": 0,
        "segments_skipped_already_speaker": 0,
        "segments_assigned": 0,
        "segments_no_clear_winner": 0,
    }

    updated = 0
    for seg in segments or []:
        if seg.get("start_sec") is None or seg.get("end_sec") is None:
            continue
        if _is_pid(seg.get("speaker")):
            stats["segments_skipped_already_speaker"] += 1
            continue
        s0, s1 = float(seg["start_sec"]), float(seg["end_sec"])
        if s1 <= s0:
            continue
        seg_dur = s1 - s0
        if seg_dur < min_seg_dur_sec:
            stats["segments_skipped_short"] += 1
            continue
        stats["segments_considered"] += 1

        w0 = s0 - tolerance_sec
        w1 = s1 + tolerance_sec
        by_pid: dict[str, float] = defaultdict(float)
        seg_text_lc = norm((seg.get("text") or ""))

        for u in resolved_utts:
            ov = overlap(w0, w1, u["start_sec"], u["end_sec"])
            if ov > 0:
                by_pid[u["p_id"]] += ov

        for u in dual_utts:
            ov = overlap(w0, w1, u["start_sec"], u["end_sec"])
            if ov <= 0:
                continue
            pa, pb = u["p_a"], u["p_b"]
            sa = score_pid_in_text(pa, seg_text_lc, pid_terms)
            sb = score_pid_in_text(pb, seg_text_lc, pid_terms)
            tot = sa + sb
            if tot <= 0:
                by_pid[pa] += ov * 0.5
                by_pid[pb] += ov * 0.5
            else:
                by_pid[pa] += ov * (sa / tot)
                by_pid[pb] += ov * (sb / tot)

        if not by_pid:
            stats["segments_no_clear_winner"] += 1
            continue

        ranked = sorted(by_pid.items(), key=lambda kv: (-kv[1], kv[0]))
        best_pid, best_sec = ranked[0]
        second_sec = ranked[1][1] if len(ranked) > 1 else 0.0

        if best_sec < min_overlap_sec:
            stats["segments_no_clear_winner"] += 1
            continue
        if second_sec > 0:
            # Perfect tie (common when slash labels split overlap 50/50 with no name-in-text signal).
            if abs(best_sec - second_sec) < 1e-9:
                pred_pid = sorted(by_pid.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
            else:
                if (best_sec - second_sec) < min_best_minus_second_sec:
                    stats["segments_no_clear_winner"] += 1
                    continue
                if (best_sec / second_sec) < min_best_to_second_ratio:
                    stats["segments_no_clear_winner"] += 1
                    continue
                pred_pid = best_pid
        else:
            pred_pid = best_pid

        seg["speaker"] = pred_pid
        seg["speaker_raw"] = None
        updated += 1
        stats["segments_assigned"] += 1

    return updated, stats


def merge_global_pids_into_speakers_rollup(
    segment_rollup: list[dict[str, Any]], global_pids: set[str]
) -> list[dict[str, Any]]:
    """Ensure every human-clip `p_id` has a synthetic rollup row (stub if no segment hits)."""
    by_pid: dict[str, dict[str, Any]] = {}
    for r in segment_rollup or []:
        pid = r.get("p_id")
        if _is_pid(pid):
            by_pid[pid] = r
    for pid in sorted(global_pids):
        if not _is_pid(pid) or pid in by_pid:
            continue
        by_pid[pid] = {
            "speaker_id": f"human_clip:{pid}",
            "p_id": pid,
            "label_raw": None,
            "is_stub": True,
            "segment_count": 0,
            "total_duration_sec": 0.0,
            "first_seen_sec": 0.0,
        }
    return [by_pid[pid] for pid in sorted(by_pid.keys())]


def build_synthetic_speakers_rollup(segments: list[dict]) -> list[dict[str, Any]]:
    """One rollup row per distinct p_id present on segments."""
    by_pid: dict[str, dict[str, Any]] = {}
    for seg in segments or []:
        pid = seg.get("speaker")
        if not _is_pid(pid):
            continue
        s0, s1 = seg.get("start_sec"), seg.get("end_sec")
        if s0 is None or s1 is None:
            continue
        dur = max(0.0, float(s1) - float(s0))
        if pid not in by_pid:
            by_pid[pid] = {
                "speaker_id": f"human_clip:{pid}",
                "p_id": pid,
                "label_raw": None,
                "is_stub": True,
                "segment_count": 0,
                "total_duration_sec": 0.0,
                "first_seen_sec": float(s0),
            }
        row = by_pid[pid]
        row["segment_count"] += 1
        row["total_duration_sec"] += dur
        row["first_seen_sec"] = min(row["first_seen_sec"], float(s0))
    out = []
    for pid in sorted(by_pid.keys()):
        r = by_pid[pid]
        r["total_duration_sec"] = round(float(r["total_duration_sec"]), 3)
        r["first_seen_sec"] = round(float(r["first_seen_sec"]), 3)
        out.append(r)
    return out


def load_asset_ids_file(path: Path) -> set[str]:
    out: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        out.add(line)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--fps", type=float, default=30.0)
    ap.add_argument("--tolerance-sec", type=float, default=1.25)
    ap.add_argument("--min-human-utt-dur-sec", type=float, default=0.4)
    ap.add_argument("--min-overlap-sec", type=float, default=0.22)
    ap.add_argument("--min-best-minus-second-sec", type=float, default=0.12)
    ap.add_argument("--min-best-to-second-ratio", type=float, default=1.25)
    ap.add_argument("--min-seg-dur-sec", type=float, default=0.12)
    ap.add_argument("--only-asset-ids-file", type=Path, default=None)
    ap.add_argument("--limit-assets", type=int, default=0)
    args = ap.parse_args()

    run_id = now_run_id()
    audit_path = AUDIT_DIR / f"{run_id}.jsonl"

    restrict: set[str] | None = None
    if args.only_asset_ids_file:
        restrict = load_asset_ids_file(Path(args.only_asset_ids_file))

    by_asset: dict[str, list[dict]] = defaultdict(list)
    if not CLIP_MANIFEST.exists():
        print(f"Missing {CLIP_MANIFEST}", file=sys.stderr)
        return 1
    with CLIP_MANIFEST.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                m = json.loads(line)
            except json.JSONDecodeError:
                continue
            aid = m.get("asset_id")
            if aid:
                by_asset[aid].append(m)

    people_index = build_people_index()
    pid_terms = build_pid_terms(ROOT)
    n_examined = n_eligible = n_updated = n_skipped = 0

    asset_ids = sorted(by_asset.keys())
    if restrict is not None:
        asset_ids = [a for a in asset_ids if a in restrict]

    for aid in asset_ids:
        if args.limit_assets and n_examined >= args.limit_assets:
            break
        n_examined += 1
        rows = by_asset[aid]
        chosen, pick_reason = pick_manifest_row(rows, fps=float(args.fps), people_index=people_index)
        roster_id = chosen.get("roster_id")
        seg_rel = chosen.get("segment_path")
        if not roster_id or not seg_rel:
            append_jsonl(audit_path, {"run_id": run_id, "asset_id": aid, "kind": "skip", "reason": "bad_manifest"})
            n_skipped += 1
            continue

        transcript_path = TRANSCRIPTS_DIR / f"{aid}.transcript.json"
        clip_path = HUMAN_DIR / seg_rel
        if not transcript_path.exists() or not clip_path.exists():
            append_jsonl(
                audit_path,
                {
                    "run_id": run_id,
                    "asset_id": aid,
                    "kind": "missing_inputs",
                    "transcript": transcript_path.exists(),
                    "clip": clip_path.exists(),
                },
            )
            n_skipped += 1
            continue

        transcript = json.loads(transcript_path.read_text(encoding="utf-8"))
        if not is_no_diar_transcript(transcript):
            append_jsonl(audit_path, {"run_id": run_id, "asset_id": aid, "kind": "skip", "reason": "not_no_diar"})
            n_skipped += 1
            continue

        n_eligible += 1
        clip = json.loads(clip_path.read_text(encoding="utf-8"))
        human_utts = parse_human_clip_utterances(clip.get("human_clip_text", ""), fps=float(args.fps))

        resolved_utts, dual_utts = classify_human_utts(
            human_utts,
            people_index,
            min_human_utt_dur_sec=float(args.min_human_utt_dur_sec),
        )
        global_pids = global_pids_from_classified(resolved_utts, dual_utts)

        n_assigned, ostats = assign_segments_from_human_overlaps(
            transcript.get("segments") or [],
            resolved_utts,
            dual_utts,
            len(human_utts or []),
            pid_terms,
            tolerance_sec=float(args.tolerance_sec),
            min_overlap_sec=float(args.min_overlap_sec),
            min_best_minus_second_sec=float(args.min_best_minus_second_sec),
            min_best_to_second_ratio=float(args.min_best_to_second_ratio),
            min_seg_dur_sec=float(args.min_seg_dur_sec),
        )

        segment_pids = {
            s.get("speaker") for s in transcript.get("segments") or [] if _is_pid(s.get("speaker"))
        }
        existing_people = set(transcript.get("people_ids") or [])
        desired_people = sorted(existing_people | global_pids | segment_pids)

        base_rollup = build_synthetic_speakers_rollup(transcript.get("segments") or [])
        desired_speakers = merge_global_pids_into_speakers_rollup(base_rollup, global_pids)

        def _rollup_pid_set(sp: Any) -> set[str]:
            return {
                r.get("p_id")
                for r in (sp or [])
                if isinstance(r, dict) and _is_pid(r.get("p_id"))
            }

        transcript_changed = (
            n_assigned > 0
            or set(desired_people) != existing_people
            or _rollup_pid_set(desired_speakers) != _rollup_pid_set(transcript.get("speakers"))
        )

        if not transcript_changed:
            append_jsonl(
                audit_path,
                {
                    "run_id": run_id,
                    "asset_id": aid,
                    "kind": "no_assignments",
                    "roster_id": roster_id,
                    "pick_reason": pick_reason,
                    "manifest_row_count": len(rows),
                    "overlap_stats": ostats,
                    "global_pids_from_human_clip": sorted(global_pids),
                },
            )
            n_skipped += 1
            continue

        # Normalize empty speakers_raw list → dict
        if isinstance(transcript.get("speakers_raw"), list):
            transcript["speakers_raw"] = {}

        prior_audit = get_speaker_resolution_audit(transcript, asset_id=aid)
        audit_body: dict = {
            "schema_version": 2,
            "run_id": run_id,
            "resolved_at": now_iso(),
            "method": "human_clip_segment_overlap_no_diar",
            "source": {
                "roster_id": roster_id,
                "clip_segment_path": seg_rel,
                "clip_pick_reason": pick_reason,
                "manifest_row_count_for_asset": len(rows),
            },
            "params": {
                "fps_assumed": float(args.fps),
                "tolerance_sec": float(args.tolerance_sec),
                "min_human_utt_dur_sec": float(args.min_human_utt_dur_sec),
                "min_overlap_sec": float(args.min_overlap_sec),
                "min_best_minus_second_sec": float(args.min_best_minus_second_sec),
                "min_best_to_second_ratio": float(args.min_best_to_second_ratio),
                "min_seg_dur_sec": float(args.min_seg_dur_sec),
            },
            "result": {
                "overlap_stats": ostats,
                "segments_assigned": n_assigned,
                "global_pids_from_human_clip": sorted(global_pids),
                "global_people_tags_only": bool(n_assigned == 0 and global_pids),
            },
        }
        if prior_audit:
            audit_body["prior_speaker_resolution_audit"] = prior_audit
        set_resolution_audit_on_transcript(transcript, aid, audit_body, use_sidecar=True)

        transcript["diarized"] = False
        transcript["people_ids"] = desired_people
        transcript["speakers"] = desired_speakers

        rec = {
            "run_id": run_id,
            "asset_id": aid,
            "kind": (
                "applied_global_tags"
                if (n_assigned == 0 and global_pids)
                else "applied"
            ),
            "roster_id": roster_id,
            "clip_segment_path": seg_rel,
            "pick_reason": pick_reason,
            "segments_assigned": n_assigned,
            "global_pids_from_human_clip": sorted(global_pids),
            "overlap_stats": ostats,
        }
        append_jsonl(audit_path, rec)

        if not args.dry_run:
            atomic_write_json(transcript_path, transcript)
        n_updated += 1

    append_jsonl(
        audit_path,
        {
            "run_id": run_id,
            "kind": "summary",
            "at": now_iso(),
            "examined_assets": n_examined,
            "eligible_no_diar": n_eligible,
            "updated": n_updated,
            "skipped": n_skipped,
            "dry_run": bool(args.dry_run),
        },
    )

    print(
        json.dumps(
            {
                "run_id": run_id,
                "examined_assets": n_examined,
                "eligible_no_diar": n_eligible,
                "updated": n_updated,
                "skipped": n_skipped,
                "dry_run": bool(args.dry_run),
                "audit_log": str(audit_path.relative_to(ROOT)),
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
