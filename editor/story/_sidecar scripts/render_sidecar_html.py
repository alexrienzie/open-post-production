#!/usr/bin/env python3
"""Render a v1 sidecar JSON as a self-contained HTML beat-review view.

Layout: scenes as collapsible
sections, V1 spine clips as nested collapsibles, ride-alongs in spine bodies,
with badges, classification chips, transcript snippets, speakers, annotations.

Data sources joined at render time:
  - sidecar (clip_id, scene assignment, annotations)
  - resolver index (timeline frame positions, masterclip_id, file_id)
  - editorial_catalog.sqlite (asset metadata, source_path, classifications, shoot_date)
  - per-asset transcript JSON (text segments + speaker per segment)

Usage:
    python render_sidecar_html.py <sidecar.json> --resolver <resolver.json>
        --catalog <editorial_catalog.sqlite> --transcripts <dir> --out <out.html>
"""
import argparse
import datetime
import html
import json
import os
import re
import sqlite3
import sys
from pathlib import Path


# ------- formatting helpers ----------------------------------------------------

def _fmt_secs(s):
    if s is None:
        return ""
    s = float(s)
    m, sec = divmod(s, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{int(h)}:{int(m):02d}:{sec:06.3f}"
    return f"{int(m)}:{sec:06.3f}"


def _fmt_dur(s):
    if s is None or s < 0:
        return ""
    return f"{s:.2f}s"


def _fmt_runtime(s):
    """Format a runtime span as MM:SS.ss (e.g. 2679.13 -> '44:39.13')."""
    if s is None or s < 0:
        return ""
    minutes = int(s // 60)
    secs = s - minutes * 60
    return f"{minutes:02d}:{secs:05.2f}"


def _shorten_source_path(p):
    """Replace the verbose source-root prefix (drive + top-level project folder)
    with '~\\' for compact display. Keep the original in titles/hovers so the
    real path is still recoverable."""
    if not p:
        return p
    return re.sub(r"^[A-Za-z]:[\\/][^\\/]+[\\/]", "~\\\\", p)


def _trunc(s, n):
    s = (s or "").replace("\n", " ").strip()
    if len(s) <= n:
        return s
    return s[: n - 1].rstrip() + "…"


def _filename(path):
    if not path:
        return ""
    return path.replace("/", "\\").split("\\")[-1]


# ------- data access -----------------------------------------------------------

class Ctx:
    def __init__(self, sidecar, resolver, con, transcripts_dir, dataset_catalog_dir=None):
        self.sc = sidecar
        self.resolver = resolver
        self.con = con
        self.transcripts_dir = transcripts_dir
        self.dataset_catalog_dir = dataset_catalog_dir  # path to dataset/assets/
        self.fps = float(sidecar.get("frame_rate", 24000/1001))
        self.r_by_key = {self._keyt(e["key"]): e for e in resolver["entries"]}
        self._asset_cache = {}
        self._transcript_cache = {}
        self._classifications_cache = {}  # aid -> {bucket, type}
        self._subject_cache = {}          # aid -> subject string (interviewee name etc.)
        # Beat-level overlays (Vogler, Hauge, …) and film-level frameworks
        # (Vonnegut shape, etc.) come from project_beats.json — a project-level
        # source-of-truth distinct from the per-Act sidecar.
        self._beat_overlays: dict[str, dict] = {}
        self._film_frameworks: dict = {}
        # Cross-cutting threads (e.g., Scientific Method for FOIA investigation)
        # come from editor/story/threads/*.json. Each scene can belong to N
        # threads; we build a reverse index scene_id -> [(thread, stage_label)].
        self._threads: list[dict] = []
        self._threads_by_scene: dict[str, list[tuple]] = {}
        try:
            story_root = Path(__file__).resolve().parent.parent.parent / "story"
            project_path = story_root / "project_beats.json"
            if project_path.exists():
                pb = json.loads(project_path.read_text(encoding="utf-8"))
                for b in pb.get("beats", []) or []:
                    bid = b.get("editor_beat_id")
                    if bid:
                        self._beat_overlays[bid] = b.get("overlays") or {}
                self._film_frameworks = pb.get("frameworks") or {}
            threads_dir = story_root / "threads"
            if threads_dir.exists():
                for p in sorted(threads_dir.glob("*.json")):
                    try:
                        t = json.loads(p.read_text(encoding="utf-8"))
                    except Exception:
                        continue
                    self._threads.append(t)
                    for st in t.get("stages") or []:
                        for scene_id in st.get("scenes") or []:
                            self._threads_by_scene.setdefault(scene_id, []).append(
                                (t, st)
                            )
        except Exception:
            pass

    def beat_overlays(self, beat_id: str) -> dict:
        # Sidecars sometimes use suffixed beat ids (e.g., 'b_10_remainder' when
        # a Save-the-Cat beat spans both treatment Act II and Act III). project_beats
        # carries the canonical 'b_10' entry. Fall back to the stripped form.
        if beat_id in self._beat_overlays:
            return self._beat_overlays[beat_id]
        for suffix in ("_remainder", "_partial", "_late", "_early", "_mid"):
            if beat_id.endswith(suffix):
                canonical = beat_id[: -len(suffix)]
                if canonical in self._beat_overlays:
                    return self._beat_overlays[canonical]
        return {}

    def film_frameworks(self) -> dict:
        return self._film_frameworks

    def thread_memberships(self, scene_id: str) -> list[tuple]:
        """Return list of (thread_dict, stage_dict) tuples for any threads
        that include this scene_id."""
        return self._threads_by_scene.get(scene_id, [])

    def all_threads(self) -> list[dict]:
        return self._threads

    def classifications(self, aid):
        """Pull asset_classifications from per-asset video.json / audio.json / stills.json
        (the catalog SQLite drops this field)."""
        if aid in self._classifications_cache:
            return self._classifications_cache[aid]
        if not aid or not self.dataset_catalog_dir:
            self._classifications_cache[aid] = {}
            return {}
        for sub, ext in (("video", ".video.json"), ("audio", ".audio.json"), ("stills", ".still.json")):
            p = self.dataset_catalog_dir / sub / f"{aid}{ext}"
            if p.exists():
                try:
                    r = json.loads(p.read_text(encoding="utf-8"))
                    cls = r.get("asset_classifications") or {}
                    self._classifications_cache[aid] = cls
                    return cls
                except Exception:
                    pass
        self._classifications_cache[aid] = {}
        return {}

    def subject(self, aid):
        """For interview clips, derive subject from transcript speakers (dominant non-interviewer)."""
        if aid in self._subject_cache:
            return self._subject_cache[aid]
        t = self.transcript(aid)
        if not t:
            self._subject_cache[aid] = ""
            return ""
        # Use transcript's speakers list (per-asset speaker rollup)
        speakers = t.get("speakers") or []
        # Known interviewer p_ids (the filmmakers — set to yours); main subject = the longest other speaker
        INTERVIEWERS = {"p_alex_rienzie", "p_connor_burkesmith"}
        best = None
        for s in speakers:
            pid = s.get("p_id") or ""
            if pid in INTERVIEWERS:
                continue
            dur = s.get("total_duration_sec") or 0
            if best is None or dur > best.get("total_duration_sec", 0):
                best = s
        if best:
            name = best.get("label_raw") or best.get("p_id") or ""
            # Convert p_id to a name if label_raw is generic
            if name.startswith("Speaker ") and best.get("p_id"):
                # Try a friendlier rendering from p_id (e.g., p_michelino_sunseri -> Michelino Sunseri)
                pid = best["p_id"].replace("p_", "").replace("_", " ").title()
                name = pid
            self._subject_cache[aid] = name
            return name
        # Fall back to transcript's analysis summary's first proper noun (cheap, but useful)
        summary = (t.get("analysis") or {}).get("summary_one_line", "")
        self._subject_cache[aid] = ""
        return ""

    @staticmethod
    def _keyt(k):
        return (k.get("asset_id"), k.get("source_in_frames"), k.get("source_out_frames"),
                k.get("timeline_start_frames"), k.get("track"))

    def asset(self, aid):
        if aid in self._asset_cache:
            return self._asset_cache[aid]
        if not aid:
            self._asset_cache[aid] = {}
            return {}
        row = self.con.execute("SELECT * FROM asset WHERE asset_id=?", (aid,)).fetchone()
        d = dict(row) if row else {}
        self._asset_cache[aid] = d
        return d

    def transcript(self, aid):
        if aid in self._transcript_cache:
            return self._transcript_cache[aid]
        if not aid:
            self._transcript_cache[aid] = None
            return None
        p = self.transcripts_dir / f"{aid}.transcript.json"
        if not p.exists():
            self._transcript_cache[aid] = None
            return None
        try:
            t = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            t = None
        self._transcript_cache[aid] = t
        return t

    def segments_overlap(self, aid, src_in_sec, src_out_sec):
        """Return list of (start, end, speaker_p_id, text) overlapping the source window."""
        t = self.transcript(aid)
        if not t:
            return []
        segs = t.get("segments") or t.get("transcript") or []
        out = []
        for s in segs:
            if not isinstance(s, dict):
                continue
            ss = s.get("start_sec") if s.get("start_sec") is not None else s.get("start")
            se = s.get("end_sec") if s.get("end_sec") is not None else s.get("end")
            text = s.get("text")
            if ss is None or se is None or text is None:
                continue
            if se < src_in_sec or ss > src_out_sec:
                continue
            spk = s.get("speaker") or s.get("speaker_p_id") or s.get("speaker_raw") or ""
            out.append((ss, se, spk, text.strip()))
        return out

    def speakers_in_clip(self, aid, src_in_sec, src_out_sec):
        """Aggregate speakers in this clip's source window. Group unidentified
        diarizations (p_id=None or empty) into a single "Unknown" bucket so the
        HTML doesn't show one chip per speaker_raw."""
        from collections import defaultdict
        secs_by_pid = defaultdict(float)
        for ss, se, pid, text in self.segments_overlap(aid, src_in_sec, src_out_sec):
            overlap_start = max(ss, src_in_sec)
            overlap_end = min(se, src_out_sec)
            # Normalize empty/None pid to a single bucket so "Unknown" doesn't fragment
            bucket = pid if pid else None
            secs_by_pid[bucket] += max(0, overlap_end - overlap_start)
        if not secs_by_pid:
            return []
        out = []
        for pid, secs in sorted(secs_by_pid.items(), key=lambda x: -(x[1])):
            if pid:
                # Resolve canonical name from people registry via catalog or transcript speakers list
                name = None
                # Try transcript speakers list first (rich label_raw)
                t = self.transcript(aid)
                if t:
                    for s in (t.get("speakers") or []):
                        if s.get("p_id") == pid:
                            # Prefer the canonical name from people.json mapping (label_raw is "Speaker N")
                            # Convert p_id slug to display name as fallback
                            break
                if not name:
                    name = pid.replace("p_", "").replace("_", " ").title()
                out.append({"name": name, "p_id": pid, "seconds": round(secs, 2)})
            else:
                out.append({"name": None, "p_id": None, "seconds": round(secs, 2)})
        return out

    def clip_geometry(self, annotation):
        """Return tl_start_f, tl_end_f for a clip via resolver, or fallback."""
        k = annotation["key"]
        rr = self.r_by_key.get(self._keyt(k))
        tl_start = k["timeline_start_frames"]
        if rr:
            tl_end = rr.get("timeline_end_frames")
        else:
            tl_end = None
        if tl_end is None and k["timeline_start_frames"] is not None and k["timeline_start_frames"] >= 0:
            tl_end = k["timeline_start_frames"] + (k["source_out_frames"] - k["source_in_frames"])
        return tl_start, tl_end


# ------- HTML CSS ports old layout ---------------------------------------------

CSS = """
:root {
  --bg: #0d1117; --bg-soft: #161b22; --bg-card: #1c2128; --bg-row: #1a1f26;
  --fg: #e6edf3; --fg-dim: #8b949e; --fg-faint: #6e7681;
  --border: #30363d;
  --accent: #d29922;
  --spine: #3fb950; --ride: #8b949e;
  --v1: #58a6ff; --v2: #79c0ff; --v3: #a5d6ff;
  --a1: #db61a2; --a2: #ff8fc9; --a3: #ffb3d8; --a4: #ffd4e8; --a5: #ffd4e8; --a6: #ffd4e8;
  --mono: ui-monospace, 'SF Mono', 'Cascadia Mono', Menlo, Consolas, monospace;
}
* { box-sizing: border-box; }
body { background: var(--bg); color: var(--fg); font: 14px/1.5 -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; margin: 0 auto; padding: 24px; max-width: 1500px; }
h1, h2, h3 { font-weight: 500; line-height: 1.25; }
h1 { font-size: 22px; margin: 0 0 4px; }
h2 { font-size: 17px; margin: 0; color: var(--fg); }
h3 { font-size: 13px; margin: 0 0 4px; color: var(--fg-dim); }
.muted { color: var(--fg-dim); }
.faint { color: var(--fg-faint); font-size: 12px; }
.mono { font-family: var(--mono); font-size: 12px; }
.empty { color: var(--fg-faint); font-style: italic; }

.badge { display: inline-block; padding: 1px 6px; border-radius: 3px; font-size: 11px; font-family: var(--mono); background: var(--bg-soft); border: 1px solid var(--border); color: var(--fg-dim); }
.badge.spine { color: var(--spine); border-color: var(--spine); }
.badge.ride { color: var(--ride); }
.badge.ph { color: var(--accent); border-color: var(--accent); }
.badge.aspine { color: var(--a1); border-color: var(--a1); background: rgba(219,97,162,0.1); }
.badge.mc { color: #ffcc66; border-color: #ffcc66; background: rgba(255,204,102,0.08); margin-left: 4px; }
.mc-angles { font-family: var(--mono); font-size: 10px; color: #b8a060; margin-top: 2px; padding-left: 4px; }
.mc-angles .mc-angles-label { color: #888; }
.mc-angles .mc-angle { display: inline-block; padding: 0 4px; margin-right: 4px; border: 1px solid #4a3a20; border-radius: 2px; color: #d4b878; }
.chip.chip-unknown { background: var(--bg-soft); color: var(--fg-faint); font-style: italic; border-color: var(--border); }
.cls-chip.cls-date { background: #1a2235; color: #79c0ff; }
.badge.t-V1 { color: var(--v1); border-color: var(--v1); }
.badge.t-V2 { color: var(--v2); border-color: var(--v2); }
.badge.t-V3 { color: var(--v3); border-color: var(--v3); }
.badge.t-A1 { color: var(--a1); border-color: var(--a1); }
.badge.t-A2 { color: var(--a2); border-color: var(--a2); }
.badge.t-A3 { color: var(--a3); border-color: var(--a3); }
.badge.t-A4, .badge.t-A5, .badge.t-A6 { color: var(--a4); border-color: var(--a4); }

.cls-chip { display: inline-block; padding: 1px 6px; border-radius: 2px; font-size: 10px; font-family: var(--mono); background: var(--bg-soft); color: var(--fg-dim); margin-right: 2px; }
.cls-bucket-in_house_priority_ht { background: #2a3a2d; color: #7ee0a0; }
.cls-bucket-in_house_priority { background: #2a3a2d; color: #7ee0a0; }
.cls-bucket-in_house_other { background: #2d2d2d; color: #b0b0b0; }
.cls-bucket-archival { background: #3a2a2d; color: #ff8fa0; }
.cls-type-interview { background: #2d3a4a; color: #79c0ff; }
.cls-type-verite { background: #3a3a2d; color: #d4d479; }
.cls-type-stock { background: #2d2d2d; color: #b0b0b0; }
.cls-type-broll { background: #2d3a2d; color: #79d479; }
.cls-type-news { background: #4a2d3a; color: #ff79c0; }

.date-chip { display: inline-block; padding: 1px 6px; border-radius: 2px; font-size: 10px; font-family: var(--mono); background: #1a2235; color: #79c0ff; margin-left: 6px; }

header.beat { border-bottom: 1px solid var(--border); padding-bottom: 16px; margin-bottom: 16px; }
header.beat .totals { display: flex; gap: 16px; flex-wrap: wrap; margin-top: 8px; font-size: 13px; color: var(--fg-dim); }
header.beat .totals b { color: var(--fg); font-weight: 500; }
header.beat .totals .group { padding-right: 16px; border-right: 1px solid var(--border); }
header.beat .totals .group:last-child { border-right: none; }
header.beat .anchor { background: var(--bg-soft); border-left: 3px solid var(--accent); padding: 8px 12px; margin-top: 10px; color: var(--fg); font-size: 13px; }

section.beat-section { border-top: 2px solid var(--accent); margin-top: 32px; padding-top: 12px; }
section.beat-section:first-of-type { margin-top: 16px; }
section.beat-section > header.beat-section-header { padding: 8px 0 12px; }
section.beat-section > header.beat-section-header h1 { font-size: 22px; margin: 0 0 6px; color: var(--fg); }
section.beat-section > header.beat-section-header .totals { display: flex; gap: 14px; flex-wrap: wrap; font-size: 12px; color: var(--fg-dim); margin-top: 4px; }
section.beat-section > header.beat-section-header .totals b { color: var(--fg); font-weight: 500; }
section.beat-section > header.beat-section-header .totals .group { padding-right: 14px; border-right: 1px solid var(--border); }
section.beat-section > header.beat-section-header .totals .group:last-child { border-right: none; }
section.beat-section > header.beat-section-header .anchor { background: var(--bg-soft); border-left: 3px solid var(--accent); padding: 6px 10px; margin-top: 8px; color: var(--fg); font-size: 12px; }
/* Per-beat framework overlays (Vogler, Hauge, …) shown as a chip row */
.beat-overlays { display: flex; flex-wrap: wrap; gap: 6px; margin: 4px 0 6px; font-size: 11px; color: var(--fg-dim); font-family: var(--mono); }
.overlay-chip { display: inline-block; padding: 1px 6px; border: 1px solid; border-radius: 2px; }
.overlay-chip .ov-label { font-weight: 600; opacity: 0.7; margin-right: 4px; text-transform: uppercase; font-size: 9px; letter-spacing: 0.5px; }
.overlay-chip.vogler { color: #a5d6ff; border-color: #1a4068; background: rgba(88,166,255,0.06); }
.overlay-chip.hauge { color: #ffd4e8; border-color: #6e2a4a; background: rgba(219,97,162,0.06); }
.overlay-chip.harmon, .overlay-chip.yorke, .overlay-chip.truby { color: #d4d4aa; border-color: #4a4a2a; background: rgba(200,200,140,0.05); }
/* Vonnegut emotional-shape sparkline (Layer 2) — at the top of Act HTML */
.vonnegut-shape { display: flex; align-items: center; gap: 12px; margin: 8px 0 4px; padding: 8px 10px; background: var(--bg-soft); border: 1px solid var(--border); border-radius: 3px; font-family: var(--mono); font-size: 11px; color: var(--fg-dim); }
.vonnegut-shape .vshape-label { color: var(--fg); font-weight: 600; min-width: 110px; }
.vonnegut-shape svg { display: block; }
.vonnegut-shape .vshape-curve { stroke: #79c0ff; stroke-width: 1.5; fill: none; }
.vonnegut-shape .vshape-zero { stroke: #444; stroke-width: 0.5; stroke-dasharray: 2,3; }
.vonnegut-shape .vshape-point { fill: #79c0ff; }
.vonnegut-shape .vshape-point.this-act { fill: #ff8c42; stroke: #ff8c42; stroke-width: 2; }
.vonnegut-shape .vshape-point-label { fill: #888; font-size: 8px; font-family: var(--mono); }
.vonnegut-shape .vshape-point-label.this-act { fill: #ff8c42; font-weight: 600; }
.vonnegut-shape .vshape-warning { color: #ffcc66; flex: 1; font-size: 10px; }
/* Cross-cutting thread chips (Layer 3 frameworks like Scientific Method) */
.thread-chips { display: inline-flex; flex-wrap: wrap; gap: 4px; margin-left: 8px; vertical-align: middle; }
.thread-chip { display: inline-block; padding: 1px 6px; font-family: var(--mono); font-size: 9px; border-radius: 2px; color: #c9b37e; border: 1px solid #5e4a20; background: rgba(255,204,102,0.07); }
.thread-chip .tc-label { color: #888; margin-right: 4px; text-transform: uppercase; letter-spacing: 0.4px; font-size: 8px; }
.thread-chip .tc-stage { font-weight: 600; color: #ffcc66; }
/* Track strip — mini Premiere-style timeline at the top of each beat.
   Color families match the V/A badge palette (V = blue, A = pink). Spine
   status is shown via a green left-edge accent rather than its own color
   family, so V1-vs-A-spine stays distinguishable from track family.       */
/* Mini-timeline: beat-runtime bar at the top of the act */
.mini-timeline { margin: 12px 0 20px; }
.mini-timeline .mt-label-row { display: flex; justify-content: space-between; font-size: 12px; color: var(--fg-dim); margin-bottom: 4px; }
.mini-timeline .mt-title { letter-spacing: 0.04em; text-transform: uppercase; }
.mini-timeline .mt-total { font-family: var(--mono); color: var(--fg); }
.mini-timeline .mt-bar { display: flex; width: 100%; height: 38px; border: 1px solid var(--border); border-radius: 4px; overflow: hidden; background: var(--bg-soft); }
.mini-timeline .mt-seg { position: relative; display: flex; flex-direction: column; justify-content: center; align-items: center; color: var(--fg); font-size: 11px; padding: 2px 4px; overflow: hidden; white-space: nowrap; border-right: 1px solid var(--border); box-sizing: border-box; }
.mini-timeline .mt-seg:last-child { border-right: none; }
.mini-timeline .mt-seg:hover { filter: brightness(1.15); cursor: default; }
.mini-timeline .mt-seglabel { font-weight: 600; max-width: 100%; overflow: hidden; text-overflow: ellipsis; }
.mini-timeline .mt-seglabel.mt-narrow { font-family: var(--mono); font-size: 10px; }
.mini-timeline .mt-runtime { font-family: var(--mono); font-size: 10px; color: var(--fg-dim); }
/* Beat tints (rotated palette by Save-the-Cat slot id) */
.mini-timeline .mt-b_06 { background: #1f3a5c; }
.mini-timeline .mt-b_07 { background: #3a2854; }
.mini-timeline .mt-b_08 { background: #2c4a38; }
.mini-timeline .mt-b_09 { background: #5a4020; }
.mini-timeline .mt-b_10 { background: #5c2a2a; }
.mini-timeline .mt-b_01, .mini-timeline .mt-b_02, .mini-timeline .mt-b_03, .mini-timeline .mt-b_04, .mini-timeline .mt-b_05 { background: #2c2c34; }
.mini-timeline .mt-b_11, .mini-timeline .mt-b_12, .mini-timeline .mt-b_13, .mini-timeline .mt-b_14, .mini-timeline .mt-b_15 { background: #2c2c34; }

.track-strip { position: relative; margin: 10px 0 8px; background: #0a0a0a; border: 1px solid #1f2428; border-radius: 3px; padding: 4px 6px; }
.track-strip .track-row { position: relative; height: 14px; margin: 1px 0; }
.track-strip .track-label { position: absolute; left: 0; top: 0; width: 28px; height: 14px; font-family: var(--mono); font-size: 9px; color: #888; text-align: right; padding-right: 4px; line-height: 14px; }
.track-strip .track-body { position: absolute; left: 34px; right: 4px; top: 0; height: 14px; background: #060606; }
.track-strip .clip-box { position: absolute; top: 0; height: 14px; font-family: var(--mono); font-size: 9px; text-align: left; line-height: 14px; overflow: hidden; white-space: nowrap; padding: 0 2px; border-radius: 1px; box-sizing: border-box; }
/* V family (blue, matches --v1/v2/v3 badge palette) */
.track-strip .clip-box.v-spine   { background: #1c3a5a; border: 1px solid #58a6ff; color: #d6e6f8; }
.track-strip .clip-box.v-overlay { background: #1c2e44; border: 1px solid #79c0ff; color: #b8d4ec; }
/* A family (pink, matches --a1/a2 badge palette) */
.track-strip .clip-box.a-spine   { background: #4a1e3a; border: 1px solid #db61a2; color: #fcd4e6; border-left-width: 3px; border-left-color: #3fb950; }
.track-strip .clip-box.a-ride    { background: #2a1620; border: 1px solid #8a3e68; color: #e0a8c4; }
.track-strip .clip-box.unassigned{ background: #3a2a2a; border: 1px solid #8b949e; color: #d4c8c8; }
.track-strip .scene-marker { position: absolute; top: 0; bottom: 0; width: 1px; background: #ffcc66; opacity: 0.5; pointer-events: none; }
.track-strip .scene-marker.first { background: #ff8c42; opacity: 0.8; }
.track-strip-legend { font-family: var(--mono); font-size: 9px; color: #666; margin-top: 2px; padding-left: 34px; }
.track-strip-divider { height: 2px; background: #2a3340; margin: 2px 4px 2px 34px; border-radius: 1px; }
/* Scene name banner above the V tracks */
.track-strip .scene-banner-row { position: relative; height: 14px; margin: 1px 0 3px; }
.track-strip .scene-banner-body { position: absolute; left: 34px; right: 4px; top: 0; height: 14px; }
.track-strip .scene-banner-box { position: absolute; top: 0; height: 14px; font-family: var(--mono); font-size: 9px; line-height: 14px; padding: 0 4px; overflow: hidden; white-space: nowrap; text-overflow: ellipsis; background: rgba(255,140,66,0.18); border: 1px solid #ff8c42; color: #ffcc99; border-radius: 2px; box-sizing: border-box; }
/* Virtual multicam V-angle rows (dashed = not placed; available via multicam source) */
.track-strip .clip-box.v-mc-angle { background: #1a2a3a; border: 1px dashed #79c0ff; color: #b8d4ec; opacity: 0.85; }
.track-strip .track-label.mc { color: #79c0ff; font-style: italic; }

details.scene { background: var(--bg-soft); border: 1px solid var(--border); border-radius: 6px; margin: 12px 0; overflow: hidden; }
details.scene > summary { cursor: pointer; padding: 14px 16px; list-style: none; display: flex; align-items: center; gap: 12px; }
details.scene > summary::-webkit-details-marker { display: none; }
details.scene > summary::before { content: "▸"; color: var(--fg-faint); font-size: 11px; flex: 0 0 12px; }
details.scene[open] > summary::before { content: "▾"; color: var(--fg); }
details.scene > summary:hover { background: var(--bg); }
details.scene .summary-text { flex: 1; }
details.scene .summary-meta { display: flex; gap: 10px; flex-wrap: wrap; font-size: 12px; color: var(--fg-dim); }
details.scene .pill { background: var(--bg); border: 1px solid var(--border); border-radius: 3px; padding: 1px 8px; }
details.scene .scene-body { padding: 0 16px 12px; }
details.scene .purpose { color: var(--fg); font-size: 13px; margin: 0 0 6px; }
details.scene .exp-note { color: var(--fg-dim); font-size: 12px; font-style: italic; margin: 0 0 8px; }

details.clip { background: var(--bg-card); border: 1px solid var(--border); border-radius: 4px; margin: 6px 0; overflow: hidden; }
details.clip > summary { cursor: pointer; padding: 8px 12px; list-style: none; display: grid; grid-template-columns: 110px 280px 110px 130px 1fr; gap: 12px; align-items: start; font-size: 12px; }
details.clip > summary::-webkit-details-marker { display: none; }
details.clip > summary:hover { background: var(--bg-row); }
details.clip .col-id { font-family: var(--mono); }
details.clip .col-id .mono { font-size: 12px; }
details.clip .col-src .file { font-weight: 500; }
details.clip .col-src .class { display: block; margin-top: 2px; font-size: 11px; }
details.clip .col-src .path { display: block; color: var(--fg-faint); font-size: 11px; font-family: var(--mono); margin-top: 2px; word-break: break-all; }
details.clip .col-times { font-family: var(--mono); font-size: 11px; color: var(--fg-dim); }
details.clip .col-times .beat-time { color: var(--accent); }
details.clip .col-tx .tx { display: block; color: var(--fg); font-style: italic; font-size: 12px; line-height: 1.4; margin-top: 4px; }
details.clip .col-tx-line { display: flex; align-items: center; gap: 4px; }

.speakers { display: flex; gap: 4px; flex-wrap: wrap; }
.chip { background: var(--bg-soft); border: 1px solid var(--border); border-radius: 10px; padding: 1px 8px; font-size: 11px; color: var(--fg); }
.chip-sec { color: var(--fg-faint); margin-left: 4px; }

.annots { display: flex; gap: 6px; flex-wrap: wrap; margin: 4px 0 6px; }
.annot { background: var(--bg-soft); border: 1px solid var(--border); border-radius: 3px; padding: 2px 8px; font-size: 11px; }
.annot-tag { color: var(--fg-faint); font-size: 9px; font-family: var(--mono); margin-right: 4px; letter-spacing: 0.5px; }
.annot-sub { color: var(--fg-dim); }
.annot.lt { border-left: 3px solid #79c0ff; }
.annot.loc { border-left: 3px solid #ffb066; }
.annot.date { border-left: 3px solid #d29922; }

.ride-list { padding: 0 12px 8px; border-top: 1px solid var(--border); }
.ride-row { display: grid; grid-template-columns: 110px 280px 110px 130px 1fr; gap: 12px; padding: 6px 0; border-bottom: 1px dashed var(--border); font-size: 12px; align-items: start; }
.ride-row:last-child { border-bottom: none; }
.ride-row.orphan { background: rgba(210, 153, 34, 0.05); }
"""


# ------- rendering -------------------------------------------------------------

def _classification_chips(ctx, aid):
    cls = ctx.classifications(aid)
    bucket = cls.get("bucket", "")
    asset_type = cls.get("type", "")
    chips = []
    if bucket:
        chips.append(f'<span class="cls-chip cls-bucket-{html.escape(bucket)}" title="bucket">{html.escape(bucket)}</span>')
    if asset_type:
        chips.append(f'<span class="cls-chip cls-type-{html.escape(asset_type)}" title="type">{html.escape(asset_type)}</span>')
    # Primary timeline date as a chip next to the type
    asset_row = ctx.asset(aid)
    ptd = asset_row.get("primary_timeline_date") if asset_row else None
    if ptd:
        chips.append(f'<span class="cls-chip cls-date" title="primary timeline date">{html.escape(ptd)}</span>')
    return " ".join(chips) if chips else '<span class="faint">—</span>'


def _speaker_chips(speakers):
    if not speakers:
        return ""
    chips = []
    for sp in speakers:
        nm = sp.get("name") or sp.get("p_id") or "unknown"
        secs = sp.get("seconds")
        pid = sp.get("p_id") or ""
        chips.append(
            f'<span class="chip" title="{html.escape(pid)}">{html.escape(nm)}'
            + (f' <span class="chip-sec">{secs:.1f}s</span>' if isinstance(secs, (int, float)) else '')
            + '</span>'
        )
    return '<div class="speakers">' + " ".join(chips) + '</div>'


def _annotations_html(ann):
    parts = []
    lt = ann.get("lower_third")
    if lt:
        nm = html.escape(lt.get("name") or "")
        ti = html.escape(lt.get("title") or "")
        parts.append(f'<span class="annot lt"><span class="annot-tag">LOWER-THIRD</span><b>{nm}</b>'
                     + (f' <span class="annot-sub">— {ti}</span>' if ti else '') + '</span>')
    loc = ann.get("location_title")
    if loc:
        pr = html.escape(loc.get("primary") or "")
        sec = html.escape(loc.get("secondary") or "")
        parts.append(f'<span class="annot loc"><span class="annot-tag">TITLE</span><b>{pr}</b>'
                     + (f' <span class="annot-sub">/ {sec}</span>' if sec else '') + '</span>')
    dt = ann.get("date_tracker")
    if dt:
        ds = html.escape(dt.get("date") or "")
        tm = html.escape(dt.get("time") or "")
        note = html.escape(dt.get("time_note") or "")
        parts.append(f'<span class="annot date"><span class="annot-tag">DATE</span>{ds}'
                     + (f' · {tm}' if tm else '')
                     + (f' <span class="annot-sub">({note})</span>' if note else '') + '</span>')
    return f'<div class="annots">{"".join(parts)}</div>' if parts else ""


def _clip_summary_cells(ctx, ann, beat_start_f):
    k = ann["key"]
    track = k["track"]
    aid = k["asset_id"]
    clip_id = ann.get("clip_id") or ""
    is_spine = (track == "V1")
    asset = ctx.asset(aid)
    src_in = k["source_in_frames"] / ctx.fps if k["source_in_frames"] is not None else None
    src_out = k["source_out_frames"] / ctx.fps if k["source_out_frames"] is not None else None
    src_dur = (src_out - src_in) if (src_in is not None and src_out is not None) else None
    tl_start_f, tl_end_f = ctx.clip_geometry(ann)
    seq_s = tl_start_f / ctx.fps if tl_start_f is not None and tl_start_f >= 0 else None
    seq_e = tl_end_f / ctx.fps if tl_end_f is not None and tl_end_f >= 0 else None
    beat_in = (tl_start_f - beat_start_f) / ctx.fps if (tl_start_f is not None and tl_start_f >= 0) else None
    beat_out = (tl_end_f - beat_start_f) / ctx.fps if (tl_end_f is not None and tl_end_f >= 0) else None

    src_path = asset.get("source_path") or ""
    fn = _filename(src_path) or ann.get("name") or "(unknown)"

    is_aspine = bool(ann.get("is_audio_spine"))
    pair_track = ann.get("_stereo_track_pair")
    track_badge_label = f"{track}+{pair_track[1:]}" if pair_track else track
    track_badge_title = (f"{track} (paired with {pair_track} — stereo pair, content "
                         f"identical to the merged annotation)") if pair_track else track
    is_multicam = bool(ann.get("is_multicam_ref"))
    id_html = (
        f'<span class="mono">{html.escape(clip_id)}</span><br>'
        f'<span class="badge t-{html.escape(track)}" title="{html.escape(track_badge_title)}">{html.escape(track_badge_label)}</span> '
        + ('<span class="badge spine">SPINE</span>' if is_spine
           else ('<span class="badge aspine">AUDIO-SPINE</span>' if is_aspine
                 else '<span class="badge ride">ride</span>'))
        + (' <span class="badge mc" title="Multicam — A-track ref into a nested source sequence; double-click in Premiere to see angles">MC</span>' if is_multicam else '')
    )
    src_html = (
        f'<span class="file" title="{html.escape(src_path)}">{html.escape(fn)}</span>'
        f'<span class="class">{_classification_chips(ctx, aid)}</span>'
        f'<span class="path" title="{html.escape(src_path)}">'
        f'{html.escape(_shorten_source_path(src_path))}</span>'
    )
    times_html = (
        f'<span>in <b>{_fmt_secs(src_in)}</b></span><br>'
        f'<span>out <b>{_fmt_secs(src_out)}</b></span><br>'
        f'<span class="faint">dur {_fmt_dur(src_dur)}</span>'
    )
    beat_line = (
        f'<br><span class="beat-time">beat <b>{_fmt_secs(beat_in)}</b> → {_fmt_secs(beat_out)}</span>'
        if beat_in is not None else ''
    )
    seq_html = (
        f'<span>seq <b>{_fmt_secs(seq_s)}</b></span><br>'
        f'<span>→ {_fmt_secs(seq_e)}</span>{beat_line}'
    )

    # Speakers via transcript overlap
    speakers = ctx.speakers_in_clip(aid, src_in or 0, src_out or 0) if aid else []
    speaker_html = _speaker_chips(speakers) or '<span class="faint">—</span>'
    # Date intentionally not repeated here — primary_timeline_date chip already
    # appears in classification chips alongside bucket/type (see _classification_chips).
    spk_html = f'<div class="col-tx-line">{speaker_html}</div>'

    # Transcript text — show in full; editor wants to read all overlapping
    # transcript content for used clips.
    segs = ctx.segments_overlap(aid, src_in or 0, src_out or 0) if aid else []
    tx = " ".join(s[3] for s in segs)
    tx_title = f"transcript: {tx}" if tx else ""
    tx_inner = f'&ldquo;{html.escape(tx)}&rdquo;' if tx else '<span class="empty">[no transcript]</span>'
    tx_html = f'<span class="tx" title="{html.escape(tx_title)}">{tx_inner}</span>'

    # Multicam video angles available in the source sequence (visible in Premiere
    # when the editor double-clicks the audio multicam clip).
    mc_angles = ann.get("multicam_v_angles") or []
    if mc_angles:
        chips = []
        for ang in mc_angles[:8]:
            fname = ang.get("name") or "?"
            aid_v = ang.get("asset_id") or ""
            tip = f"{fname} · {aid_v[:12]}"
            chips.append(f'<span class="mc-angle" title="{html.escape(tip)}">{html.escape(fname)}</span>')
        more = f' <span class="mc-angles-label">+{len(mc_angles)-8} more</span>' if len(mc_angles) > 8 else ""
        angles_html = (f'<div class="mc-angles">'
                       f'<span class="mc-angles-label">linked V angles ({len(mc_angles)}):</span> '
                       f'{"".join(chips)}{more}</div>')
        tx_html = tx_html + angles_html

    return id_html, src_html, times_html, seq_html, spk_html, tx_html


def _render_ride_row(ctx, ann, beat_start_f):
    id_, src, t1, t2, spk, tx = _clip_summary_cells(ctx, ann, beat_start_f)
    return (
        f'<div class="ride-row">'
        f'<div class="col-id">{id_}</div>'
        f'<div class="col-src">{src}</div>'
        f'<div class="col-times">{t1}</div>'
        f'<div class="col-times">{t2}</div>'
        f'<div class="col-tx">{spk}{tx}</div>'
        f'</div>'
    )


def _render_spine_with_rides(ctx, spine, rides, beat_start_f):
    id_, src, t1, t2, spk, tx = _clip_summary_cells(ctx, spine, beat_start_f)
    annots = _annotations_html(spine)
    rides_html = "".join(_render_ride_row(ctx, r, beat_start_f) for r in rides)
    body = f'<div class="ride-list">{rides_html}</div>' if rides else ''
    return (
        f'<details class="clip">'
        f'<summary>'
        f'<div class="col-id">{id_}</div>'
        f'<div class="col-src">{src}</div>'
        f'<div class="col-times">{t1}</div>'
        f'<div class="col-times">{t2}</div>'
        f'<div class="col-tx">{annots}{spk}{tx}</div>'
        f'</summary>'
        f'{body}'
        f'</details>'
    )


def _dedupe_stereo_pairs(annotations):
    """Collapse adjacent A-track stereo pairs (A1+A2, A3+A4, A5+A6, ...) where
    both clipitems share the same content key except track. Premiere often
    splits stereo audio into separate A1/A2 clipitems; the data is duplicated
    (same asset_id, src in/out, timeline start, transcript text). Keep the
    odd-track annotation (A1, A3, A5), drop the even-track twin (A2, A4, A6),
    and annotate the kept one with `_stereo_track_pair` for display."""
    # Group A-track annotations by (asset_id, src_in, src_out, tl_start)
    by_key: dict[tuple, list] = {}
    for a in annotations:
        k = a.get("key") or {}
        tr = k.get("track") or ""
        if not tr.startswith("A"):
            continue
        ck = (k.get("asset_id"), k.get("source_in_frames"),
              k.get("source_out_frames"), k.get("timeline_start_frames"))
        # Skip groups where any key field is None (can't reliably match)
        if any(x is None for x in ck):
            continue
        by_key.setdefault(ck, []).append(a)

    dropped_ids = set()
    for ck, anns in by_key.items():
        if len(anns) < 2:
            continue
        # Sort by track-number ascending so A1 comes before A2
        def _trk_num(a):
            try: return int((a.get("key") or {}).get("track", "A999")[1:])
            except ValueError: return 999
        anns_sorted = sorted(anns, key=_trk_num)
        # Walk pairs: A(2n-1) + A(2n)
        i = 0
        while i < len(anns_sorted) - 1:
            cur, nxt = anns_sorted[i], anns_sorted[i + 1]
            cur_n, nxt_n = _trk_num(cur), _trk_num(nxt)
            if cur_n % 2 == 1 and nxt_n == cur_n + 1:
                cur["_stereo_track_pair"] = nxt["key"]["track"]
                dropped_ids.add(id(nxt))
                i += 2
            else:
                i += 1
    return [a for a in annotations if id(a) not in dropped_ids]


def _group_clips(ctx, scene_annotations):
    """Split scene annotations into (spine, rides) groups.
    V1 clips are spines; A1 clips marked is_audio_spine (or audio_spine) are
    also spines. V1 clips marked _force_ride are demoted to rides (editorial
    override — used e.g. for trial sketches where the audio anchors the cut)."""
    # Dedupe stereo A-track pairs (A1+A2, A3+A4, etc.) before grouping.
    scene_annotations = _dedupe_stereo_pairs(list(scene_annotations))
    spines_v1 = [a for a in scene_annotations
                 if a["key"]["track"] == "V1" and not a.get("_force_ride")]
    spines_audio = [a for a in scene_annotations
                    if a.get("is_audio_spine") or a.get("audio_spine")]
    spines = sorted(spines_v1 + spines_audio,
                    key=lambda a: a["key"]["timeline_start_frames"] or 0)
    spine_ids = {id(s) for s in spines}
    rides = [a for a in scene_annotations if id(a) not in spine_ids]

    groups = []
    # Build spine timeline windows
    spine_windows = []
    for sp in spines:
        ts, te = ctx.clip_geometry(sp)
        spine_windows.append((ts, te, sp))
        groups.append((sp, []))

    # Assign each ride to spine whose timeline window contains it
    for ride in rides:
        rk = ride["key"]
        rts = rk["timeline_start_frames"]
        if rts is None or rts < 0:
            # -1 sentinel — assign to first spine with same asset_id, else first spine
            assigned = False
            for i, (ts, te, sp) in enumerate(spine_windows):
                if sp["key"]["asset_id"] == rk["asset_id"]:
                    groups[i][1].append(ride); assigned = True; break
            if not assigned and groups:
                groups[0][1].append(ride)
            continue
        # Find spine whose window contains rts
        for i, (ts, te, sp) in enumerate(spine_windows):
            if ts is not None and te is not None and ts <= rts < te:
                groups[i][1].append(ride); break
        else:
            # Fall through: attach to last spine before rts
            best = None
            for i, (ts, te, sp) in enumerate(spine_windows):
                if ts is not None and ts <= rts:
                    best = i
            if best is not None:
                groups[best][1].append(ride)
            elif groups:
                groups[0][1].append(ride)
    return groups


def _render_act_beat_timeline(sc) -> str:
    """Mini-timeline at the top of the Act HTML: each beat as a proportional
    horizontal bar segment, width = beat runtime / act runtime. Labels show
    beat id, name, and runtime; hover for full detail."""
    beats = sc.get("beats") or []
    if not beats:
        return ""
    fps = float(sc.get("frame_rate") or (24000/1001))
    total = 0
    spans = []
    for b in beats:
        rng = b.get("timeline_range_frames") or [0, 0]
        if not rng or len(rng) < 2:
            continue
        n = max(0, rng[1] - rng[0])
        total += n
        spans.append((b, n))
    if total <= 0:
        return ""
    # Each beat gets a CSS color via class b-<id>; fall back to accent.
    parts = ['<div class="mini-timeline">']
    parts.append('<div class="mt-label-row"><span class="mt-title">Act runtime</span> <span class="mt-total">'
                 f'{_fmt_runtime(total/fps)}</span></div>')
    parts.append('<div class="mt-bar">')
    for b, n in spans:
        pct = 100.0 * n / total
        secs_b = n / fps
        bid = b.get("id") or ""
        label = b.get("label") or bid
        tip = f"{bid} — {label}  ·  {_fmt_runtime(secs_b)}  ·  frames {b.get('timeline_range_frames')}"
        # Show inline label only if segment is wide enough (≥8%); else hover-only.
        inline = (f'<span class="mt-seglabel">{html.escape(bid)} · {html.escape(label)}</span>'
                  if pct >= 8 else f'<span class="mt-seglabel mt-narrow">{html.escape(bid)}</span>')
        parts.append(f'<div class="mt-seg mt-{html.escape(bid)}" style="width:{pct:.3f}%" '
                     f'title="{html.escape(tip)}">{inline}'
                     f'<span class="mt-runtime">{_fmt_runtime(secs_b)}</span></div>')
    parts.append('</div></div>')
    return "".join(parts)


def _render_vonnegut_sparkline(ctx, this_act_beat_ids: list[str]) -> str:
    """Inline SVG sparkline of the Vonnegut emotional shape across the ladder's 15
    beats, with the current Act's beats highlighted. Reads from project_beats.json
    `frameworks.vonnegut_shape.valence_per_beat`. Returns "" if no data."""
    fw = ctx.film_frameworks().get("vonnegut_shape") or {}
    valences = fw.get("valence_per_beat") or {}
    if not valences:
        return ""

    # Stable ordering: by beat id sort (b_01..b_15). Use natural numeric sort.
    def _beat_num(bid):
        try:
            return int(bid.split("_")[1])
        except Exception:
            return 999
    beat_ids = sorted(valences.keys(), key=_beat_num)
    if not beat_ids:
        return ""

    vmin = fw.get("scale_min", -3)
    vmax = fw.get("scale_max", 3)
    title = fw.get("title", "Vonnegut shape")
    warning = fw.get("warning", "")

    # SVG geometry
    width = 360
    height = 64
    margin_x = 8
    margin_y = 6
    inner_w = width - 2 * margin_x
    inner_h = height - 2 * margin_y
    n = len(beat_ids)
    xs = [margin_x + (i / max(1, n - 1)) * inner_w for i in range(n)]

    def _y(v: float) -> float:
        # Higher valence = higher on screen (smaller y)
        t = (v - vmin) / (vmax - vmin) if vmax > vmin else 0.5
        return margin_y + (1 - t) * inner_h

    ys = [_y(valences[bid]) for bid in beat_ids]
    points = " ".join(f"{xs[i]:.1f},{ys[i]:.1f}" for i in range(n))
    zero_y = _y(0)

    # Determine "highlight" set — beats in this act, with stripped suffixes
    def _strip_suffix(bid):
        for suffix in ("_remainder", "_partial", "_late", "_early", "_mid"):
            if bid.endswith(suffix):
                return bid[: -len(suffix)]
        return bid
    highlight = {_strip_suffix(b) for b in this_act_beat_ids}

    pts_svg = []
    for i, bid in enumerate(beat_ids):
        is_act = bid in highlight
        cls = "vshape-point this-act" if is_act else "vshape-point"
        tip = f"{bid}: {valences[bid]:+d}"
        r = 3 if is_act else 2
        pts_svg.append(
            f'<circle class="{cls}" cx="{xs[i]:.1f}" cy="{ys[i]:.1f}" r="{r}">'
            f'<title>{html.escape(tip)}</title></circle>'
        )
        # Inline beat label below the lowest point row, only on highlighted beats
        if is_act:
            pts_svg.append(
                f'<text class="vshape-point-label this-act" '
                f'x="{xs[i]:.1f}" y="{height - 1:.1f}" text-anchor="middle">'
                f'{html.escape(bid)}</text>'
            )

    svg = (
        f'<svg width="{width}" height="{height + 10}" viewBox="0 0 {width} {height + 10}">'
        f'<line class="vshape-zero" x1="{margin_x}" y1="{zero_y:.1f}" x2="{width - margin_x}" y2="{zero_y:.1f}"/>'
        f'<polyline class="vshape-curve" points="{points}"/>'
        + "".join(pts_svg)
        + '</svg>'
    )

    warning_html = (f'<span class="vshape-warning" title="{html.escape(warning)}">⚠ {html.escape(warning[:100])}'
                    + ('…' if len(warning) > 100 else '') + '</span>'
                    if warning else '')

    return (f'<div class="vonnegut-shape">'
            f'<span class="vshape-label">Vonnegut · {html.escape(title)}</span>'
            f'{svg}{warning_html}</div>')


def _render_track_strip(ctx, beat):
    """Mini Premiere-style timeline at the top of a beat section.

    One row per non-empty track (V1, V2, …, A1, A2, …). Each clipitem is an
    absolute-positioned box, position + width are percentages of the beat's
    range. Box label = clip_id (c####/a####). Scene boundaries drawn as
    vertical orange lines so the editor can see how scenes carve the strip.
    """
    rng_f = beat.get("timeline_range_frames") or [0, 0]
    b_lo, b_hi = rng_f
    b_span = b_hi - b_lo
    if b_span <= 0:
        return ""  # zero-length placeholder beat — nothing to draw

    bid = beat.get("id")
    beat_anns = [a for a in ctx.sc["annotations"] if a.get("beat") == bid]
    if not beat_anns:
        return ""

    # Group annotations by track. Preserve a stable order: V1..Vn ascending,
    # then A1..An ascending; ignore empty tracks.
    from collections import defaultdict
    by_track: dict[str, list[dict]] = defaultdict(list)
    for a in beat_anns:
        track = (a.get("key") or {}).get("track") or ""
        if track:
            by_track[track].append(a)

    def _sort_key(track_name):
        # Premiere-style ordering: V tracks high→low (V5..V1), then A tracks
        # low→high (A1..An). V1 and A1 end up adjacent at the visual centre.
        try:
            num = int(track_name[1:])
        except ValueError:
            num = 999
        if track_name.startswith("V"):
            return (0, -num)  # V5 sorts before V1
        return (1, num)        # A1 sorts before A2

    tracks_ordered = sorted(by_track.keys(), key=_sort_key)

    # Virtual multicam V rows. For each multicam audio annotation, expose the
    # underlying source-sequence V-track angles as availability windows so the
    # track strip reflects "video coverage IS available throughout, just not
    # placed on the outer V tracks." Per-angle row, deduped by asset_id.
    from collections import defaultdict
    mc_angle_windows: dict[str, list[tuple[float, float, dict]]] = defaultdict(list)
    mc_angle_name: dict[str, str] = {}
    for a in beat_anns:
        if not a.get("is_multicam_ref"):
            continue
        t = a.get("timing") or {}
        ts, te = t.get("timeline_start_sec"), t.get("timeline_end_sec")
        if ts is None or te is None:
            continue
        for ang in a.get("multicam_v_angles") or []:
            aid = ang.get("asset_id")
            if not aid:
                continue
            mc_angle_windows[aid].append((ts, te, ang))
            mc_angle_name[aid] = ang.get("name") or "?"
    # Sort angles by total coverage descending (most-covering = first / closest to V1)
    mc_angles_sorted = sorted(
        mc_angle_windows.keys(),
        key=lambda aid: -sum(e - s for s, e, _ in mc_angle_windows[aid]),
    )

    # Insert virtual mc rows just before the A section so they sit between V1
    # and the V→A divider (the "available video coverage" zone).
    a_start_idx = next(
        (i for i, t in enumerate(tracks_ordered) if t.startswith("A")),
        len(tracks_ordered),
    )
    # We splice synthetic track names "mc1", "mc2", ... in at a_start_idx
    mc_track_names = [f"mc{i+1}" for i in range(len(mc_angles_sorted))]
    tracks_ordered = (tracks_ordered[:a_start_idx] +
                      mc_track_names +
                      tracks_ordered[a_start_idx:])
    a_start_idx += len(mc_track_names)
    # Map mc_track_name -> aid for later
    mc_track_to_aid = dict(zip(mc_track_names, mc_angles_sorted))

    parts = ['<div class="track-strip">']

    # Scene name banner at the top — one labeled bar per scene, spanning its range
    scenes = beat.get("scenes") or []
    if scenes:
        parts.append('<div class="scene-banner-row"><div class="scene-banner-body">')
        for s in scenes:
            # Scenes flagged content_removed carry timeline_range_frames: null; skip them
            # on the strip — they're informational only (no on-timeline geometry).
            srng = s.get("timeline_range_frames") or [b_lo, b_lo]
            sf_lo, sf_hi = srng
            if sf_hi <= sf_lo:
                continue
            left = max(0, (sf_lo - b_lo) / b_span * 100)
            width = max(0.2, (sf_hi - sf_lo) / b_span * 100)
            if left + width > 100:
                width = max(0.2, 100 - left)
            label = s.get("label") or s.get("id") or ""
            parts.append(
                f'<div class="scene-banner-box" '
                f'style="left: {left:.3f}%; width: {width:.3f}%" '
                f'title="{html.escape(s.get("id") or "")} · {html.escape(label)}">'
                f'{html.escape(label)}</div>'
            )
        parts.append('</div></div>')

    # Scene boundary markers (drawn full-height across the strip)
    scenes = beat.get("scenes") or []
    scene_marker_html = []
    for i, s in enumerate(scenes):
        s_lo = (s.get("timeline_range_frames") or [b_lo, b_lo])[0]
        if s_lo <= b_lo or s_lo >= b_hi:
            continue
        pct = (s_lo - b_lo) / b_span * 100
        # Scale percent into the body width: body starts at 34px-from-left, but
        # since the marker sits inside .track-strip (which contains all rows),
        # we approximate by overlaying inside each row's body via inline style
        # in the loop below. Keeping markers per-row keeps the layout simple.
        scene_marker_html.append((pct, s.get("label") or s.get("id") or "?"))

    for ti, track in enumerate(tracks_ordered):
        if ti == a_start_idx and ti > 0:
            parts.append('<div class="track-strip-divider"></div>')

        is_mc_row = track in mc_track_to_aid
        if is_mc_row:
            aid = mc_track_to_aid[track]
            ang_name = mc_angle_name.get(aid, "?")
            label_html = (
                f'<div class="track-label mc" title="multicam V-angle: '
                f'{html.escape(ang_name)} ({aid[:12]})">{html.escape(track)}</div>'
            )
            parts.append('<div class="track-row">')
            parts.append(label_html)
            parts.append('<div class="track-body">')

            # Scene boundary lines
            for pct, slabel in scene_marker_html:
                cls = "scene-marker first" if pct < 0.01 else "scene-marker"
                parts.append(f'<div class="{cls}" style="left: {pct:.2f}%" title="scene: {html.escape(slabel)}"></div>')

            # Merge overlapping windows for this angle (avoid visual duplication)
            windows = sorted(mc_angle_windows[aid], key=lambda x: x[0])
            merged: list[tuple[float, float]] = []
            for s, e, _ in windows:
                if merged and s <= merged[-1][1] + 0.5:
                    merged[-1] = (merged[-1][0], max(merged[-1][1], e))
                else:
                    merged.append((s, e))

            for ts, te in merged:
                tlsf = int(round(ts * ctx.fps))
                tlef = int(round(te * ctx.fps))
                left_pct = max(0.0, (tlsf - b_lo) / b_span * 100)
                width_pct = max(0.15, (tlef - tlsf) / b_span * 100)
                if left_pct + width_pct > 100:
                    width_pct = max(0.15, 100 - left_pct)
                short = (ang_name[:14] + "…") if len(ang_name) > 15 else ang_name
                title = f"{track} · {ang_name} · {ts:.2f}-{te:.2f}s · available via multicam (not placed)"
                parts.append(
                    f'<div class="clip-box v-mc-angle" '
                    f'style="left: {left_pct:.3f}%; width: {width_pct:.3f}%" '
                    f'title="{html.escape(title)}">{html.escape(short)}</div>'
                )

            parts.append('</div></div>')  # track-body, track-row
            continue

        clips = by_track[track]
        clips.sort(key=lambda a: (a.get("timing") or {}).get("timeline_start_sec") or 0)

        parts.append('<div class="track-row">')
        parts.append(f'<div class="track-label">{html.escape(track)}</div>')
        parts.append('<div class="track-body">')

        # Scene boundary lines inside this row
        for pct, slabel in scene_marker_html:
            cls = "scene-marker first" if pct < 0.01 else "scene-marker"
            parts.append(f'<div class="{cls}" style="left: {pct:.2f}%" title="scene: {html.escape(slabel)}"></div>')

        for a in clips:
            t = a.get("timing") or {}
            ts = t.get("timeline_start_sec")
            te = t.get("timeline_end_sec")
            if ts is None or te is None:
                continue
            tlsf = int(round(ts * ctx.fps))
            tlef = int(round(te * ctx.fps))
            left_pct = max(0.0, (tlsf - b_lo) / b_span * 100)
            width_pct = max(0.15, (tlef - tlsf) / b_span * 100)
            # Clamp so a clip overrunning the beat (rare) doesn't break layout
            if left_pct + width_pct > 100:
                width_pct = max(0.15, 100 - left_pct)

            cid = a.get("clip_id") or "?"
            kind = track[0]  # V or A
            is_spine = bool(a.get("audio_spine"))
            is_force = bool(a.get("_force_ride"))
            if kind == "V":
                css_cls = "v-spine" if track == "V1" else "v-overlay"
            else:  # A
                css_cls = "a-spine" if is_spine and not is_force else "a-ride"

            fn = (a.get("asset") or {}).get("filename") or "?"
            title = f"{cid} · {track} · {fn} · {ts:.2f}-{te:.2f}s"
            parts.append(
                f'<div class="clip-box {css_cls}" '
                f'style="left: {left_pct:.3f}%; width: {width_pct:.3f}%" '
                f'title="{html.escape(title)}">{html.escape(cid)}</div>'
            )

        parts.append('</div></div>')  # track-body, track-row

    n_mc_rows = len(mc_track_names)
    legend_extra = (f' · {n_mc_rows} multicam V-angle availability row(s) (dashed)'
                    if n_mc_rows else '')
    parts.append('<div class="track-strip-legend">'
                 f'{len(tracks_ordered) - n_mc_rows} placed tracks{legend_extra} · '
                 f'{len(scenes)} scene(s) · hover boxes for clip details</div>')
    parts.append('</div>')  # track-strip
    return "\n".join(parts)


def _render_beat_section(ctx, beat):
    """v2 only: render a beat as a non-collapsible section containing its
    scenes (collapsible) + per-beat unassigned annotations."""
    from collections import Counter
    bid = beat.get("id")
    label = beat.get("label", "")
    rng_f = beat.get("timeline_range_frames", [0, 0])
    rng_s = beat.get("timeline_range_seconds", [0, 0])
    runtime = (rng_s[1] - rng_s[0]) if rng_s and len(rng_s) >= 2 else 0
    scenes = beat.get("scenes", [])

    # Per-beat annotation counts
    beat_anns = [a for a in ctx.sc["annotations"] if a.get("beat") == bid]
    beat_track_counts = Counter(a["key"]["track"] for a in beat_anns)
    n_v1 = beat_track_counts.get("V1", 0)
    n_audio = sum(beat_track_counts[t] for t in beat_track_counts if t.startswith("A"))

    parts = []
    parts.append(f'<section class="beat-section" id="{html.escape(bid)}">')
    parts.append('<header class="beat-section-header">')
    parts.append(f'<h1>{html.escape(bid)} — {html.escape(label)}</h1>')
    parts.append('<div class="totals">')
    parts.append(f'<div class="group">runtime <b>{_fmt_runtime(runtime)}</b></div>')
    parts.append(f'<div class="group">scenes <b>{len(scenes)}</b></div>')
    parts.append(f'<div class="group">V1 <b>{n_v1}</b> · audio <b>{n_audio}</b></div>')
    parts.append(f'<div class="group">timeline <b>{_fmt_secs(rng_s[0])} → {_fmt_secs(rng_s[1])}</b></div>')
    parts.append('</div>')
    if beat.get("boundary_anchors", {}).get("description"):
        parts.append(f'<div class="anchor">{html.escape(beat["boundary_anchors"]["description"])}</div>')

    # Framework overlays (Vogler, Hauge, …) sourced from project_beats.json.
    # Each entry of `overlays` is either a plain string (e.g., "Crossing the
    # First Threshold") or a dict (e.g., {stage_id, name, movement}). We render
    # each as a labeled chip in framework order.
    overlays = ctx.beat_overlays(bid)
    overlay_chips = []
    for fw_key in ("vogler", "hauge", "harmon", "yorke", "truby"):
        ov = overlays.get(fw_key)
        if not ov:
            continue
        if isinstance(ov, dict):
            text = ov.get("name") or ov.get("stage_id") or ""
            movement = ov.get("movement")
            tip = f"{fw_key} · {text}" + (f" ({movement})" if movement else "")
        else:
            text = str(ov)
            tip = f"{fw_key} · {text}"
        if not text:
            continue
        overlay_chips.append(
            f'<span class="overlay-chip {html.escape(fw_key)}" title="{html.escape(tip)}">'
            f'<span class="ov-label">{html.escape(fw_key)}</span>{html.escape(text)}</span>'
        )
    if overlay_chips:
        parts.append('<div class="beat-overlays">' + "".join(overlay_chips) + '</div>')

    parts.append('</header>')

    # Track strip: mini Premiere-style timeline (V/A boxes labeled with clip_id)
    strip_html = _render_track_strip(ctx, beat)
    if strip_html:
        parts.append(strip_html)

    for sc_def in scenes:
        parts.append(_render_scene(ctx, sc_def, open_default=False, beat_start_f=rng_f[0] if rng_f else 0))

    beat_unassigned = [a for a in beat_anns if a.get("scene") is None]
    if beat_unassigned:
        parts.append(f'<details class="scene"><summary><div class="summary-text"><h2>Unassigned in {html.escape(bid)} ({len(beat_unassigned)})</h2><h3 class="mono">(no scene)</h3></div></summary>')
        parts.append(f'<div class="scene-body"><p class="exp-note">Annotations in {html.escape(bid)} not yet assigned to a scene.</p>')
        for ann in beat_unassigned:
            parts.append(_render_ride_row(ctx, ann, rng_f[0] if rng_f else 0))
        parts.append('</div></details>')

    parts.append('</section>')
    return "\n".join(parts)


def _render_scene(ctx, scene, open_default, beat_start_f=0):
    sid = scene["id"]
    scene_anns = [a for a in ctx.sc["annotations"] if a.get("scene") == sid]
    groups = _group_clips(ctx, scene_anns)
    spine_n = len(groups)
    ride_n = sum(len(r) for _, r in groups)
    from collections import Counter
    track_counts = Counter(a["key"]["track"] for a in scene_anns)
    tracks_html = " · ".join(f'<span class="badge t-{t}">{t}:{n}</span>' for t, n in sorted(track_counts.items()))
    rng_f = scene.get("timeline_range_frames") or [0, 0]
    runtime = (rng_f[1] - rng_f[0]) / ctx.fps if rng_f else 0
    exp_mapping = scene.get("expectation_mapping") or []
    rng_s = scene.get("timeline_range_seconds") or [0, 0]

    clip_html = "".join(_render_spine_with_rides(ctx, sp, rides, beat_start_f) for sp, rides in groups)

    pills = [
        f'<span class="pill">runtime <b>{_fmt_runtime(runtime)}</b></span>',
        f'<span class="pill">spine <b>{spine_n}</b> · ride <b>{ride_n}</b></span>',
        f'<span class="pill">timeline <b>{_fmt_secs(rng_s[0])} → {_fmt_secs(rng_s[1])}</b></span>',
    ]
    pills += [f'<span class="pill">{html.escape(e)}</span>' for e in exp_mapping]
    pills_html = " ".join(pills)

    # Cross-cutting thread membership chips (Layer 3 frameworks) — DISABLED.
    # Re-enable by restoring the chip-building block below; data is still
    # available via ctx.thread_memberships(sid).
    thread_chips_html = ""

    return (
        f'<details class="scene" {"open" if open_default else ""}>'
        f'<summary>'
        f'<div class="summary-text">'
        f'<h2>{html.escape(scene.get("label") or sid)}{thread_chips_html}</h2>'
        f'<h3 class="mono">{html.escape(sid)}</h3>'
        f'</div>'
        f'<div class="summary-meta">{pills_html}<br>{tracks_html}</div>'
        f'</summary>'
        f'<div class="scene-body">'
        + (f'<p class="purpose">{html.escape(scene.get("purpose", ""))}</p>' if scene.get("purpose") else "")
        + clip_html
        + '</div></details>'
    )


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("sidecar")
    ap.add_argument("--resolver", required=True)
    ap.add_argument("--catalog", required=True)
    ap.add_argument("--transcripts", required=True)
    ap.add_argument("--dataset-catalog", required=True, help="path to dataset/assets/ (for asset_classifications)")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    sc = json.loads(Path(args.sidecar).read_text(encoding="utf-8"))
    resolver = json.loads(Path(args.resolver).read_text(encoding="utf-8"))
    con = sqlite3.connect(args.catalog)
    con.row_factory = sqlite3.Row
    ctx = Ctx(sc, resolver, con, Path(args.transcripts), Path(args.dataset_catalog))

    # Refresh timestamp: use sidecar mtime (latest content change). The HTML
    # is just a view of that content, so the sidecar's freshness is what the
    # editor cares about. Fall back to "now" if mtime is unreadable.
    try:
        _mtime = os.path.getmtime(args.sidecar)
        refresh_dt = datetime.datetime.fromtimestamp(_mtime)
    except OSError:
        refresh_dt = datetime.datetime.now()
    refresh_str = refresh_dt.strftime("%Y-%m-%d %H:%M")

    from collections import Counter
    schema = sc.get("schema_version", 1)

    out = []
    out.append('<!DOCTYPE html><html><head>')
    if schema >= 2:
        out.append(f'<title>{html.escape(sc.get("act_id", "act"))} — {html.escape(sc.get("label", ""))}</title>')
    else:
        out.append(f'<title>{html.escape(sc.get("beat_id", "beat"))} — {html.escape(sc.get("label", ""))}</title>')
    out.append(f'<style>{CSS}</style></head><body>')

    if schema >= 2:
        # ===== v2: Act-scoped =====
        track_counts = Counter(a["key"]["track"] for a in sc["annotations"])
        n_spine = track_counts.get("V1", 0)
        n_audio = sum(track_counts[t] for t in track_counts if t.startswith("A"))
        rng = sc.get("timeline_range_seconds") or [0, 0]
        runtime = rng[1] - rng[0]

        out.append('<header class="beat">')
        out.append(f'<h1>{html.escape(sc.get("act_id", "act"))} — {html.escape(sc.get("label", ""))}</h1>')
        out.append(f'<div class="muted mono">{html.escape(sc.get("xml_source", ""))}</div>')
        out.append(f'<div class="muted mono">refreshed {html.escape(refresh_str)}</div>')
        out.append('<div class="totals">')
        out.append(f'<div class="group">runtime <b>{_fmt_runtime(runtime)}</b></div>')
        out.append(f'<div class="group">beats <b>{len(sc.get("beats", []))}</b> · scenes <b>{sum(len(b.get("scenes", [])) for b in sc.get("beats", []))}</b></div>')
        out.append(f'<div class="group">V1 spine <b>{n_spine}</b> · audio <b>{n_audio}</b></div>')
        tracks_html = " · ".join(f'<span class="badge t-{t}">{t}:{n}</span>' for t, n in sorted(track_counts.items()))
        out.append(f'<div class="group">tracks {tracks_html}</div>')
        out.append('</div>')
        # Vonnegut sparkline DISABLED. Re-enable by restoring
        # _render_vonnegut_sparkline call here.
        out.append('</header>')

        # Mini-timeline: beat runtime as proportional horizontal bar segments.
        mt_html = _render_act_beat_timeline(sc)
        if mt_html:
            out.append(mt_html)

        for beat in sc.get("beats", []):
            out.append(_render_beat_section(ctx, beat))

    else:
        # ===== v1: per-beat =====
        track_counts = Counter(a["key"]["track"] for a in sc["annotations"])
        n_spine = track_counts.get("V1", 0)
        n_overlay = sum(track_counts[t] for t in track_counts if t.startswith("V") and t != "V1")
        n_audio = sum(track_counts[t] for t in track_counts if t.startswith("A"))
        rng = sc.get("timeline_range_seconds") or [0, 0]
        runtime = rng[1] - rng[0]

        out.append('<header class="beat">')
        out.append(f'<h1>{html.escape(sc["beat_id"])} — {html.escape(sc.get("label", ""))}</h1>')
        out.append(f'<div class="muted mono">{html.escape(sc.get("xml_source", ""))}</div>')
        out.append(f'<div class="muted mono">refreshed {html.escape(refresh_str)}</div>')
        out.append('<div class="totals">')
        out.append(f'<div class="group">runtime <b>{_fmt_runtime(runtime)}</b></div>')
        out.append(f'<div class="group">scenes <b>{len(sc.get("scenes", []))}</b></div>')
        out.append(f'<div class="group">V1 spine <b>{n_spine}</b> · V2/V3 overlays <b>{n_overlay}</b> · audio <b>{n_audio}</b></div>')
        tracks_html = " · ".join(f'<span class="badge t-{t}">{t}:{n}</span>' for t, n in sorted(track_counts.items()))
        out.append(f'<div class="group">tracks {tracks_html}</div>')
        out.append('</div>')
        if sc.get("boundary_anchors", {}).get("description"):
            out.append(f'<div class="anchor">{html.escape(sc["boundary_anchors"]["description"])}</div>')
        out.append('</header>')

        for sc_def in sc.get("scenes", []):
            out.append(_render_scene(ctx, sc_def, open_default=False))

        unassigned = [a for a in sc["annotations"] if a.get("scene") is None]
        if unassigned:
            out.append(f'<details class="scene"><summary><div class="summary-text"><h2>Unassigned annotations ({len(unassigned)})</h2><h3 class="mono">(no scene)</h3></div></summary>')
            out.append('<div class="scene-body"><p class="exp-note">Annotations without a scene assignment.</p>')
            for ann in unassigned:
                out.append(_render_ride_row(ctx, ann, 0))
            out.append('</div></details>')

    out.append('</body></html>')
    Path(args.out).write_text("\n".join(out), encoding="utf-8")
    print(f"wrote {args.out} ({Path(args.out).stat().st_size} bytes)")
    if schema >= 2:
        n_scenes = sum(len(b.get("scenes", [])) for b in sc.get("beats", []))
        print(f"  beats: {len(sc.get('beats', []))}, total scenes: {n_scenes}, annotations: {len(sc['annotations'])}")
    else:
        print(f"  scenes: {len(sc.get('scenes', []))}, annotations: {len(sc['annotations'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
