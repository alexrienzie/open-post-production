#!/usr/bin/env python3
"""Two-column scene view — forked from render_sidecar_html.py.
v2: split content per side, add tags + per-beat strip.

For each scene's drop-down, two parallel vertical columns:
  - AUDIO  (left)  — dominant A-track for the scene  → speakers + transcript
  - VIDEO  (right) — dominant V-track for the scene  → visual summary (no transcript;
                                                       the audio side already carries it)
"Dominant" = the track with the most total source-seconds of content in this
scene (auto-resolves to A1 / V1 for most scenes; adapts if e.g. A3 carries
the spine). Stereo A1+A2 pairs with identical content collapse to one row.

Every clip card carries a TAGS row: asset_type · filename · timeline date.

Each beat opens with a TRACK STRIP (mini Premiere timeline) showing every
clipitem across V/A tracks within the beat — for fast spot-checking of cuts
and gaps before diving into scene detail.

Outputs to a sibling filename so it doesn't overwrite the canonical HTML —
e.g. `actII.html` → `actII_2col.html`.

Usage:
  py render_sidecar_html_2col.py <sidecar_path> [--out html_path]
"""
from __future__ import annotations
import argparse, html, json
from collections import defaultdict
from pathlib import Path


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _fmt_tc(secs: float) -> str:
    if secs is None or secs < 0: return "—"
    s = int(secs)
    return f"{s // 60}:{s % 60:02d}"


def _fmt_runtime(secs: float) -> str:
    if secs is None or secs < 0: return "—"
    s = int(secs)
    if s < 60: return f"{s}s"
    return f"{s // 60}m{s % 60:02d}s"


def _speakers_inline(a: dict) -> str:
    """Short display string for verified diarized speakers, by seconds desc."""
    sps = a.get("speakers") or []
    pieces = []
    for sp in sps:
        pid = sp.get("p_id")
        secs = sp.get("seconds") or 0
        if pid and pid.startswith("p_"):
            name = pid[2:].replace("_", " ").title()
            pieces.append(f'<span class="spk-named">{html.escape(name)}</span><span class="spk-secs">·{secs:.1f}s</span>')
        elif secs:
            pieces.append(f'<span class="spk-unk">?·{secs:.1f}s</span>')
    return " ".join(pieces)


def _scene_track_seconds(anns: list[dict], track: str, fps: float) -> float:
    total = 0
    for a in anns:
        k = a.get("key") or {}
        if k.get("track") != track:
            continue
        si = k.get("source_in_frames"); so = k.get("source_out_frames")
        if si is not None and so is not None and so > si:
            total += (so - si) / fps
    return total


def _dedupe_stereo_pairs(anns: list[dict]) -> list[dict]:
    """Collapse A1+A2 (or A3+A4, etc.) pairs with identical content (same
    asset_id + source_in + source_out + timeline_start) into a single row tagged
    with the lower-numbered track. Same logic as the canonical renderer.
    Only applies when the higher track is exactly one more than the lower."""
    out = []
    seen_key = {}
    for a in sorted(anns, key=lambda x: ((x.get("key") or {}).get("timeline_start_frames") or 0,
                                         (x.get("key") or {}).get("track") or "")):
        k = a.get("key") or {}
        track = k.get("track") or ""
        # Build content-identity key without track
        ck = (k.get("asset_id"), k.get("source_in_frames"), k.get("source_out_frames"),
              k.get("timeline_start_frames"))
        if ck in seen_key:
            prior = seen_key[ck]
            prior_track = (prior.get("key") or {}).get("track") or ""
            # If pair is consecutive (e.g. A1+A2), mark prior as "A1+2"
            if prior_track.startswith("A") and track.startswith("A"):
                try:
                    p_n = int(prior_track[1:]); n = int(track[1:])
                    if n == p_n + 1:
                        prior["_stereo_track_pair"] = f"{prior_track}+{n}"
                        continue
                except ValueError:
                    pass
        seen_key[ck] = a
        out.append(a)
    return out


def _is_audio_spine_flag(a: dict) -> bool:
    return bool(a.get("audio_spine") or a.get("is_audio_spine"))


def _asset_type_tag(a: dict) -> str:
    cls = ((a.get("asset") or {}).get("classifications") or {})
    # classifications.type carries the editor-facing taxonomy
    # (interview / b_roll / verite / aerial / timelapse / archival)
    return cls.get("type") or cls.get("asset_type") or "?"


def _date_tag(a: dict) -> str:
    asset = a.get("asset") or {}
    return asset.get("primary_timeline_date") or asset.get("shoot_date") or ""


SOURCE_TREE_DIRNAME = "Project"  # folder your camera originals live under (match dataset/_lib/asset_classifications)


def _rel_source_path(a: dict) -> str:
    """Strip the project-root prefix from asset.source_path so the tag shows the
    relative path (shoot folder + filename) — gives editorial context that the
    bare basename doesn't. e.g.
      <RAID>\\<project>\\2025-11-14_Interview\\C1299.MP4
        → 2025-11-14_Interview / C1299.MP4
    Falls back to the basename when no recognized root is detected."""
    sp = (a.get("asset") or {}).get("source_path") or ""
    if not sp: return ""
    # Normalize separators
    sp_norm = sp.replace("\\", "/")
    # Strip everything up to and including the source-tree root folder
    # (set SOURCE_TREE_DIRNAME to the folder your camera originals live under).
    low = sp_norm.lower()
    marker = SOURCE_TREE_DIRNAME.lower() + "/"
    idx = low.find(marker)
    if idx >= 0:
        rel = sp_norm[idx + len(marker):]
    else:
        # Fallback: keep just basename if we can't recognize the root
        rel = sp_norm.rsplit("/", 1)[-1]
    # Render with " / " separators for legibility
    return rel.replace("/", " / ")


def _visual_summary(a: dict) -> str:
    """Single-string visual summary for the video side. v3: consolidated to
    just chunk_action (the action description) — setting/camera/editorial-notes
    dropped to keep the column scannable. Chunk-level today; swap to per-shot
    dense_caption later (single point of change here).

    Returns "" if the field is missing or not a string."""
    action = a.get("chunk_action")
    if isinstance(action, str) and action.strip():
        return action.strip()
    return ""


# ─────────────────────────────────────────────────────────────────────────────
# CSS
# ─────────────────────────────────────────────────────────────────────────────

CSS = """
:root {
  --bg: #0d1117; --bg-soft: #161b22; --bg-card: #1c2128;
  --fg: #e6edf3; --fg-dim: #8b949e; --fg-faint: #6e7681;
  --border: #30363d;
  --v: #58a6ff; --a: #db61a2;
  --spine: #3fb950;
  --accent: #d29922;
  --mono: ui-monospace, 'SF Mono', Menlo, Consolas, monospace;
}
* { box-sizing: border-box; }
body { background: var(--bg); color: var(--fg); font-family: -apple-system, system-ui, sans-serif; margin: 0; padding: 16px 24px; line-height: 1.45; }
h1, h2, h3, h4 { margin: 0; }
h1 { font-size: 22px; }
h2 { font-size: 16px; color: var(--accent); margin: 24px 0 6px; letter-spacing: 0.02em; }
h3 { font-size: 14px; font-weight: 600; }
.mono { font-family: var(--mono); }
.muted { color: var(--fg-dim); }
.faint { color: var(--fg-faint); }

header.act { border-bottom: 1px solid var(--border); padding-bottom: 12px; margin-bottom: 16px; }
header.act .sub { color: var(--fg-dim); font-size: 12px; font-family: var(--mono); margin-top: 4px; }
header.act .v1note { color: var(--accent); font-size: 12px; margin-top: 6px; }

/* Mini-timeline (same idea as the canonical renderer, simpler) */
.mt { margin: 12px 0 18px; }
.mt-bar { display: flex; width: 100%; height: 32px; border: 1px solid var(--border); border-radius: 4px; overflow: hidden; background: var(--bg-soft); }
.mt-seg { display: flex; align-items: center; justify-content: center; flex-direction: column; padding: 2px 4px; color: var(--fg); font-size: 11px; border-right: 1px solid var(--border); overflow: hidden; white-space: nowrap; }
.mt-seg:last-child { border-right: none; }
.mt-seg .lab { font-weight: 600; max-width: 100%; overflow: hidden; text-overflow: ellipsis; }
.mt-seg .rt { font-family: var(--mono); font-size: 10px; color: var(--fg-dim); }
.mt-b_06 { background: #1f3a5c; }
.mt-b_07 { background: #3a2854; }
.mt-b_08 { background: #2c4a38; }
.mt-b_09 { background: #5a4020; }
.mt-b_10 { background: #5c2a2a; }

/* Beat header */
.beat-header { margin: 24px 0 8px; padding-bottom: 4px; border-bottom: 1px solid var(--border); display: flex; align-items: baseline; gap: 12px; }
.beat-header h2 { margin: 0; }
.beat-header .beat-rt { font-family: var(--mono); font-size: 11px; color: var(--fg-dim); }

/* Scene <details> */
details.scene { margin: 8px 0; background: var(--bg-soft); border: 1px solid var(--border); border-radius: 4px; padding: 0; }
details.scene > summary { padding: 10px 12px; cursor: pointer; list-style: none; user-select: none; }
details.scene > summary::-webkit-details-marker { display: none; }
details.scene > summary::before { content: '▸ '; color: var(--fg-faint); display: inline-block; width: 16px; }
details.scene[open] > summary::before { content: '▾ '; }
details.scene > summary .s-label { font-size: 14px; font-weight: 600; color: var(--fg); }
details.scene > summary .s-id { font-family: var(--mono); font-size: 10px; color: var(--fg-faint); margin-left: 8px; }
details.scene > summary .s-meta { font-family: var(--mono); font-size: 11px; color: var(--fg-dim); margin-left: 12px; }
details.scene > summary .s-proposed { color: var(--accent); margin-left: 8px; font-size: 10px; text-transform: uppercase; letter-spacing: 0.04em; }
details.scene > summary .s-dom { float: right; font-family: var(--mono); font-size: 11px; color: var(--fg-dim); }
details.scene > .scene-body { padding: 12px; border-top: 1px solid var(--border); }
.purpose { color: var(--fg-dim); font-size: 13px; font-style: italic; margin: 0 0 12px; padding: 0 4px; border-left: 2px solid var(--fg-faint); padding-left: 10px; }

/* 2-col body — time-aligned grid. Audio and video cells at the same
   timeline_start share a grid-row, giving rough horizontal alignment by
   start time. Empty cells preserve rough timing when one side has no event. */
.twocol-aligned { display: grid; grid-template-columns: 1fr 1fr; gap: 6px 16px; align-items: start; background: #0a0a0a; border: 1px solid #1f2428; border-radius: 4px; padding: 10px; }
.aligned-header { grid-row: 1; font-family: var(--mono); font-size: 10px; letter-spacing: 0.08em; text-transform: uppercase; color: var(--fg-dim); padding: 0 0 6px; border-bottom: 1px solid var(--border); }
.h-audio { grid-column: 1; color: var(--a); }
.h-video { grid-column: 2; color: var(--v); }
.cell-audio { grid-column: 1; }
.cell-video { grid-column: 2; }

/* "See above" — for repeat video segments of the same source asset within a scene */
.see-above { color: var(--fg-faint); font-style: italic; font-size: 12px; padding: 4px 0; }
.clip-repeat { opacity: 0.85; }

/* Clip card */
.clip { background: var(--bg-card); border: 1px solid var(--border); border-radius: 3px; padding: 6px 8px; margin-bottom: 6px; font-size: 12px; }
.clip:last-child { margin-bottom: 0; }
.clip-audio { border-left: 3px solid var(--a); }
.clip-video { border-left: 3px solid var(--v); }
.clip-spine { border-left-color: var(--spine); }
.clip-header { display: flex; gap: 8px; align-items: baseline; font-family: var(--mono); font-size: 10px; color: var(--fg-dim); margin-bottom: 2px; }
.clip-header .tl { color: var(--accent); }
.clip-header .track { color: var(--fg-faint); }
.clip-header .pair { color: var(--accent); font-weight: 700; }
.clip-fn { font-family: var(--mono); font-size: 11px; color: var(--fg-dim); margin-bottom: 3px; word-break: break-all; }
.clip-spk { font-size: 11px; margin-bottom: 3px; }
.spk-named { color: #ffcc66; font-weight: 600; }
.spk-secs { color: var(--fg-faint); font-family: var(--mono); font-size: 10px; margin-left: 2px; margin-right: 6px; }
.spk-unk { color: var(--fg-faint); font-family: var(--mono); font-size: 10px; margin-right: 6px; }
.clip-text { color: var(--fg); font-size: 12px; line-height: 1.4; padding: 4px 0 0; }
.clip-silent { color: var(--fg-faint); font-style: italic; font-size: 11px; }

/* Scene summary speaker badges */
.s-spk { display: inline-block; font-family: var(--mono); font-size: 10px; padding: 1px 6px; border-radius: 10px; background: var(--bg-card); color: var(--fg-dim); margin-right: 4px; }
.s-spk-dom { color: var(--accent); border: 1px solid var(--accent); }

/* Per-clip tags row */
.clip-tags { display: flex; flex-wrap: wrap; gap: 4px; margin: 4px 0 5px; }
.tag { font-family: var(--mono); font-size: 10px; padding: 1px 6px; border-radius: 10px; background: var(--bg-card); color: var(--fg-dim); border: 1px solid var(--border); white-space: nowrap; }
.tag-type { color: #d29922; border-color: #4a3c10; background: #2a210a; text-transform: uppercase; letter-spacing: 0.03em; }
.tag-fn { color: var(--fg-dim); background: #0d1117; }
.tag-src { color: var(--fg-dim); background: #0d1117; max-width: 100%; white-space: normal; word-break: break-word; overflow-wrap: anywhere; line-height: 1.35; }
.tag-date { color: #79c0ff; border-color: #1f3a5c; background: #0e1c2d; }
.spine-tag { font-family: var(--mono); font-size: 10px; color: var(--spine); margin-left: auto; padding-left: 4px; }
.overlay-tag { font-family: var(--mono); font-size: 10px; color: #ffb3d8; margin-left: auto; padding-left: 4px; text-transform: uppercase; letter-spacing: 0.04em; }
.cont-tag { font-family: var(--mono); font-size: 10px; color: var(--fg-faint); padding-left: 4px; font-style: italic; }
.clip-cont { opacity: 0.78; border-style: dashed; }
.clip-cont .vs-action { display: none; }

/* Foldable "other audio" beneath a dominant-track clip */
details.other-audio { margin: -2px 0 6px 16px; }
details.other-audio > summary { font-family: var(--mono); font-size: 10px; color: var(--fg-faint); cursor: pointer; padding: 2px 6px; list-style: none; user-select: none; }
details.other-audio > summary::-webkit-details-marker { display: none; }
details.other-audio > summary::before { content: '▸ '; }
details.other-audio[open] > summary::before { content: '▾ '; }
details.other-audio > summary:hover { color: var(--accent); }
details.other-audio[open] > summary { color: var(--accent); }
details.other-audio .clip { background: #14161c; opacity: 0.92; margin-bottom: 4px; }

/* Video-side visual summary */
.vs-meta { font-size: 11px; color: var(--fg-dim); margin: 0 0 4px; font-style: italic; }
.vs-setting { color: #b8d4ec; }
.vs-camera { color: var(--fg-dim); font-family: var(--mono); }
.vs-action { color: var(--fg); }
.vs-notes { font-size: 11px; color: var(--fg-dim); margin-top: 4px; padding-top: 4px; border-top: 1px dashed var(--border); }
.vs-moments { margin-top: 4px; padding-top: 4px; border-top: 1px dashed var(--border); }
.vs-mom { font-size: 11px; color: var(--fg); padding: 1px 0; }

/* Per-beat track strip — Premiere-style mini timeline at top of each beat */
.strip { margin: 8px 0 16px; padding: 8px 10px; background: #0a0a0a; border: 1px solid #1f2428; border-radius: 4px; }
.strip-meta { font-size: 10px; padding-bottom: 6px; }
.strip-scene-bar { position: relative; height: 18px; margin-bottom: 4px; padding-left: 30px; }
.strip-scene-tab { position: absolute; top: 0; height: 16px; font-family: var(--mono); font-size: 10px; color: var(--fg-dim); background: var(--bg-soft); border: 1px solid var(--border); border-radius: 2px; padding: 0 4px; line-height: 16px; overflow: hidden; white-space: nowrap; text-overflow: ellipsis; box-sizing: border-box; }
.strip-scene-tab:hover { color: var(--fg); background: var(--bg-card); z-index: 2; overflow: visible; }
.strip-tracks { display: block; }
.strip-row { position: relative; height: 14px; margin: 1px 0; display: flex; align-items: stretch; }
.strip-label { width: 26px; font-size: 9px; color: #888; text-align: right; padding-right: 4px; line-height: 14px; flex-shrink: 0; }
.strip-body { position: relative; flex: 1; height: 14px; background: #060606; border-radius: 1px; }
.strip-row.strip-v1 { border-bottom: 1px solid #2a2a2a; padding-bottom: 2px; height: 16px; }
.strip-row.strip-v1 .strip-body, .strip-row.strip-v1 .strip-label { height: 14px; }
.strip-clip { position: absolute; top: 0; height: 14px; border-radius: 1px; box-sizing: border-box; }
.strip-v .strip-clip { background: #1c2e44; border: 1px solid #79c0ff; }
.strip-v .strip-clip.strip-spine { background: #1c3a5a; border-color: #58a6ff; border-left: 3px solid var(--spine); }
.strip-a .strip-clip { background: #2a1620; border: 1px solid #8a3e68; }
.strip-a .strip-clip.strip-spine { background: #4a1e3a; border-color: #db61a2; border-left: 3px solid var(--spine); }
.strip-clip:hover { filter: brightness(1.4); cursor: default; z-index: 3; }
"""


# ─────────────────────────────────────────────────────────────────────────────
# Rendering
# ─────────────────────────────────────────────────────────────────────────────

def _build_visible_video_segments(scene_anns: list[dict]) -> list[dict]:
    """Compute the VIDEO timeline as the viewer actually sees it: at every frame,
    the visible clip is the highest-numbered V-track with a clip at that frame
    (V2 b-roll covers V1 a-roll, V3 graphics cover V2, etc.). Returns merged
    segments in timeline order — a V1 interview clip interrupted by V2 b-roll
    becomes three segments: V1[start, B_in], V2[B_in, B_out], V1[B_out, end].

    Each segment dict: {start, end, v_num, ann, is_continuation}
    is_continuation=True for V1 segments that resume after a higher-track cover
    (so the renderer can mark them visually without duplicating the action text)."""
    # Collect V-track clips with usable geometry
    v_clips = []
    for a in scene_anns:
        k = a.get("key") or {}
        track = k.get("track") or ""
        if not (track.startswith("V") and len(track) > 1):
            continue
        try:
            v_num = int(track[1:])
        except ValueError:
            continue
        tl = k.get("timeline_start_frames")
        si = k.get("source_in_frames") or 0
        so = k.get("source_out_frames") or 0
        if tl is None or tl < 0 or so <= si:
            continue
        v_clips.append({"v_num": v_num, "start": tl, "end": tl + (so - si), "ann": a})
    if not v_clips:
        return []

    # Sweep all change points; at each frame interval pick the highest covering V-track
    points = sorted({p for c in v_clips for p in (c["start"], c["end"])})
    raw_segments = []
    for i in range(len(points) - 1):
        s, e = points[i], points[i + 1]
        if s == e: continue
        covering = [c for c in v_clips if c["start"] <= s and c["end"] >= e]
        if not covering: continue
        winner = max(covering, key=lambda c: c["v_num"])
        raw_segments.append({"start": s, "end": e, "v_num": winner["v_num"], "ann": winner["ann"]})

    # Merge consecutive segments from the SAME ann instance (no break inside the clip)
    # ("See above" handling — for separate annotations sharing the same asset_id —
    # lives in the scene renderer, which is where we know the full scene context.)
    merged = []
    for seg in raw_segments:
        if merged and merged[-1]["ann"] is seg["ann"]:
            merged[-1]["end"] = seg["end"]
        else:
            merged.append(dict(seg))
    return merged


def _audio_overlaps_at(scene_anns: list[dict], dom_track: str, seg_start: int, seg_end: int) -> list[dict]:
    """Return the non-dominant A-track annotations that overlap a given timeline
    window — for surfacing 'other audio at this clip' (backup mics, music, nat)
    as foldables beneath the dominant track."""
    out = []
    for a in scene_anns:
        k = a.get("key") or {}
        t = k.get("track") or ""
        if not t.startswith("A") or t == dom_track:
            continue
        tl = k.get("timeline_start_frames")
        si = k.get("source_in_frames") or 0
        so = k.get("source_out_frames") or 0
        if tl is None or tl < 0 or so <= si:
            continue
        clip_start, clip_end = tl, tl + (so - si)
        if clip_end <= seg_start or clip_start >= seg_end:
            continue
        out.append(a)
    return out


def render_scene(scene: dict, scene_anns: list[dict], fps: float) -> str:
    sid = scene["id"]
    label = scene.get("label") or sid
    purpose = scene.get("purpose") or ""
    rng = scene.get("timeline_range_frames") or [0, 0]
    s_lo, s_hi = rng
    runtime = max(0, (s_hi - s_lo) / fps)
    proposed = scene.get("proposed")

    tracks_present = sorted({((a.get("key") or {}).get("track") or "") for a in scene_anns} - {""})
    a_tracks_present = [t for t in tracks_present if t.startswith("A")]
    v_tracks_present = [t for t in tracks_present if t.startswith("V")]

    # AUDIO: dominant track (most source-seconds) is what the viewer primarily hears.
    # Other A-tracks (backup mics, music, nat) get folded as expandable footnotes per clip.
    a_secs = {t: _scene_track_seconds(scene_anns, t, fps) for t in a_tracks_present}
    dom_a = max(a_secs, key=a_secs.get) if a_secs else None
    a_other = [t for t in a_tracks_present if t != dom_a]

    audio_anns = [a for a in scene_anns if (a.get("key") or {}).get("track") == dom_a] if dom_a else []
    audio_anns = _dedupe_stereo_pairs(audio_anns)
    audio_anns.sort(key=lambda a: (a.get("key") or {}).get("timeline_start_frames") or 0)

    # VIDEO: build the actually-visible timeline. Highest V-track at each frame wins.
    visible_segments = _build_visible_video_segments(scene_anns)

    parts = []
    parts.append('<details class="scene">')
    parts.append('<summary>')
    parts.append(f'<span class="s-label">{html.escape(label)}</span>')
    parts.append(f'<span class="s-id mono">{html.escape(sid)}</span>')
    parts.append(f'<span class="s-meta">{_fmt_tc(s_lo/fps)}–{_fmt_tc(s_hi/fps)} · {_fmt_runtime(runtime)} · {len(scene_anns)} ann</span>')
    if proposed:
        parts.append('<span class="s-proposed">proposed</span>')
    parts.append(f'<span class="s-dom">audio: {dom_a or "—"}{" (+" + ",".join(a_other) + ")" if a_other else ""} · video: visible stack of {",".join(v_tracks_present) or "—"}</span>')
    parts.append('</summary>')

    parts.append('<div class="scene-body">')
    if purpose:
        parts.append(f'<p class="purpose">{html.escape(purpose)}</p>')

    # ── Time-aligned 2-column grid ──
    # Both columns share a row index keyed off unified timeline_start events.
    # When an audio clip and a video segment share the same start, they share a
    # row → roughly aligned horizontally. Empty cells preserve the rough timing
    # when one side has no event at that frame.
    parts.append('<div class="twocol-aligned">')

    # Row 1 = headers
    other_blurb = f"  ·  other tracks: {', '.join(a_other)} (foldable per clip)" if a_other else ""
    audio_hdr = f"AUDIO — what's heard ({dom_a}){other_blurb}" if dom_a else "AUDIO — (no audio tracks)"
    video_hdr = "VIDEO — what's visible (highest V-track wins per frame)" if visible_segments else "VIDEO — (no video tracks)"
    parts.append(f'<div class="aligned-header h-audio">{html.escape(audio_hdr)}</div>')
    parts.append(f'<div class="aligned-header h-video">{html.escape(video_hdr)}</div>')

    # Build unified events keyed by timeline_start_frames
    events_by_start: dict[int, dict] = {}
    for a in audio_anns:
        start = (a.get("key") or {}).get("timeline_start_frames") or 0
        events_by_start.setdefault(start, {"audio": None, "video": None})
        events_by_start[start]["audio"] = a
    for seg in visible_segments:
        events_by_start.setdefault(seg["start"], {"audio": None, "video": None})
        events_by_start[seg["start"]]["video"] = seg

    seen_asset_ids: set = set()
    for row_idx, start in enumerate(sorted(events_by_start.keys()), start=2):
        e = events_by_start[start]
        # Audio cell
        a = e["audio"]
        if a:
            parts.append(f'<div class="cell-audio" style="grid-row:{row_idx}">')
            parts.append(_render_clip_card(a, "audio", fps))
            # Other-audio foldable for this clip's window
            k = a.get("key") or {}
            tl = k.get("timeline_start_frames") or 0
            si = k.get("source_in_frames") or 0
            so = k.get("source_out_frames") or 0
            if so > si:
                others = _audio_overlaps_at(scene_anns, dom_a, tl, tl + (so - si))
                if others:
                    tracks_str = ", ".join(sorted({(o.get("key") or {}).get("track") for o in others if (o.get("key") or {}).get("track")}))
                    parts.append('<details class="other-audio"><summary>')
                    parts.append(f'+ other audio here ({len(others)} clip{"s" if len(others)!=1 else ""} on {tracks_str})')
                    parts.append('</summary>')
                    for o in sorted(others, key=lambda x: ((x.get("key") or {}).get("track") or "", (x.get("key") or {}).get("timeline_start_frames") or 0)):
                        parts.append(_render_clip_card(o, "audio", fps))
                    parts.append('</details>')
            parts.append('</div>')

        # Video cell — track seen asset_ids for "See above"
        seg = e["video"]
        if seg:
            aid = (seg["ann"].get("key") or {}).get("asset_id")
            is_repeat = bool(aid and aid in seen_asset_ids)
            if aid:
                seen_asset_ids.add(aid)
            parts.append(f'<div class="cell-video" style="grid-row:{row_idx}">')
            parts.append(_render_visible_segment(seg, fps, is_repeat=is_repeat))
            parts.append('</div>')

    parts.append('</div>')  # close .twocol-aligned
    parts.append('</div>')  # close .scene-body
    parts.append('</details>')
    return "".join(parts)


def _render_visible_segment(seg: dict, fps: float, is_repeat: bool = False) -> str:
    """Render a video segment as a clip card. seg["start"]/end are the VISIBLE
    timeline range (may be shorter than the annotation's full range, when
    interrupted by a higher V-track). `is_repeat` is set when the same source
    asset_id has already appeared earlier in this scene — replaces the action
    text with 'See above ↑' to avoid duplicating the chunk-level summary."""
    a = seg["ann"]
    k = a.get("key") or {}
    visible_dur_secs = max(0, (seg["end"] - seg["start"]) / fps)
    track = k.get("track") or "?"
    asset = a.get("asset") or {}
    fn = asset.get("filename") or ((k.get("asset_id") or "")[:10]) or "?"
    atype = _asset_type_tag(a)
    rel_path = _rel_source_path(a)
    date = _date_tag(a)

    cls = "clip clip-video"
    if track == "V1" or _is_audio_spine_flag(a):
        cls += " clip-spine"
    if is_repeat:
        cls += " clip-repeat"

    parts = [f'<div class="{cls}">']
    parts.append('<div class="clip-header">')
    parts.append(f'<span class="tl">{_fmt_tc(seg["start"]/fps)}</span>')
    parts.append(f'<span class="muted">{_fmt_runtime(visible_dur_secs)}</span>')
    parts.append(f'<span class="track">{html.escape(track)}</span>')
    if track == "V1":
        parts.append('<span class="spine-tag">a-roll</span>')
    elif track and track.startswith("V"):
        try:
            if int(track[1:]) >= 2:
                parts.append('<span class="overlay-tag">b-roll over</span>')
        except ValueError:
            pass
    parts.append('</div>')

    # Tags row — asset_type, date, relative source path (path last so wrapping doesn't push date out of view)
    parts.append('<div class="clip-tags">')
    if atype and atype != "?":
        parts.append(f'<span class="tag tag-type">{html.escape(atype)}</span>')
    if date:
        parts.append(f'<span class="tag tag-date">{html.escape(date)}</span>')
    if rel_path:
        parts.append(f'<span class="tag tag-src">{html.escape(rel_path)}</span>')
    else:
        parts.append(f'<span class="tag tag-fn">{html.escape(fn[:48])}</span>')
    parts.append('</div>')

    # Action description, OR "See above" for repeats of the same source asset
    if is_repeat:
        parts.append('<div class="see-above">See above ↑</div>')
    else:
        action = _visual_summary(a)
        if action:
            if len(action) > 500: action = action[:496] + "…"
            parts.append(f'<div class="clip-text vs-action">{html.escape(action)}</div>')
        else:
            parts.append('<div class="clip-silent">(no visual summary available — silent b-roll or missing chunk metadata)</div>')

    parts.append('</div>')
    return "".join(parts)


def _render_clip_card(a: dict, kind: str, fps: float) -> str:
    """One AUDIO clip card. (Video is now rendered by _render_visible_segment
    which takes the visible-segment span, not the full annotation duration.)
    Layout:
      header:   tl@ · duration · track · spine?
      tags:     [asset_type] [source path] [date]
      content:  speakers + transcript
    """
    k = a.get("key") or {}
    tl = k.get("timeline_start_frames") or 0
    src_in = k.get("source_in_frames") or 0
    src_out = k.get("source_out_frames") or 0
    dur_secs = max(0, (src_out - src_in) / fps)
    track = k.get("track") or "?"
    pair = a.get("_stereo_track_pair")
    asset = a.get("asset") or {}
    fn = asset.get("filename") or ((k.get("asset_id") or "")[:10]) or "?"
    atype = _asset_type_tag(a)
    rel_path = _rel_source_path(a)
    date = _date_tag(a)

    cls = f"clip clip-{kind}"
    if _is_audio_spine_flag(a):
        cls += " clip-spine"

    parts = [f'<div class="{cls}">']
    parts.append('<div class="clip-header">')
    parts.append(f'<span class="tl">{_fmt_tc(tl/fps)}</span>')
    parts.append(f'<span class="muted">{_fmt_runtime(dur_secs)}</span>')
    if pair:
        parts.append(f'<span class="pair">{html.escape(pair)}</span>')
    else:
        parts.append(f'<span class="track">{html.escape(track)}</span>')
    if _is_audio_spine_flag(a):
        parts.append('<span class="spine-tag">spine</span>')
    parts.append('</div>')

    parts.append('<div class="clip-tags">')
    if atype and atype != "?":
        parts.append(f'<span class="tag tag-type">{html.escape(atype)}</span>')
    if date:
        parts.append(f'<span class="tag tag-date">{html.escape(date)}</span>')
    if rel_path:
        parts.append(f'<span class="tag tag-src">{html.escape(rel_path)}</span>')
    else:
        parts.append(f'<span class="tag tag-fn">{html.escape(fn[:48])}</span>')
    parts.append('</div>')

    spk = _speakers_inline(a)
    txt = (a.get("transcript_text") or "").strip()
    if spk:
        parts.append(f'<div class="clip-spk">{spk}</div>')
    if txt:
        if len(txt) > 500: txt = txt[:496] + "…"
        parts.append(f'<div class="clip-text">{html.escape(txt)}</div>')
    elif not spk:
        parts.append('<div class="clip-silent">(no transcript / no speakers)</div>')
    parts.append('</div>')
    return "".join(parts)


def _render_beat_track_strip(beat: dict, beat_anns: list[dict], fps: float) -> str:
    """Mini Premiere-style horizontal track strip for the whole beat — for fast
    spot-checking which tracks have content where. One row per active track
    (V3, V2, V1, A1, A2, …). Each clip = a box positioned by timeline_start,
    width ∝ duration. Tooltips on hover show details. No clip-id labels — the
    strip is for shape-of-the-cut, not deep inspection."""
    brng = beat.get("timeline_range_frames") or [0, 0]
    b_lo, b_hi = brng
    span = max(1, b_hi - b_lo)

    # Bucket by track. Skip -1-sentinel transition refs.
    by_track = defaultdict(list)
    for a in beat_anns:
        k = a.get("key") or {}
        tl = k.get("timeline_start_frames")
        if tl is None or tl < 0: continue
        track = k.get("track")
        if not track: continue
        by_track[track].append(a)
    if not by_track:
        return ""

    # Track order: V3, V2, V1, A1, A2, … (video on top, audio underneath, spine V1 above the divider)
    def _track_sort_key(t):
        if t.startswith("V"):
            try: n = int(t[1:])
            except ValueError: n = 99
            return (0, -n)  # higher V number on top (V3, V2, V1)
        if t.startswith("A"):
            try: n = int(t[1:])
            except ValueError: n = 99
            return (1, n)
        return (2, t)

    tracks = sorted(by_track.keys(), key=_track_sort_key)

    parts = ['<div class="strip">']
    parts.append(f'<div class="strip-meta mono muted">beat strip · {len(beat_anns)} clipitems · {_fmt_tc(b_lo/fps)}–{_fmt_tc(b_hi/fps)} · {_fmt_runtime(span/fps)}</div>')

    # Scene boundary tick markers (vertical lines across all tracks)
    scenes = beat.get("scenes") or []
    if scenes:
        parts.append('<div class="strip-scene-bar">')
        for s in scenes:
            sr = s.get("timeline_range_frames") or [b_lo, b_lo]
            s_lo, s_hi = sr
            if s_hi <= b_lo or s_lo >= b_hi: continue
            left = max(0, (s_lo - b_lo) / span * 100)
            width = max(0.1, min(100 - left, (s_hi - s_lo) / span * 100))
            label = s.get("label") or s["id"]
            parts.append(f'<div class="strip-scene-tab" style="left:{left:.2f}%; width:{width:.2f}%" '
                         f'title="{html.escape(s["id"])} — {html.escape(label)} · '
                         f'{_fmt_tc(s_lo/fps)}–{_fmt_tc(s_hi/fps)}">{html.escape(label)}</div>')
        parts.append('</div>')

    parts.append('<div class="strip-tracks">')
    for track in tracks:
        is_video = track.startswith("V")
        is_v1 = track == "V1"
        parts.append(f'<div class="strip-row strip-{"v" if is_video else "a"}{ " strip-v1" if is_v1 else ""}">')
        parts.append(f'<span class="strip-label mono">{html.escape(track)}</span>')
        parts.append('<div class="strip-body">')
        for a in by_track[track]:
            k = a.get("key") or {}
            tl = k.get("timeline_start_frames") or 0
            si = k.get("source_in_frames") or 0
            so = k.get("source_out_frames") or 0
            dur = max(1, so - si)
            left = max(0, (tl - b_lo) / span * 100)
            width = max(0.05, dur / span * 100)
            if left + width > 100:
                width = max(0.05, 100 - left)
            fn = ((a.get("asset") or {}).get("filename") or "?")[:32]
            spk_summary = ""
            for sp in (a.get("speakers") or [])[:2]:
                pid = sp.get("p_id") or ""
                if pid.startswith("p_"):
                    spk_summary += " " + pid[2:].replace("_", " ").title()
            tip = f"{track} · {_fmt_tc(tl/fps)} · {_fmt_runtime(dur/fps)} · {fn}{spk_summary}"
            spine = " strip-spine" if (_is_audio_spine_flag(a) or is_v1) else ""
            parts.append(f'<div class="strip-clip{spine}" style="left:{left:.3f}%; width:{width:.3f}%" '
                         f'title="{html.escape(tip)}"></div>')
        parts.append('</div></div>')
    parts.append('</div>')  # strip-tracks
    parts.append('</div>')  # strip
    return "".join(parts)


def render_act_beat_timeline(sc: dict) -> str:
    """Mini-timeline at the top of the act (same idea as the canonical renderer)."""
    beats = sc.get("beats") or []
    if not beats: return ""
    fps = float(sc.get("frame_rate") or (24000/1001))
    total = 0
    spans = []
    for b in beats:
        rng = b.get("timeline_range_frames") or [0, 0]
        n = max(0, rng[1] - rng[0])
        total += n
        spans.append((b, n))
    if total <= 0: return ""
    parts = ['<div class="mt"><div class="mt-bar">']
    for b, n in spans:
        pct = 100 * n / total
        bid = b.get("id") or ""
        label = b.get("label") or bid
        parts.append(f'<div class="mt-seg mt-{html.escape(bid)}" style="width:{pct:.3f}%" '
                     f'title="{html.escape(bid)} — {html.escape(label)} · {_fmt_runtime(n/fps)}">'
                     f'<span class="lab">{html.escape(bid)} · {html.escape(label)}</span>'
                     f'<span class="rt">{_fmt_runtime(n/fps)}</span></div>')
    parts.append('</div></div>')
    return "".join(parts)


def render_sidecar(sidecar_path: Path, out_path: Path) -> int:
    sc = json.loads(sidecar_path.read_text(encoding="utf-8"))
    fps = float(sc.get("frame_rate") or (24000/1001))
    anns = sc.get("annotations") or []
    beats = sc.get("beats") or []

    # Group annotations by scene
    by_scene = defaultdict(list)
    for a in anns:
        by_scene[a.get("scene")].append(a)

    rng_s = sc.get("timeline_range_seconds") or [0, 0]
    runtime = max(0, rng_s[1] - rng_s[0])

    parts = []
    parts.append('<!DOCTYPE html><html><head>')
    parts.append(f'<title>{html.escape(sc.get("act_id",""))} — {html.escape(sc.get("label",""))} — 2-column (experimental)</title>')
    parts.append(f'<style>{CSS}</style></head><body>')

    parts.append('<header class="act">')
    parts.append(f'<h1>{html.escape(sc.get("act_id",""))} — {html.escape(sc.get("label",""))}  '
                 f'<span class="muted" style="font-size:13px">(2-column experimental)</span></h1>')
    parts.append(f'<div class="sub">{html.escape(sc.get("xml_source",""))}  ·  runtime <b>{_fmt_runtime(runtime)}</b>  ·  '
                 f'{len(anns)} annotations  ·  {len(beats)} beats / {sum(len(b.get("scenes") or []) for b in beats)} scenes</div>')
    parts.append('<div class="v1note">v1 experimental layout — dominant A-track on left, dominant V-track on right, '
                 'within each scene drop-down. Iterate freely.</div>')
    parts.append('</header>')

    parts.append(render_act_beat_timeline(sc))

    for beat in beats:
        bid = beat["id"]
        blabel = beat.get("label") or ""
        brng = beat.get("timeline_range_frames") or [0, 0]
        bruntime = max(0, (brng[1] - brng[0]) / fps)
        parts.append('<div class="beat-header">')
        parts.append(f'<h2>{html.escape(bid)} — {html.escape(blabel)}</h2>')
        parts.append(f'<span class="beat-rt">{_fmt_tc(brng[0]/fps)}–{_fmt_tc(brng[1]/fps)} · {_fmt_runtime(bruntime)}</span>')
        parts.append('</div>')

        # Collect all annotations whose timeline falls inside this beat (for the strip)
        b_lo, b_hi = brng
        beat_anns = [a for a in anns
                     if b_lo <= ((a.get("key") or {}).get("timeline_start_frames") or -1) < b_hi]
        parts.append(_render_beat_track_strip(beat, beat_anns, fps))

        for scene in (beat.get("scenes") or []):
            sid = scene["id"]
            parts.append(render_scene(scene, by_scene.get(sid, []), fps))

    parts.append('</body></html>')
    out_path.write_text("".join(parts), encoding="utf-8")
    print(f"wrote {out_path} ({out_path.stat().st_size // 1024} KB)")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("sidecar", nargs="?", default="actII",
                    help="'actI' / 'actII' / 'actIII' or a path to a sidecar.json")
    ap.add_argument("--out", default=None,
                    help="explicit output path (default: sibling of canonical actII.html with _2col suffix)")
    args = ap.parse_args()

    editor = Path(r"E:\open-post-stack\editor")
    if args.sidecar in ("actI", "actII", "actIII"):
        sidecar = editor / "story/sidecars" / f"{args.sidecar}.sidecar.json"
        default_out = editor / "story/html views" / f"{args.sidecar}_2col.html"
    else:
        sidecar = Path(args.sidecar)
        default_out = sidecar.with_suffix(".2col.html")

    out = Path(args.out) if args.out else default_out
    return render_sidecar(sidecar, out)


if __name__ == "__main__":
    raise SystemExit(main())
