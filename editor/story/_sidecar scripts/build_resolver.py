#!/usr/bin/env python3
"""
Build the resolver index: content_key -> current clipitem-NNNN for a given XML export.

Content key = (asset_id, source_in_frames, source_out_frames, timeline_start_frames, track)
where:
    asset_id is parsed from the pathurl (SHA256 stem of the proxy filename, OR from a
        manifest if pathurls have been remapped to original filenames after Stage B reorg)
    source_in/out_frames are <in>/<out> in the sequence rate
    timeline_start_frames is <start>
    track is V1/V2/V3/A1/A2/A3, derived from <video>/<audio> + track index

Usage:
    python build_resolver.py <xml> --out <resolver.json>
                                  [--asset-map <_index/asset_map.json>]   for post-Stage-B pathurls
"""

import argparse
import hashlib
import json
import os
import re
import sys
import urllib.parse
import xml.etree.ElementTree as ET
from pathlib import Path


SHA_RE = re.compile(r'([0-9a-f]{64})\.', re.IGNORECASE)
PATHURL_OLD_RE = re.compile(
    r'/derivative%20media/([^/]+)/([0-9a-f]{64})\.([A-Za-z0-9]+)', re.IGNORECASE,
)


def asset_id_from_pathurl(pathurl: str, asset_map_inv: dict) -> str | None:
    """Resolve asset_id from a pathurl. Handles both old (asset-id-flat) and new
    (mirrored-tree, original filenames) layouts. asset_map_inv maps
    relative_path -> asset_id for the mirrored-tree case."""
    decoded = urllib.parse.unquote(pathurl)
    # Old layout: pathurl contains the asset_id directly
    m = SHA_RE.search(decoded)
    if m:
        return m.group(1).lower()
    # New layout: lookup by relative path
    # decoded looks like: file://localhost/E:/open-post-stack/derivative media/<rel>
    # Strip the prefix
    marker = "/derivative media/"
    idx = decoded.find(marker)
    if idx == -1:
        return None
    rel = decoded[idx + len(marker):].replace("/", "\\")
    return asset_map_inv.get(rel) or asset_map_inv.get(rel.lower())


def build_index(xml_path: Path, asset_map_path: Path | None = None) -> dict:
    tree = ET.parse(str(xml_path))
    root = tree.getroot()

    asset_map_inv = {}
    if asset_map_path and asset_map_path.exists():
        with open(asset_map_path, "r", encoding="utf-8") as f:
            am = json.load(f)
        for aid, kinds in am.get("entries", {}).items():
            for kind, info in kinds.items():
                rel = info.get("relative_path")
                if rel:
                    asset_map_inv[rel] = aid

    # Walk the sequence's <media>/<video|audio>/<track>/<clipitem> structure.
    # Track index within each media type for V1/A1 naming.
    entries = []
    near_misses = []  # clipitems where we couldn't resolve asset_id (e.g., placeholder)

    seq = root.find("sequence")
    if seq is None:
        raise SystemExit("no <sequence> in XML root")

    # File-level pathurl cache: file_id -> (asset_id, pathurl, name)
    file_id_to_asset = {}
    # First pass: <file id="..."> blocks at any depth, capture asset_id from pathurl
    for f in seq.iter("file"):
        fid = f.attrib.get("id")
        if not fid:
            continue
        pathurl_el = f.find("pathurl")
        if pathurl_el is None or not pathurl_el.text:
            continue
        aid = asset_id_from_pathurl(pathurl_el.text, asset_map_inv)
        name_el = f.find("name")
        file_id_to_asset[fid] = {
            "asset_id": aid,
            "pathurl": pathurl_el.text,
            "name": name_el.text if name_el is not None else None,
        }

    # Second pass: clipitems with track context
    for media in seq.findall("media"):
        for media_type in ("video", "audio"):
            md = media.find(media_type)
            if md is None:
                continue
            track_prefix = "V" if media_type == "video" else "A"
            for track_idx, track_el in enumerate(md.findall("track"), start=1):
                track_label = f"{track_prefix}{track_idx}"
                for ci in track_el.findall("clipitem"):
                    ci_id = ci.attrib.get("id")
                    name_el = ci.find("name")
                    mc_el = ci.find("masterclipid")
                    start_el = ci.find("start")
                    end_el = ci.find("end")
                    in_el = ci.find("in")
                    out_el = ci.find("out")
                    # The clipitem references a file either inline or by id only
                    file_el = ci.find("file")
                    file_id = file_el.attrib.get("id") if file_el is not None else None
                    file_info = file_id_to_asset.get(file_id, {})

                    def as_int(el):
                        try:
                            return int(el.text) if el is not None and el.text else None
                        except ValueError:
                            return None

                    entry = {
                        "clipitem_id": ci_id,
                        "name": name_el.text if name_el is not None else None,
                        "masterclip_id": mc_el.text if mc_el is not None else None,
                        "file_id": file_id,
                        "track": track_label,
                        "key": {
                            "asset_id": file_info.get("asset_id"),
                            "source_in_frames": as_int(in_el),
                            "source_out_frames": as_int(out_el),
                            "timeline_start_frames": as_int(start_el),
                            "track": track_label,
                        },
                        "timeline_end_frames": as_int(end_el),
                        "pathurl": file_info.get("pathurl"),
                    }
                    if entry["key"]["asset_id"] is None:
                        near_misses.append({
                            "clipitem_id": ci_id, "name": entry["name"],
                            "reason": "could not resolve asset_id from pathurl",
                            "pathurl": file_info.get("pathurl"),
                            "track": track_label,
                        })
                    entries.append(entry)

    return {
        "schema_version": 1,
        "xml_source": str(xml_path),
        "xml_sha256": hashlib.sha256(open(xml_path, "rb").read()).hexdigest(),
        "rebuilt_at_utc": __import__("time").strftime("%Y-%m-%dT%H:%M:%SZ", __import__("time").gmtime()),
        "asset_map_used": str(asset_map_path) if asset_map_path else None,
        "n_clipitems": len(entries),
        "n_unresolved": len(near_misses),
        "entries": entries,
        "near_misses": near_misses,
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--xml", required=True)
    ap.add_argument("--asset-map", default=None, help="_index/asset_map.json for resolving mirrored-tree pathurls")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    idx = build_index(Path(args.xml), Path(args.asset_map) if args.asset_map else None)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(idx, f, indent=2, ensure_ascii=False)

    print(f"clipitems: {idx['n_clipitems']}")
    print(f"unresolved (no asset_id): {idx['n_unresolved']}")
    print(f"output: {out_path}")
    return 0 if idx["n_unresolved"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
