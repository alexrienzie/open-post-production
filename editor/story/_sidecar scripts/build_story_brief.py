#!/usr/bin/env python3
"""Generate a story-review brief from the Act sidecar.

Hierarchical Markdown (Act -> Beat -> Scene -> Clips), designed for an LLM
doing narrative feedback. Pulls everything from the sidecar's denormalized
fields -- no SQLite/transcript dereferences needed.

Per-clip:
  - Runtime timestamp at first appearance of the source asset
  - clip_id, asset_classifications.type tag (interview/verite/b_roll/...)
  - For interviews: subject (derived non-interviewer speaker)
  - For audio-spine clips paired with sketches: note that audio carries the cut
  - One semantic description per source asset (first use only -- reuses just
    note "(same source as cXXXX)")
  - Speaker-prefixed transcript snippet for the clip's source window

Usage:
  py build_story_brief.py <sidecar.json> --out <brief.md>
"""

from __future__ import annotations
import argparse
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Optional


def _fmt_ts(sec: Optional[float]) -> str:
    if sec is None:
        return "?:??"
    sec = float(sec)
    m, s = divmod(sec, 60)
    return str(int(m)) + ":" + str(int(s)).zfill(2)


def _pid_to_name(pid: Optional[str]) -> str:
    if not pid or not pid.startswith("p_"):
        return "Unknown"
    return pid.replace("p_", "").replace("_", " ").title()


_HEX_GUID = re.compile(r"^[0-9A-Fa-f]{16,}$")


def _speaker_label_for_seg(seg: dict, fallback: str = "Unknown speaker") -> str:
    """Render a clean speaker prefix for a transcript segment.
    Handles: p_id, 'Speaker N', raw diarization GUIDs (-> 'Unknown speaker')."""
    pid = (seg.get("speaker") or "").strip()
    if pid.startswith("p_"):
        return _pid_to_name(pid)
    if pid.lower().startswith("speaker "):
        return pid
    if pid and _HEX_GUID.match(pid):
        return fallback
    if pid:
        return pid
    return fallback


def _merge_transcript(segs: list) -> str:
    """Group consecutive segments by speaker, render as 'Name: text'."""
    if not segs:
        return ""
    lines = []
    cur_spk = None
    cur_text = []
    for seg in segs:
        spk = _speaker_label_for_seg(seg)
        txt = (seg.get("text") or "").strip()
        if not txt:
            continue
        if spk != cur_spk:
            if cur_text:
                lines.append(cur_spk + ": " + " ".join(cur_text))
            cur_spk = spk
            cur_text = [txt]
        else:
            cur_text.append(txt)
    if cur_text:
        lines.append(cur_spk + ": " + " ".join(cur_text))
    return "\n".join(lines)


def _asset_type_tag(cls: dict) -> str:
    t = (cls or {}).get("type") or ""
    return t.replace("_", "-") if t else "--"


def _render_clip(ann: dict, by_clip: dict, seen_assets: dict) -> list:
    out = []
    cid = ann.get("clip_id") or "?"
    timing = ann.get("timing") or {}
    ts = timing.get("timeline_start_sec")
    asset = ann.get("asset") or {}
    cls = asset.get("classifications") or {}
    type_tag = _asset_type_tag(cls)
    track = ann["key"].get("track") or "?"
    aid = ann["key"].get("asset_id")
    audio_spine = ann.get("audio_spine") or ann.get("is_audio_spine")

    header_parts = ["- **" + _fmt_ts(ts) + "** `" + cid + "`", "*" + type_tag + "*"]
    if audio_spine:
        header_parts.append("**[audio spine]**")
    if track != "V1":
        header_parts.append("(" + track + ")")
    header = " -- ".join(header_parts[:2]) + " " + " ".join(header_parts[2:])

    first_use_cid = seen_assets.get(aid)
    if first_use_cid is None and aid:
        seen_assets[aid] = cid
        filename = asset.get("filename") or "?"
        subj_obj = ann.get("subject")
        subj_name = subj_obj.get("name") if subj_obj else None
        gem_sub = (ann.get("chunk_subject") or "").strip()
        gem_act = (ann.get("chunk_action") or "").strip()
        desc_parts = []
        if subj_name and type_tag == "interview":
            desc_parts.append("**Subject:** " + subj_name)
        if gem_sub:
            desc_parts.append(gem_sub)
        if gem_act:
            desc_parts.append(gem_act)
        desc = " -- ".join(desc_parts) if desc_parts else "*(no semantic description)*"
        out.append(header + " * `" + filename + "`")
        out.append("  - " + desc)
    elif first_use_cid:
        out.append(header + " *(same source as `" + first_use_cid + "`)*")
    else:
        out.append(header)

    segs = ann.get("transcript_segments") or []
    if segs:
        body = _merge_transcript(segs)
        if body:
            for line in body.split("\n"):
                out.append("  > " + line)
    else:
        tt = (ann.get("transcript_text") or "").strip()
        if tt:
            out.append("  > " + tt)
    return out


def render_brief(sidecar: dict) -> str:
    lines = []
    act_id = sidecar.get("act_id", "act")
    act_label = sidecar.get("label", "")
    rng = sidecar.get("timeline_range_seconds") or [0, 0]
    runtime = rng[1] - rng[0] if rng else 0
    lines.append("# " + act_label + " -- Story Review Brief")
    lines.append("")
    lines.append("Runtime: **" + _fmt_ts(runtime) + "** * Beats: " +
                 str(len(sidecar.get("beats", []))) + " * Annotations: " +
                 str(len(sidecar.get("annotations", []))))
    lines.append("")
    lines.append("> One semantic description per source asset (first use only -- reuses note '(same source as cXXXX)'). Speaker-prefixed transcript snippets cover each clip's source window. Audio-spine markers flag stretches where the audio drives the cut and the V1 is decorative.")
    lines.append("")

    by_clip = {a.get("clip_id"): a for a in sidecar.get("annotations", []) if a.get("clip_id")}
    seen_assets = {}

    for beat in sidecar.get("beats", []):
        bid = beat.get("id")
        blabel = beat.get("label") or bid
        brng = beat.get("timeline_range_seconds") or [0, 0]
        bdur = brng[1] - brng[0] if brng else 0
        lines.append("## " + bid + " -- " + blabel + "  *(" + _fmt_ts(bdur) + ")*")
        lines.append("")

        for scene in beat.get("scenes", []):
            sid = scene.get("id")
            slabel = scene.get("label") or sid
            srng = scene.get("timeline_range_seconds") or [0, 0]
            sdur = srng[1] - srng[0] if srng else 0
            spine_mode = scene.get("spine_mode")
            lines.append("### " + sid + " -- " + slabel + "  *(" + _fmt_ts(srng[0]) + "-" + _fmt_ts(srng[1]) + ", " + _fmt_ts(sdur) + ")*")
            if scene.get("purpose"):
                lines.append("*" + scene["purpose"] + "*")
                lines.append("")
            if spine_mode == "audio":
                lines.append("> *Audio carries this scene; V1 sketches are decorative.*")
                lines.append("")

            scene_anns = [a for a in sidecar.get("annotations", []) if a.get("scene") == sid]
            spine_clips = [
                a for a in scene_anns
                if (a["key"].get("track") == "V1" and not a.get("_force_ride"))
                or a.get("audio_spine") or a.get("is_audio_spine")
            ]
            spine_clips.sort(key=lambda a: a["key"].get("timeline_start_frames") or 0)

            if not spine_clips:
                lines.append("*(no spine clips)*")
                lines.append("")
                continue

            for ann in spine_clips:
                lines.extend(_render_clip(ann, by_clip, seen_assets))
                lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def _safe_write(target: Path, text: str, retries: int = 3) -> None:
    payload = text.encode("utf-8")
    exp = hashlib.sha256(payload).hexdigest()
    for attempt in range(1, retries + 1):
        target.write_bytes(payload)
        actual = target.read_bytes()
        if hashlib.sha256(actual).hexdigest() == exp:
            return
        time.sleep(0.5 * attempt)
    raise RuntimeError("write verify failed after " + str(retries) + " attempts")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("sidecar")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    sidecar = json.loads(Path(args.sidecar).read_text(encoding="utf-8"))
    md = render_brief(sidecar)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    _safe_write(out, md)
    print("wrote " + str(out) + " (" + str(out.stat().st_size) + " bytes, " + str(len(md.splitlines())) + " lines, verified hash)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
