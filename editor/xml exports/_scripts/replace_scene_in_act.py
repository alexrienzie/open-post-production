"""Replace a scene-window in an Act xmeml with a self-contained scene xmeml.

Goal: take an Act-level Premiere export (e.g. project_act II_proxies_20260522.xml)
and swap out a single scene's frame range with the contents of a sandbox
scene XML emitted from `scene_workspace/`. The rest of the Act is preserved
with a ripple-shift downstream by the delta in scene duration.

Usage
-----
    py "editor\\xml exports\\_scripts\\replace_scene_in_act.py" \\
        --act    "editor\\xml exports\\project_act II_proxies_20260522.xml" \\
        --scene  "editor\\xml exports\\scene_workspace\\scene_b_06_s05_the_criminal.v2.xml" \\
        --replace-frames 6956-8568 \\
        --suffix criminal_v2

Output lands at `editor/xml exports/project_act II_<ts>_<suffix>.xml`.

What it does (in order)
-----------------------
1. Parse both XMLs.
2. Compute delta = scene_total_frames - (replace_end - replace_start).
3. Walk every track in the Act:
   a. Remove clipitems whose timeline `<start>` falls within [replace_start, replace_end).
   b. Shift clipitems whose start >= replace_end by +delta.
   c. Shift transitionitems by the same delta if their start/end falls past replace_end.
4. Map scene's file-defs onto the Act's file-id namespace:
   - If a scene file has the same `<pathurl>` as an existing Act file, reuse the Act's id.
   - Otherwise allocate a fresh id above Act's max file-N and keep the scene's full body.
5. Renumber scene clipitem ids above Act's max clipitem-N to avoid collisions.
6. Offset every scene clipitem `<start>`/`<end>` by +replace_start, then append to the
   corresponding Act tracks (V1→V1, A2→A2, etc.).
7. Update `<sequence><duration>` to extend by delta.
8. Strip dangling `<link>` refs (defensive — should already be clean per the
   xml_README invariant).
9. Force open/close form on empty content elements; write with literal double-quoted
   XML declaration; output to alongside the Act XML.

Invariants honored (see ../xml_README.md)
-----------------------------------------
- NTSC 23.976 throughout; no clipitem timing is recomputed from seconds.
- Pathurls in new file-defs use Windows-style `file://localhost/E%3a/...` encoding.
- `<file>` first occurrence carries full body; subsequent are stubs.
- `<transitionitem>` shifts in lockstep with surrounding clipitems on ripple.
- Reference stubs self-close; empty content elements open/close.
- XML declaration uses double quotes (`<?xml version="1.0" encoding="UTF-8"?>`).
"""

from __future__ import annotations

import argparse
import copy
import datetime
import sys
from pathlib import Path

from lxml import etree

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
import _pproticks as ticks  # noqa: E402


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--act", required=True, type=Path, help="Source Act xmeml")
    p.add_argument("--scene", required=True, type=Path, help="Scene xmeml to splice in")
    p.add_argument("--replace-frames", required=True,
                   help="Frame range in the Act to replace, e.g. 6956-8568")
    p.add_argument("--suffix", default="scene_replace", help="Output filename suffix")
    p.add_argument("--output", type=Path, help="Override output path (default: alongside the Act)")
    p.add_argument("--dry-run", action="store_true", help="Print the plan without writing")
    return p.parse_args()


def _parse_frame_range(spec: str) -> tuple[int, int]:
    if "-" not in spec:
        raise SystemExit(f"--replace-frames expects 'START-END', got {spec!r}")
    a, b = spec.split("-", 1)
    return int(a), int(b)


def _txt(el: etree._Element, tag: str) -> str | None:
    c = el.find(tag)
    return c.text if c is not None else None


def _frame(el: etree._Element, tag: str) -> int | None:
    """Return int frame from a child element, or None if absent / non-integer / -1 sentinel."""
    t = _txt(el, tag)
    if t is None or t == "-1":
        return None
    try:
        v = int(t)
    except ValueError:
        return None
    return v


def _is_sentinel(el: etree._Element, tag: str) -> bool:
    return _txt(el, tag) == "-1"


def _shift_frame(el: etree._Element, tag: str, delta: int) -> None:
    """Add `delta` to the child element's integer text, leaving -1 sentinels alone."""
    c = el.find(tag)
    if c is None or c.text is None or c.text == "-1":
        return
    try:
        c.text = str(int(c.text) + delta)
    except ValueError:
        pass


def _max_id(root: etree._Element, prefix: str) -> int:
    """Highest integer N across attributes id='prefix-N' found anywhere in the tree."""
    hi = 0
    for el in root.iter():
        idv = el.get("id")
        if not idv or not idv.startswith(prefix + "-"):
            continue
        try:
            n = int(idv.split("-", 1)[1])
        except ValueError:
            continue
        if n > hi:
            hi = n
    return hi


def _collect_file_defs(root: etree._Element) -> dict[str, etree._Element]:
    """{file_id: <file>} for every full-body <file> def (skips stubs)."""
    defs: dict[str, etree._Element] = {}
    for f in root.iter("file"):
        fid = f.get("id")
        if not fid or fid in defs:
            continue
        if len(list(f)) > 0:  # has children = full body
            defs[fid] = f
    return defs


def _pathurl_of(file_el: etree._Element) -> str | None:
    pu = file_el.find("pathurl")
    return pu.text if pu is not None else None


def _stub_file_in_place(ci: etree._Element, new_fid: str) -> None:
    """Replace a clipitem's <file> child with a <file id="new_fid"/> stub."""
    f = ci.find("file")
    if f is None:
        return
    parent = f.getparent()
    idx = list(parent).index(f)
    parent.remove(f)
    stub = etree.Element("file", id=new_fid)
    parent.insert(idx, stub)


def _force_open_close_empty_content(root: etree._Element) -> None:
    """Force open/close form on empty content elements per xml_README invariant.
    Reference stubs (elements with `id` attr) stay self-closing."""
    for el in root.iter():
        if el.text is None and len(el) == 0 and "id" not in el.attrib:
            el.text = ""


def main() -> None:
    args = _parse_args()
    replace_start, replace_end = _parse_frame_range(args.replace_frames)
    if replace_end <= replace_start:
        raise SystemExit("--replace-frames must be START<END")

    act_path: Path = args.act
    scene_path: Path = args.scene
    if not act_path.exists():
        raise SystemExit(f"Act XML not found: {act_path}")
    if not scene_path.exists():
        raise SystemExit(f"Scene XML not found: {scene_path}")

    parser = etree.XMLParser(remove_blank_text=False)
    act_tree = etree.parse(str(act_path), parser)
    scene_tree = etree.parse(str(scene_path), parser)
    act_root = act_tree.getroot()
    scene_root = scene_tree.getroot()

    act_seq = act_root.find(".//sequence")
    scene_seq = scene_root.find(".//sequence")
    if act_seq is None or scene_seq is None:
        raise SystemExit("Both XMLs must contain a <sequence>")

    scene_total = int(_txt(scene_seq, "duration") or 0)
    delta = scene_total - (replace_end - replace_start)
    print(f"replace_start={replace_start} replace_end={replace_end} (window {replace_end-replace_start} frames)")
    print(f"scene_total={scene_total} frames; delta = {delta:+} frames "
          f"({delta * 1001 / 24000:+.2f}s)")

    # === Step A-PRE: snapshot all full <file> bodies + max IDs BEFORE any removal
    # Two things have to be captured before step A mutates the tree:
    #   (1) Full <file> bodies — step A may remove the clipitem that carried
    #       the FIRST occurrence body, leaving downstream stubs orphaned. The
    #       body-attachment post-pass restores them from this snapshot.
    #   (2) Max ID numbers for file/clipitem/masterclip — step A removes
    #       clipitems whose ID/file-ref values are EXTREMA in the Act. If we
    #       compute max after removal, the "fresh ID" allocator below collides
    #       with surviving snapshot IDs (e.g. C0449 got file-388, colliding
    #       with the C0050 file-388 that lived only inside removed clipitems).
    act_full_defs_snapshot = _collect_file_defs(act_root)
    act_max_file_n = _max_id(act_root, "file")
    act_max_clip_n = _max_id(act_root, "clipitem")
    act_max_mc_n = _max_id(act_root, "masterclip")
    print(f"Snapshot: {len(act_full_defs_snapshot)} full <file> defs in Act before modification "
          f"(max-id: file-{act_max_file_n}, clipitem-{act_max_clip_n}, masterclip-{act_max_mc_n})")

    # === Step A: walk Act tracks, remove old scene clipitems + ripple-shift ====
    act_media = act_seq.find("media")
    if act_media is None:
        raise SystemExit("Act <sequence> has no <media>")
    act_video = act_media.find("video")
    act_audio = act_media.find("audio")
    if act_video is None or act_audio is None:
        raise SystemExit("Act has no <video> or <audio> media")

    act_v_tracks = act_video.findall("track")
    act_a_tracks = act_audio.findall("track")

    removed_count = 0
    shifted_count = 0
    transition_count = 0

    def _process_track(trk: etree._Element, label: str) -> None:
        nonlocal removed_count, shifted_count, transition_count
        # Remove clipitems whose start falls in [replace_start, replace_end)
        for ci in list(trk.findall("clipitem")):
            s = _frame(ci, "start")
            if s is None:
                # start=-1 sentinel: try resolving via end. If end is in range, also remove.
                e = _frame(ci, "end")
                if e is not None and replace_start <= e <= replace_end:
                    trk.remove(ci)
                    removed_count += 1
                continue
            if replace_start <= s < replace_end:
                trk.remove(ci)
                removed_count += 1
            elif s >= replace_end:
                _shift_frame(ci, "start", delta)
                _shift_frame(ci, "end", delta)
                shifted_count += 1
        # Walk transitionitems, shift if past replace_end
        for ti in list(trk.findall("transitionitem")):
            s = _frame(ti, "start")
            e = _frame(ti, "end")
            if s is None and e is None:
                continue
            # Drop transitionitems wholly inside the replaced window
            if (s is not None and replace_start <= s < replace_end) or \
               (e is not None and replace_start < e <= replace_end):
                if (s is None or s < replace_end) and (e is None or e <= replace_end):
                    trk.remove(ti)
                    removed_count += 1
                    continue
            if s is not None and s >= replace_end:
                _shift_frame(ti, "start", delta)
            if e is not None and e >= replace_end:
                _shift_frame(ti, "end", delta)
            if (s is not None and s >= replace_end) or (e is not None and e >= replace_end):
                transition_count += 1

    for i, trk in enumerate(act_v_tracks, start=1):
        _process_track(trk, f"V{i}")
    for i, trk in enumerate(act_a_tracks, start=1):
        _process_track(trk, f"A{i}")
    print(f"Act-side: removed {removed_count} items, shifted {shifted_count} clipitems "
          f"+ {transition_count} transitionitems")

    # === Step B: map scene file-ids to Act file-id namespace =================
    # Use the pre-removal snapshot so Act file-ids whose carrier got removed
    # in step A still resolve here.
    act_pathurl_to_fid: dict[str, str] = {}
    for fid, f in act_full_defs_snapshot.items():
        pu = _pathurl_of(f)
        if pu:
            act_pathurl_to_fid[pu] = fid

    scene_file_defs = _collect_file_defs(scene_root)
    scene_to_act_fid: dict[str, str] = {}
    new_file_defs: dict[str, etree._Element] = {}  # final_fid -> <file> body

    next_file_n = act_max_file_n + 1
    for sfid, sf in scene_file_defs.items():
        spu = _pathurl_of(sf)
        if spu and spu in act_pathurl_to_fid:
            # Reuse Act's existing file id. The Act's body (from snapshot) will
            # be re-attached during the post-pass.
            scene_to_act_fid[sfid] = act_pathurl_to_fid[spu]
        else:
            # New source — assign fresh file id and stash the body for post-pass attachment.
            new_fid = f"file-{next_file_n}"
            next_file_n += 1
            scene_to_act_fid[sfid] = new_fid
            sf_copy = copy.deepcopy(sf)
            sf_copy.set("id", new_fid)
            new_file_defs[new_fid] = sf_copy

    reused_count = sum(1 for v in scene_to_act_fid.values() if v in act_full_defs_snapshot)
    print(f"File-id remap: {reused_count} reused (carrier may have been removed; "
          f"will re-attach body in post-pass), {len(new_file_defs)} new defs")

    # === Step C: renumber scene clipitem ids above Act's max ==================
    next_clip_n = act_max_clip_n + 1
    scene_to_act_clipid: dict[str, str] = {}
    for ci in scene_root.iter("clipitem"):
        old = ci.get("id")
        if not old or old in scene_to_act_clipid:
            continue
        scene_to_act_clipid[old] = f"clipitem-{next_clip_n}"
        next_clip_n += 1

    # Also remap masterclip ids (they live as <masterclipid>TEXT</masterclipid>)
    next_mc_n = act_max_mc_n + 1
    scene_to_act_mcid: dict[str, str] = {}
    for mc in scene_root.iter("masterclipid"):
        old = mc.text
        if not old or old in scene_to_act_mcid:
            continue
        scene_to_act_mcid[old] = f"masterclip-{next_mc_n}"
        next_mc_n += 1

    # Apply remaps to scene clipitem ids + their <link><linkclipref> + masterclipid + file refs.
    for ci in scene_root.iter("clipitem"):
        old = ci.get("id")
        if old and old in scene_to_act_clipid:
            ci.set("id", scene_to_act_clipid[old])
        mc = ci.find("masterclipid")
        if mc is not None and mc.text in scene_to_act_mcid:
            mc.text = scene_to_act_mcid[mc.text]
        f = ci.find("file")
        if f is not None:
            ofid = f.get("id")
            if ofid in scene_to_act_fid:
                target_fid = scene_to_act_fid[ofid]
                # Stub the scene's body — the post-pass will attach the canonical
                # body (from snapshot or new_file_defs) wherever the first stub appears.
                _stub_file_in_place(ci, target_fid)
        for link in ci.findall("link"):
            ref = link.find("linkclipref")
            if ref is not None and ref.text in scene_to_act_clipid:
                ref.text = scene_to_act_clipid[ref.text]

    # === Step D: offset scene clipitem start/end by +replace_start ===========
    for ci in scene_root.iter("clipitem"):
        # start/end ONLY — source in/out and pproTicks stay put
        s = ci.find("start"); e = ci.find("end")
        if s is not None and s.text and s.text != "-1":
            try: s.text = str(int(s.text) + replace_start)
            except ValueError: pass
        if e is not None and e.text and e.text != "-1":
            try: e.text = str(int(e.text) + replace_start)
            except ValueError: pass
    # Same for transitionitems in the scene
    for ti in scene_root.iter("transitionitem"):
        s = ti.find("start"); e = ti.find("end")
        if s is not None and s.text and s.text != "-1":
            try: s.text = str(int(s.text) + replace_start)
            except ValueError: pass
        if e is not None and e.text and e.text != "-1":
            try: e.text = str(int(e.text) + replace_start)
            except ValueError: pass

    # === Step E: insert scene clipitems into Act tracks in TIME order ========
    # Premiere reads clipitems in document order and expects them to be in
    # time order within each track. Appending at the end would put our new
    # criminal-scene clips (frames 7076+) AFTER the downstream clips (9926+)
    # in the XML, which causes Premiere to silently skip everything past the
    # last in-order item. Insert each new clipitem at the right document
    # position (before the first existing clipitem with a later start frame).
    def _insert_in_time_order(track: etree._Element, new_ci: etree._Element) -> None:
        s_el = new_ci.find("start")
        if s_el is None or s_el.text in (None, "-1"):
            track.append(new_ci)
            return
        try:
            new_start = int(s_el.text)
        except ValueError:
            track.append(new_ci)
            return
        children = list(track)
        insert_idx = len(children)
        for i, el in enumerate(children):
            if el.tag not in ("clipitem", "transitionitem"):
                continue
            e_s = el.find("start")
            if e_s is None or e_s.text in (None, "-1"):
                continue
            try:
                e_val = int(e_s.text)
            except ValueError:
                continue
            if e_val > new_start:
                insert_idx = i
                break
        track.insert(insert_idx, new_ci)

    scene_v_tracks = scene_seq.find("media/video").findall("track")
    scene_a_tracks = scene_seq.find("media/audio").findall("track")

    def _block_insert(act_track: etree._Element, scene_track: etree._Element) -> tuple[int, int]:
        """Insert the scene track's children as a CONTIGUOUS block, preserving the
        scene's exact document order.

        Why not per-clip time-order insertion: Premiere exports some clips with
        start=-1 (and sometimes end=-1) — their timeline position is INFERRED from
        the neighbouring clips' document order (a clip with end=N back-computes its
        start from its own length; a fully -1 clip abuts its document neighbours).
        Re-sorting by <start> (and appending the -1 ones at the end) destroys that
        context, so Premiere drops/misplaces them on import. Keeping the scene's
        intra-track order intact reproduces the exact placement the scene XML had.

        Anchor: before the first surviving Act clip whose start >= replace_start
        (i.e. the post-ripple downstream content); -1-start Act clips are skipped
        for the anchor search (they resolve via their own links downstream)."""
        block = [copy.deepcopy(el) for el in scene_track
                 if el.tag in ("clipitem", "transitionitem")]
        anchor = len(list(act_track))
        for idx, el in enumerate(list(act_track)):
            if el.tag not in ("clipitem", "transitionitem"):
                continue
            s = el.find("start")
            if s is None or s.text in (None, "-1"):
                continue
            try:
                if int(s.text) >= replace_start:
                    anchor = idx
                    break
            except ValueError:
                continue
        for off, el in enumerate(block):
            act_track.insert(anchor + off, el)
        n_clip = sum(1 for el in block if el.tag == "clipitem")
        n_neg = sum(1 for el in block if el.tag == "clipitem" and (el.findtext("start") == "-1"))
        return n_clip, n_neg

    appended = {"V": 0, "A": 0}
    neg_total = 0
    for i, s_trk in enumerate(scene_v_tracks):
        if i >= len(act_v_tracks):
            # Scene has more video tracks than Act — skip (or could expand Act, but rare)
            continue
        nc, nn = _block_insert(act_v_tracks[i], s_trk)
        appended["V"] += nc; neg_total += nn
    for i, s_trk in enumerate(scene_a_tracks):
        if i >= len(act_a_tracks):
            continue
        nc, nn = _block_insert(act_a_tracks[i], s_trk)
        appended["A"] += nc; neg_total += nn
    print(f"Scene-side: inserted {appended['V']} video + {appended['A']} audio clipitems "
          f"(scene document order preserved; {neg_total} carry start=-1, positioned by document order)")

    # === Step F: post-pass — ensure every file-id has exactly one full body ==
    # After removal + insertion, some file-ids may have only stubs in the tree
    # (their carrier was removed in step A, or they're new sources). Walk every
    # clipitem; for each file-id that's not yet body-carrier, attach the full
    # body (from snapshot for reused ids, from new_file_defs for new sources)
    # to its first stub occurrence in the tree.

    # Build the body source pool: act snapshot + new defs.
    available_bodies: dict[str, etree._Element] = {}
    for fid, body in act_full_defs_snapshot.items():
        available_bodies[fid] = body
    for fid, body in new_file_defs.items():
        available_bodies[fid] = body

    # Find current bodies in the tree (after removal+insertion).
    current_bodies = {fid for fid in _collect_file_defs(act_root)}

    # Walk in track order: V1, V2, ..., A1, A2, ...; first stub per id wins the body.
    attached_count = 0
    for trk in act_v_tracks + act_a_tracks:
        for ci in trk.findall("clipitem"):
            f = ci.find("file")
            if f is None:
                continue
            fid = f.get("id")
            if not fid or fid in current_bodies:
                continue
            # f is currently a stub for `fid`. Replace it with the full body.
            if fid not in available_bodies:
                print(f"  WARN: clipitem-{ci.get('id')} refs file-{fid} but no body available")
                continue
            body = copy.deepcopy(available_bodies[fid])
            parent = f.getparent()
            idx = list(parent).index(f)
            parent.remove(f)
            parent.insert(idx, body)
            current_bodies.add(fid)
            attached_count += 1
    print(f"Body-attachment post-pass: re-attached {attached_count} full <file> defs")

    # === Step G: extend sequence duration ====================================
    dur_el = act_seq.find("duration")
    if dur_el is not None and dur_el.text:
        old_dur = int(dur_el.text)
        new_dur = old_dur + delta
        dur_el.text = str(new_dur)
        print(f"Sequence duration: {old_dur} -> {new_dur} (+{delta})")

    # === Step H: strip dangling <link> refs (defensive) ======================
    all_clipitem_ids: set[str] = set()
    for ci in act_root.iter("clipitem"):
        cid = ci.get("id")
        if cid:
            all_clipitem_ids.add(cid)
    dropped_links = 0
    for ci in act_root.iter("clipitem"):
        for link in list(ci.findall("link")):
            ref = link.find("linkclipref")
            if ref is None or ref.text not in all_clipitem_ids:
                ci.remove(link)
                dropped_links += 1
    if dropped_links:
        print(f"Dropped {dropped_links} dangling <link> refs")

    # === Step H2: recompute <link> trackindex/clipindex ======================
    # Premiere resolves sync links by (mediatype, trackindex, clipindex), NOT by
    # linkclipref. clipindex = the target clip's 1-based position within its track,
    # COUNTING clipitems AND transitionitems in document order (verified 237/237 on a
    # known-good export). Any clip add/remove/shift invalidates these — and this was
    # NEVER recomputed, so every prior splice (incl. the criminal one that built this
    # Act) left stale indices → audio linked to the wrong video "N down the line".
    # Recompute from each link target's true current position.
    pos: dict[str, tuple[str, int, int]] = {}  # id -> (mediatype, trackindex, position)
    for media, mt in ((act_video, "video"), (act_audio, "audio")):
        if media is None:
            continue
        for ti, trk in enumerate(media.findall("track"), start=1):
            n = 0
            for el in list(trk):
                if el.tag in ("clipitem", "transitionitem"):
                    n += 1
                    if el.tag == "clipitem":
                        cid = el.get("id")
                        if cid:
                            pos[cid] = (mt, ti, n)
    fixed_idx = 0
    for ci in act_root.iter("clipitem"):
        for link in ci.findall("link"):
            ref = link.find("linkclipref")
            if ref is None or ref.text not in pos:
                continue
            mt, ti, n = pos[ref.text]
            mt_el = link.find("mediatype"); ti_el = link.find("trackindex"); ci_el = link.find("clipindex")
            if mt_el is not None and mt_el.text != mt:
                mt_el.text = mt; fixed_idx += 1
            if ti_el is not None and ti_el.text != str(ti):
                ti_el.text = str(ti); fixed_idx += 1
            if ci_el is not None and ci_el.text != str(n):
                ci_el.text = str(n); fixed_idx += 1
    print(f"Link-index recompute: corrected {fixed_idx} trackindex/clipindex values "
          f"across {len(pos)} clips (fixes this splice + any prior stale indices)")

    # === Step I: write output ================================================
    _force_open_close_empty_content(act_root)
    body = etree.tostring(act_root, pretty_print=True, encoding="UTF-8")
    decl = b'<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE xmeml>\n'
    output_path = args.output
    if output_path is None:
        ts = datetime.datetime.now().strftime("%Y%m%dT%H%M")
        # Output naming convention (xml_README §Output naming):
        #   project_act*_[proxies|original]_[date|datetime]_[contextual modifiers].xml
        # Preserve the source's act prefix and proxies|original token, then
        # append the fresh datetime + suffix.
        stem = act_path.stem
        if "_proxies_" in stem:
            prefix = stem.split("_proxies_", 1)[0] + "_proxies"
        elif "_original_" in stem:
            prefix = stem.split("_original_", 1)[0] + "_original"
        else:
            # Source didn't use the convention — fall back to whole stem.
            prefix = stem
        out_name = f"{prefix}_{ts}_{args.suffix}.xml"
        output_path = act_path.parent / out_name

    if args.dry_run:
        print(f"DRY RUN: would write {output_path}  ({len(body):,} bytes)")
        return

    output_path.write_bytes(decl + body)
    size_kb = output_path.stat().st_size / 1024
    print(f"Wrote: {output_path}  ({size_kb:.1f} KB)")


if __name__ == "__main__":
    main()
