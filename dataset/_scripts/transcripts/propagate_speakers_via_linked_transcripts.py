#!/usr/bin/env python3
"""
Copy segment-level p_* speakers from a donor transcript to linked sibling assets when
time windows overlap and machine text matches strongly (same recording / timeline).

For each connected component of catalog assets reachable from human clip manifest
seeds (via `linked_assets` edges and legacy flat link fields until migrated),
picks the best donor: most segments with p_* (tie-break: asset is a manifest id).

Before matching, estimates a **time offset** (seconds) on a coarse grid so donor and
receiver timelines can align; stores `linked_alignment` on the **provenance sidecar**
only (no extra bulk on `*.transcript.json` beyond the existing pointer).

Propagates to every other asset in the component that has a transcript file, without
overwriting existing p_* on segments.

Updates people_ids union and rebuilds speakers[] rollup (synthetic) on receivers
when any segments change.

Auditable via speaker_resolution_audit sidecar (method: linked_transcript_speaker_propagation).

Usage:
  python _scripts/transcripts/propagate_speakers_via_linked_transcripts.py --dry-run
  python _scripts/transcripts/propagate_speakers_via_linked_transcripts.py
"""
from __future__ import annotations

import argparse
import bisect
import difflib
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
TRANSCRIPTS_DIR = ROOT / "assets" / "catalog" / "transcripts"
AUDIT_DIR = ROOT / "_audit"

sys.path.insert(0, str(ROOT / "_scripts"))
from backfill_no_diar_speakers_from_human_clips import (  # noqa: E402
    build_synthetic_speakers_rollup,
    merge_global_pids_into_speakers_rollup,
)
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))  # shared modules live at _scripts root
from human_link_components import (  # noqa: E402
    discover_components,
    load_manifest_asset_ids,
)
from resolve_speakers_from_human_transcripts import norm, overlap  # noqa: E402
from transcript_provenance import (  # noqa: E402
    atomic_write_json,
    get_speaker_resolution_audit,
    merge_sidecar_fields,
    set_resolution_audit_on_transcript,
)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def now_run_id() -> str:
    return datetime.now(timezone.utc).strftime("speaker_propagate_linked_%Y%m%dT%H%M%SZ")


def _is_pid(s: Any) -> bool:
    return isinstance(s, str) and s.startswith("p_") and len(s) > 2


def append_jsonl(path: Path, rec: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def text_similarity(a: str, b: str) -> float:
    na, nb = norm(a), norm(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    return float(difflib.SequenceMatcher(None, na, nb).ratio())


def build_donor_index(segments: list[dict]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for s in segments or []:
        if not _is_pid(s.get("speaker")):
            continue
        s0, s1 = s.get("start_sec"), s.get("end_sec")
        if s0 is None or s1 is None:
            continue
        try:
            f0, f1 = float(s0), float(s1)
        except (TypeError, ValueError):
            continue
        if f1 <= f0:
            continue
        t = norm(s.get("text") or "")
        if len(t) < 2:
            continue
        rows.append(
            {
                "start_sec": f0,
                "end_sec": f1,
                "text_norm": t,
                "text_raw": s.get("text") or "",
                "speaker": s.get("speaker"),
            }
        )
    return rows


def _prep_donor_sorted(donor_rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[float]]:
    dr = sorted(donor_rows, key=lambda d: float(d["start_sec"]))
    starts = [float(d["start_sec"]) for d in dr]
    return dr, starts


def _donors_overlapping_window(
    dr: list[dict[str, Any]],
    starts: list[float],
    w0: float,
    w1: float,
    *,
    time_shift: float,
    prune_sec: float,
):
    """Donor intervals [d0,d1] + time_shift overlap [w0, w1]. `starts` sorted."""
    lo = bisect.bisect_left(starts, w0 - prune_sec - time_shift)
    for k in range(lo, len(dr)):
        d = dr[k]
        d0 = float(d["start_sec"]) + time_shift
        if d0 >= w1:
            break
        d1 = float(d["end_sec"]) + time_shift
        if d1 <= w0:
            continue
        yield d


def shift_donor_rows(rows: list[dict[str, Any]], offset_sec: float) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for r in rows:
        out.append(
            {
                **r,
                "start_sec": float(r["start_sec"]) + offset_sec,
                "end_sec": float(r["end_sec"]) + offset_sec,
            }
        )
    return out


def estimate_time_offset_sec(
    donor_rows: list[dict[str, Any]],
    recv_segments: list[dict],
    *,
    tolerance_sec: float,
    min_overlap_sec: float,
    min_recv_chars: int,
    grid_step_sec: float,
    grid_half_width_sec: float,
    grid_min_text_sim: float,
    max_recv_windows: int,
    donor_prune_sec: float,
) -> tuple[float, float]:
    """
    Maximize sum over receiver segments of (best_sim * overlap) where sim >= grid_min_text_sim.
    Returns (best_offset_sec, best_aggregate_score).
    """
    recv_windows: list[tuple[float, float, str]] = []
    for seg in recv_segments or []:
        s0, s1 = seg.get("start_sec"), seg.get("end_sec")
        if s0 is None or s1 is None:
            continue
        try:
            f0, f1 = float(s0), float(s1)
        except (TypeError, ValueError):
            continue
        if f1 <= f0:
            continue
        rtxt = seg.get("text") or ""
        if len(norm(rtxt)) < min_recv_chars:
            continue
        recv_windows.append((f0, f1, rtxt))

    if not recv_windows or not donor_rows:
        return 0.0, 0.0

    if len(recv_windows) > max_recv_windows:
        n = max_recv_windows
        recv_windows = [recv_windows[int(i * (len(recv_windows) - 1) / max(1, n - 1))] for i in range(n)]

    dr, starts = _prep_donor_sorted(donor_rows)

    best_off = 0.0
    best_score = -1.0
    n_steps = max(0, int((2 * grid_half_width_sec) / max(grid_step_sec, 1e-6)))
    for i in range(n_steps + 1):
        off = -grid_half_width_sec + i * grid_step_sec
        total = 0.0
        for f0, f1, rtxt in recv_windows:
            w0, w1 = f0 - tolerance_sec, f1 + tolerance_sec
            best = 0.0
            for d in _donors_overlapping_window(dr, starts, w0, w1, time_shift=off, prune_sec=donor_prune_sec):
                d0s = float(d["start_sec"]) + off
                d1s = float(d["end_sec"]) + off
                ov = overlap(w0, w1, d0s, d1s)
                if ov < min_overlap_sec:
                    continue
                sim = text_similarity(rtxt, d["text_raw"])
                if sim < grid_min_text_sim:
                    continue
                best = max(best, sim * ov)
            total += best
        if total > best_score:
            best_score = total
            best_off = off

    if best_score <= 0:
        return 0.0, 0.0
    return best_off, best_score


def propagate_receiver(
    donor_rows: list[dict[str, Any]],
    transcript: dict[str, Any],
    *,
    tolerance_sec: float,
    min_overlap_sec: float,
    min_text_sim: float,
    min_text_sim_margin: float,
    min_recv_seg_chars: int,
    donor_prune_sec: float,
) -> tuple[int, dict[str, int]]:
    stats = defaultdict(int)
    segs = transcript.get("segments") or []
    dr, starts = _prep_donor_sorted(donor_rows)
    updated = 0
    for seg in segs:
        if _is_pid(seg.get("speaker")):
            stats["skipped_already_pid"] += 1
            continue
        s0, s1 = seg.get("start_sec"), seg.get("end_sec")
        if s0 is None or s1 is None:
            stats["skipped_bad_time"] += 1
            continue
        try:
            f0, f1 = float(s0), float(s1)
        except (TypeError, ValueError):
            stats["skipped_bad_time"] += 1
            continue
        if f1 <= f0:
            stats["skipped_bad_time"] += 1
            continue
        rtxt = seg.get("text") or ""
        if len(norm(rtxt)) < min_recv_seg_chars:
            stats["skipped_short_text"] += 1
            continue

        w0, w1 = f0 - tolerance_sec, f1 + tolerance_sec
        scored: list[tuple[float, float, str]] = []
        for d in _donors_overlapping_window(dr, starts, w0, w1, time_shift=0.0, prune_sec=donor_prune_sec):
            ov = overlap(w0, w1, float(d["start_sec"]), float(d["end_sec"]))
            if ov < min_overlap_sec:
                continue
            sim = text_similarity(rtxt, d["text_raw"])
            scored.append((sim, ov, d["speaker"]))

        if not scored:
            stats["no_candidate"] += 1
            continue
        scored.sort(key=lambda x: (-x[0], -x[1], x[2]))
        best_sim, best_ov, best_pid = scored[0]
        second_sim = scored[1][0] if len(scored) > 1 else 0.0
        if best_sim < min_text_sim:
            stats["low_text_sim"] += 1
            continue
        if (best_sim - second_sim) < min_text_sim_margin:
            stats["ambiguous_text"] += 1
            continue

        seg["speaker"] = best_pid
        updated += 1
        stats["assigned"] += 1

    return updated, dict(stats)


def pick_donor(comp: set[str], manifest_ids: set[str]) -> str | None:
    best: tuple[int, int, int, str] | None = None
    for aid in comp:
        p = TRANSCRIPTS_DIR / f"{aid}.transcript.json"
        if not p.exists():
            continue
        try:
            t = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        segs = t.get("segments") or []
        pid_n = sum(1 for s in segs if _is_pid(s.get("speaker")))
        manifest_bonus = 1 if aid in manifest_ids else 0
        key = (pid_n, manifest_bonus, len(segs), aid)
        if best is None or key > best:
            best = key
    if best is None or best[0] <= 0:
        return None
    return best[3]


def refresh_people_and_speakers(transcript: dict[str, Any]) -> None:
    segs = transcript.get("segments") or []
    segment_pids = {s.get("speaker") for s in segs if _is_pid(s.get("speaker"))}
    existing = set(transcript.get("people_ids") or [])
    transcript["people_ids"] = sorted(existing | segment_pids)
    base = build_synthetic_speakers_rollup(segs)
    transcript["speakers"] = merge_global_pids_into_speakers_rollup(base, set())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--tolerance-sec", type=float, default=1.0)
    ap.add_argument("--min-overlap-sec", type=float, default=0.25)
    ap.add_argument("--min-text-sim", type=float, default=0.82)
    ap.add_argument("--min-text-sim-margin", type=float, default=0.04)
    ap.add_argument("--min-recv-seg-chars", type=int, default=4)
    ap.add_argument("--offset-grid-step-sec", type=float, default=2.0)
    ap.add_argument("--offset-grid-half-width-sec", type=float, default=24.0)
    ap.add_argument("--offset-grid-min-text-sim", type=float, default=0.72)
    ap.add_argument(
        "--max-recv-windows-offset",
        type=int,
        default=72,
        help="Subsample receiver segments for offset grid search only (full propagation still uses all segments).",
    )
    ap.add_argument(
        "--donor-prune-sec",
        type=float,
        default=180.0,
        help="When scanning sorted donors, ignore any donor starting this far before the window.",
    )
    ap.add_argument("--limit-components", type=int, default=0)
    args = ap.parse_args()

    manifest_ids = load_manifest_asset_ids()
    if not manifest_ids:
        print("No manifest asset ids; missing clip_segments_manifest.jsonl?", file=sys.stderr)
        return 1

    run_id = now_run_id()
    audit_path = AUDIT_DIR / f"{run_id}.jsonl"
    components = discover_components(manifest_ids)
    if args.limit_components:
        components = components[: int(args.limit_components)]

    n_comp = n_skipped_no_donor = n_receivers = n_seg_updates = 0
    for comp in components:
        n_comp += 1
        donor_id = pick_donor(comp, manifest_ids)
        if not donor_id:
            append_jsonl(
                audit_path,
                {"run_id": run_id, "kind": "skip_component", "reason": "no_donor_with_pid_segments", "size": len(comp)},
            )
            n_skipped_no_donor += 1
            continue

        donor_path = TRANSCRIPTS_DIR / f"{donor_id}.transcript.json"
        donor_t = json.loads(donor_path.read_text(encoding="utf-8"))
        donor_rows = build_donor_index(donor_t.get("segments") or [])
        if not donor_rows:
            append_jsonl(
                audit_path,
                {"run_id": run_id, "kind": "skip_component", "reason": "donor_index_empty", "donor_asset_id": donor_id},
            )
            n_skipped_no_donor += 1
            continue

        seeds_in = sorted(comp & manifest_ids)
        receivers = sorted(a for a in comp if a != donor_id)

        for rid in receivers:
            rp = TRANSCRIPTS_DIR / f"{rid}.transcript.json"
            if not rp.exists():
                append_jsonl(
                    audit_path,
                    {
                        "run_id": run_id,
                        "kind": "skip_receiver",
                        "donor_asset_id": donor_id,
                        "receiver_asset_id": rid,
                        "reason": "no_transcript",
                    },
                )
                continue

            recv_t = json.loads(rp.read_text(encoding="utf-8"))
            recv_segs = recv_t.get("segments") or []
            off_sec, off_score = estimate_time_offset_sec(
                donor_rows,
                recv_segs,
                tolerance_sec=float(args.tolerance_sec),
                min_overlap_sec=float(args.min_overlap_sec),
                min_recv_chars=max(8, int(args.min_recv_seg_chars)),
                grid_step_sec=float(args.offset_grid_step_sec),
                grid_half_width_sec=float(args.offset_grid_half_width_sec),
                grid_min_text_sim=float(args.offset_grid_min_text_sim),
                max_recv_windows=int(args.max_recv_windows_offset),
                donor_prune_sec=float(args.donor_prune_sec),
            )
            shifted = shift_donor_rows(donor_rows, off_sec)

            n_before = sum(1 for s in recv_segs if _is_pid(s.get("speaker")))
            updated, pst = propagate_receiver(
                shifted,
                recv_t,
                tolerance_sec=float(args.tolerance_sec),
                min_overlap_sec=float(args.min_overlap_sec),
                min_text_sim=float(args.min_text_sim),
                min_text_sim_margin=float(args.min_text_sim_margin),
                min_recv_seg_chars=int(args.min_recv_seg_chars),
                donor_prune_sec=float(args.donor_prune_sec),
            )
            n_after = sum(1 for s in recv_t.get("segments") or [] if _is_pid(s.get("speaker")))

            alignment_block = {
                "donor_asset_id": donor_id,
                "offset_sec_applied": round(off_sec, 4),
                "offset_grid_score": round(off_score, 4),
                "estimated_at": now_iso(),
                "method": "grid_search_overlap_text",
                "params": {
                    "grid_step_sec": float(args.offset_grid_step_sec),
                    "grid_half_width_sec": float(args.offset_grid_half_width_sec),
                    "grid_min_text_sim": float(args.offset_grid_min_text_sim),
                    "max_recv_windows_offset": int(args.max_recv_windows_offset),
                    "donor_prune_sec": float(args.donor_prune_sec),
                },
            }

            rec_line = {
                "run_id": run_id,
                "kind": "receiver_result",
                "donor_asset_id": donor_id,
                "receiver_asset_id": rid,
                "manifest_seeds_in_component": seeds_in,
                "segments_assigned": updated,
                "segments_pid_before": n_before,
                "segments_pid_after": n_after,
                "offset_sec_applied": alignment_block["offset_sec_applied"],
                "diag": pst,
            }
            append_jsonl(audit_path, rec_line)

            if not args.dry_run and (
                updated > 0 or abs(off_sec) > 1e-6 or off_score > 1e-6
            ):
                merge_sidecar_fields(rid, {"linked_alignment": alignment_block})

            if updated <= 0:
                continue

            n_receivers += 1
            n_seg_updates += updated
            refresh_people_and_speakers(recv_t)

            prior = get_speaker_resolution_audit(recv_t, asset_id=rid)
            audit_body: dict[str, Any] = {
                "schema_version": 2,
                "run_id": run_id,
                "resolved_at": now_iso(),
                "method": "linked_transcript_speaker_propagation",
                "source": {
                    "donor_asset_id": donor_id,
                    "manifest_seeds_in_component": seeds_in,
                },
                "params": {
                    "tolerance_sec": float(args.tolerance_sec),
                    "min_overlap_sec": float(args.min_overlap_sec),
                    "min_text_sim": float(args.min_text_sim),
                    "min_text_sim_margin": float(args.min_text_sim_margin),
                    "min_recv_seg_chars": int(args.min_recv_seg_chars),
                    "offset_sec_applied": alignment_block["offset_sec_applied"],
                    "offset_estimation": alignment_block["params"],
                },
                "result": {
                    "segments_assigned": updated,
                    "propagation_diag": pst,
                    "segments_pid_before": n_before,
                    "segments_pid_after": n_after,
                    "linked_alignment": alignment_block,
                },
            }
            if prior:
                audit_body["prior_speaker_resolution_audit"] = prior

            if not args.dry_run:
                set_resolution_audit_on_transcript(recv_t, rid, audit_body, use_sidecar=True)
                atomic_write_json(rp, recv_t)

    append_jsonl(
        audit_path,
        {
            "run_id": run_id,
            "kind": "summary",
            "at": now_iso(),
            "components_total": n_comp,
            "components_skipped_no_donor": n_skipped_no_donor,
            "receivers_updated": n_receivers,
            "segments_assigned_total": n_seg_updates,
            "dry_run": bool(args.dry_run),
        },
    )

    print(
        json.dumps(
            {
                "run_id": run_id,
                "components": n_comp,
                "components_skipped_no_donor": n_skipped_no_donor,
                "receivers_updated": n_receivers,
                "segments_assigned_total": n_seg_updates,
                "dry_run": bool(args.dry_run),
                "audit_log": str(audit_path.relative_to(ROOT)),
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
