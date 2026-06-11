#!/usr/bin/env python3
"""
Resolve diarized transcript speakers (segments[].speaker, speakers[].p_id) using
human transcript clip segments with timestamps + speaker labels.

Key design goals:
- Conservative: only resolve when evidence is strong (time overlap support).
- Idempotent: re-running should not churn fields when nothing changes.
- Auditable:
  - Append-only JSONL audit log per run under `_audit/`
  - Per-transcript `speaker_resolution_audit` in `_audit/transcript_provenance/<asset_id>.json`
    (transcript carries a `speaker_provenance` pointer; see `transcript_provenance.py`)

Inputs:
- assets/transcripts/*.transcript.json
- assets/_human transcripts/clip_segments_manifest.jsonl
- assets/_human transcripts/clip_segments/htr_*.json
- people/people.json (canonical names, aliases, name_resolution_rules)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[2]
TRANSCRIPTS_DIR = ROOT / "assets" / "catalog" / "transcripts"
HUMAN_DIR = ROOT / "assets" / "catalog" / "human_transcripts"
CLIP_MANIFEST = HUMAN_DIR / "clip_segments_manifest.jsonl"
CLIP_SEGMENTS_DIR = HUMAN_DIR / "clip_segments"
PEOPLE_JSON = ROOT / "people" / "people.json"
AUDIT_DIR = ROOT / "_audit"

from transcript_provenance import set_resolution_audit_on_transcript  # noqa: E402


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", s).strip().lower()


def atomic_write_json(path: Path, data: dict) -> None:
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


# ---------- time parsing ----------

_TC_RE = re.compile(
    r"^\s*(?P<h>\d{1,2})[:;](?P<m>\d{2})[:;](?P<s>\d{2})[:;](?P<f>\d{1,2})\s*$"
)


def tc_to_seconds(tc: str, fps: float) -> float | None:
    """
    Convert HH:MM:SS:FF (or HH;MM;SS;FF) timecode to seconds using assumed fps.
    """
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
    """
    Parse the common Word-export clip format:
      <start_tc> - <end_tc>
      <speaker_label>
      <text...>

    Returns list of:
      {start_sec, end_sec, speaker_label, text}
    """
    lines = [ln.rstrip("\n") for ln in (human_clip_text or "").splitlines()]
    out: list[dict] = []

    i = 0
    while i < len(lines):
        line = lines[i].strip()
        # Timestamp line like "00:00:52:18 - 00:01:02:00"
        if " - " in line and any(c.isdigit() for c in line[:2]):
            left, right = [p.strip() for p in line.split(" - ", 1)]
            s0 = tc_to_seconds(left, fps=fps)
            s1 = tc_to_seconds(right, fps=fps)
            if s0 is None or s1 is None:
                i += 1
                continue
            speaker_label = ""
            text_lines: list[str] = []

            # Next line is speaker label (often)
            if i + 1 < len(lines):
                speaker_label = lines[i + 1].strip()
            j = i + 2
            # Accumulate text until next timestamp-ish line
            while j < len(lines):
                nxt = lines[j].strip()
                if " - " in nxt and _TC_RE.match(nxt.split(" - ", 1)[0].strip()):
                    break
                # skip empty lines but keep spacing stable-ish
                if nxt != "":
                    text_lines.append(nxt)
                j += 1
            text = " ".join(text_lines).strip()
            out.append(
                {
                    "start_sec": float(s0),
                    "end_sec": float(s1),
                    "speaker_label": speaker_label,
                    "text": text,
                }
            )
            i = j
            continue
        i += 1
    return out


# ---------- people resolution ----------

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
        terms = [p.get("canonical_name", "")] + list(p.get("aliases") or [])
        for t in terms:
            tn = norm(t)
            if not tn:
                continue
            term_to_pid.setdefault(tn, pid)

    rules = people.get("name_resolution_rules") or []
    return PeopleIndex(term_to_pid=term_to_pid, rules=rules)


def resolve_label_to_pid(label: str, people_index: PeopleIndex) -> tuple[str | None, dict]:
    """
    Attempt to resolve a human speaker label to a single p_*.

    Returns (pid_or_none, evidence_dict)
    """
    raw = (label or "").strip()
    ln = norm(raw)
    evidence: dict[str, Any] = {"raw_label": raw}

    if not ln or ln in {"unknown", "speaker", "speaker 1", "speaker 2", "speaker 3", "speaker 4"}:
        evidence["reason"] = "unresolved_generic"
        return None, evidence

    # Apply simple name_resolution_rules for exact-pattern labels (e.g., "Mike")
    for r in people_index.rules:
        pat = norm(r.get("pattern", ""))
        if not pat:
            continue
        if ln == pat:
            default_pid = r.get("default_resolution")
            exceptions = set(r.get("exceptions") or [])
            if raw in exceptions:
                evidence["reason"] = "rule_exception"
                evidence["rule_pattern"] = r.get("pattern")
                return None, evidence
            evidence["reason"] = "rule_default"
            evidence["rule_pattern"] = r.get("pattern")
            evidence["pid"] = default_pid
            return default_pid, evidence

    # Split composite labels like "Alex/ his Dad" or "Michelino/Connor"
    parts = re.split(r"[;/,]| and |&|\+", raw)
    parts = [p.strip() for p in parts if p.strip()]

    resolved: list[tuple[str, str]] = []  # (pid, matched_term)
    for p in parts:
        pn = norm(p)
        if not pn:
            continue
        pid = people_index.term_to_pid.get(pn)
        if pid:
            resolved.append((pid, p))

    # If label itself resolves (not split)
    if not resolved:
        pid = people_index.term_to_pid.get(ln)
        if pid:
            evidence["reason"] = "direct_match"
            evidence["matched_term"] = raw
            evidence["pid"] = pid
            return pid, evidence

    uniq = sorted({pid for pid, _ in resolved})
    if len(uniq) == 1:
        evidence["reason"] = "composite_single"
        evidence["matched_terms"] = [t for _, t in resolved]
        evidence["pid"] = uniq[0]
        return uniq[0], evidence

    evidence["reason"] = "composite_ambiguous"
    evidence["candidate_pids"] = uniq
    evidence["matched_terms"] = [t for _, t in resolved]
    return None, evidence


# ---------- overlap-based mapping ----------

def overlap(a0: float, a1: float, b0: float, b1: float) -> float:
    lo = max(a0, b0)
    hi = min(a1, b1)
    return max(0.0, hi - lo)


def build_speaker_map_from_overlaps(
    transcript_segments: list[dict],
    human_utts: list[dict],
    people_index: PeopleIndex,
    *,
    tolerance_sec: float,
    min_utt_dur_sec: float,
    min_support_sec: float,
    min_support_delta_sec: float,
    min_best_to_second_ratio: float,
    enforce_unique_pid: bool,
    allowed_pids: set[str] | None,
) -> tuple[dict[str, str], dict]:
    """
    Returns (speaker_raw_guid -> p_id mapping, stats dict)
    """
    # Filter transcript segs with a real diarization id
    segs = [
        s
        for s in (transcript_segments or [])
        if s.get("speaker_raw") not in (None, "")
        and s.get("start_sec") is not None
        and s.get("end_sec") is not None
    ]
    segs.sort(key=lambda s: float(s.get("start_sec") or 0.0))

    # Parse/resolve human utterances
    resolved_utts = []
    label_stats = {"total_utts": 0, "resolved_utts": 0, "unresolved_utts": 0, "ambiguous_labels": 0}
    label_evidence_samples: list[dict] = []
    for u in human_utts or []:
        label_stats["total_utts"] += 1
        dur = float(u["end_sec"]) - float(u["start_sec"])
        if dur < min_utt_dur_sec:
            continue
        pid, ev = resolve_label_to_pid(u.get("speaker_label", ""), people_index)
        if pid:
            if allowed_pids is not None and pid not in allowed_pids:
                label_stats["unresolved_utts"] += 1
                ev2 = dict(ev)
                ev2["reason"] = "pid_not_allowed"
                ev2["pid"] = pid
                if len(label_evidence_samples) < 10:
                    label_evidence_samples.append(ev2)
                continue
            label_stats["resolved_utts"] += 1
            resolved_utts.append({**u, "p_id": pid, "_label_ev": ev})
        else:
            label_stats["unresolved_utts"] += 1
            if ev.get("reason") == "composite_ambiguous":
                label_stats["ambiguous_labels"] += 1
        if len(label_evidence_samples) < 10 and ev.get("reason") not in (None, ""):
            label_evidence_samples.append(ev)

    # Build support matrix: guid -> pid -> support_seconds
    support_sec: dict[str, dict[str, float]] = {}
    support_utt: dict[str, dict[str, int]] = {}

    # Two-pointer overlap scan
    j = 0
    for u in resolved_utts:
        u0 = float(u["start_sec"]) - tolerance_sec
        u1 = float(u["end_sec"]) + tolerance_sec
        pid = u["p_id"]

        while j < len(segs) and float(segs[j]["end_sec"]) < u0:
            j += 1
        k = j
        while k < len(segs) and float(segs[k]["start_sec"]) <= u1:
            s = segs[k]
            s0 = float(s["start_sec"])
            s1 = float(s["end_sec"])
            ov = overlap(u0, u1, s0, s1)
            if ov > 0:
                guid = s["speaker_raw"]
                support_sec.setdefault(guid, {})
                support_utt.setdefault(guid, {})
                support_sec[guid][pid] = support_sec[guid].get(pid, 0.0) + ov
                support_utt[guid][pid] = support_utt[guid].get(pid, 0) + 1
            k += 1

    mapping: dict[str, str] = {}
    mapping_stats = {
        "speaker_guids_with_support": 0,
        "speaker_guids_mapped": 0,
        "speaker_guids_below_threshold": 0,
        "speaker_guids_low_margin": 0,
        "speaker_guids_tied": 0,
        "speaker_guids_removed_by_unique_pid": 0,
    }
    per_guid: dict[str, Any] = {}

    for guid, pid_map in support_sec.items():
        mapping_stats["speaker_guids_with_support"] += 1
        items = sorted(pid_map.items(), key=lambda kv: (-kv[1], kv[0]))
        best_pid, best_sec = items[0]
        second_sec = items[1][1] if len(items) > 1 else 0.0
        ratio = (best_sec / second_sec) if second_sec > 0 else float("inf")

        per_guid[guid] = {
            "best_pid": best_pid,
            "best_support_sec": round(best_sec, 3),
            "second_support_sec": round(second_sec, 3),
            "best_to_second_ratio": (round(ratio, 3) if ratio != float("inf") else None),
            "support_by_pid_sec": {p: round(sec, 3) for p, sec in sorted(pid_map.items(), key=lambda kv: -kv[1])},
            "support_by_pid_utterances": support_utt.get(guid, {}),
        }

        if best_sec < min_support_sec:
            mapping_stats["speaker_guids_below_threshold"] += 1
            continue
        if len(items) > 1:
            if abs(best_sec - second_sec) < 0.001:
                mapping_stats["speaker_guids_tied"] += 1
                continue
            if (best_sec - second_sec) < min_support_delta_sec:
                mapping_stats["speaker_guids_low_margin"] += 1
                continue
            if second_sec > 0 and ratio < min_best_to_second_ratio:
                mapping_stats["speaker_guids_low_margin"] += 1
                continue

        mapping[guid] = best_pid
        mapping_stats["speaker_guids_mapped"] += 1

    # Enforce at most one GUID per p_id
    if enforce_unique_pid and mapping:
        pid_to_guids: dict[str, list[str]] = {}
        for guid, pid in mapping.items():
            pid_to_guids.setdefault(pid, []).append(guid)
        for pid, guids in pid_to_guids.items():
            if len(guids) <= 1:
                continue
            guids_sorted = sorted(
                guids,
                key=lambda g: float(per_guid.get(g, {}).get("best_support_sec") or 0.0),
                reverse=True,
            )
            keep = guids_sorted[0]
            for g in guids_sorted[1:]:
                if g in mapping:
                    del mapping[g]
                    mapping_stats["speaker_guids_removed_by_unique_pid"] += 1

    stats = {
        "label_stats": label_stats,
        "label_evidence_samples": label_evidence_samples,
        "mapping_stats": mapping_stats,
        "per_guid": per_guid,
        "allowed_pids": sorted(list(allowed_pids)) if allowed_pids is not None else None,
    }
    return mapping, stats


def apply_mapping_to_transcript(rec: dict, mapping: dict[str, str]) -> tuple[bool, dict]:
    """
    Update:
      - speakers[].p_id
      - segments[].speaker
      - people_ids union with mapped p_ids

    Returns (changed, diff_stats)
    """
    changed = False
    diff = {"segments_updated": 0, "speakers_updated": 0, "people_ids_added": 0}

    # speakers rollup
    speakers = rec.get("speakers") or []
    for sp in speakers:
        guid = sp.get("speaker_id")
        if not guid:
            continue
        pid = mapping.get(guid)
        if pid and sp.get("p_id") != pid:
            sp["p_id"] = pid
            diff["speakers_updated"] += 1
            changed = True

    # segments
    for seg in rec.get("segments") or []:
        guid = seg.get("speaker_raw")
        if not guid:
            continue
        pid = mapping.get(guid)
        if pid and seg.get("speaker") != pid:
            seg["speaker"] = pid
            diff["segments_updated"] += 1
            changed = True

    # people_ids union
    mapped_pids = sorted(set(mapping.values()))
    existing = set(rec.get("people_ids") or [])
    new = sorted(existing | set(mapped_pids))
    if new != sorted(existing):
        rec["people_ids"] = new
        diff["people_ids_added"] = len(set(new) - existing)
        changed = True

    return changed, diff


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="Process only first N clip manifests (debug).")
    ap.add_argument("--fps", type=float, default=30.0, help="Assumed frame rate for HH:MM:SS:FF timecodes.")
    ap.add_argument("--tolerance-sec", type=float, default=1.25, help="Time tolerance applied around each human utterance window.")
    ap.add_argument("--min-utt-dur-sec", type=float, default=0.4, help="Ignore human utterances shorter than this.")
    ap.add_argument("--min-support-sec", type=float, default=20.0, help="Min overlap seconds required to map a diarized GUID to a p_id.")
    ap.add_argument("--min-support-delta-sec", type=float, default=10.0, help="Require (best - second) support seconds >= this (when second exists).")
    ap.add_argument("--min-best-to-second-ratio", type=float, default=1.8, help="Require best/second >= this (when second exists).")
    ap.add_argument("--enforce-unique-pid", action="store_true", help="Allow at most one diarization GUID per p_id per transcript (keep strongest).")
    ap.add_argument("--restrict-to-asset-people-ids", action="store_true", help="Only allow mapping to p_ids already present on transcript.people_ids.")
    ap.add_argument("--restrict-to-human-clip-pids", action="store_true", help="Only allow mapping to p_ids that appear in resolvable human labels for that clip.")
    ap.add_argument("--dry-run", action="store_true", help="Do not write transcript updates; still write audit log.")
    args = ap.parse_args()

    if not CLIP_MANIFEST.exists():
        raise SystemExit(f"Missing {CLIP_MANIFEST}")
    if not PEOPLE_JSON.exists():
        raise SystemExit(f"Missing {PEOPLE_JSON}")

    run_id = datetime.now(timezone.utc).strftime("speaker_resolve_%Y%m%dT%H%M%SZ")
    audit_path = AUDIT_DIR / f"{run_id}.jsonl"
    people_index = build_people_index()

    n_examined = n_missing_transcript = n_updated = n_no_mapping = 0
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
        n_examined += 1

        if not transcript_path.exists() or not clip_path.exists():
            append_jsonl(
                audit_path,
                {
                    "run_id": run_id,
                    "at": now_iso(),
                    "kind": "missing_inputs",
                    "asset_id": asset_id,
                    "roster_id": roster_id,
                    "transcript_exists": transcript_path.exists(),
                    "clip_exists": clip_path.exists(),
                    "segment_path": seg_rel,
                },
            )
            if not transcript_path.exists():
                n_missing_transcript += 1
            continue

        try:
            transcript = json.loads(transcript_path.read_text(encoding="utf-8"))
            clip = json.loads(clip_path.read_text(encoding="utf-8"))
        except Exception as e:
            append_jsonl(
                audit_path,
                {
                    "run_id": run_id,
                    "at": now_iso(),
                    "kind": "read_error",
                    "asset_id": asset_id,
                    "roster_id": roster_id,
                    "segment_path": seg_rel,
                    "error": str(e),
                },
            )
            continue

        human_utts = parse_human_clip_utterances(clip.get("human_clip_text", ""), fps=args.fps)

        # Build allowed set for this clip mapping (optional restrictions)
        allowed_pids: set[str] | None = None
        if args.restrict_to_asset_people_ids:
            allowed_pids = set(transcript.get("people_ids") or [])
        if args.restrict_to_human_clip_pids:
            clip_pids = set()
            for u in human_utts:
                pid, _ev = resolve_label_to_pid(u.get("speaker_label", ""), people_index)
                if pid:
                    clip_pids.add(pid)
            allowed_pids = clip_pids if allowed_pids is None else (allowed_pids & clip_pids)
        mapping, stats = build_speaker_map_from_overlaps(
            transcript.get("segments") or [],
            human_utts,
            people_index,
            tolerance_sec=float(args.tolerance_sec),
            min_utt_dur_sec=float(args.min_utt_dur_sec),
            min_support_sec=float(args.min_support_sec),
            min_support_delta_sec=float(args.min_support_delta_sec),
            min_best_to_second_ratio=float(args.min_best_to_second_ratio),
            enforce_unique_pid=bool(args.enforce_unique_pid),
            allowed_pids=allowed_pids,
        )

        if not mapping:
            n_no_mapping += 1
            append_jsonl(
                audit_path,
                {
                    "run_id": run_id,
                    "at": now_iso(),
                    "kind": "no_mapping",
                    "asset_id": asset_id,
                    "roster_id": roster_id,
                    "segment_path": seg_rel,
                    "params": {
                        "fps": args.fps,
                        "tolerance_sec": args.tolerance_sec,
                        "min_utt_dur_sec": args.min_utt_dur_sec,
                        "min_support_sec": args.min_support_sec,
                        "min_support_delta_sec": args.min_support_delta_sec,
                        "min_best_to_second_ratio": args.min_best_to_second_ratio,
                        "enforce_unique_pid": bool(args.enforce_unique_pid),
                        "restrict_to_asset_people_ids": bool(args.restrict_to_asset_people_ids),
                        "restrict_to_human_clip_pids": bool(args.restrict_to_human_clip_pids),
                    },
                    "stats": stats.get("mapping_stats"),
                    "label_stats": stats.get("label_stats"),
                },
            )
            continue

        changed, diff = apply_mapping_to_transcript(transcript, mapping)

        # Per-record in-file audit block (compact)
        audit_block = {
            "schema_version": 1,
            "run_id": run_id,
            "resolved_at": now_iso(),
            "method": "human_transcript_time_overlap",
            "source": {
                "roster_id": roster_id,
                "clip_segment_path": seg_rel,
            },
            "params": {
                "fps_assumed": args.fps,
                "tolerance_sec": args.tolerance_sec,
                "min_utt_dur_sec": args.min_utt_dur_sec,
                "min_support_sec": args.min_support_sec,
                "min_support_delta_sec": args.min_support_delta_sec,
                "min_best_to_second_ratio": args.min_best_to_second_ratio,
                "enforce_unique_pid": bool(args.enforce_unique_pid),
                "restrict_to_asset_people_ids": bool(args.restrict_to_asset_people_ids),
                "restrict_to_human_clip_pids": bool(args.restrict_to_human_clip_pids),
            },
            "result": {
                "mapped_speaker_guids": len(mapping),
                "mapping": mapping,
                "diff": diff,
                "label_stats": stats.get("label_stats"),
                "mapping_stats": stats.get("mapping_stats"),
            },
        }

        append_jsonl(
            audit_path,
            {
                "run_id": run_id,
                "at": now_iso(),
                "kind": "applied" if changed else "no_change",
                "asset_id": asset_id,
                "roster_id": roster_id,
                "segment_path": seg_rel,
                "params": audit_block["params"],
                "mapping": mapping,
                "diff": diff,
                "label_stats": stats.get("label_stats"),
                "mapping_stats": stats.get("mapping_stats"),
                # Keep detailed per-guid support only in external audit log
                "per_guid_support": stats.get("per_guid"),
                "label_evidence_samples": stats.get("label_evidence_samples"),
            },
        )

        if changed:
            if not args.dry_run:
                set_resolution_audit_on_transcript(
                    transcript, asset_id, audit_block, use_sidecar=True
                )
                atomic_write_json(transcript_path, transcript)
            n_updated += 1

    # Summary record
    append_jsonl(
        audit_path,
        {
            "run_id": run_id,
            "at": now_iso(),
            "kind": "summary",
            "examined_clip_manifests": n_examined,
            "missing_transcript": n_missing_transcript,
            "updated_transcripts": n_updated,
            "no_mapping": n_no_mapping,
            "dry_run": bool(args.dry_run),
            "params": {
                "fps": args.fps,
                "tolerance_sec": args.tolerance_sec,
                "min_utt_dur_sec": args.min_utt_dur_sec,
                "min_support_sec": args.min_support_sec,
                "min_support_delta_sec": args.min_support_delta_sec,
                "min_best_to_second_ratio": args.min_best_to_second_ratio,
                "enforce_unique_pid": bool(args.enforce_unique_pid),
                "restrict_to_asset_people_ids": bool(args.restrict_to_asset_people_ids),
                "restrict_to_human_clip_pids": bool(args.restrict_to_human_clip_pids),
            },
            "audit_log": str(audit_path.relative_to(ROOT)),
        },
    )

    print(json.dumps(
        {
            "run_id": run_id,
            "examined": n_examined,
            "missing_transcript": n_missing_transcript,
            "updated": n_updated,
            "no_mapping": n_no_mapping,
            "dry_run": bool(args.dry_run),
            "audit_log": str(audit_path),
        },
        indent=2,
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

