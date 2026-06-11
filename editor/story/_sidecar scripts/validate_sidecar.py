#!/usr/bin/env python3
"""Validate a per-beat sidecar JSON against the spec.

Exit codes: 0 pass, 1 warnings, 2 errors.

Usage: python validate_sidecar.py <sidecar.json> [--xml <xml>] [--resolver <_resolver.json>]

Checks (per editor/story/sidecars/sidecars_README.md v1):
  - schema_version == 1
  - required top-level fields present
  - every annotation has a complete 5-tuple key (asset_id, source_in_frames,
    source_out_frames, timeline_start_frames, track)
  - clip_ids are unique within sidecar
  - every annotation.scene (if set) matches a scenes[].id
  - graphics_overlays[].timeline_start_seconds within timeline_range_seconds
  - if --resolver provided: every content key resolves to exactly one clipitem
  - if --xml provided: xml_sha256 matches current XML
"""

import argparse
import hashlib
import json
import sys
from pathlib import Path


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("sidecar")
    ap.add_argument("--xml", default=None, help="optional: verify xml_sha256")
    ap.add_argument("--resolver", default=None, help="optional: verify every key resolves")
    args = ap.parse_args()

    errors = []
    warnings = []

    try:
        sc = json.loads(Path(args.sidecar).read_text(encoding="utf-8"))
    except Exception as e:
        print(f"ERROR: could not parse sidecar JSON: {e}", file=sys.stderr)
        return 2

    if sc.get("schema_version") != 1:
        errors.append(f"schema_version != 1 (got {sc.get('schema_version')})")
    for f in ("beat_id", "label", "xml_source", "timeline_range_seconds", "frame_rate", "scenes", "annotations"):
        if f not in sc:
            errors.append(f"missing required top-level field: {f}")

    # scene id set
    scene_ids = {s.get("id") for s in sc.get("scenes", []) if s.get("id")}

    # clip_id uniqueness + key completeness
    clip_ids_seen = {}
    REQUIRED_KEY_FIELDS = ("asset_id", "source_in_frames", "source_out_frames", "timeline_start_frames", "track")
    sentinel_timeline = []
    for i, ann in enumerate(sc.get("annotations", [])):
        # key check
        key = ann.get("key") or {}
        for f in REQUIRED_KEY_FIELDS:
            if key.get(f) is None:
                errors.append(f"annotation[{i}] (clip_id={ann.get('clip_id')}): key.{f} is None")
        # xmeml -1 sentinel (warning, not error -- documented)
        if key.get("timeline_start_frames") == -1:
            sentinel_timeline.append((i, ann.get("clip_id"), ann.get("name"), ann.get("key", {}).get("track")))
        # clip_id uniqueness
        cid = ann.get("clip_id")
        if cid:
            if cid in clip_ids_seen:
                errors.append(f"duplicate clip_id '{cid}' at annotations[{i}] and annotations[{clip_ids_seen[cid]}]")
            clip_ids_seen[cid] = i
        # scene ref
        sref = ann.get("scene")
        if sref and sref not in scene_ids:
            errors.append(f"annotation[{i}] (clip_id={cid}): scene='{sref}' not in scenes[]")

    # graphics overlays in range
    tr = sc.get("timeline_range_seconds", [0, float("inf")])
    for i, ov in enumerate(sc.get("graphics_overlays", [])):
        ts = ov.get("timeline_start_seconds")
        if ts is None or ts < tr[0] or ts > tr[1]:
            errors.append(f"graphics_overlays[{i}] (clip_id={ov.get('clip_id')}): timeline_start_seconds {ts} outside range {tr}")

    # XML SHA check
    if args.xml:
        xml_path = Path(args.xml)
        if xml_path.exists():
            actual_sha = hashlib.sha256(xml_path.read_bytes()).hexdigest()
            stored = sc.get("xml_sha256")
            if stored and stored != actual_sha:
                warnings.append(f"xml_sha256 mismatch: sidecar={stored[:16]}... actual={actual_sha[:16]}... (XML may have changed since sidecar was last updated)")
        else:
            warnings.append(f"--xml path does not exist: {xml_path}")

    # Resolver check
    if args.resolver:
        try:
            idx = json.loads(Path(args.resolver).read_text(encoding="utf-8"))
            # Build a key->clipitem map for fast lookup
            def key_tuple(k):
                return (k.get("asset_id"), k.get("source_in_frames"), k.get("source_out_frames"), k.get("timeline_start_frames"), k.get("track"))
            resolver_keys = {key_tuple(e["key"]): e["clipitem_id"] for e in idx.get("entries", [])}
            unresolved = 0
            for ann in sc.get("annotations", []):
                kt = key_tuple(ann.get("key") or {})
                if kt not in resolver_keys:
                    if ann.get("key", {}).get("timeline_start_frames") == -1:
                        # known xmeml sentinel; just warn
                        continue
                    warnings.append(f"annotation key not resolved by resolver: clip_id={ann.get('clip_id')} aid={kt[0][:12] if kt[0] else 'None'}...")
                    unresolved += 1
            print(f"resolver check: {len(sc.get('annotations', [])) - unresolved} resolved / {len(sc.get('annotations', []))} total")
        except Exception as e:
            warnings.append(f"could not load resolver: {e}")

    # Report
    print(f"\nsidecar: {args.sidecar}")
    print(f"  scenes:              {len(sc.get('scenes', []))}")
    print(f"  annotations:         {len(sc.get('annotations', []))}")
    print(f"  graphics_overlays:   {len(sc.get('graphics_overlays', []))}")
    if sentinel_timeline:
        print(f"\n  xmeml -1 sentinel (linked-audio inherits position): {len(sentinel_timeline)} annotations")
        for i, cid, name, track in sentinel_timeline[:5]:
            print(f"    {cid:<8} track={track:<3}  {name}")
        if len(sentinel_timeline) > 5:
            print(f"    ... and {len(sentinel_timeline) - 5} more")
    if warnings:
        print(f"\nWARNINGS ({len(warnings)}):")
        for w in warnings:
            print(f"  {w}")
    if errors:
        print(f"\nERRORS ({len(errors)}):")
        for e in errors[:20]:
            print(f"  {e}")
        return 2
    print(f"\n{'PASS' if not warnings else 'PASS WITH WARNINGS'}")
    return 1 if warnings else 0


if __name__ == "__main__":
    sys.exit(main())
