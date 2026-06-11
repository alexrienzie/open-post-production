#!/usr/bin/env python3
"""
Add per-segment speaker confidence fields to transcript records.

Why:
Natural-language video editors should gate "speaker-based" actions (clip selection,
auto-labeling, speaker-scoped search) on confidence, and fall back to text-only when
speaker is uncertain.

This script adds a lightweight, local confidence heuristic WITHOUT redoing the full
human↔machine alignment.

Writes:
- Updates assets/transcripts/*.transcript.json in place (atomic)
- Append-only audit log under _audit/speaker_confidence_<timestamp>.jsonl

Segment fields added (when speaker is present):
  segments[].speaker_confidence = { "score": float, "level": "high|medium|low", "reason": str }

Transcript-level provenance:
  speaker_confidence_audit lives in `_audit/transcript_provenance/<asset_id>.json` with
  `speaker_resolution_audit` (pointer `speaker_provenance` on the transcript).
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
TRANSCRIPTS_DIR = ROOT / "assets" / "catalog" / "transcripts"

from transcript_provenance import (  # noqa: E402
    asset_id_from_transcript_path,
    attach_pointer,
    get_speaker_confidence_audit,
    get_speaker_resolution_audit,
    strip_inline_audits,
    write_sidecar_merged,
)
AUDIT_DIR = ROOT / "_audit"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def atomic_write_json(path: Path, data: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def append_jsonl(path: Path, rec: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def clamp01(x: float) -> float:
    return 0.0 if x < 0 else (1.0 if x > 1 else x)


def score_segment(
    *,
    seg_dur: float,
    speaker_total_dur: float | None,
    is_mapped_guid: bool,
    min_seg_dur_high: float,
    min_guid_total_high: float,
    min_seg_dur_medium: float,
    min_guid_total_medium: float,
) -> tuple[float, str, str]:
    """
    Return (score, level, reason)
    """
    # Default: anything not mapped is low confidence
    if not is_mapped_guid:
        base = 0.15
        # longer segments still have some utility, but we avoid over-trusting
        dur_boost = min(0.15, max(0.0, seg_dur - 2.0) / 20.0)
        return clamp01(base + dur_boost), "low", "guid_not_mapped"

    # Mapped guid: use duration-based evidence
    spk_total = float(speaker_total_dur or 0.0)

    # Compute a smooth score
    seg_component = clamp01((seg_dur - 0.8) / 6.0)  # ~0 at <0.8s, ~1 at ~6.8s
    spk_component = clamp01((spk_total - 10.0) / 120.0)  # ~0 at <10s total, ~1 at ~130s+
    score = clamp01(0.35 + 0.35 * seg_component + 0.30 * spk_component)

    # Level thresholds (hard gates for UX)
    if seg_dur >= min_seg_dur_high and spk_total >= min_guid_total_high:
        return max(score, 0.85), "high", "mapped_guid_strong_evidence"
    if seg_dur >= min_seg_dur_medium and spk_total >= min_guid_total_medium:
        return max(score, 0.60), "medium", "mapped_guid_moderate_evidence"
    return min(score, 0.59), "low", "mapped_guid_weak_evidence"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--min-seg-dur-high", type=float, default=4.0)
    ap.add_argument("--min-guid-total-high", type=float, default=60.0)
    ap.add_argument("--min-seg-dur-medium", type=float, default=2.0)
    ap.add_argument("--min-guid-total-medium", type=float, default=20.0)
    args = ap.parse_args()

    run_id = datetime.now(timezone.utc).strftime("speaker_confidence_%Y%m%dT%H%M%SZ")
    audit_path = AUDIT_DIR / f"{run_id}.jsonl"

    paths = sorted(TRANSCRIPTS_DIR.glob("*.transcript.json"))
    if args.limit:
        paths = paths[: int(args.limit)]

    examined = updated_files = 0
    totals = {
        "segments_examined": 0,
        "segments_with_speaker": 0,
        "segments_high": 0,
        "segments_medium": 0,
        "segments_low": 0,
    }

    for p in paths:
        examined += 1
        try:
            rec = json.loads(p.read_text(encoding="utf-8"))
        except Exception as e:
            append_jsonl(audit_path, {"run_id": run_id, "at": now_iso(), "kind": "read_error", "path": str(p), "error": str(e)})
            continue

        aid = asset_id_from_transcript_path(p)
        mapping: dict[str, str] = {}
        audit = get_speaker_resolution_audit(rec, asset_id=aid) or {}
        if isinstance(audit, dict):
            mapping = ((audit.get("result") or {}).get("mapping") or {}) if isinstance(audit.get("result"), dict) else {}
        mapped_guids = set(mapping.keys())

        # Speaker rollup duration by guid
        guid_total_dur: dict[str, float] = {}
        for sp in rec.get("speakers") or []:
            sid = sp.get("speaker_id")
            if not sid:
                continue
            td = sp.get("total_duration_sec")
            try:
                guid_total_dur[sid] = float(td) if td is not None else 0.0
            except Exception:
                guid_total_dur[sid] = 0.0

        changed = False
        per_file = {"segments_high": 0, "segments_medium": 0, "segments_low": 0, "segments_with_speaker": 0}

        for seg in rec.get("segments") or []:
            totals["segments_examined"] += 1
            speaker = seg.get("speaker")
            if not speaker:
                continue

            totals["segments_with_speaker"] += 1
            per_file["segments_with_speaker"] += 1

            s0 = seg.get("start_sec")
            s1 = seg.get("end_sec")
            try:
                seg_dur = max(0.0, float(s1) - float(s0))
            except Exception:
                seg_dur = 0.0

            guid = seg.get("speaker_raw")
            is_mapped = bool(guid) and guid in mapped_guids
            spk_total = guid_total_dur.get(guid) if guid else None

            score, level, reason = score_segment(
                seg_dur=seg_dur,
                speaker_total_dur=spk_total,
                is_mapped_guid=is_mapped,
                min_seg_dur_high=float(args.min_seg_dur_high),
                min_guid_total_high=float(args.min_guid_total_high),
                min_seg_dur_medium=float(args.min_seg_dur_medium),
                min_guid_total_medium=float(args.min_guid_total_medium),
            )

            prev = seg.get("speaker_confidence")
            new = {"score": round(float(score), 3), "level": level, "reason": reason}
            if prev != new:
                seg["speaker_confidence"] = new
                changed = True

            if level == "high":
                totals["segments_high"] += 1
                per_file["segments_high"] += 1
            elif level == "medium":
                totals["segments_medium"] += 1
                per_file["segments_medium"] += 1
            else:
                totals["segments_low"] += 1
                per_file["segments_low"] += 1

        # Transcript-level audit/provenance
        audit_block: dict[str, Any] = {
            "schema_version": 1,
            "run_id": run_id,
            "computed_at": now_iso(),
            "params": {
                "min_seg_dur_high": args.min_seg_dur_high,
                "min_guid_total_high": args.min_guid_total_high,
                "min_seg_dur_medium": args.min_seg_dur_medium,
                "min_guid_total_medium": args.min_guid_total_medium,
            },
            "totals": per_file,
            "uses_mapping_from_speaker_resolution_audit": bool(bool(mapping)),
        }
        prev_conf = get_speaker_confidence_audit(rec, asset_id=aid)
        if prev_conf != audit_block:
            changed = True

        if changed:
            updated_files += 1
            append_jsonl(audit_path, {
                "run_id": run_id,
                "at": now_iso(),
                "kind": "updated" if not args.dry_run else "would_update",
                "asset_id": rec.get("asset_id"),
                "path": str(p.relative_to(ROOT)),
                "totals": per_file,
            })
            if not args.dry_run:
                write_sidecar_merged(aid, speaker_confidence_audit=audit_block)
                strip_inline_audits(rec)
                attach_pointer(rec, aid)
                atomic_write_json(p, rec)

    append_jsonl(audit_path, {
        "run_id": run_id,
        "at": now_iso(),
        "kind": "summary",
        "examined_files": examined,
        "updated_files": updated_files,
        "dry_run": bool(args.dry_run),
        "totals": totals,
    })

    print(json.dumps({
        "run_id": run_id,
        "examined_files": examined,
        "updated_files": updated_files,
        "dry_run": bool(args.dry_run),
        "audit_log": str(audit_path.relative_to(ROOT)),
        "totals": totals,
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

