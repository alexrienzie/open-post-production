"""Cut-boundary checks and optional Gemini suggested-span application for sidecars.

Three evaluators:

  eval_cut_boundaries(sidecar)
    Flags annotations whose source in/out cut mid-word in inlined transcript
    segments. Returns a list of issue dicts (one per offending annotation).

  eval_visual_cut_deltas(sidecar)
    For each consecutive annotation pair on the same track (timeline order),
    computes SigLIP cosine similarity between the OUT frame of the leading clip
    and the IN frame of the trailing clip. Mutates the sidecar in place:
    attaches `siglip_delta_out` to each leading annotation (last annotation per
    track gets none). Lazy-loads the SigLIP FAISS index — heavy on first call,
    ~400 ms / pair after that.

  apply_suggested_spans(sidecar, ...)
    Mutates source in/out to match Gemini's `chunk_suggested_span` when it's
    a sub-range of the current trim. Returns (count, log).

CLI (for standalone runs / threshold calibration):

  py editor/sidecar_cut_eval.py visual-cut <sidecar.json>
  py editor/sidecar_cut_eval.py calibrate  <sidecar.json>

"""

from __future__ import annotations

import sys
from collections import defaultdict
from pathlib import Path
from typing import Any


def _frames_to_sec(frames: int | None, fps: float) -> float | None:
    if frames is None:
        return None
    return frames / fps


def _sec_to_frames(sec: float, fps: float) -> int:
    return max(0, int(round(sec * fps)))


def eval_cut_boundaries(
    sidecar: dict,
    *,
    boundary_thresh_sec: float = 0.12,
) -> list[dict[str, Any]]:
    """Flag annotations whose source in/out cut mid-word in inlined transcript segments."""
    fps = float(sidecar.get("frame_rate") or 23.976)
    issues: list[dict[str, Any]] = []
    for ann in sidecar.get("annotations") or []:
        if not isinstance(ann, dict):
            continue
        key = ann.get("key") or {}
        aid = key.get("asset_id")
        if not aid:
            continue
        src_in_f = key.get("source_in_frames")
        src_out_f = key.get("source_out_frames")
        if src_in_f is None or src_out_f is None:
            continue
        src_in = src_in_f / fps
        src_out = src_out_f / fps
        segs = ann.get("transcript_segments") or []
        if not segs:
            continue
        in_hits: list[str] = []
        out_hits: list[str] = []
        for seg in segs:
            if not isinstance(seg, dict):
                continue
            ss = seg.get("start_sec")
            se = seg.get("end_sec")
            text = (seg.get("text") or "").strip()
            if ss is None or se is None or not text:
                continue
            if ss + boundary_thresh_sec < src_in < se - boundary_thresh_sec:
                in_hits.append(text[:60])
            if ss + boundary_thresh_sec < src_out < se - boundary_thresh_sec:
                out_hits.append(text[:60])
        if not in_hits and not out_hits:
            continue
        issues.append({
            "clip_id": ann.get("clip_id"),
            "asset_id": aid,
            "track": key.get("track"),
            "source_in_sec": round(src_in, 3),
            "source_out_sec": round(src_out, 3),
            "mid_word_in": in_hits[:3],
            "mid_word_out": out_hits[:3],
        })
    return issues


_SIGLIP_INDEX_CACHE: Any = None  # lazy singleton


def _load_siglip_cut_index() -> Any:
    """Lazy-load + cache the SigLIPCutIndex. Returns None if FAISS files
    aren't available on this drive (e.g. a machine without the index
    side-loaded)."""
    global _SIGLIP_INDEX_CACHE
    if _SIGLIP_INDEX_CACHE is not None:
        return _SIGLIP_INDEX_CACHE
    editor_root = Path(__file__).resolve().parent
    repo_root = editor_root.parent
    # The primitive lives under dataset/_scripts/; workspace_paths is
    # the cross-platform indexes-dir resolver.
    sys.path.insert(0, str(repo_root / "dataset" / "_scripts" / "mac"))
    sys.path.insert(0, str(repo_root / "dataset" / "_scripts"))
    try:
        from siglip_cut_delta import SigLIPCutIndex  # noqa: E402
        from workspace_paths import indexes_dir  # noqa: E402
    except ImportError as e:
        print(f"  [eval_visual_cut_deltas] cannot import primitive: {e}", file=sys.stderr)
        return None
    idx_dir = indexes_dir()
    meta_path = idx_dir / "clip_embeddings.faiss.meta.json"
    faiss_path = idx_dir / "clip_embeddings.faiss"
    if not meta_path.exists() or not faiss_path.exists():
        print(f"  [eval_visual_cut_deltas] FAISS files missing at {idx_dir} — skipping", file=sys.stderr)
        return None
    _SIGLIP_INDEX_CACHE = SigLIPCutIndex(meta_path, faiss_path)
    return _SIGLIP_INDEX_CACHE


def eval_visual_cut_deltas(
    sidecar: dict,
    *,
    cut_index: Any = None,
    strip_debug_fields: bool = True,
) -> int:
    """For each consecutive annotation pair on the same track (timeline order),
    compute SigLIP cosine similarity between the leading clip's OUT frame and
    the trailing clip's IN frame. Attaches the result to the **leading**
    annotation as `siglip_delta_out`. The last annotation per track gets no
    `siglip_delta_out`.

    Returns count of pairs scored. Returns 0 (no-op) if SigLIP FAISS index
    isn't available on this drive.

    Sidecar mutated in place. Caller should write back to disk.

    Result shape on each annotation:
      ann["siglip_delta_out"] = {
        "ok": True,
        "cosine_similarity": 0.91,
        "cosine_distance": 0.09,
        "interpretation": "clean" | "soft" | "hard",
        "same_asset": True/False,
        "out_ts_sec": <float>, "in_ts_sec": <float>,
      }
    With strip_debug_fields=False, also includes `out` and `in` sub-dicts with
    embedding_pk / offset_sec / abs_time_sec for traceability.
    """
    if cut_index is None:
        cut_index = _load_siglip_cut_index()
    if cut_index is None:
        return 0

    fps = float(sidecar.get("frame_rate") or 24000 / 1001)
    annotations = sidecar.get("annotations") or []

    # Group by track, sort by timeline_start (i.e. timeline order, not source order)
    by_track: dict[str, list[dict]] = defaultdict(list)
    for ann in annotations:
        if not isinstance(ann, dict):
            continue
        key = ann.get("key") or {}
        track = key.get("track")
        if track is None:
            continue
        by_track[track].append(ann)
    for track in by_track:
        by_track[track].sort(
            key=lambda a: (a.get("key") or {}).get("timeline_start_frames", 0)
        )

    # Clear any stale siglip_delta_out from prior runs (so removed cuts disappear)
    for ann in annotations:
        if isinstance(ann, dict) and "siglip_delta_out" in ann:
            del ann["siglip_delta_out"]

    n_pairs = 0
    n_failed = 0
    for track, anns in by_track.items():
        for i in range(len(anns) - 1):
            ann_out = anns[i]
            ann_in = anns[i + 1]
            ko = ann_out.get("key") or {}
            ki = ann_in.get("key") or {}
            aid_out = ko.get("asset_id")
            aid_in = ki.get("asset_id")
            sof = ko.get("source_out_frames")
            sif = ki.get("source_in_frames")
            if not aid_out or not aid_in or sof is None or sif is None:
                continue
            # Outgoing clip's last visible frame is the frame BEFORE source_out.
            # source_out is exclusive (xmeml convention); the last shown frame is
            # source_out - 1 (in frames) ≈ (sof - 1) / fps.
            ts_out = max(0.0, (int(sof) - 1) / fps)
            ts_in = int(sif) / fps
            result = cut_index.compute_cut_delta(aid_out, ts_out, aid_in, ts_in)
            if not result.get("ok"):
                n_failed += 1
                ann_out["siglip_delta_out"] = {
                    "ok": False,
                    "reason": result.get("reason"),
                    "out_ts_sec": round(ts_out, 3),
                    "in_ts_sec": round(ts_in, 3),
                }
                n_pairs += 1
                continue
            attached: dict[str, Any] = {
                "ok": True,
                "cosine_similarity": round(result["cosine_similarity"], 4),
                "cosine_distance": round(result["cosine_distance"], 4),
                "interpretation": result["interpretation"],
                "same_asset": result["same_asset"],
                "out_ts_sec": round(ts_out, 3),
                "in_ts_sec": round(ts_in, 3),
            }
            if not strip_debug_fields:
                attached["out"] = result["out"]
                attached["in"] = result["in"]
            ann_out["siglip_delta_out"] = attached
            n_pairs += 1
    if n_failed:
        print(f"  [eval_visual_cut_deltas] {n_pairs} pairs scored "
              f"({n_failed} failed — no SigLIP coverage on one or both sides)",
              file=sys.stderr)
    return n_pairs


def visual_cut_distribution(sidecar: dict) -> dict[str, Any]:
    """Walk an already-evaluated sidecar's `siglip_delta_out` entries and
    summarize the cosine distribution. Returns a dict with counts + percentiles
    + a suggested flagging threshold (the 10th percentile of cosine — i.e. the
    lowest 10% of cuts by visual similarity are the most jarring).

    Useful as a calibration / status check after eval_visual_cut_deltas().
    """
    cos_values: list[float] = []
    n_scored = 0
    n_failed = 0
    by_interp: dict[str, int] = {"clean": 0, "soft": 0, "hard": 0}
    same_asset = 0
    for ann in sidecar.get("annotations") or []:
        if not isinstance(ann, dict):
            continue
        sd = ann.get("siglip_delta_out")
        if not isinstance(sd, dict):
            continue
        if not sd.get("ok"):
            n_failed += 1
            continue
        n_scored += 1
        cos = sd.get("cosine_similarity")
        if cos is not None:
            cos_values.append(float(cos))
        interp = sd.get("interpretation")
        if interp in by_interp:
            by_interp[interp] += 1
        if sd.get("same_asset"):
            same_asset += 1

    summary: dict[str, Any] = {
        "n_scored": n_scored,
        "n_failed": n_failed,
        "by_interpretation": by_interp,
        "same_asset_cuts": same_asset,
        "cross_asset_cuts": n_scored - same_asset,
    }
    if cos_values:
        cos_values.sort()
        def pct(p: int) -> float:
            i = min(len(cos_values) - 1, len(cos_values) * p // 100)
            return round(cos_values[i], 4)
        summary["cosine_percentiles"] = {
            "p05": pct(5), "p10": pct(10), "p25": pct(25),
            "p50": pct(50), "p75": pct(75), "p95": pct(95),
        }
        # Suggested flagging threshold: cuts BELOW p10 cosine are the
        # lowest-similarity (most jarring) — those most worth reviewing.
        summary["suggested_flag_threshold_cosine"] = pct(10)
        summary["suggested_flag_threshold_meaning"] = (
            "cuts with cosine_similarity <= this are in the bottom 10% by "
            "visual continuity — surface for editor review"
        )
    return summary


def apply_suggested_spans(
    sidecar: dict,
    *,
    min_delta_frames: int = 2,
    max_shift_sec: float = 30.0,
) -> tuple[int, list[dict[str, Any]]]:
    """Update annotation source in/out from `chunk_suggested_span` when present.

    Only applies when the suggested span lies inside the current source window and
    changes at least `min_delta_frames` on either edge. Returns (count_updated, log).
    """
    fps = float(sidecar.get("frame_rate") or 23.976)
    max_shift_f = int(round(max_shift_sec * fps))
    updated = 0
    log: list[dict[str, Any]] = []
    for ann in sidecar.get("annotations") or []:
        if not isinstance(ann, dict):
            continue
        span = ann.get("chunk_suggested_span")
        if not isinstance(span, dict):
            continue
        start = span.get("start_sec")
        end = span.get("end_sec")
        if start is None or end is None or float(end) <= float(start):
            continue
        key = ann.get("key") or {}
        cur_in = key.get("source_in_frames")
        cur_out = key.get("source_out_frames")
        if cur_in is None or cur_out is None:
            continue
        new_in = _sec_to_frames(float(start), fps)
        new_out = _sec_to_frames(float(end), fps)
        if new_in < cur_in or new_out > cur_out:
            # suggested span must be a sub-range of current trim
            continue
        if abs(new_in - cur_in) < min_delta_frames and abs(new_out - cur_out) < min_delta_frames:
            continue
        if abs(new_in - cur_in) > max_shift_f or abs(new_out - cur_out) > max_shift_f:
            log.append({
                "clip_id": ann.get("clip_id"),
                "skipped": "shift_too_large",
                "delta_in_frames": new_in - cur_in,
                "delta_out_frames": new_out - cur_out,
            })
            continue
        key["source_in_frames"] = new_in
        key["source_out_frames"] = new_out
        timing = ann.get("timing")
        if isinstance(timing, dict):
            timing["source_in_sec"] = round(new_in / fps, 3)
            timing["source_out_sec"] = round(new_out / fps, 3)
            timing["source_duration_sec"] = round((new_out - new_in) / fps, 3)
        ann["_applied_suggested_span"] = True
        updated += 1
        log.append({
            "clip_id": ann.get("clip_id"),
            "asset_id": key.get("asset_id"),
            "old_in_frames": cur_in,
            "old_out_frames": cur_out,
            "new_in_frames": new_in,
            "new_out_frames": new_out,
            "span": span,
        })
    return updated, log


# ---------------- CLI (for standalone visual-cut runs + threshold calibration) ----------------

def _safe_write_json(path: Path, obj: dict) -> None:
    """Atomic JSON write (matches the refresh_act_sidecar.py convention)."""
    import json
    import os
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _cli_visual_cut(sidecar_path: Path, *, no_write: bool = False) -> int:
    """Standalone: load sidecar, run eval_visual_cut_deltas, write back."""
    import json
    sc = json.loads(sidecar_path.read_text(encoding="utf-8"))
    print(f"=== visual-cut eval on {sidecar_path.name} ===")
    n_anns = len(sc.get("annotations") or [])
    print(f"  annotations: {n_anns}")
    n = eval_visual_cut_deltas(sc)
    print(f"  pairs scored: {n}")
    if no_write:
        print("  (--no-write: not writing back)")
    else:
        _safe_write_json(sidecar_path, sc)
        print(f"  written: {sidecar_path}")
    # Always print distribution after a run
    print()
    _print_distribution(sc)
    return 0


def _cli_calibrate(sidecar_path: Path) -> int:
    """Standalone: read a sidecar that has already been visual-cut-evaluated
    and print the cosine distribution + suggested flagging threshold."""
    import json
    sc = json.loads(sidecar_path.read_text(encoding="utf-8"))
    print(f"=== visual-cut distribution on {sidecar_path.name} ===")
    _print_distribution(sc)
    return 0


def _print_distribution(sc: dict) -> None:
    import json
    summary = visual_cut_distribution(sc)
    if summary["n_scored"] == 0:
        print("  no scored cuts found — run `visual-cut` first.")
        return
    print(f"  scored cuts:        {summary['n_scored']}")
    print(f"  failed (no cov):    {summary['n_failed']}")
    print(f"  same-asset cuts:    {summary['same_asset_cuts']}  (within-take ramps)")
    print(f"  cross-asset cuts:   {summary['cross_asset_cuts']}")
    print(f"  by interpretation:  {summary['by_interpretation']}")
    if "cosine_percentiles" in summary:
        print(f"  cosine percentiles: {summary['cosine_percentiles']}")
        print(f"  suggested flag:     cosine_similarity <= {summary['suggested_flag_threshold_cosine']}")
        print(f"                      ({summary['suggested_flag_threshold_meaning']})")


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Sidecar cut evaluators (mid-word + SigLIP visual delta)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("visual-cut",
                        help="Run SigLIP visual-cut delta over a sidecar (mutates in place by default)")
    sp.add_argument("sidecar", type=Path, help="path to <act>.sidecar.json")
    sp.add_argument("--no-write", action="store_true",
                    help="compute + print distribution but don't write siglip_delta_out back")
    sp.set_defaults(func=lambda a: _cli_visual_cut(a.sidecar, no_write=a.no_write))

    sp = sub.add_parser("calibrate",
                        help="Print cosine distribution + suggested flagging threshold for an already-evaluated sidecar")
    sp.add_argument("sidecar", type=Path, help="path to <act>.sidecar.json (must have siglip_delta_out)")
    sp.set_defaults(func=lambda a: _cli_calibrate(a.sidecar))

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
