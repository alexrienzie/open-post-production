"""Cut audit: structured diagnosis of xmeml + sidecar + editor_notes.

Given a Premiere xmeml export, the Act sidecar, and the editor_notes folder,
produces a structured report of issues an LLM (or human) should review before
the next iteration:

  - Source-bound violations (src_out exceeds asset.duration_sec)
  - Clipitems whose source window violates an editor_notes 'avoid' window
    (regex-extracted from notes typed 'avoid' / 'stability' that say "avoid src X-Ys")
  - B-roll-bucket clipitems whose asset has NO editor_notes at all (unknown shots)
  - Sidecar annotations with empty `rationale` (editorial intent uncaptured)
  - Editor_notes 'usage' claims (`Used src X-Ys`) that don't match any clipitem
    in the actual cut for that asset (likely stale notes)
  - Per-scene V-track coverage: gaps in V1 (anchor) or V3 (b-roll) within the
    scene's timeline range

Read-only. Does not write to catalog, sidecar, or xmeml. Editorial mutations
happen via dataset/_scripts/ + the sidecar refresh pipeline; this module just
surfaces what an editorial agent should fix.

Status (as built):

  EARNED — these were motivated by friction in this iteration:
    * Join layer (xml ↔ asset_map ↔ catalog ↔ editor_notes ↔ sidecar). Any future
      editor tool that needs to reason about a cut will reuse this. Foundational.
    * SOURCE_OOB. Caught a real workspace bug (C1686 clipitem-1048: src 54-66s
      vs. asset.duration_sec=20.525 -- filename collision or catalog mismatch).
    * NOTES_MISSING (b_roll bucket only). Caught C0051 (no notes).
    * RATIONALE_MISSING (per-scene). Global count is noise (~90%); per-scene
      pointer is genuinely actionable -- only listed per-scene in the report.

  FRAGILE — earned check, weak implementation:
    * _detect_avoid_violation. Wobble windows in C0150/C0147 are real signal;
      regex-on-prose is two false-positive rounds deep in one session
      (sentence-scoped + parenthetical-stripping mitigates but doesn't fix).
      Real fix: add structured fields to editor_notes schema
      (avoid_src_windows: [[0.0, 10.0]]) and parse those instead. Earn after
      ~20 more editor_notes when the prose patterns stabilize.

  SPECULATIVE — not earned by any iteration so far; reconsider only when a
  concrete iteration uncovers a real bug they would have caught:
    * Scene `visual_hole_frames` (union V-track coverage gap)
    * Scene `v3_gaps_frames`
    * _detect_stale_usage (same regex fragility as _detect_avoid_violation,
      with the additional problem that "stale" is a pattern that only matters
      when editor_notes are written ahead of the cut -- rare today).

Growth pattern: add new checks ONLY when an iteration would have caught a real
bug with them. Resist refactoring checks into a "rule engine" / "scoring
framework" until 3+ checks share enough structure to make the abstraction
obvious. We are not there.

CLI:
    py editor\\queries\\cut_audit.py "editor\\xml exports\\<file>.xml"
    py editor\\queries\\cut_audit.py <xml> --sidecar editor\\story\\sidecars\\actII.sidecar.json --json out.json
    py editor\\queries\\cut_audit.py <xml> --scene b_06_s05_the_criminal   # filter to one scene

Design notes:

* Frame rate is NTSC 23.976 — `pproTicks = frame * 10_594_584_000`. We work
  in frames throughout; seconds are derived (frames / 24000 * 1001).
* asset_id resolution from a clipitem's <file><pathurl> uses the
  derivative media/_index/asset_map.json reverse index. Falls back to None
  (and a flag) if the pathurl doesn't match a known proxy path.
* "Stale usage claim" is detected with a deliberately loose regex:
  `src\\s+([\\d.]+)\\s*[-\\u2013]\\s*([\\d.]+)\\s*s?`. The note may mention
  multiple windows; each is checked independently against the union of
  source windows actually used for that asset in the xmeml. A window is
  considered "still in cut" if it overlaps any actual usage by at least 1s.
* We deliberately DON'T re-validate xmeml structural invariants here
  (transitionitems, -1 sentinels, sequence duration) -- that's the job of
  story/_sidecar scripts/validate_xml_structure.py.
"""

from __future__ import annotations

# Allow `py editor/queries/cut_audit.py ...` to work even though we use
# relative imports below. Script mode sets __package__ to None and doesn't
# put parents on sys.path -- fix that here (same pattern as retrieval.py).
if __name__ == "__main__" and (__package__ in (None, "")):
    import sys
    from pathlib import Path as _P

    sys.path.insert(0, str(_P(__file__).resolve().parents[2]))
    __package__ = "editor.queries"

import argparse
import json
import re
import sqlite3
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional
from urllib.parse import unquote

from ._paths import editorial_catalog_sqlite_path, repo_root

# NTSC 23.976 = 24000/1001 fps. Exact integer ticks per frame.
FPS_NUM = 24000
FPS_DEN = 1001
SEC_PER_FRAME = FPS_DEN / FPS_NUM

# Loose match for "src X-Ys" / "src X to Ys" / "src X–Ys" in editor_notes text.
# Captures pairs; multiple matches allowed in one note.
SRC_WINDOW_RE = re.compile(
    r"src\s+(\d+(?:\.\d+)?)\s*(?:-|–|to)\s*(\d+(?:\.\d+)?)\s*s?",
    re.IGNORECASE,
)


def f2s(frames: int) -> float:
    return frames * SEC_PER_FRAME


def s2f(seconds: float) -> int:
    return round(seconds / SEC_PER_FRAME)


@dataclass
class ClipAudit:
    clip_id: str
    track: str
    name: str
    asset_id: Optional[str]
    timeline_start_frames: int
    timeline_end_frames: int
    source_in_frames: int
    source_out_frames: int
    timeline_start_sec: float
    timeline_end_sec: float
    source_in_sec: float
    source_out_sec: float
    asset_duration_sec: Optional[float]
    asset_bucket: Optional[str]
    editor_notes_present: bool
    editor_tags: list[str] = field(default_factory=list)
    rationale_present: bool = False
    flags: list[str] = field(default_factory=list)
    flag_detail: dict = field(default_factory=dict)


@dataclass
class SceneAudit:
    scene_id: str
    label: str
    beat_id: str
    timeline_range_frames: tuple[int, int]
    v1_clip_count: int = 0
    v3_clip_count: int = 0
    # SPECULATIVE: "Visual hole" = no V-track (excl. graphics) covers this span ->
    # black on screen. Not earned by any iteration so far; in v3d every V3 b-roll
    # span has either V1 talking-head under or is intentional cutaway. Reconsider
    # if a real "black hole" bug appears in a later iteration.
    visual_hole_frames: list[tuple[int, int]] = field(default_factory=list)
    # SPECULATIVE: V3-only gap. Same status -- the one V3 gap surfaced in v3d
    # (290.12-307.39s in b_06_s05) is just the V1 talking-head section before
    # b-roll rolls in. Normal, not actionable.
    v3_gaps_frames: list[tuple[int, int]] = field(default_factory=list)
    annotations_missing_rationale: list[str] = field(default_factory=list)


@dataclass
class CutAuditReport:
    xml_path: str
    sidecar_path: Optional[str]
    clip_audits: list[ClipAudit] = field(default_factory=list)
    scene_audits: list[SceneAudit] = field(default_factory=list)
    summary: dict = field(default_factory=dict)
    stale_usage_claims: list[dict] = field(default_factory=list)


def _build_asset_map_reverse() -> dict:
    """{relative_path (lowercase, backslashes) -> asset_id} from asset_map.json."""
    p = repo_root() / "derivative media" / "_index" / "asset_map.json"
    j = json.loads(p.read_text(encoding="utf-8"))
    rev = {}
    for aid, entry in j.get("entries", {}).items():
        for sub in entry.values():
            if isinstance(sub, dict) and "relative_path" in sub:
                key = sub["relative_path"].replace("/", "\\").lower()
                rev[key] = aid
    return rev


def _pathurl_to_relpath(pathurl: str) -> Optional[str]:
    decoded = unquote(pathurl)
    marker = "derivative media/"
    idx = decoded.lower().find(marker)
    if idx < 0:
        return None
    return decoded[idx + len(marker):]


def _load_asset_meta(asset_ids: set[str]) -> dict[str, dict]:
    """{asset_id -> {duration_sec, bucket, asset_type, filename, semantic_subject}}."""
    if not asset_ids:
        return {}
    out = {}
    con = sqlite3.connect(str(editorial_catalog_sqlite_path()))
    con.row_factory = sqlite3.Row
    ph = ",".join("?" * len(asset_ids))
    rows = con.execute(
        f"SELECT asset_id, duration_sec, bucket, asset_type, filename, semantic_subject "
        f"FROM asset WHERE asset_id IN ({ph})",
        list(asset_ids),
    ).fetchall()
    con.close()
    for r in rows:
        out[r["asset_id"]] = dict(r)
    return out


def _load_editor_notes(asset_ids: set[str]) -> dict[str, dict]:
    out = {}
    notes_dir = repo_root() / "dataset" / "assets" / "catalog" / "editor_notes"
    for aid in asset_ids:
        p = notes_dir / f"{aid}_editor_notes.json"
        if not p.exists():
            continue
        out[aid] = json.loads(p.read_text(encoding="utf-8"))
    return out


def _extract_video_clipitems(xml_path: Path) -> tuple[list[dict], dict]:
    """Return (clipitems, files_by_id). Each clipitem has the keys consumed downstream."""
    tree = ET.parse(xml_path)
    root = tree.getroot()
    seq = root.find(".//sequence")
    video = seq.find(".//media/video")
    files = {}
    for f in root.findall(".//file"):
        fid = f.get("id")
        pu = f.find("pathurl")
        if pu is not None and pu.text:
            files[fid] = pu.text
    clipitems = []
    for t_idx, track in enumerate(video.findall("track")):
        # V1 = idx 0, V2 = idx 1, V3 = idx 2, ...
        track_name = f"V{t_idx + 1}"
        for ci in track.findall("clipitem"):
            start_el = ci.find("start")
            end_el = ci.find("end")
            in_el = ci.find("in")
            out_el = ci.find("out")
            name_el = ci.find("name")
            file_el = ci.find("file")
            if None in (start_el, end_el, in_el, out_el):
                continue
            file_id = file_el.get("id") if file_el is not None else None
            clipitems.append({
                "clip_id": ci.get("id"),
                "track": track_name,
                "name": name_el.text if name_el is not None else "",
                "start": int(start_el.text),
                "end": int(end_el.text),
                "in": int(in_el.text),
                "out": int(out_el.text),
                "file_id": file_id,
                "pathurl": files.get(file_id),
            })
    return clipitems, files


def _build_sidecar_index(sidecar_path: Path) -> dict:
    """Return {(asset_id, src_in, src_out, tl_start, track): annotation}.

    Plus a 'scenes' key with list of (beat_id, scene_dict).
    """
    j = json.loads(sidecar_path.read_text(encoding="utf-8"))
    ann_by_key = {}
    for a in j.get("annotations", []):
        k = a.get("key", {})
        key = (
            k.get("asset_id"),
            k.get("source_in_frames"),
            k.get("source_out_frames"),
            k.get("timeline_start_frames"),
            k.get("track"),
        )
        ann_by_key[key] = a
    scenes = []
    for beat in j.get("beats", []):
        for sc in beat.get("scenes", []) or []:
            scenes.append((beat.get("id"), sc))
    return {"annotations": ann_by_key, "scenes": scenes}


_PAREN_RE = re.compile(r"\([^)]*\)")
_SENT_SPLIT_RE = re.compile(r"[.!?](?:\s|$)")


def _strip_parentheticals(text: str) -> str:
    """Remove `(...)` spans. Parentheticals routinely hold cross-asset references
    like "(C0150, src 25-31)" or "(earlier v3 plan proposed src 0-10)" that are
    commentary, not primary claims about THIS asset.
    """
    return _PAREN_RE.sub(" ", text)


def _sentences(text: str) -> list[str]:
    """Split on . ! ? boundaries. Loose but good enough for editor_notes prose."""
    return [s.strip() for s in _SENT_SPLIT_RE.split(text) if s.strip()]


def _detect_stale_usage(
    notes_doc: dict,
    actual_windows_for_asset: list[tuple[int, int]],
) -> list[dict]:
    """For each (start_sec, end_sec) mentioned in a 'usage' note, check whether
    any actual cut window overlaps by >=1s. Returns one dict per mention
    that is NOT in the cut.

    Only `usage`-typed notes are scanned (stability/framing notes quote
    historical iteration windows or reference framings as info, not claims).
    Parentheticals are stripped first (they routinely hold cross-asset
    references). Sentences containing disqualifying tokens (v1/v2/previously/
    avoid/rejected) are also skipped -- those windows aren't current claims.

    SPECULATIVE/FRAGILE: regex-on-prose. Two false-positive rounds in one
    session (the C0150 "v1 used src 0-10" and C0051 "(C0150, src 25-31)"
    cross-reference). Per-sentence + paren-strip mitigates but doesn't fix.
    "Stale" is also a pattern that only matters when notes are written ahead
    of the cut -- rare today (~7 editor_notes across the workspace), so this
    check is mostly inert. Real fix: structured fields on editor_notes
    (`usage_src_windows: [[10.0, 33.5]]`) parsed exactly, no regex.
    """
    if not notes_doc:
        return []
    stale = []
    actual_sec = [(f2s(a), f2s(b)) for a, b in actual_windows_for_asset]
    for n in notes_doc.get("notes", []) or []:
        text = n.get("text") or ""
        ntype = (n.get("type") or "").lower()
        if ntype != "usage":
            continue
        cleaned = _strip_parentheticals(text)
        for sent in _sentences(cleaned):
            sl = sent.lower()
            if any(tok in sl for tok in ("v1 ", "v2 ", "previously", "rejected", "avoid")):
                continue
            for m in SRC_WINDOW_RE.finditer(sent):
                note_start = float(m.group(1))
                note_end = float(m.group(2))
                if note_end <= note_start:
                    continue
                overlap_any = any(
                    max(0.0, min(b, note_end) - max(a, note_start)) >= 1.0
                    for (a, b) in actual_sec
                )
                if not overlap_any:
                    stale.append({
                        "note_type": ntype,
                        "claimed_window_sec": [note_start, note_end],
                        "note_text_excerpt": text[:160],
                    })
    return stale


def _detect_avoid_violation(
    notes_doc: dict,
    cut_src_in_sec: float,
    cut_src_out_sec: float,
) -> list[dict]:
    """Find src windows the notes flag as "avoid" that the cut overlaps.

    Per-sentence (not per-note) check: a note like "v3d uses src 10-33.5s ...
    Avoid src 0-10s wobble." has two windows but only the second is an avoid
    claim. Restricting to sentences that themselves contain an avoid-token
    catches the right one without false-positiving on the usage window.

    FRAGILE: regex-on-prose. The check itself is earned (C0150/C0147 wobble
    avoidance is real editorial signal). Same fix as _detect_stale_usage:
    add structured `avoid_src_windows: [[0.0, 10.0]]` to editor_notes
    schema once we have enough notes to see the shape.
    """
    if not notes_doc:
        return []
    violations = []
    AVOID_TOKENS = ("avoid", "skip", "don't use", "do not use", "stay away")
    for n in notes_doc.get("notes", []) or []:
        text = n.get("text") or ""
        ntype = (n.get("type") or "").lower()
        cleaned = _strip_parentheticals(text)
        for sent in _sentences(cleaned):
            sl = sent.lower()
            if not (ntype == "avoid" or any(tok in sl for tok in AVOID_TOKENS)):
                continue
            for m in SRC_WINDOW_RE.finditer(sent):
                avoid_s = float(m.group(1))
                avoid_e = float(m.group(2))
                ov = max(0.0, min(cut_src_out_sec, avoid_e) - max(cut_src_in_sec, avoid_s))
                if ov > 0.5:
                    violations.append({
                        "avoid_window_sec": [avoid_s, avoid_e],
                        "cut_window_sec": [cut_src_in_sec, cut_src_out_sec],
                        "overlap_sec": ov,
                        "note_text_excerpt": text[:160],
                    })
    return violations


def _compute_track_gaps(
    clip_spans: list[tuple[int, int]],
    scene_range: tuple[int, int],
) -> list[tuple[int, int]]:
    """Given list of (start, end) frame spans on a track and a scene window,
    return list of (gap_start, gap_end) frame pairs where the track is empty.
    """
    a, b = scene_range
    spans = sorted([(max(s, a), min(e, b)) for s, e in clip_spans if e > a and s < b])
    gaps = []
    cursor = a
    for s, e in spans:
        if s > cursor:
            gaps.append((cursor, s))
        cursor = max(cursor, e)
    if cursor < b:
        gaps.append((cursor, b))
    return gaps


def audit_cut(xml_path: Path, sidecar_path: Optional[Path]) -> CutAuditReport:
    clipitems, _files = _extract_video_clipitems(xml_path)
    rev = _build_asset_map_reverse()
    # Resolve asset_id per clipitem (skip transition sentinels with no real range)
    for ci in clipitems:
        ci["asset_id"] = None
        if ci["pathurl"]:
            rel = _pathurl_to_relpath(ci["pathurl"])
            if rel:
                ci["asset_id"] = rev.get(rel.replace("/", "\\").lower())

    resolved_aids = {ci["asset_id"] for ci in clipitems if ci["asset_id"]}
    asset_meta = _load_asset_meta(resolved_aids)
    notes_by_aid = _load_editor_notes(resolved_aids)

    # Index actual source windows per asset (only forward, non-sentinel ranges)
    src_windows_by_aid: dict[str, list[tuple[int, int]]] = {}
    for ci in clipitems:
        if not ci["asset_id"]:
            continue
        if ci["in"] < 0 or ci["out"] < 0 or ci["out"] <= ci["in"]:
            continue
        src_windows_by_aid.setdefault(ci["asset_id"], []).append((ci["in"], ci["out"]))

    sidecar_index = {"annotations": {}, "scenes": []}
    if sidecar_path and sidecar_path.exists():
        sidecar_index = _build_sidecar_index(sidecar_path)

    clip_audits: list[ClipAudit] = []
    all_stale: list[dict] = []
    for ci in clipitems:
        aid = ci["asset_id"]
        meta = asset_meta.get(aid, {}) if aid else {}
        notes_doc = notes_by_aid.get(aid, {}) if aid else {}
        tags = notes_doc.get("tags") or []
        # Sidecar annotation lookup
        key = (aid, ci["in"], ci["out"], ci["start"], ci["track"])
        ann = sidecar_index["annotations"].get(key)
        rationale = ann.get("rationale") if ann else None
        # Skip flagging anything for transition sentinels
        is_sentinel = ci["in"] < 0 or ci["out"] < 0
        flags: list[str] = []
        flag_detail: dict = {}
        # Detect Premiere graphics/lower-thirds: name="Graphic" or no pathurl.
        # These never resolve to an asset_id (no proxy), and they don't carry
        # editorial-cut concerns -- they're titling/graphics tracks.
        is_graphic = (ci["name"] or "").lower().startswith("graphic") or not ci["pathurl"]
        if is_graphic:
            flags.append("PREMIERE_GRAPHIC")
        elif not aid:
            flags.append("UNRESOLVED_ASSET")
        if is_sentinel:
            flags.append("TRANSITION_SENTINEL")
        else:
            # Source-bounds check
            if aid and meta.get("duration_sec"):
                dur = float(meta["duration_sec"])
                src_out_sec = f2s(ci["out"])
                if src_out_sec > dur + 0.05:
                    flags.append("SOURCE_OOB")
                    flag_detail["source_oob"] = {
                        "src_out_sec": round(src_out_sec, 3),
                        "asset_duration_sec": round(dur, 3),
                    }
            # avoid-window violations
            if aid:
                avs = _detect_avoid_violation(
                    notes_doc, f2s(ci["in"]), f2s(ci["out"])
                )
                if avs:
                    flags.append("AVOID_WINDOW_HIT")
                    flag_detail["avoid_window_hits"] = avs
            # missing notes (only for b-roll bucket)
            if aid and meta.get("bucket") == "b_roll" and aid not in notes_by_aid:
                flags.append("NOTES_MISSING")
            # missing rationale (only when annotation exists)
            if ann is not None and not rationale:
                flags.append("RATIONALE_MISSING")
        clip_audits.append(ClipAudit(
            clip_id=ci["clip_id"],
            track=ci["track"],
            name=ci["name"],
            asset_id=aid,
            timeline_start_frames=ci["start"],
            timeline_end_frames=ci["end"],
            source_in_frames=ci["in"],
            source_out_frames=ci["out"],
            timeline_start_sec=round(f2s(ci["start"]), 3) if ci["start"] >= 0 else -1.0,
            timeline_end_sec=round(f2s(ci["end"]), 3) if ci["end"] >= 0 else -1.0,
            source_in_sec=round(f2s(ci["in"]), 3) if ci["in"] >= 0 else -1.0,
            source_out_sec=round(f2s(ci["out"]), 3) if ci["out"] >= 0 else -1.0,
            asset_duration_sec=meta.get("duration_sec"),
            asset_bucket=meta.get("bucket"),
            editor_notes_present=aid in notes_by_aid if aid else False,
            editor_tags=tags,
            rationale_present=bool(rationale),
            flags=flags,
            flag_detail=flag_detail,
        ))

    # Stale-usage detection is per asset, not per clipitem -- aggregate once.
    for aid, notes_doc in notes_by_aid.items():
        actual = src_windows_by_aid.get(aid, [])
        stale = _detect_stale_usage(notes_doc, actual)
        for s in stale:
            s["asset_id"] = aid
            s["filename"] = notes_doc.get("filename")
            s["actual_windows_sec"] = [
                [round(f2s(a), 2), round(f2s(b), 2)] for a, b in actual
            ]
            all_stale.append(s)

    # Per-scene coverage
    scene_audits: list[SceneAudit] = []
    by_track: dict[str, list[tuple[int, int]]] = {}
    union_spans: list[tuple[int, int]] = []
    for ci in clipitems:
        if ci["start"] < 0 or ci["end"] < 0:
            continue
        # Skip graphics from coverage union -- titles/lower-thirds don't count
        # as "visual cover" of an A1 audio anchor.
        if (ci["name"] or "").lower().startswith("graphic"):
            continue
        by_track.setdefault(ci["track"], []).append((ci["start"], ci["end"]))
        union_spans.append((ci["start"], ci["end"]))
    for beat_id, sc in sidecar_index["scenes"]:
        rng = tuple(sc.get("timeline_range_frames") or [0, 0])  # type: ignore
        v1_spans = by_track.get("V1", [])
        v3_spans = by_track.get("V3", [])
        sa = SceneAudit(
            scene_id=sc.get("id"),
            label=sc.get("label", ""),
            beat_id=beat_id,
            timeline_range_frames=rng,
            v1_clip_count=sum(1 for s, e in v1_spans if s < rng[1] and e > rng[0]),
            v3_clip_count=sum(1 for s, e in v3_spans if s < rng[1] and e > rng[0]),
            # SPECULATIVE -- see SceneAudit field comments. Kept for now so the
            # report can show coverage at a glance, but no iteration has actually
            # benefited from these yet.
            visual_hole_frames=_compute_track_gaps(union_spans, rng),
            v3_gaps_frames=_compute_track_gaps(v3_spans, rng),
        )
        # Annotations missing rationale within this scene
        for ann in sidecar_index["annotations"].values():
            if ann.get("scene") != sa.scene_id:
                continue
            if ann.get("key", {}).get("track", "").startswith("V") and not ann.get("rationale"):
                sa.annotations_missing_rationale.append(ann.get("clip_id"))
        scene_audits.append(sa)

    # Summary counters
    summary = {
        "total_clipitems": len(clip_audits),
        "by_track": {},
        "flags_count": {},
        "stale_usage_count": len(all_stale),
        "scenes": len(scene_audits),
    }
    for c in clip_audits:
        summary["by_track"].setdefault(c.track, 0)
        summary["by_track"][c.track] += 1
        for f in c.flags:
            summary["flags_count"][f] = summary["flags_count"].get(f, 0) + 1

    return CutAuditReport(
        xml_path=str(xml_path),
        sidecar_path=str(sidecar_path) if sidecar_path else None,
        clip_audits=clip_audits,
        scene_audits=scene_audits,
        summary=summary,
        stale_usage_claims=all_stale,
    )


def _fmt_frame_range(rng: tuple[int, int]) -> str:
    return f"{rng[0]}-{rng[1]} ({f2s(rng[0]):.2f}-{f2s(rng[1]):.2f}s)"


def render_report(rep: CutAuditReport, *, scene_filter: Optional[str] = None) -> str:
    lines: list[str] = []
    lines.append(f"# Cut Audit: {Path(rep.xml_path).name}")
    if rep.sidecar_path:
        lines.append(f"Sidecar: {Path(rep.sidecar_path).name}")
    lines.append("")
    lines.append("## Summary")
    for k, v in rep.summary.items():
        if isinstance(v, dict):
            lines.append(f"  {k}:")
            for k2, v2 in v.items():
                lines.append(f"    {k2}: {v2}")
        else:
            lines.append(f"  {k}: {v}")
    lines.append("")

    # Scenes
    lines.append("## Scenes")
    for sa in rep.scene_audits:
        if scene_filter and sa.scene_id != scene_filter:
            continue
        lines.append(f"  [{sa.beat_id}] {sa.scene_id} - {sa.label} {_fmt_frame_range(sa.timeline_range_frames)}")
        lines.append(f"     V1 clipitems: {sa.v1_clip_count}, V3 clipitems: {sa.v3_clip_count}")
        if sa.visual_hole_frames:
            for g in sa.visual_hole_frames:
                lines.append(f"     ! visual hole (no V-track): {_fmt_frame_range(g)}")
        if sa.v3_gaps_frames and not sa.visual_hole_frames:
            # Only show V3-only gaps when there's no bigger problem
            for g in sa.v3_gaps_frames:
                lines.append(f"     V3 gap (V1/V2 may cover): {_fmt_frame_range(g)}")
        if sa.annotations_missing_rationale:
            lines.append(f"     rationale-missing ({len(sa.annotations_missing_rationale)}): {', '.join(sa.annotations_missing_rationale)}")
    lines.append("")

    # Flagged clipitems -- exclude noisy categories from the global list:
    # PREMIERE_GRAPHIC (always unresolved; not editorial), TRANSITION_SENTINEL
    # (structural xml artifact), RATIONALE_MISSING (already shown per-scene).
    NOISY = {"PREMIERE_GRAPHIC", "TRANSITION_SENTINEL", "RATIONALE_MISSING"}
    flagged = [c for c in rep.clip_audits if set(c.flags) - NOISY]
    if scene_filter:
        target = next((s for s in rep.scene_audits if s.scene_id == scene_filter), None)
        if target:
            a, b = target.timeline_range_frames
            flagged = [c for c in flagged if c.timeline_start_frames < b and c.timeline_end_frames > a]
    lines.append(f"## Flagged clipitems ({len(flagged)}) -- actionable only (noisy categories suppressed)")
    for c in flagged:
        lines.append(
            f"  [{c.track}] {c.clip_id} {c.name} tl={c.timeline_start_sec:.2f}-{c.timeline_end_sec:.2f}s "
            f"src={c.source_in_sec:.2f}-{c.source_out_sec:.2f}s flags={c.flags}"
        )
        if c.flag_detail:
            lines.append(f"    detail: {json.dumps(c.flag_detail, indent=None, default=str)}")
    lines.append("")

    # Stale usage claims
    stale = rep.stale_usage_claims
    if scene_filter:
        # Only show stale claims for assets used in the filtered scene
        scene_aids = {c.asset_id for c in rep.clip_audits if c.asset_id and
                      c.timeline_start_frames < rep.scene_audits[0].timeline_range_frames[1]}
        # Simpler: don't filter -- stale claims are global
    lines.append(f"## Stale editor_notes 'usage' claims ({len(stale)})")
    for s in stale:
        lines.append(
            f"  {s['filename']} aid={s['asset_id'][:16]} claimed src={s['claimed_window_sec'][0]}-{s['claimed_window_sec'][1]}s "
            f"(actual cut windows: {s['actual_windows_sec']})"
        )
        lines.append(f"    excerpt: {s['note_text_excerpt']}")
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    ap.add_argument("xml", help="Path to xmeml v4 export")
    ap.add_argument("--sidecar", help="Path to Act sidecar JSON", default=None)
    ap.add_argument("--scene", help="Filter report to one scene id", default=None)
    ap.add_argument("--json", dest="json_out", help="Write structured JSON to this path", default=None)
    args = ap.parse_args(argv)
    xml_path = Path(args.xml)
    sidecar_path = Path(args.sidecar) if args.sidecar else None
    if sidecar_path is None:
        # Try the canonical Act II sidecar by default
        candidate = repo_root() / "editor" / "story" / "sidecars" / "actII.sidecar.json"
        if candidate.exists():
            sidecar_path = candidate
    rep = audit_cut(xml_path, sidecar_path)
    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps(asdict(rep), indent=2, default=str),
            encoding="utf-8",
        )
    print(render_report(rep, scene_filter=args.scene))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
