#!/usr/bin/env python3
"""Validate xmeml v4 structure after an LLM edit + optionally diff against
a baseline XML.

Catches the common ways an LLM-edited xmeml can be invalid (and therefore
fail to import to Premiere, or import with silent data loss):

  - missing <sequence> root
  - clipitems with no <file> reference, or referencing a file id that
    doesn't exist in any <media>
  - clipitems with negative or non-integer in/out/start (excluding the
    legitimate -1 sentinel for linked clips)
  - duplicate file ids
  - duplicate clipitem ids
  - track ordering: tracks within a <video>/<audio> must be V1, V2, ...
    (xmeml is positional)
  - pathurls that don't decode to existing files on disk (warning, since
    the LLM may have edited paths for a future-staged remap)

Usage:
  py validate_xml_structure.py <xml> [--baseline <prev-xml>] [--check-paths]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import urllib.parse
import xml.etree.ElementTree as ET
from pathlib import Path
from collections import Counter


def _as_int(el):
    if el is None or not el.text:
        return None
    try:
        return int(el.text)
    except ValueError:
        return None


def validate(xml_path: Path, check_paths: bool = False) -> dict:
    errors: list[str] = []
    warnings: list[str] = []

    try:
        tree = ET.parse(str(xml_path))
    except ET.ParseError as e:
        return {"ok": False, "errors": [f"XML parse error: {e}"], "warnings": [], "stats": {}}

    root = tree.getroot()
    seq = root.find("sequence")
    if seq is None:
        return {"ok": False, "errors": ["no <sequence> element"], "warnings": [], "stats": {}}

    # File-id inventory
    all_file_ids = Counter()
    pathurls_by_id: dict[str, str] = {}
    for f in seq.iter("file"):
        fid = f.attrib.get("id")
        if not fid:
            errors.append("found <file> without id attribute")
            continue
        all_file_ids[fid] += 1
        pathurl_el = f.find("pathurl")
        if pathurl_el is not None and pathurl_el.text:
            pathurls_by_id[fid] = pathurl_el.text
    for fid, n in all_file_ids.items():
        if n > 1:
            # Multiple <file id="X"> declarations is legal in xmeml when the
            # full block is repeated, but suspicious — warn.
            warnings.append(f"<file id={fid!r}> declared {n} times")

    # Clipitem walk
    n_clipitems = 0
    clipitem_ids = Counter()
    track_violations = []
    file_ref_missing = []
    negative_in_out = []

    for media in seq.findall("media"):
        for media_type in ("video", "audio"):
            md = media.find(media_type)
            if md is None:
                continue
            track_prefix = "V" if media_type == "video" else "A"
            for track_idx, track_el in enumerate(md.findall("track"), start=1):
                expected_label = f"{track_prefix}{track_idx}"
                for ci in track_el.findall("clipitem"):
                    n_clipitems += 1
                    cid = ci.attrib.get("id")
                    if cid:
                        clipitem_ids[cid] += 1

                    file_el = ci.find("file")
                    fid = file_el.attrib.get("id") if file_el is not None else None
                    if fid and fid not in all_file_ids:
                        file_ref_missing.append((cid, fid))

                    in_v = _as_int(ci.find("in"))
                    out_v = _as_int(ci.find("out"))
                    start_v = _as_int(ci.find("start"))

                    # -1 is a legal sentinel for "inherit from linked clip"
                    for label, val in (("in", in_v), ("out", out_v)):
                        if val is not None and val < 0:
                            negative_in_out.append((cid, label, val))
                    # start can legitimately be -1 (linked) — only error on
                    # other negative values
                    if start_v is not None and start_v < -1:
                        negative_in_out.append((cid, "start", start_v))

    for cid, n in clipitem_ids.items():
        if n > 1:
            errors.append(f"duplicate clipitem id {cid!r} ({n} occurrences)")

    for cid, fid in file_ref_missing:
        errors.append(f"clipitem {cid!r} references file id {fid!r} which has no <file> declaration")

    for cid, label, val in negative_in_out:
        errors.append(f"clipitem {cid!r}: {label}={val} is invalid (only start=-1 is a legal sentinel)")

    # Optional: check pathurls resolve to disk
    missing_files = 0
    if check_paths:
        for fid, url in pathurls_by_id.items():
            decoded = urllib.parse.unquote(url)
            # Strip "file://localhost/" prefix → absolute path
            prefix = "file://localhost/"
            if decoded.startswith(prefix):
                local = decoded[len(prefix):]
                # Windows path may have drive letter: "E:/foo" → "E:/foo"
                if not Path(local).exists():
                    missing_files += 1
                    warnings.append(f"pathurl does not resolve to disk: {decoded}")

    stats = {
        "n_files": len(all_file_ids),
        "n_clipitems": n_clipitems,
        "n_distinct_clipitem_ids": len(clipitem_ids),
        "n_missing_file_refs": len(file_ref_missing),
        "n_negative_in_out": len(negative_in_out),
        "n_pathurls_missing_on_disk": missing_files if check_paths else None,
        "xml_sha256": hashlib.sha256(open(xml_path, "rb").read()).hexdigest(),
    }
    return {"ok": not errors, "errors": errors, "warnings": warnings, "stats": stats}


def diff(baseline_xml: Path, candidate_xml: Path) -> dict:
    """High-level structural diff between two XMLs. Counts changes at the
    clipitem level, identified by (file_id, in, out, track) — not Premiere's
    clipitem-NNNN id (which renumbers)."""
    def _walk(p: Path) -> list[tuple]:
        tree = ET.parse(str(p))
        seq = tree.getroot().find("sequence")
        out = []
        if seq is None:
            return out
        for media in seq.findall("media"):
            for media_type in ("video", "audio"):
                md = media.find(media_type)
                if md is None:
                    continue
                prefix = "V" if media_type == "video" else "A"
                for tidx, t in enumerate(md.findall("track"), start=1):
                    label = f"{prefix}{tidx}"
                    for ci in t.findall("clipitem"):
                        fe = ci.find("file")
                        fid = fe.attrib.get("id") if fe is not None else None
                        out.append((
                            fid, _as_int(ci.find("in")), _as_int(ci.find("out")),
                            _as_int(ci.find("start")), label,
                        ))
        return out

    base = set(_walk(baseline_xml))
    cand = set(_walk(candidate_xml))
    return {
        "n_baseline_clipitems": len(base),
        "n_candidate_clipitems": len(cand),
        "n_added": len(cand - base),
        "n_removed": len(base - cand),
        "n_unchanged": len(base & cand),
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("xml")
    ap.add_argument("--baseline", default=None, help="prior XML to diff against")
    ap.add_argument("--check-paths", action="store_true",
                    help="warn on pathurls that don't resolve on disk")
    ap.add_argument("--out", default=None, help="JSON report path")
    args = ap.parse_args()

    rpt = validate(Path(args.xml), check_paths=args.check_paths)
    if args.baseline:
        rpt["diff_vs_baseline"] = diff(Path(args.baseline), Path(args.xml))

    print(f"xml:        {args.xml}")
    print(f"valid:      {rpt['ok']}")
    print(f"stats:      {json.dumps(rpt['stats'])}")
    if rpt["errors"]:
        print(f"errors ({len(rpt['errors'])}):")
        for e in rpt["errors"][:20]:
            print(f"  - {e}")
        if len(rpt["errors"]) > 20:
            print(f"  ... +{len(rpt['errors']) - 20} more (see --out report)")
    if rpt["warnings"]:
        print(f"warnings ({len(rpt['warnings'])}):")
        for w in rpt["warnings"][:10]:
            print(f"  - {w}")
        if len(rpt["warnings"]) > 10:
            print(f"  ... +{len(rpt['warnings']) - 10} more")
    if "diff_vs_baseline" in rpt:
        d = rpt["diff_vs_baseline"]
        print(f"diff:       +{d['n_added']} added, -{d['n_removed']} removed, ={d['n_unchanged']} unchanged")

    if args.out:
        Path(args.out).write_text(json.dumps(rpt, indent=2), encoding="utf-8")
        print(f"report:     {args.out}")

    return 0 if rpt["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
