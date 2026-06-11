#!/usr/bin/env python3
"""Pre-flight diff for an XML insertion plan against the source xmeml.

Read-only. Reports everything that would change in the affected window so you
can sanity-check before letting insert_video_clips.py write anything:

- clipitems straddling the ripple point (these won't shift; warn)
- clipitems with -1 sentinels = transition boundary markers in/near window
- transitionitems that would shift (or stay put if straddling ripple)
- file defs: which insertions reuse an existing <file id="..."> vs add new
- target track current contents (would be removed by replace_in_window)
- sequence format vs intended (mismatch -> proxy clips render with black bars)
- sidecar drift: sidecar.json.xml_source vs the plan's source_xml

Usage:
  py editor/xml\\ exports/_scripts/preflight_xml_diff.py \\
      --plan "editor/xml exports/_plans/<my_plan>.json"
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from lxml import etree

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))
import _pproticks as ticks  # noqa: E402


def _load_plan(plan_path):
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    plan.setdefault("fps", 23.976023976)
    return plan


def _resolve_source_xml(plan_path, source_xml):
    p = Path(source_xml)
    if p.is_absolute() and p.exists():
        return p
    for c in [plan_path.parent.parent / source_xml, plan_path.parent / source_xml, Path.cwd() / source_xml]:
        if c.exists():
            return c.resolve()
    raise SystemExit(f"source_xml not found: {source_xml}")


def _track_label(kind, idx):
    return f"{'V' if kind == 'video' else 'A'}{idx}"


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--plan", required=True)
    ap.add_argument("--sidecar", help="path to actII.sidecar.json (for drift check)")
    args = ap.parse_args()

    plan_path = Path(args.plan).resolve()
    plan = _load_plan(plan_path)
    fps = float(plan.get("fps", 23.976023976))
    source = _resolve_source_xml(plan_path, plan["source_xml"])
    target = plan["target_video_track"]
    insertions = plan.get("insertions", [])

    print(f"[plan]     {plan_path.name}")
    print(f"[source]   {source.name}")
    print(f"[target]   {target}")
    print(f"[inserts]  {len(insertions)}")
    print()

    tree = etree.parse(str(source))
    root = tree.getroot()
    seq = root.find("sequence")
    media = seq.find("media")

    # Compute affected window
    tl_starts = [ticks.sec_to_frame(i["timeline_start_sec"], fps=fps) for i in insertions]
    tl_ends = [ticks.sec_to_frame(i["timeline_end_sec"], fps=fps) for i in insertions]
    win_start, win_end = min(tl_starts), max(tl_ends)
    ripple_after = plan.get("ripple_after_sec")
    ripple_delta = plan.get("ripple_delta_sec")
    ripple_frame = ticks.sec_to_frame(float(ripple_after), fps=fps) if ripple_after is not None else None
    delta_frames = ticks.sec_to_frame(float(ripple_delta), fps=fps) if ripple_delta else 0

    print(f"[window]   timeline frames {win_start}..{win_end} ({win_start/fps:.2f}-{win_end/fps:.2f}s)")
    if ripple_frame is not None:
        print(f"[ripple]   +{delta_frames}f ({ripple_delta}s) after frame {ripple_frame} ({ripple_after}s)")
    print()

    # 1. Walk every clipitem on every track; classify
    straddlers = []
    sentinel_pairs = []
    target_track_existing = []
    for kind in ("video", "audio"):
        kind_el = media.find(kind)
        for tidx, track in enumerate(kind_el.findall("track"), 1):
            label = _track_label(kind, tidx)
            for ci in track.findall("clipitem"):
                try:
                    s = int(ci.findtext("start") or "0")
                    e = int(ci.findtext("end") or "0")
                except ValueError:
                    continue
                name = (ci.findtext("name") or "")[:38]
                # Sentinel check
                if s == -1 or e == -1:
                    if ripple_frame is not None:
                        # interesting only if its non-sentinel edge is >= ripple_frame
                        non = e if s == -1 else s
                        if non >= ripple_frame:
                            sentinel_pairs.append((label, ci.get("id"), s, e, name))
                    continue
                # Straddler check (ripple point falls inside this clipitem)
                if ripple_frame is not None and s < ripple_frame < e:
                    straddlers.append((label, ci.get("id"), s, e, name))
                # Target track contents (would be removed if replace_in_window)
                if label == target and e > win_start and s < win_end:
                    target_track_existing.append((ci.get("id"), s, e, name))

    if straddlers:
        print(f"[WARN] {len(straddlers)} clipitem(s) straddle the ripple point — they will NOT shift:")
        for lbl, cid, s, e, n in straddlers:
            print(f"   {lbl}  {cid:18s}  {s}-{e}  {n!r}")
        print()
    else:
        print("[OK] no clipitems straddle the ripple point")

    if sentinel_pairs:
        print(f"[INFO] {len(sentinel_pairs)} clipitem(s) with -1 sentinels (transition boundary markers) at/after ripple:")
        for lbl, cid, s, e, n in sentinel_pairs[:8]:
            print(f"   {lbl}  {cid:18s}  start={s} end={e}  {n!r}")
        if len(sentinel_pairs) > 8:
            print(f"   ... +{len(sentinel_pairs) - 8} more")
        print("   These edges are computed from <transitionitem> position; confirm transitions shift too")
        print()

    # 2. Transitionitems in/after window
    trans_in_window = []
    trans_shifted = 0
    for kind in ("video", "audio"):
        kind_el = media.find(kind)
        for tidx, track in enumerate(kind_el.findall("track"), 1):
            label = _track_label(kind, tidx)
            for tr in track.findall("transitionitem"):
                try:
                    s = int(tr.findtext("start") or "0")
                    e = int(tr.findtext("end") or "0")
                except ValueError:
                    continue
                effname = "?"
                eff = tr.find("effect")
                if eff is not None:
                    effname = eff.findtext("name") or "?"
                if ripple_frame is not None and s >= ripple_frame:
                    trans_shifted += 1
                    if s <= win_end + 100:  # close to window
                        trans_in_window.append((label, s, e, effname))
                elif ripple_frame is not None and e > ripple_frame:
                    trans_in_window.append((label, s, e, effname + " [STRADDLES RIPPLE!]"))

    if ripple_frame is not None:
        print(f"[transitions] {trans_shifted} transitionitem(s) will shift +{delta_frames}f")
        for lbl, s, e, n in trans_in_window:
            print(f"   {lbl}  start={s} end={e}  {n!r}")
        print()

    # 3. File def reuse vs new
    existing_files_by_name = {}
    for fe in root.iter("file"):
        nm = fe.findtext("name")
        if nm:
            existing_files_by_name.setdefault(nm.strip(), fe.get("id"))
    import sqlite3
    repo = _HERE.parent.parent.parent
    cat_db = repo / "indexes" / "editorial_catalog.sqlite"
    if cat_db.exists():
        con = sqlite3.connect(str(cat_db))
        ph = ",".join("?" * len(insertions))
        rows = con.execute(
            f"SELECT asset_id, filename FROM asset WHERE asset_id IN ({ph})",
            [i["asset_id"] for i in insertions],
        ).fetchall()
        con.close()
        fn_by_aid = dict(rows)
    else:
        fn_by_aid = {}
    print("[files] per insertion:")
    for i in insertions:
        aid = i["asset_id"]
        fn = fn_by_aid.get(aid, "?")
        if fn in existing_files_by_name:
            print(f"   reuse  {fn:14s}  -> {existing_files_by_name[fn]}  ({aid[:12]}..)")
        else:
            print(f"   NEW    {fn:14s}  (no existing file def; will allocate)")
    print()

    # 4. Target track contents
    if plan.get("replace_in_window"):
        if target_track_existing:
            print(f"[replace] {len(target_track_existing)} clipitem(s) on {target} would be REMOVED in window {win_start}-{win_end}:")
            for cid, s, e, n in target_track_existing:
                print(f"   {cid:18s}  {s}-{e}  {n!r}")
        else:
            print(f"[replace] target {target} has no clipitems in window; nothing to remove")
        print()
    else:
        if target_track_existing:
            print(f"[WARN] target {target} already has {len(target_track_existing)} clipitem(s) in window")
            print("       replace_in_window is FALSE; new inserts will conflict")
            for cid, s, e, n in target_track_existing:
                print(f"   {cid:18s}  {s}-{e}  {n!r}")
            print()

    # 5. Sequence format check
    sf = plan.get("sequence_format")
    fmt = media.find("video").find("format").find("samplecharacteristics")
    cur_w = fmt.findtext("width")
    cur_h = fmt.findtext("height")
    if sf:
        target_w, target_h = int(sf.get("width", 0)), int(sf.get("height", 0))
        if (cur_w, cur_h) != (str(target_w), str(target_h)):
            print(f"[format] sequence is {cur_w}x{cur_h}; plan will change to {target_w}x{target_h}")
        else:
            print(f"[format] sequence already {cur_w}x{cur_h}; plan matches")
    else:
        print(f"[format] sequence is {cur_w}x{cur_h}; plan does NOT override")
        if cur_w == "3840" and cur_h == "2160":
            print("         WARN: proxies are 1280x720; clips will show with black bars")
    print()

    # 6. Sidecar drift
    if args.sidecar:
        sc_path = Path(args.sidecar)
    else:
        sc_path = repo / "editor" / "story" / "sidecars" / "actII.sidecar.json"
    if sc_path.exists():
        sc = json.loads(sc_path.read_text(encoding="utf-8"))
        sc_xml = sc.get("xml_source", "")
        if source.name in sc_xml:
            print(f"[sidecar] xml_source matches plan source ({source.name})")
        else:
            print(f"[sidecar] DRIFT: sidecar.xml_source={sc_xml!r}")
            print(f"          plan.source_xml={source.name!r}")
            print("          Run refresh_act_sidecar.py --xml ... before editorial work assumes alignment")
    print()

    print("[preflight DONE]  No XML was written.  Review above before running insert_video_clips.py without --dry-run.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
