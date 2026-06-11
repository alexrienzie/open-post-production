#!/usr/bin/env python3
"""
Speaker accuracy review:
Compare resolved machine transcript speakers (segments[].speaker) against human
transcript clip utterances with time windows + speaker labels.

Outputs:
- _runs/speaker_accuracy_<run_id>.json  (aggregate + per-asset summaries)
- _runs/speaker_accuracy_<run_id>_samples.jsonl (append-only samples of mismatches)

Notes:
- Human timecodes are HH:MM:SS:FF (or with ';') and require an assumed FPS.
- We evaluate only human utterances that can be resolved to a single p_*.
- We score each utterance by majority overlap duration across machine segments
  within [start-tol, end+tol].
"""

from __future__ import annotations

import argparse
import json
import os
import re
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[2]
TRANSCRIPTS_DIR = ROOT / "assets" / "catalog" / "transcripts"
HUMAN_DIR = ROOT / "assets" / "catalog" / "human_transcripts"
CLIP_MANIFEST = HUMAN_DIR / "clip_segments_manifest.jsonl"
PEOPLE_JSON = ROOT / "people" / "people.json"
RUNS_DIR = ROOT / "_runs"


def now_id() -> str:
    return datetime.now(timezone.utc).strftime("speaker_accuracy_%Y%m%dT%H%M%SZ")


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", s).strip().lower()


def atomic_write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def append_jsonl(path: Path, rec: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def iter_jsonl(path: Path) -> Iterable[dict]:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            yield json.loads(line)
        except Exception:
            continue


_TC_RE = re.compile(r"^\s*(?P<h>\d{1,2})[:;](?P<m>\d{2})[:;](?P<s>\d{2})[:;](?P<f>\d{1,2})\s*$")


def tc_to_seconds(tc: str, fps: float) -> float | None:
    m = _TC_RE.match(tc.strip())
    if not m:
        return None
    h = int(m.group("h"))
    mm = int(m.group("m"))
    ss = int(m.group("s"))
    ff = int(m.group("f"))
    if fps <= 0:
        return None
    return h * 3600 + mm * 60 + ss + (ff / fps)


def parse_human_clip_utterances(human_clip_text: str, fps: float) -> list[dict]:
    lines = [ln.rstrip("\n") for ln in (human_clip_text or "").splitlines()]
    out: list[dict] = []
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if " - " in line and _TC_RE.match(line.split(" - ", 1)[0].strip()):
            left, right = [p.strip() for p in line.split(" - ", 1)]
            s0 = tc_to_seconds(left, fps=fps)
            s1 = tc_to_seconds(right, fps=fps)
            if s0 is None or s1 is None:
                i += 1
                continue
            speaker_label = lines[i + 1].strip() if i + 1 < len(lines) else ""
            j = i + 2
            text_lines: list[str] = []
            while j < len(lines):
                nxt = lines[j].strip()
                if " - " in nxt and _TC_RE.match(nxt.split(" - ", 1)[0].strip()):
                    break
                if nxt:
                    text_lines.append(nxt)
                j += 1
            out.append(
                {
                    "start_sec": float(s0),
                    "end_sec": float(s1),
                    "speaker_label": speaker_label,
                    "text": " ".join(text_lines).strip(),
                }
            )
            i = j
            continue
        i += 1
    return out


@dataclass(frozen=True)
class PeopleIndex:
    term_to_pid: dict[str, str]
    rules: list[dict[str, Any]]


def build_people_index() -> PeopleIndex:
    people = json.loads(PEOPLE_JSON.read_text(encoding="utf-8"))
    term_to_pid: dict[str, str] = {}
    for p in people.get("people") or []:
        pid = p.get("id")
        if not pid:
            continue
        for t in [p.get("canonical_name", "")] + list(p.get("aliases") or []):
            tn = norm(t)
            if tn:
                term_to_pid.setdefault(tn, pid)
    return PeopleIndex(term_to_pid=term_to_pid, rules=people.get("name_resolution_rules") or [])


def resolve_label_to_pid(label: str, people_index: PeopleIndex) -> str | None:
    raw = (label or "").strip()
    ln = norm(raw)
    if not ln:
        return None
    if ln in {"unknown"}:
        return None
    if ln.startswith("speaker "):
        return None

    # rule exact match
    for r in people_index.rules:
        pat = norm(r.get("pattern", ""))
        if pat and ln == pat:
            default_pid = r.get("default_resolution")
            exceptions = set(r.get("exceptions") or [])
            if raw in exceptions:
                return None
            return default_pid

    # direct exact
    pid = people_index.term_to_pid.get(ln)
    if pid:
        return pid

    # composite
    parts = re.split(r"[;/,]| and |&|\+", raw)
    parts = [p.strip() for p in parts if p.strip()]
    pids = {people_index.term_to_pid.get(norm(p)) for p in parts}
    pids.discard(None)
    if len(pids) == 1:
        return next(iter(pids))
    return None


def overlap(a0: float, a1: float, b0: float, b1: float) -> float:
    lo = max(a0, b0)
    hi = min(a1, b1)
    return max(0.0, hi - lo)


def majority_machine_speaker_for_window(
    segments: list[dict],
    start_sec: float,
    end_sec: float,
    *,
    tolerance_sec: float,
) -> tuple[str | None, dict]:
    """
    Returns (predicted_pid_or_none, detail) where prediction is the speaker with
    max overlap duration within the window expanded by tolerance.
    """
    w0 = start_sec - tolerance_sec
    w1 = end_sec + tolerance_sec
    by_pid: dict[str, float] = {}
    total_ov = 0.0

    for s in segments or []:
        s0 = s.get("start_sec")
        s1 = s.get("end_sec")
        pid = s.get("speaker")
        if s0 is None or s1 is None:
            continue
        if not pid:
            continue
        ov = overlap(float(w0), float(w1), float(s0), float(s1))
        if ov <= 0:
            continue
        total_ov += ov
        by_pid[pid] = by_pid.get(pid, 0.0) + ov

    if not by_pid:
        return None, {"total_overlap_sec": 0.0, "by_pid_sec": {}}
    pred_pid, pred_sec = sorted(by_pid.items(), key=lambda kv: (-kv[1], kv[0]))[0]
    return pred_pid, {
        "total_overlap_sec": round(total_ov, 3),
        "pred_support_sec": round(pred_sec, 3),
        "by_pid_sec": {p: round(sec, 3) for p, sec in sorted(by_pid.items(), key=lambda kv: -kv[1])},
    }


def majority_machine_speaker_for_window_fast(
    segs_sorted: list[dict],
    *,
    start_sec: float,
    end_sec: float,
    tolerance_sec: float,
    start_idx_hint: int,
) -> tuple[str | None, dict, int]:
    """
    Faster version for repeated queries over the same segment list.
    Uses a moving index hint to skip segments that end before the window starts.

    Returns (pred_pid_or_none, detail, next_idx_hint)
    """
    w0 = start_sec - tolerance_sec
    w1 = end_sec + tolerance_sec

    by_pid: dict[str, float] = {}
    total_ov = 0.0

    i = max(0, start_idx_hint)
    # advance until segment might overlap
    while i < len(segs_sorted):
        s1 = segs_sorted[i].get("end_sec")
        if s1 is None:
            i += 1
            continue
        if float(s1) >= w0:
            break
        i += 1

    k = i
    while k < len(segs_sorted):
        s = segs_sorted[k]
        s0 = s.get("start_sec")
        s1 = s.get("end_sec")
        if s0 is None or s1 is None:
            k += 1
            continue
        s0f = float(s0)
        if s0f > w1:
            break
        pid = s.get("speaker")
        if not pid:
            k += 1
            continue
        ov = overlap(float(w0), float(w1), s0f, float(s1))
        if ov > 0:
            total_ov += ov
            by_pid[pid] = by_pid.get(pid, 0.0) + ov
        k += 1

    if not by_pid:
        return None, {"total_overlap_sec": 0.0, "by_pid_sec": {}}, i
    pred_pid, pred_sec = sorted(by_pid.items(), key=lambda kv: (-kv[1], kv[0]))[0]
    return (
        pred_pid,
        {
            "total_overlap_sec": round(total_ov, 3),
            "pred_support_sec": round(pred_sec, 3),
            "by_pid_sec": {p: round(sec, 3) for p, sec in sorted(by_pid.items(), key=lambda kv: -kv[1])},
        },
        i,
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fps", type=float, default=30.0)
    ap.add_argument("--tolerance-sec", type=float, default=1.25)
    ap.add_argument("--min-utt-dur-sec", type=float, default=0.4)
    ap.add_argument("--min-machine-overlap-sec", type=float, default=0.6)
    ap.add_argument("--focus-min-utt-dur-sec", type=float, default=0.0, help="If >0, compute focused confusion/per-asset for utterances >= this duration.")
    ap.add_argument("--focus-min-pred-support-sec", type=float, default=0.0, help="If >0, compute focused confusion/per-asset for pred_support_sec >= this.")
    ap.add_argument("--report-thresholds-sec", type=str, default="2,4,8", help="Comma-separated utterance duration thresholds for filtered accuracy.")
    ap.add_argument("--report-min-pred-support-sec", type=str, default="1,2,4", help="Comma-separated pred support thresholds (seconds) for filtered accuracy.")
    ap.add_argument("--max-mismatch-samples", type=int, default=2000)
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    run_id = now_id()
    out_json = RUNS_DIR / f"{run_id}.json"
    out_samples = RUNS_DIR / f"{run_id}_samples.jsonl"
    if out_samples.exists():
        out_samples.unlink()

    people_index = build_people_index()

    totals = Counter()
    confusion: dict[str, Counter] = defaultdict(Counter)  # true_pid -> Counter(pred_pid)
    per_asset: dict[str, dict] = {}
    mismatch_samples_written = 0

    # Weighted accuracy (by predicted overlap support within each utterance window)
    weighted = Counter()

    # Focused confusion/per-asset (single chosen filter)
    focus = {
        "enabled": (float(args.focus_min_utt_dur_sec) > 0.0) or (float(args.focus_min_pred_support_sec) > 0.0),
        "min_utt_dur_sec": float(args.focus_min_utt_dur_sec),
        "min_pred_support_sec": float(args.focus_min_pred_support_sec),
        "totals": Counter(),
        "confusion": defaultdict(Counter),  # true -> pred
        "per_asset": {},
    }

    # Filtered views
    def _parse_list(s: str) -> list[float]:
        out = []
        for part in (s or "").split(","):
            part = part.strip()
            if not part:
                continue
            try:
                out.append(float(part))
            except Exception:
                pass
        return sorted(set(out))

    dur_thresholds = _parse_list(args.report_thresholds_sec)
    support_thresholds = _parse_list(args.report_min_pred_support_sec)

    filtered = {
        "by_min_utt_dur_sec": {t: Counter() for t in dur_thresholds},
        "by_min_pred_support_sec": {t: Counter() for t in support_thresholds},
        "by_both": {(d, s): Counter() for d in dur_thresholds for s in support_thresholds},
    }

    for idx, m in enumerate(iter_jsonl(CLIP_MANIFEST)):
        if args.limit and idx >= args.limit:
            break
        asset_id = m.get("asset_id")
        roster_id = m.get("roster_id")
        seg_rel = m.get("segment_path")
        if not asset_id or not roster_id or not seg_rel:
            continue
        transcript_path = TRANSCRIPTS_DIR / f"{asset_id}.transcript.json"
        clip_path = HUMAN_DIR / seg_rel
        totals["clips_examined"] += 1
        if not transcript_path.exists() or not clip_path.exists():
            totals["clips_missing_inputs"] += 1
            continue

        try:
            transcript = json.loads(transcript_path.read_text(encoding="utf-8"))
            clip = json.loads(clip_path.read_text(encoding="utf-8"))
        except Exception:
            totals["clips_read_error"] += 1
            continue

        utts = parse_human_clip_utterances(clip.get("human_clip_text", ""), fps=float(args.fps))
        if not utts:
            totals["clips_no_utts"] += 1
            continue

        segs = transcript.get("segments") or []
        # Pre-sort once (we'll use a moving pointer hint per clip)
        segs_sorted = [
            s
            for s in segs
            if s.get("start_sec") is not None and s.get("end_sec") is not None
        ]
        segs_sorted.sort(key=lambda s: float(s.get("start_sec") or 0.0))

        asset_stats = per_asset.setdefault(asset_id, {
            "asset_id": asset_id,
            "roster_ids": set(),
            "utterances_scored": 0,
            "utterances_correct": 0,
            "utterances_no_truth": 0,
            "utterances_no_pred": 0,
        })
        asset_stats["roster_ids"].add(roster_id)

        if focus["enabled"]:
            fstats = focus["per_asset"].setdefault(asset_id, {
                "asset_id": asset_id,
                "roster_ids": set(),
                "utterances_scored": 0,
                "utterances_correct": 0,
                "utterances_incorrect": 0,
            })
            fstats["roster_ids"].add(roster_id)

        # Sort utterances to keep the moving index hint valid
        utts.sort(key=lambda u: float(u["start_sec"]))
        seg_idx_hint = 0

        for u in utts:
            u0 = float(u["start_sec"])
            u1 = float(u["end_sec"])
            u_dur = (u1 - u0)
            if u_dur < float(args.min_utt_dur_sec):
                continue

            true_pid = resolve_label_to_pid(u.get("speaker_label", ""), people_index)
            if not true_pid:
                totals["utterances_no_truth"] += 1
                asset_stats["utterances_no_truth"] += 1
                continue

            pred_pid, detail, seg_idx_hint = majority_machine_speaker_for_window_fast(
                segs_sorted,
                start_sec=u0,
                end_sec=u1,
                tolerance_sec=float(args.tolerance_sec),
                start_idx_hint=seg_idx_hint,
            )

            if not pred_pid or detail.get("total_overlap_sec", 0.0) < float(args.min_machine_overlap_sec):
                totals["utterances_no_pred"] += 1
                asset_stats["utterances_no_pred"] += 1
                continue

            totals["utterances_scored"] += 1
            asset_stats["utterances_scored"] += 1
            confusion[true_pid][pred_pid] += 1

            pred_support = float(detail.get("pred_support_sec", 0.0) or 0.0)
            weighted["support_sec_total"] += pred_support
            if pred_pid == true_pid:
                weighted["support_sec_correct"] += pred_support
            else:
                weighted["support_sec_incorrect"] += pred_support

            # focused confusion/per-asset
            if focus["enabled"]:
                if (focus["min_utt_dur_sec"] and u_dur < focus["min_utt_dur_sec"]):
                    pass
                elif (focus["min_pred_support_sec"] and pred_support < focus["min_pred_support_sec"]):
                    pass
                else:
                    focus["totals"]["utterances_scored"] += 1
                    focus["confusion"][true_pid][pred_pid] += 1
                    fstats = focus["per_asset"][asset_id]
                    fstats["utterances_scored"] += 1
                    if pred_pid == true_pid:
                        focus["totals"]["utterances_correct"] += 1
                        fstats["utterances_correct"] += 1
                    else:
                        focus["totals"]["utterances_incorrect"] += 1
                        fstats["utterances_incorrect"] += 1

            # filtered breakdowns
            for t in dur_thresholds:
                if u_dur >= t:
                    filtered["by_min_utt_dur_sec"][t]["scored"] += 1
                    if pred_pid == true_pid:
                        filtered["by_min_utt_dur_sec"][t]["correct"] += 1
                    else:
                        filtered["by_min_utt_dur_sec"][t]["incorrect"] += 1
            for t in support_thresholds:
                if pred_support >= t:
                    filtered["by_min_pred_support_sec"][t]["scored"] += 1
                    if pred_pid == true_pid:
                        filtered["by_min_pred_support_sec"][t]["correct"] += 1
                    else:
                        filtered["by_min_pred_support_sec"][t]["incorrect"] += 1
            for dthr in dur_thresholds:
                if u_dur < dthr:
                    continue
                for sthr in support_thresholds:
                    if pred_support < sthr:
                        continue
                    c = filtered["by_both"][(dthr, sthr)]
                    c["scored"] += 1
                    if pred_pid == true_pid:
                        c["correct"] += 1
                    else:
                        c["incorrect"] += 1

            if pred_pid == true_pid:
                totals["utterances_correct"] += 1
                asset_stats["utterances_correct"] += 1
            else:
                totals["utterances_incorrect"] += 1
                if mismatch_samples_written < int(args.max_mismatch_samples):
                    append_jsonl(out_samples, {
                        "run_id": run_id,
                        "asset_id": asset_id,
                        "roster_id": roster_id,
                        "window": {"start_sec": u0, "end_sec": u1},
                        "human": {"speaker_label": u.get("speaker_label"), "p_id": true_pid, "text": (u.get("text") or "")[:240]},
                        "machine": {"pred_p_id": pred_pid, "support": detail},
                    })
                    mismatch_samples_written += 1

    # finalize per_asset (convert roster_id sets)
    per_asset_out = {}
    for aid, s in per_asset.items():
        per_asset_out[aid] = {**s, "roster_ids": sorted(list(s["roster_ids"]))}
        scored = s["utterances_scored"] or 0
        per_asset_out[aid]["accuracy"] = (s["utterances_correct"] / scored) if scored else None

    # confusion to plain dict
    confusion_out = {tp: dict(cnt) for tp, cnt in confusion.items()}

    scored = totals["utterances_scored"] or 0
    accuracy = (totals["utterances_correct"] / scored) if scored else None
    w_total = float(weighted.get("support_sec_total") or 0.0)
    weighted_accuracy = (float(weighted.get("support_sec_correct") or 0.0) / w_total) if w_total else None

    # finalize filtered views with computed accuracies
    filtered_out = {"by_min_utt_dur_sec": {}, "by_min_pred_support_sec": {}, "by_both": {}}
    for t, c in filtered["by_min_utt_dur_sec"].items():
        s = c.get("scored", 0) or 0
        filtered_out["by_min_utt_dur_sec"][str(t)] = {
            "scored": s,
            "correct": c.get("correct", 0),
            "incorrect": c.get("incorrect", 0),
            "accuracy": (c.get("correct", 0) / s) if s else None,
        }
    for t, c in filtered["by_min_pred_support_sec"].items():
        s = c.get("scored", 0) or 0
        filtered_out["by_min_pred_support_sec"][str(t)] = {
            "scored": s,
            "correct": c.get("correct", 0),
            "incorrect": c.get("incorrect", 0),
            "accuracy": (c.get("correct", 0) / s) if s else None,
        }
    for (dthr, sthr), c in filtered["by_both"].items():
        s = c.get("scored", 0) or 0
        filtered_out["by_both"][f"dur>={dthr},support>={sthr}"] = {
            "scored": s,
            "correct": c.get("correct", 0),
            "incorrect": c.get("incorrect", 0),
            "accuracy": (c.get("correct", 0) / s) if s else None,
        }

    out = {
        "run_id": run_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "params": {
            "fps": args.fps,
            "tolerance_sec": args.tolerance_sec,
            "min_utt_dur_sec": args.min_utt_dur_sec,
            "min_machine_overlap_sec": args.min_machine_overlap_sec,
            "focus_min_utt_dur_sec": args.focus_min_utt_dur_sec,
            "focus_min_pred_support_sec": args.focus_min_pred_support_sec,
            "report_thresholds_sec": dur_thresholds,
            "report_min_pred_support_sec": support_thresholds,
            "max_mismatch_samples": args.max_mismatch_samples,
            "limit": args.limit,
        },
        "totals": dict(totals),
        "accuracy": accuracy,
        "weighted_accuracy_by_pred_support_sec": weighted_accuracy,
        "weighted_support_sec": dict(weighted),
        "filtered": filtered_out,
        "confusion": confusion_out,
        "per_asset": per_asset_out,
        "focus": None,
        "mismatch_samples_written": mismatch_samples_written,
        "mismatch_samples_jsonl": str(out_samples.relative_to(ROOT)) if out_samples.exists() else None,
    }

    if focus["enabled"]:
        f_scored = int(focus["totals"].get("utterances_scored", 0) or 0)
        f_correct = int(focus["totals"].get("utterances_correct", 0) or 0)
        f_acc = (f_correct / f_scored) if f_scored else None

        f_per_asset_out = {}
        for aid, s in focus["per_asset"].items():
            scored = s.get("utterances_scored") or 0
            acc = (s.get("utterances_correct", 0) / scored) if scored else None
            f_per_asset_out[aid] = {
                **s,
                "roster_ids": sorted(list(s["roster_ids"])),
                "accuracy": acc,
            }

        out["focus"] = {
            "min_utt_dur_sec": focus["min_utt_dur_sec"],
            "min_pred_support_sec": focus["min_pred_support_sec"],
            "totals": dict(focus["totals"]),
            "accuracy": f_acc,
            "confusion": {tp: dict(cnt) for tp, cnt in focus["confusion"].items()},
            "per_asset": f_per_asset_out,
        }
    atomic_write_json(out_json, out)

    print(json.dumps({
        "run_id": run_id,
        "accuracy": accuracy,
        "weighted_accuracy_by_pred_support_sec": weighted_accuracy,
        "utterances_scored": scored,
        "utterances_correct": totals["utterances_correct"],
        "utterances_incorrect": totals["utterances_incorrect"],
        "focus_accuracy": (out["focus"]["accuracy"] if out.get("focus") else None),
        "out_json": str(out_json),
        "samples": str(out_samples) if out_samples.exists() else None,
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

