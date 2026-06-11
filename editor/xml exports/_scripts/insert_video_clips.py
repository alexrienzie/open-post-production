#!/usr/bin/env python3
"""Insert video clipitems into an xmeml file from a JSON plan.

Round-trip safe: reads an existing Premiere FCP7 XML export, adds new
<file> defs + <clipitem> elements on the target video track, writes a NEW
xmeml file alongside the original. Never overwrites the source.

Usage:
  py editor/xml exports/_scripts/insert_video_clips.py \
      --plan "editor/xml exports/_plans/<my_plan>.json"

  --output PATH         Override output xmeml path
  --dry-run             Print the planned changes; don't write
  --validate-only       Resolve all assets + check for overlaps; don't write

The JSON plan schema:

  {
    "source_xml":         "project_act II_premiere export_20260520.xml",
    "target_video_track": "V4",
    "fps":                23.976023976,
    "output_name_suffix": "broll_test",                // appended to output filename
    "sequence_name":      "Act II (b-roll test)",      // optional, overrides <sequence><name>
    "insertions": [
      {
        "asset_id":            "0eaa22b8...",
        "label":               "example b-roll close-up",
        "timeline_start_sec":  307.39,
        "timeline_end_sec":    317.39,
        "source_in_sec":       0.0,
        "source_out_sec":      10.0
      },
      ...
    ]
  }
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path
from typing import Optional

from lxml import etree

# Make sibling helper importable
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

# Make dataset/_scripts importable for workspace_paths
_REPO = _HERE.parent.parent.parent  # editor/xml exports/_scripts -> open-post-stack
_DATASET_SCRIPTS = _REPO / "dataset" / "_scripts"
if str(_DATASET_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_DATASET_SCRIPTS))

import _pproticks as ticks  # noqa: E402
from workspace_paths import editorial_catalog_sqlite_path  # noqa: E402


# ---------------------------------------------------------------------------
# Plan + catalog lookup


def _load_plan(plan_path: Path) -> dict:
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    required = {"source_xml", "target_video_track", "insertions"}
    missing = required - set(plan)
    if missing:
        raise SystemExit(f"plan missing required keys: {missing}")
    if not plan["insertions"]:
        raise SystemExit("plan has no insertions")
    plan.setdefault("fps", 23.976023976)
    return plan


def _resolve_source_xml(plan_path: Path, source_xml: str) -> Path:
    """source_xml may be relative to the plan file dir's parent (xml exports/)."""
    p = Path(source_xml)
    if p.is_absolute() and p.exists():
        return p
    # plan_path is in xml exports/_plans/, so xml is in xml exports/
    candidates = [
        plan_path.parent.parent / source_xml,  # xml exports/<file>
        plan_path.parent / source_xml,
        Path.cwd() / source_xml,
    ]
    for c in candidates:
        if c.exists():
            return c.resolve()
    raise SystemExit(f"source_xml not found. tried: {[str(c) for c in candidates]}")


# Windows-side root for pathurl generation. The user's Premiere ALWAYS runs on
# the editing machine where derivative media lives at E:\\open-post-stack\\derivative media\\.
# We hardcode this so the test can produce a valid xmeml even when the script
# is run from a sandbox / alt-mount where _REPO resolves differently.
DERIVATIVE_MEDIA_PATHURL_ROOT = r"E:\open-post-stack\derivative media"

def _proxy_path_for(asset_map: dict, asset_id: str) -> Optional[str]:
    """Return the Windows-style proxy path for an asset, or None if no proxy.

    The path is always rooted at DERIVATIVE_MEDIA_PATHURL_ROOT (the editing machine's
    E:\\open-post-stack\\derivative media\\) so the resulting xmeml is portable to
    the user's editing machine regardless of where this script runs.
    Returns a string with Windows-style backslashes; the pathurl encoder
    handles separator normalization + percent-encoding.
    """
    e = (asset_map.get("entries") or {}).get(asset_id)
    if not e:
        return None
    proxy = e.get("video_video_proxy")
    if not proxy:
        return None
    rel = proxy.get("relative_path")
    if not rel:
        return None
    # rel is already Windows-style (backslashes from asset_map)
    return DERIVATIVE_MEDIA_PATHURL_ROOT + "\\" + rel.lstrip("\\/")


def _load_asset_map(repo_root: Path) -> dict:
    p = repo_root / "derivative media" / "_index" / "asset_map.json"
    if not p.exists():
        return {"entries": {}}
    return json.loads(p.read_text(encoding="utf-8"))


def _lookup_asset(
    catalog_db: Path,
    asset_id: str,
    asset_map: dict,
) -> dict:
    con = sqlite3.connect(str(catalog_db))
    con.row_factory = sqlite3.Row
    row = con.execute(
        "SELECT asset_id, filename, source_path, duration_sec, width, height "
        "FROM asset WHERE asset_id = ?",
        (asset_id,),
    ).fetchone()
    con.close()
    if not row:
        raise SystemExit(f"asset_id not found in catalog: {asset_id}")
    d = dict(row)
    if not d.get("filename"):
        raise SystemExit(f"asset {asset_id} missing filename")
    # Catalog width/height reflect the ORIGINAL source (often 3840x2160).
    # The actual proxy file on disk is 1280x720. We override here so the
    # <samplecharacteristics> in the new <file> def matches the proxy.
    d["_source_width"] = d.get("width") or 0
    d["_source_height"] = d.get("height") or 0
    d["width"] = 1280
    d["height"] = 720
    # Prefer the proxy path on E:\open-post-stack\derivative media\ for Premiere.
    # Fall back to catalog source_path (D:\ camera cards) if no proxy exists.
    proxy = _proxy_path_for(asset_map, asset_id)
    if proxy is not None:
        d["source_path"] = proxy
        d["_path_source"] = "proxy"
    elif d.get("source_path"):
        d["_path_source"] = "catalog_source_path"
        print(f"  WARN: {d.get('filename')} has no proxy entry; falling back to {d['source_path']}")
    else:
        raise SystemExit(f"asset {asset_id} has neither proxy nor source_path")
    return d


# ---------------------------------------------------------------------------
# XML inspection helpers


def _max_numeric_id(root: etree._Element, attribute_prefix: str, tags: list[str]) -> int:
    """Return the max NNN in id="<prefix>NNN" across given tag names."""
    max_n = 0
    for tag in tags:
        for el in root.iter(tag):
            val = el.get("id") or ""
            if val.startswith(attribute_prefix):
                try:
                    n = int(val[len(attribute_prefix) :])
                    if n > max_n:
                        max_n = n
                except ValueError:
                    pass
    return max_n


def _max_masterclip_id(root: etree._Element) -> int:
    max_n = 0
    for el in root.iter("masterclipid"):
        v = (el.text or "").strip()
        if v.startswith("masterclip-"):
            try:
                n = int(v[len("masterclip-") :])
                if n > max_n:
                    max_n = n
            except ValueError:
                pass
    return max_n


def _existing_file_id_by_filename(root: etree._Element) -> dict[str, str]:
    """Map filename (e.g. 'C0150.MP4') -> existing 'file-NNN' id (if defined)."""
    out: dict[str, str] = {}
    for fe in root.iter("file"):
        fid = fe.get("id")
        if not fid:
            continue
        name_el = fe.find("name")
        if name_el is not None and name_el.text:
            out.setdefault(name_el.text.strip(), fid)
    return out


def _existing_masterclipid_by_filename(root: etree._Element) -> dict[str, str]:
    """Map filename -> masterclipid string reused for that source file."""
    out: dict[str, str] = {}
    for ci in root.iter("clipitem"):
        name = (ci.findtext("name") or "").strip()
        mcid = (ci.findtext("masterclipid") or "").strip()
        if name and mcid and name not in out:
            out[name] = mcid
    return out


def _track_clipitem_ranges(track: etree._Element) -> list[tuple[int, int]]:
    out = []
    for ci in track.findall("clipitem"):
        try:
            s = int(ci.findtext("start") or "0")
            e = int(ci.findtext("end") or "0")
            out.append((s, e))
        except ValueError:
            pass
    return out


def _video_tracks(root: etree._Element) -> list[etree._Element]:
    seq = root.find("sequence")
    if seq is None:
        raise SystemExit("xmeml has no <sequence>")
    media = seq.find("media")
    if media is None:
        raise SystemExit("xmeml sequence has no <media>")
    video = media.find("video")
    if video is None:
        raise SystemExit("xmeml sequence/media has no <video>")
    return video.findall("track")


def _resolve_target_track(root: etree._Element, target: str) -> etree._Element:
    """target like 'V4'."""
    if not target.upper().startswith("V"):
        raise SystemExit(f"target_video_track must start with V; got {target!r}")
    try:
        n = int(target[1:])
    except ValueError:
        raise SystemExit(f"can't parse track index from {target!r}")
    tracks = _video_tracks(root)
    if n < 1 or n > len(tracks):
        raise SystemExit(
            f"target {target} out of range: xmeml has V1..V{len(tracks)}"
        )
    return tracks[n - 1]


# ---------------------------------------------------------------------------
# Building new XML elements


def _build_file_def(
    *,
    file_id: str,
    filename: str,
    source_path: str,
    duration_frames: int,
    width: int,
    height: int,
) -> etree._Element:
    """Build the full <file id=...>...</file> body (first occurrence)."""
    f = etree.Element("file", id=file_id)
    etree.SubElement(f, "name").text = filename
    etree.SubElement(f, "pathurl").text = ticks.windows_path_to_pathurl(source_path)
    rate = etree.SubElement(f, "rate")
    etree.SubElement(rate, "timebase").text = "24"
    etree.SubElement(rate, "ntsc").text = "TRUE"
    etree.SubElement(f, "duration").text = str(int(duration_frames))
    tc = etree.SubElement(f, "timecode")
    tc_rate = etree.SubElement(tc, "rate")
    etree.SubElement(tc_rate, "timebase").text = "24"
    etree.SubElement(tc_rate, "ntsc").text = "TRUE"
    etree.SubElement(tc, "string").text = "00:00:00:00"
    etree.SubElement(tc, "frame").text = "0"
    etree.SubElement(tc, "displayformat").text = "NDF"
    media = etree.SubElement(f, "media")
    video = etree.SubElement(media, "video")
    vsc = etree.SubElement(video, "samplecharacteristics")
    vsc_rate = etree.SubElement(vsc, "rate")
    etree.SubElement(vsc_rate, "timebase").text = "24"
    etree.SubElement(vsc_rate, "ntsc").text = "TRUE"
    etree.SubElement(vsc, "width").text = str(int(width or 1280))
    etree.SubElement(vsc, "height").text = str(int(height or 720))
    etree.SubElement(vsc, "anamorphic").text = "FALSE"
    etree.SubElement(vsc, "pixelaspectratio").text = "square"
    etree.SubElement(vsc, "fielddominance").text = "none"
    audio = etree.SubElement(media, "audio")
    asc = etree.SubElement(audio, "samplecharacteristics")
    etree.SubElement(asc, "depth").text = "16"
    etree.SubElement(asc, "samplerate").text = "48000"
    etree.SubElement(audio, "channelcount").text = "2"
    return f



def fit_to_frame_scale(
    src_w: int, src_h: int, *, seq_w: int = 1280, seq_h: int = 720, mode: str = "fill"
) -> float:
    """Premiere Motion>Scale percent to fit/fill a still into the sequence frame.

    100 = source pixels mapped 1:1 onto the sequence (verified against our 1280x720
    video proxies, which sit at scale=100). To resize a `src_w x src_h` still:
      - mode="fit"  -> contain: whole image visible, letterbox/pillarbox  (min ratio)
      - mode="fill" -> cover:   fills frame, crops the overflow axis      (max ratio)

    Baking this into a clip's Basic Motion `scale` SURVIVES the FCP7 XML round-trip,
    unlike Premiere's per-clip "Scale to Frame Size" toggle (no xmeml representation,
    dropped on every export — hence the manual re-marking).
    """
    if not src_w or not src_h:
        return 100.0
    rw, rh = seq_w / float(src_w), seq_h / float(src_h)
    r = min(rw, rh) if mode == "fit" else max(rw, rh)
    return round(r * 100.0, 3)


def _build_basic_motion_filter(scale: float = 100.0) -> etree._Element:
    """Emit a Basic Motion <filter> block at the given Scale percent.

    Every existing video clipitem in this xmeml has one of these. Premiere
    needs the filter to know how to transform the clip into the frame;
    without it the clip flickers between displayed and black during scrub
    and playback. `scale` defaults to 100 (1:1) — correct for our 1280x720
    video proxies. STILLS at other dimensions should pass a value from
    `fit_to_frame_scale()` so they import already framed (a baked scale
    survives XML; the "Scale to Frame Size" toggle does not).
    """
    f = etree.Element("filter")
    eff = etree.SubElement(f, "effect")
    etree.SubElement(eff, "name").text = "Basic Motion"
    etree.SubElement(eff, "effectid").text = "basic"
    etree.SubElement(eff, "effectcategory").text = "motion"
    etree.SubElement(eff, "effecttype").text = "motion"
    etree.SubElement(eff, "mediatype").text = "video"
    etree.SubElement(eff, "pproBypass").text = "false"

    def _scalar(pid, name, vmin, vmax, value):
        p = etree.SubElement(eff, "parameter", authoringApp="PremierePro")
        etree.SubElement(p, "parameterid").text = pid
        etree.SubElement(p, "name").text = name
        etree.SubElement(p, "valuemin").text = str(vmin)
        etree.SubElement(p, "valuemax").text = str(vmax)
        etree.SubElement(p, "value").text = str(value)

    def _xy(pid, name, x, y):
        p = etree.SubElement(eff, "parameter", authoringApp="PremierePro")
        etree.SubElement(p, "parameterid").text = pid
        etree.SubElement(p, "name").text = name
        val = etree.SubElement(p, "value")
        etree.SubElement(val, "horiz").text = str(x)
        etree.SubElement(val, "vert").text = str(y)

    _scalar("scale", "Scale", 0, 1000, scale)
    _scalar("rotation", "Rotation", -8640, 8640, 0)
    _xy("center", "Center", 0, 0)
    _xy("centerOffset", "Anchor Point", 0, 0)
    _scalar("antiflicker", "Anti-flicker Filter", "0.0", "1.0", 0)
    _scalar("leftcrop", "Left", "0.0", "100.0", 0)
    _scalar("topcrop", "Top", "0.0", "100.0", 0)
    _scalar("rightcrop", "Right", "0.0", "100.0", 0)
    _scalar("bottomcrop", "Bottom", "0.0", "100.0", 0)
    return f


def _build_clipitem(
    *,
    clipitem_id: str,
    masterclip_id: str,
    filename: str,
    duration_frames: int,
    start_frame: int,
    end_frame: int,
    in_frame: int,
    out_frame: int,
    file_element_or_stub: etree._Element,
    track_index_for_link: int = 1,
) -> etree._Element:
    ci = etree.Element("clipitem", id=clipitem_id)
    etree.SubElement(ci, "masterclipid").text = masterclip_id
    etree.SubElement(ci, "name").text = filename
    etree.SubElement(ci, "enabled").text = "TRUE"
    etree.SubElement(ci, "duration").text = str(int(duration_frames))
    rate = etree.SubElement(ci, "rate")
    etree.SubElement(rate, "timebase").text = "24"
    etree.SubElement(rate, "ntsc").text = "TRUE"
    etree.SubElement(ci, "start").text = str(int(start_frame))
    etree.SubElement(ci, "end").text = str(int(end_frame))
    etree.SubElement(ci, "in").text = str(int(in_frame))
    etree.SubElement(ci, "out").text = str(int(out_frame))
    etree.SubElement(ci, "pproTicksIn").text = str(ticks.ticks_for_frame(in_frame))
    etree.SubElement(ci, "pproTicksOut").text = str(ticks.ticks_for_frame(out_frame))
    etree.SubElement(ci, "alphatype").text = "none"
    # Premiere expects these at clipitem level on every video clipitem.
    etree.SubElement(ci, "pixelaspectratio").text = "square"
    etree.SubElement(ci, "anamorphic").text = "FALSE"
    # The file element: either a full def (first occurrence) or a stub.
    ci.append(file_element_or_stub)
    # Basic Motion filter: required for Premiere to render the clip stably.
    # Without it, the clip flickers between visible and black during scrub.
    ci.append(_build_basic_motion_filter())
    # Self-link: tells Premiere this is a video clipitem.
    link = etree.SubElement(ci, "link")
    etree.SubElement(link, "linkclipref").text = clipitem_id
    etree.SubElement(link, "mediatype").text = "video"
    etree.SubElement(link, "trackindex").text = str(track_index_for_link)
    etree.SubElement(link, "clipindex").text = "1"
    # Minimal logginginfo / colorinfo to match Premiere's style.
    li = etree.SubElement(ci, "logginginfo")
    for tag in (
        "description",
        "scene",
        "shottake",
        "lognote",
        "good",
        "originalvideofilename",
        "originalaudiofilename",
    ):
        etree.SubElement(li, tag)
    co = etree.SubElement(ci, "colorinfo")
    for tag in ("lut", "lut1", "asc_sop", "asc_sat", "lut2"):
        etree.SubElement(co, tag)
    return ci


# ---------------------------------------------------------------------------
# Overlap check + ordered insert


def _check_no_overlap(
    track_ranges: list[tuple[int, int]],
    new_ranges: list[tuple[int, int]],
) -> list[str]:
    errors = []
    all_ranges = sorted(track_ranges + new_ranges)
    for i in range(1, len(all_ranges)):
        a_start, a_end = all_ranges[i - 1]
        b_start, b_end = all_ranges[i]
        if a_end > b_start:
            errors.append(
                f"  overlap: [{a_start},{a_end}) intersects [{b_start},{b_end})"
            )
    return errors


def _insert_ordered(track: etree._Element, new_clipitems: list[etree._Element]) -> None:
    """Insert into the track ordered by <start>, keeping any non-clipitem children
    in place (e.g. track attributes are stored on the element itself)."""
    existing = list(track.findall("clipitem"))
    # Strip existing clipitems out so we can re-append in start-order.
    for ci in existing:
        track.remove(ci)
    combined = existing + new_clipitems
    combined.sort(key=lambda ci: int(ci.findtext("start") or "0"))
    for ci in combined:
        track.append(ci)



# ---------------------------------------------------------------------------
# Ripple, replace, sequence-format helpers


def _all_clipitems(root):
    """Iterate every <clipitem> in the sequence (all V/A tracks)."""
    seq = root.find("sequence")
    if seq is None:
        return
    media = seq.find("media")
    if media is None:
        return
    for kind in ("video", "audio"):
        kind_el = media.find(kind)
        if kind_el is None:
            continue
        for track in kind_el.findall("track"):
            for ci in track.findall("clipitem"):
                yield ci


def _all_transitionitems(root):
    """Iterate every <transitionitem> in the sequence (all V/A tracks).

    Transitions (audio crossfades, video dissolves) live on the same tracks
    as clipitems but as a separate element type. They MUST be shifted with
    the surrounding clipitems or the cuts break: clipitems whose start or
    end is the sentinel -1 take their edge from the transition's start/end,
    so a stale transition position leaves the clipitem in an inconsistent
    state and audio/video plays wrong.
    """
    seq = root.find("sequence")
    if seq is None:
        return
    media = seq.find("media")
    if media is None:
        return
    for kind in ("video", "audio"):
        kind_el = media.find(kind)
        if kind_el is None:
            continue
        for track in kind_el.findall("track"):
            for ti in track.findall("transitionitem"):
                yield ti


def ripple_shift_after(root, ripple_frame: int, delta_frames: int) -> dict:
    """Shift every clipitem AND transitionitem whose <start> >= ripple_frame
    by +delta_frames.

    Also extends <sequence><duration> to keep the timeline coherent.

    Returns: stats dict with counts shifted / straddled.
    """
    shifted = 0
    transitions_shifted = 0
    straddled = []
    for ci in _all_clipitems(root):
        try:
            s = int(ci.findtext("start") or "0")
            e = int(ci.findtext("end") or "0")
        except ValueError:
            continue
        # Premiere uses start=-1 OR end=-1 as a "through-edit pair" sentinel:
        # two clipitems share a continuous source but are split on the timeline.
        # Both halves must have their non-sentinel field shifted independently.
        is_through_edit = (s == -1) or (e == -1)
        if is_through_edit:
            if s >= 0 and s >= ripple_frame:
                ci.find("start").text = str(s + delta_frames)
                shifted += 1
            if e >= 0 and e >= ripple_frame:
                ci.find("end").text = str(e + delta_frames)
                shifted += 1
            continue
        if s >= ripple_frame:
            ci.find("start").text = str(s + delta_frames)
            ci.find("end").text = str(e + delta_frames)
            shifted += 1
        elif e > ripple_frame:
            straddled.append({"id": ci.get("id"), "name": ci.findtext("name"), "start": s, "end": e})
    # Shift transitionitems on every track. Transitions don't have through-edit
    # sentinels and don't straddle in the usual sense -- a transition fully sits
    # between two clipitems. Shift it if start >= ripple_frame.
    for ti in _all_transitionitems(root):
        s_el = ti.find("start")
        e_el = ti.find("end")
        if s_el is None or e_el is None:
            continue
        try:
            s = int(s_el.text or "0")
            e = int(e_el.text or "0")
        except ValueError:
            continue
        if s >= ripple_frame:
            s_el.text = str(s + delta_frames)
            e_el.text = str(e + delta_frames)
            transitions_shifted += 1
        elif e > ripple_frame:
            # Transition straddles ripple point -- very unusual; warn but don't shift
            straddled.append({"id": "transitionitem", "name": "(transition)", "start": s, "end": e})

    seq = root.find("sequence")
    dur_el = seq.find("duration")
    cur = int(dur_el.text or "0")
    dur_el.text = str(cur + delta_frames)
    return {
        "shifted": shifted,
        "transitions_shifted": transitions_shifted,
        "straddled": straddled,
        "delta_frames": delta_frames,
    }


def delete_clipitems_in_window(track, win_start: int, win_end: int) -> int:
    """Remove clipitems on `track` whose [start, end) intersects [win_start, win_end).

    Returns count deleted.
    """
    to_remove = []
    for ci in track.findall("clipitem"):
        try:
            s = int(ci.findtext("start") or "0")
            e = int(ci.findtext("end") or "0")
        except ValueError:
            continue
        if e > win_start and s < win_end:
            to_remove.append(ci)
    for ci in to_remove:
        track.remove(ci)
    return len(to_remove)


def set_sequence_format(root, width: int, height: int) -> None:
    """Override <sequence><media><video><format><samplecharacteristics>{width,height}."""
    seq = root.find("sequence")
    if seq is None:
        return
    media = seq.find("media")
    if media is None:
        return
    video = media.find("video")
    if video is None:
        return
    fmt = video.find("format")
    if fmt is None:
        return
    sc = fmt.find("samplecharacteristics")
    if sc is None:
        return
    w = sc.find("width")
    h = sc.find("height")
    if w is not None:
        w.text = str(int(width))
    if h is not None:
        h.text = str(int(height))


# ---------------------------------------------------------------------------
# Safe atomic write (bindfs-aware)


def _atomic_safe_write(path: Path, data: bytes) -> None:
    """Write bytes via /tmp + dd + sha verify, retrying up to 3x.

    This mirrors the safe-write pattern documented in the project memory for
    E:\\open-post-stack (bindfs mount truncates large writes silently)."""
    sha_expected = hashlib.sha256(data).hexdigest()
    with tempfile.NamedTemporaryFile(prefix=path.stem + "_", suffix=path.suffix, delete=False) as tmp:
        tmp.write(data)
        tmp_path = Path(tmp.name)
    last_err = None
    try:
        for attempt in range(1, 4):
            shutil.copyfile(tmp_path, path)
            try:
                with path.open("rb") as fh:
                    on_disk = fh.read()
            except Exception as e:
                last_err = e
                time.sleep(0.5)
                continue
            sha_actual = hashlib.sha256(on_disk).hexdigest()
            if sha_actual == sha_expected and len(on_disk) == len(data):
                return
            last_err = RuntimeError(
                f"sha mismatch attempt {attempt}: "
                f"expected len={len(data)} sha={sha_expected[:16]}, "
                f"got len={len(on_disk)} sha={sha_actual[:16]}"
            )
            time.sleep(0.5)
        raise RuntimeError(f"safe_write failed after 3 attempts: {last_err}")
    finally:
        try:
            tmp_path.unlink()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Main


def run(
    plan_path: Path,
    *,
    output_override: Optional[Path] = None,
    dry_run: bool = False,
    validate_only: bool = False,
    catalog_db: Optional[Path] = None,
) -> int:
    plan = _load_plan(plan_path)
    source_xml = _resolve_source_xml(plan_path, plan["source_xml"])
    fps = float(plan.get("fps") or 23.976023976)
    target_track_name = plan["target_video_track"]
    catalog_db = catalog_db or editorial_catalog_sqlite_path()
    asset_map = _load_asset_map(_REPO)

    print(f"[plan]     {plan_path.name}")
    print(f"[source]   {source_xml.name}")
    print(f"[target]   {target_track_name}")
    print(f"[fps]      {fps}")
    print(f"[inserts]  {len(plan['insertions'])}")
    print()

    parser = etree.XMLParser(remove_blank_text=False, strip_cdata=False)
    tree = etree.parse(str(source_xml), parser)
    root = tree.getroot()

    # 1) Apply ripple shift if requested. Must happen BEFORE we measure track ranges.
    ripple_after_sec = plan.get("ripple_after_sec")
    ripple_delta_sec = plan.get("ripple_delta_sec")
    if ripple_after_sec is not None and ripple_delta_sec:
        ripple_frame = ticks.sec_to_frame(float(ripple_after_sec), fps=fps)
        delta_frames = ticks.sec_to_frame(float(ripple_delta_sec), fps=fps)
        stats = ripple_shift_after(root, ripple_frame, delta_frames)
        print(
            f"[ripple] +{delta_frames}f ({ripple_delta_sec}s) after frame {ripple_frame} "
            f"({ripple_after_sec}s) | clipitems_shifted={stats['shifted']} "
            f"transitions_shifted={stats.get('transitions_shifted', 0)} "
            f"straddled={len(stats['straddled'])}"
        )
        if stats["straddled"]:
            print("  WARN: clipitems straddle the ripple point (not shifted):")
            for s in stats["straddled"][:5]:
                print(f"    {s['name']} ({s['id']}) start={s['start']} end={s['end']}")
        print()

    target_track = _resolve_target_track(root, target_track_name)
    existing_files = _existing_file_id_by_filename(root)
    existing_masterclips = _existing_masterclipid_by_filename(root)
    max_clipitem = _max_numeric_id(root, "clipitem-", ["clipitem"])
    max_file = _max_numeric_id(root, "file-", ["file"])
    max_master = _max_masterclip_id(root)

    print(f"[ids] max clipitem-{max_clipitem}, file-{max_file}, masterclip-{max_master}")
    print(f"[ids] existing file defs: {len(existing_files)}")
    print()

    track_ranges = _track_clipitem_ranges(target_track)
    new_clipitems: list[etree._Element] = []
    new_ranges: list[tuple[int, int]] = []
    log_rows: list[dict] = []

    for ins in plan["insertions"]:
        asset_id = ins["asset_id"]
        meta = _lookup_asset(catalog_db, asset_id, asset_map)
        filename = meta["filename"]
        source_path = meta["source_path"]
        src_duration_frames = ticks.sec_to_frame(meta["duration_sec"] or 0.0, fps=fps)

        timeline_start_f = ticks.sec_to_frame(ins["timeline_start_sec"], fps=fps)
        timeline_end_f = ticks.sec_to_frame(ins["timeline_end_sec"], fps=fps)
        source_in_f = ticks.sec_to_frame(ins["source_in_sec"], fps=fps)
        source_out_f = ticks.sec_to_frame(ins["source_out_sec"], fps=fps)

        if timeline_end_f <= timeline_start_f:
            raise SystemExit(f"insertion {asset_id[:12]}: timeline_end <= timeline_start")
        if source_out_f <= source_in_f:
            raise SystemExit(f"insertion {asset_id[:12]}: source_out <= source_in")

        # Auto-snap source_out so timeline_span == source_span (avoids speed-change
        # badges in Premiere from sub-frame rounding). Snap only if the mismatch is
        # within +/- 2 frames; bigger mismatches reflect intentional speed change
        # and we leave them alone (with the explicit warning).
        tl_span = timeline_end_f - timeline_start_f
        src_span = source_out_f - source_in_f
        if tl_span != src_span and abs(tl_span - src_span) <= 2:
            snapped_out_f = source_in_f + tl_span
            if src_duration_frames > 0 and snapped_out_f > src_duration_frames:
                snapped_out_f = src_duration_frames  # clamp to clip end
                # ... and also pull timeline_end back to match
                timeline_end_f = timeline_start_f + (snapped_out_f - source_in_f)
            print(
                f"  SNAP: {filename}: snapped source_out {source_out_f}->{snapped_out_f}f "
                f"to match timeline span {tl_span}f (was {src_span}f)"
            )
            source_out_f = snapped_out_f
            src_span = source_out_f - source_in_f

        if source_out_f > src_duration_frames and src_duration_frames > 0:
            print(
                f"  WARN: {filename}: source_out_sec={ins['source_out_sec']:.2f}s "
                f"({source_out_f}f) > duration {src_duration_frames}f. "
                f"Premiere will likely clamp."
            )

        if tl_span != src_span:
            print(
                f"  NOTE: {filename}: timeline span {tl_span}f != source span "
                f"{src_span}f -- Premiere will interpret this as a speed change. "
                f"Adjust source_in_sec/source_out_sec to match if unintended."
            )

        # Reuse or allocate file id
        if filename in existing_files:
            file_id = existing_files[filename]
            file_element = etree.Element("file", id=file_id)  # stub
            file_status = "reuse"
        else:
            max_file += 1
            file_id = f"file-{max_file}"
            existing_files[filename] = file_id
            file_element = _build_file_def(
                file_id=file_id,
                filename=filename,
                source_path=source_path,
                duration_frames=src_duration_frames,
                width=meta.get("width") or 1280,
                height=meta.get("height") or 720,
            )
            file_status = "new"

        # Reuse or allocate masterclip id
        if filename in existing_masterclips:
            masterclip_id = existing_masterclips[filename]
            mc_status = "reuse"
        else:
            max_master += 1
            masterclip_id = f"masterclip-{max_master}"
            existing_masterclips[filename] = masterclip_id
            mc_status = "new"

        # Allocate clipitem id
        max_clipitem += 1
        clipitem_id = f"clipitem-{max_clipitem}"

        # Track index for the self-link: parse from target_track_name (V3 -> 3).
        _ti = int(target_track_name[1:]) if target_track_name[1:].isdigit() else 1
        ci = _build_clipitem(
            clipitem_id=clipitem_id,
            masterclip_id=masterclip_id,
            filename=filename,
            duration_frames=src_duration_frames,
            start_frame=timeline_start_f,
            end_frame=timeline_end_f,
            in_frame=source_in_f,
            out_frame=source_out_f,
            file_element_or_stub=file_element,
            track_index_for_link=_ti,
        )
        new_clipitems.append(ci)
        new_ranges.append((timeline_start_f, timeline_end_f))
        label = ins.get("label", "")
        log_rows.append(
            {
                "asset_id": asset_id[:12],
                "filename": filename,
                "label": label,
                "clipitem": clipitem_id,
                "file": f"{file_id} ({file_status})",
                "masterclip": f"{masterclip_id} ({mc_status})",
                "timeline_frames": f"{timeline_start_f}-{timeline_end_f}",
                "source_frames": f"{source_in_f}-{source_out_f}",
            }
        )

    print("[insertions plan]")
    for r in log_rows:
        print(
            f"  {r['filename']:14s} tl={r['timeline_frames']:>13s} src={r['source_frames']:>11s} "
            f"file={r['file']:>18s} mc={r['masterclip']:>18s} ci={r['clipitem']}"
        )
    print()

    # 2) Replace mode: delete existing clipitems on the target track that
    # intersect the plan's overall timeline window. MUST happen before the
    # overlap check so the existing-clipitems we're replacing don't trigger
    # false overlaps.
    if plan.get("replace_in_window"):
        win_start = min(s for s, _ in new_ranges)
        win_end = max(e for _, e in new_ranges)
        removed = delete_clipitems_in_window(target_track, win_start, win_end)
        print(f"[replace] removed {removed} existing clipitem(s) on {target_track_name} in window [{win_start},{win_end}]")
        # Refresh track_ranges after deletion so overlap check sees the cleaned state.
        track_ranges = _track_clipitem_ranges(target_track)

    overlaps = _check_no_overlap(track_ranges, new_ranges)
    if overlaps:
        print(f"[ERROR] overlaps detected on {target_track_name}:")
        for line in overlaps:
            print(line)
        return 3

    if validate_only:
        print("[validate-only] no overlaps. Plan resolves cleanly.")
        return 0

    # Insert clipitems into the target track, ordered by start.
    _insert_ordered(target_track, new_clipitems)

    # Update sequence <duration> if needed
    seq = root.find("sequence")
    seq_dur_el = seq.find("duration")
    cur_dur = int(seq_dur_el.text or "0")
    last_end = max(e for _, e in new_ranges + track_ranges) if (new_ranges or track_ranges) else cur_dur
    if last_end > cur_dur:
        seq_dur_el.text = str(int(last_end))
        print(f"[seq] extended duration {cur_dur} -> {last_end} frames")

    # Optional sequence name override
    if plan.get("sequence_name"):
        seq_name_el = seq.find("name")
        if seq_name_el is not None:
            seq_name_el.text = plan["sequence_name"]
            print(f"[seq] renamed -> {plan['sequence_name']!r}")

    # Output path
    if output_override:
        out_path = output_override
    else:
        ts = time.strftime("%Y%m%dT%H%M%S")
        suffix = plan.get("output_name_suffix") or "edit"
        out_path = source_xml.with_name(f"project_act II_{ts}_{suffix}.xml")

    # 3) Sequence format override (e.g., proxy-resolution match)
    sf = plan.get("sequence_format")
    if sf:
        w = int(sf.get("width") or 0)
        h = int(sf.get("height") or 0)
        if w and h:
            set_sequence_format(root, w, h)
            print(f"[seq] format -> {w}x{h}")

    # Premiere's xmeml uses mixed empty-tag conventions: reference stubs
    # like <file id="X"/> and <sequence id="X"/> must stay self-closing,
    # but empty content elements (<description>, <scene>, <lut>, etc.)
    # must use <tag></tag>. Rule: presence of an `id` attribute marks a
    # ref stub. lxml defaults all empties to self-closing, so force
    # open/close on everything else.
    for el in tree.iter():
        if el.text is None and len(el) == 0 and "id" not in el.attrib:
            el.text = ""

    # NOTE: lxml's xml_declaration=True emits single-quoted
    # `<?xml version='1.0' encoding='UTF-8'?>` and Premiere's xmeml parser
    # rejects that. Emit the declaration manually with double quotes.
    body = etree.tostring(
        tree,
        pretty_print=False,
        xml_declaration=False,
        encoding="UTF-8",
        doctype="<!DOCTYPE xmeml>",
    )
    serialized = b'<?xml version="1.0" encoding="UTF-8"?>\n' + body
    print(f"[output] {out_path}")
    print(f"[bytes]  {len(serialized):,}")

    if dry_run:
        print("[dry-run] not writing.")
        return 0

    _atomic_safe_write(out_path, serialized)
    print(f"[ok] wrote {out_path.name}")
    print()
    print("Next:")
    print(f"  1) Validate structurally:")
    print(f'     py "editor/story/_sidecar scripts/validate_xml_structure.py" \\')
    print(f'         --xml "{out_path}"')
    print(f"  2) Import to Premiere: File > Import > select this XML; opens as new sequence.")
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--plan", required=True, help="Path to the JSON insertion plan")
    ap.add_argument("--output", help="Override output XML path")
    ap.add_argument("--dry-run", action="store_true", help="Print plan, do not write")
    ap.add_argument("--validate-only", action="store_true", help="Resolve assets + check overlaps")
    ap.add_argument("--catalog-db", help="Override path to editorial_catalog.sqlite")
    args = ap.parse_args(argv)
    return run(
        Path(args.plan).resolve(),
        output_override=Path(args.output).resolve() if args.output else None,
        dry_run=args.dry_run,
        validate_only=args.validate_only,
        catalog_db=Path(args.catalog_db).resolve() if args.catalog_db else None,
    )


if __name__ == "__main__":
    raise SystemExit(main())
