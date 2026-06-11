#!/usr/bin/env python3
"""Reconcile a sidecar against a fresh resolver after a Premiere re-export.

Premiere edits can shift a clip's source in/out (trim), timeline_start (move),
or track (re-track). When the underlying content key changes, the sidecar's
annotation no longer resolves and the editorial metadata (rationale,
lower_third, etc.) is orphaned.

This script reads the sidecar + fresh resolver and produces a reconciliation
report:

  - matched:    annotation key resolves cleanly in the new resolver (no work)
  - rebind:     a single near-match (3-of-4 fields match) exists; safe to
                rebind the annotation's key to the new resolver entry
  - ambiguous:  multiple near-matches; needs human pick
  - orphaned:   no near-match — clip likely deleted or substantially altered

Usage:
  py reconcile_sidecar.py \\
    --sidecar  ../sidecars/actII_b_06.sidecar.json \\
    --resolver ../sidecars/_resolver/actII_clip_index.json \\
    [--apply]      auto-apply rebind cases (does NOT touch ambiguous/orphan)
    [--out RECONCILE.json]
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path


def _key_tuple(k: dict) -> tuple:
    return (
        k.get("asset_id"), k.get("source_in_frames"), k.get("source_out_frames"),
        k.get("timeline_start_frames"), k.get("track"),
    )


def _near_match_score(a: tuple, b: tuple) -> int:
    """Count how many of the 5 fields match."""
    return sum(1 for x, y in zip(a, b) if x == y)


def reconcile(sidecar: dict, resolver: dict) -> dict:
    resolver_entries = resolver.get("entries", [])
    # Index resolver by full key for O(1) exact-match lookup
    by_exact_key = {_key_tuple(e["key"]): e for e in resolver_entries}
    # Pre-bucket resolver entries by asset_id for cheaper near-match
    by_asset = {}
    for e in resolver_entries:
        aid = (e.get("key") or {}).get("asset_id")
        by_asset.setdefault(aid, []).append(e)

    matched, rebind, ambiguous, orphaned = [], [], [], []
    for ann in sidecar.get("annotations", []):
        ann_key = _key_tuple(ann.get("key") or {})
        if ann_key in by_exact_key:
            matched.append({"clip_id": ann.get("clip_id"), "key": ann.get("key")})
            continue

        # Near-match: prefer entries with same asset_id (much smaller search)
        candidates = by_asset.get(ann_key[0], resolver_entries)
        scored = [
            (_near_match_score(ann_key, _key_tuple(c["key"])), c)
            for c in candidates
        ]
        best = [c for s, c in scored if s == 4]  # 4-of-5 match
        if len(best) == 1:
            rebind.append({
                "clip_id": ann.get("clip_id"),
                "old_key": ann.get("key"),
                "new_key": best[0]["key"],
                "delta": {
                    field: (ann_key[i], _key_tuple(best[0]["key"])[i])
                    for i, field in enumerate(("asset_id", "source_in_frames",
                                                "source_out_frames",
                                                "timeline_start_frames", "track"))
                    if ann_key[i] != _key_tuple(best[0]["key"])[i]
                },
            })
        elif len(best) > 1:
            ambiguous.append({
                "clip_id": ann.get("clip_id"),
                "old_key": ann.get("key"),
                "candidates": [c["key"] for c in best],
            })
        else:
            # Try 3-of-5 match as a last-ditch suggestion
            three_match = [c for s, c in scored if s == 3]
            orphaned.append({
                "clip_id": ann.get("clip_id"),
                "old_key": ann.get("key"),
                "n_3_of_5_candidates": len(three_match),
                "sample_3_of_5_candidates": [c["key"] for c in three_match[:3]],
            })

    return {
        "schema_version": 1,
        "sidecar_beat_id": sidecar.get("beat_id"),
        "resolver_xml_sha256": resolver.get("xml_sha256"),
        "sidecar_xml_sha256": sidecar.get("xml_sha256"),
        "sha_mismatch": resolver.get("xml_sha256") != sidecar.get("xml_sha256"),
        "_counts": {
            "matched": len(matched),
            "rebind_candidates": len(rebind),
            "ambiguous": len(ambiguous),
            "orphaned": len(orphaned),
        },
        "matched": matched,
        "rebind": rebind,
        "ambiguous": ambiguous,
        "orphaned": orphaned,
    }


def _atomic_write(p: Path, data: dict):
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=p.stem + ".", suffix=".tmp", dir=str(p.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        shutil.move(tmp, p)
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def apply_rebinds(sidecar_path: Path, sidecar: dict, report: dict) -> int:
    """Mutate sidecar in-place: replace annotation.key with new_key for every
    rebind candidate. Skips ambiguous + orphaned. Returns number applied."""
    rebind_by_clip = {r["clip_id"]: r for r in report.get("rebind", [])}
    n = 0
    for ann in sidecar.get("annotations", []):
        cid = ann.get("clip_id")
        if cid in rebind_by_clip:
            ann["key"] = rebind_by_clip[cid]["new_key"]
            n += 1
    if n:
        _atomic_write(sidecar_path, sidecar)
    return n


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sidecar", required=True)
    ap.add_argument("--resolver", required=True)
    ap.add_argument("--apply", action="store_true",
                    help="auto-apply rebind cases (does NOT touch ambiguous/orphaned)")
    ap.add_argument("--out", default=None, help="reconciliation report path")
    args = ap.parse_args()

    sidecar_path = Path(args.sidecar)
    sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
    resolver = json.loads(Path(args.resolver).read_text(encoding="utf-8"))

    report = reconcile(sidecar, resolver)

    c = report["_counts"]
    print(f"sidecar:  {sidecar_path.name}  ({sidecar.get('beat_id')})")
    print(f"matched:           {c['matched']}")
    print(f"rebind candidates: {c['rebind_candidates']}")
    print(f"ambiguous:         {c['ambiguous']}")
    print(f"orphaned:          {c['orphaned']}")
    if report["sha_mismatch"]:
        print("NOTE: sidecar xml_sha256 differs from resolver xml_sha256 (expected after re-export)")

    if args.apply:
        n = apply_rebinds(sidecar_path, sidecar, report)
        print(f"applied {n} rebinds to {sidecar_path.name}")

    if args.out:
        _atomic_write(Path(args.out), report)
        print(f"report: {args.out}")

    return 0 if c["ambiguous"] == 0 and c["orphaned"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
