#!/usr/bin/env python3
"""QA pass on a v2 Act sidecar — for the in-session agent to spot content/scene mismatches.

Per AGENTS.md P1: this is the *I/O step* of a QA workflow. The script extracts
per-scene speaker stats + representative transcript quotes; the agent reads
the output and judges whether each scene's content actually matches its label
+ purpose. Auto-heuristics here flag candidates for review (e.g. a speaker
contributing meaningful time who isn't named in the scene purpose), but the
final call is editorial judgment.

Designed to be the standard QA step after every refresh — see editor_README.md
→ "Transcript-grounded boundary walk" → step 6 verify.

Usage:
  py qa_sidecar.py [actII | path/to/sidecar.json] [--out report.txt]

Output: a per-scene report at editor/story/_sidecar scripts/_qa_out.txt
(or --out path). Sections are ordered by timeline.
"""
from __future__ import annotations
import argparse, json, re
from collections import Counter, defaultdict
from pathlib import Path

EDITOR = Path(__file__).resolve().parents[2]

# The documentary's interviewers (set to your interviewers' p_ids) — always present, never an "anomaly"
INTERVIEWERS = {"p_alex_rienzie", "p_connor_burkesmith"}

# How much speech time qualifies as "meaningful" presence in a scene
MEANINGFUL_SECS = 5.0


def _pid_name_tokens(pid: str) -> list[str]:
    """Tokens from a p_id that might appear in a scene's purpose text."""
    if not pid or not pid.startswith("p_"):
        return []
    stem = pid[2:]
    parts = [p for p in stem.split("_") if p]
    # last token is usually the last name (most distinctive)
    return parts


def _name_in_text(pid: str, *texts: str) -> bool:
    """Heuristic: does this speaker get mentioned in the scene's purpose/flags
    text? Matches on any token from the p_id (case-insensitive, word-boundary)."""
    tokens = _pid_name_tokens(pid)
    if not tokens:
        return False
    big = " ".join(t for t in texts if t).lower()
    if not big:
        return False
    for tok in tokens:
        if len(tok) < 3:  # skip "v", "e", etc.
            continue
        if re.search(r"\b" + re.escape(tok) + r"\b", big):
            return True
    return False


def _scene_text(scene: dict) -> str:
    """Canonical editorial text for a scene: label + purpose ONLY. We deliberately
    EXCLUDE agent_flags here — those are the agent's own commentary on what the
    scene contains (e.g. 'extension: now opens with <speaker>…') and can mask
    real content/purpose mismatches when an earlier agent pass mis-categorized
    content. The QA's job is to validate against the editor's canonical purpose,
    not against earlier agent rationalizations of it."""
    return " ".join([scene.get("label") or "", scene.get("purpose") or ""])


def _top_quote(anns: list[dict], pid: str, n: int = 2) -> list[str]:
    """Pick the n most informative transcript snippets for this speaker in
    this scene. Heuristic: prefer annotations where this speaker speaks the
    longest (skips trivial ride-along clips)."""
    by_secs = []
    for a in anns:
        secs_here = 0.0
        for sp in (a.get("speakers") or []):
            if sp.get("p_id") == pid:
                secs_here = sp.get("seconds") or 0
                break
        txt = (a.get("transcript_text") or "").strip()
        if txt and secs_here > 0:
            by_secs.append((secs_here, txt))
    by_secs.sort(key=lambda x: -x[0])
    out = []
    seen = set()
    for s, t in by_secs:
        # de-dupe near-duplicate quotes (sidecar often carries the same overlap text
        # across A1 + A2 stereo + V1 video — choose one representative)
        key = t[:60]
        if key in seen:
            continue
        seen.add(key)
        # trim long quotes
        out.append(t if len(t) < 240 else t[:236] + "…")
        if len(out) >= n:
            break
    return out


def qa_report(sidecar_path: Path, out_path: Path) -> int:
    sc = json.loads(sidecar_path.read_text(encoding="utf-8"))
    anns = sc.get("annotations") or []
    beats = sc.get("beats") or []

    # Index annotations by scene
    by_scene = defaultdict(list)
    for a in anns:
        by_scene[a.get("scene")].append(a)

    findings = []
    lines = [
        f"# QA report — {sidecar_path.name}",
        f"xml_sha10={(sc.get('xml_sha256') or '')[:10]}   timeline={sc.get('timeline_range_frames')}   n_annotations={len(anns)}",
        f"",
        f"Method: per-scene speaker breakdown + representative quotes per speaker.",
        f"Auto-heuristic: any non-interviewer speaker with ≥{int(MEANINGFUL_SECS)}s of speech",
        f"whose name doesn't appear in the scene label/purpose/agent_flags is flagged ⚠ for",
        f"agent review. False positives expected (e.g. transition speakers); editorial",
        f"judgment is the final word.",
        f"",
        f"Interviewers (always excluded): {', '.join(sorted(INTERVIEWERS))}",
        f"",
        "=" * 80,
    ]

    for beat in beats:
        bid = beat["id"]
        blabel = beat.get("label") or ""
        brng = beat.get("timeline_range_frames")
        lines.append(f"\n## BEAT {bid} — {blabel}   range {brng}\n")
        for scene in (beat.get("scenes") or []):
            sid = scene["id"]
            slabel = scene.get("label") or ""
            spurpose = scene.get("purpose") or "(no purpose)"
            srng = scene.get("timeline_range_frames")
            proposed = scene.get("proposed")
            stext = _scene_text(scene)

            scene_anns = by_scene.get(sid, [])
            n_total = len(scene_anns)

            # Speaker breakdown — by seconds
            spk_secs = Counter()
            spk_anncount = Counter()
            for a in scene_anns:
                for sp in (a.get("speakers") or []):
                    pid = sp.get("p_id") or "?"
                    spk_secs[pid] += sp.get("seconds") or 0
                    spk_anncount[pid] += 1

            # Track breakdown
            tracks = Counter((a.get("key") or {}).get("track") for a in scene_anns)

            tag = " [PROPOSED]" if proposed else ""
            lines.append(f"### {sid} — '{slabel}'{tag}   range {srng}   n={n_total}")
            lines.append(f"  purpose: {spurpose}")
            tracks_html = " / ".join(f"{t}:{n}" for t, n in sorted(tracks.items()))
            lines.append(f"  tracks: {tracks_html}")

            if not scene_anns:
                lines.append(f"  ⚠ EMPTY SCENE — no annotations land in this range")
                findings.append({"scene": sid, "type": "empty_scene", "note": "No annotations fall in this scene's frame range"})
                continue

            # Speaker block
            lines.append(f"  speakers:")
            anomaly_pids = []
            sorted_spk = sorted(spk_secs.items(), key=lambda x: -x[1])
            # Total scene speech (for share computation) — exclude unknown
            named_total = sum(s for p, s in sorted_spk if p and p.startswith("p_"))
            for pid, secs in sorted_spk[:10]:
                in_purpose = _name_in_text(pid, stext)
                is_interviewer = pid in INTERVIEWERS
                is_unknown = pid in (None, "?", "") or not pid.startswith("p_")
                share = (secs / named_total) if named_total > 0 else 0
                rank = sorted_spk.index((pid, secs))
                flag = ""
                if not is_interviewer and not is_unknown and secs >= MEANINGFUL_SECS:
                    if not in_purpose:
                        # Severity: dominant (top-1 OR ≥30% share) speakers not in purpose
                        # are very likely real mismatches; minor speakers are often legit
                        # cutaways/transition voices.
                        is_dominant = (rank == 0) or (share >= 0.30)
                        flag = "  ⚠⚠ DOMINANT speaker NOT in purpose" if is_dominant else "  ⚠ minor speaker not in purpose"
                        anomaly_pids.append((pid, is_dominant))
                share_str = f"{round(share*100)}%" if named_total > 0 else "—"
                lines.append(f"    {pid:<28}  {secs:6.1f}s  ({share_str:>3} of named)  ({spk_anncount[pid]} ann){flag}")

            # Quote targets: every anomaly speaker + the top in-purpose speaker for reference
            anom_pid_set = {p for p, _ in anomaly_pids}
            quote_targets = list(anom_pid_set)
            for pid, _ in sorted_spk:
                if pid in INTERVIEWERS or pid in (None, "?", "") or not pid.startswith("p_"):
                    continue
                if pid not in quote_targets:
                    quote_targets.append(pid)
                    break
            if quote_targets:
                lines.append(f"  representative quotes:")
                for pid in quote_targets:
                    quotes = _top_quote(scene_anns, pid, n=2)
                    if quotes:
                        anom = " ⚠⚠" if pid in {p for p, d in anomaly_pids if d} else (" ⚠" if pid in anom_pid_set else "")
                        lines.append(f"    {pid}{anom}:")
                        for q in quotes:
                            lines.append(f"      \"{q}\"")

            for pid, is_dominant in anomaly_pids:
                findings.append({
                    "scene": sid,
                    "scene_label": slabel,
                    "type": "dominant_speaker_not_in_purpose" if is_dominant else "minor_speaker_not_in_purpose",
                    "speaker": pid,
                    "seconds_in_scene": round(spk_secs[pid], 1),
                    "share_of_scene_speech": f"{round((spk_secs[pid] / named_total) * 100)}%" if named_total > 0 else "—",
                    "note": f"{pid} contributes {round(spk_secs[pid], 1)}s ({round((spk_secs[pid] / named_total) * 100) if named_total > 0 else 0}% of named speech) in {sid} but isn't named in the scene's label/purpose.",
                })

    # Summary
    lines.append("\n" + "=" * 80)
    lines.append(f"\n# Summary — {len(findings)} candidate findings\n")
    by_type = Counter(f["type"] for f in findings)
    for t, n in by_type.items():
        lines.append(f"  {t}: {n}")
    lines.append("")
    if findings:
        lines.append("Findings (for agent review):")
        for f in findings:
            lines.append(f"  - [{f['type']}] {f['scene']} '{f.get('scene_label','')}'  ")
            for k, v in f.items():
                if k in ("scene", "scene_label", "type"): continue
                lines.append(f"      {k}: {v}")

    out_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"QA_DONE  findings={len(findings)}  out={out_path}")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("sidecar", nargs="?", default="actII",
                    help="'actI' / 'actII' / 'actIII' or path to a sidecar.json")
    ap.add_argument("--out", default=None,
                    help="path to write the report (default: editor/story/_sidecar scripts/_qa_out.txt)")
    args = ap.parse_args()

    if args.sidecar in ("actI", "actII", "actIII"):
        sidecar = EDITOR / "story/sidecars" / f"{args.sidecar}.sidecar.json"
    else:
        sidecar = Path(args.sidecar)

    out = Path(args.out) if args.out else (EDITOR / "story/_sidecar scripts/_qa_out.txt")
    return qa_report(sidecar, out)


if __name__ == "__main__":
    raise SystemExit(main())
