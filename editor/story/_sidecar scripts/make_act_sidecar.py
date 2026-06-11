#!/usr/bin/env python3
"""Extract an Act-scoped sidecar (schema v2) from an xmeml export.

This is the v2 evolution of make_beat_sidecar.py. Instead of one sidecar
per beat, the Act is the unit: `beats[]` lives nested at the top level,
each beat carries its own `scenes[]`, and each annotation has a `beat`
field as well as `scene`.

Why: beats vary 5x in length (b_06 = 8min, b_10 ≈ 15min), per-beat
preview MP4 rendering is no longer in the loop, and one file is easier
to keep coherent as beat cut-offs drift.

Inputs:
  --xml                xmeml export
  --beats-manifest     JSON with the beats[] partition (see schema below)
  --frame-rate         default 24000/1001 (23.976...)
  --asset-map          _index/asset_map.json for post-Stage-B pathurls
  --prior-sidecar      optional v2 Act sidecar to inherit from (re-extraction)
  --seed-v1-sidecars   optional directory of v1 per-beat sidecars to merge
                       in as a one-time migration seed
  --out                output sidecar path

Beats manifest schema:
  {
    "act_id": "actII",
    "label": "Act II",
    "timeline_range_frames": [0, 64203],
    "beats": [
      {"id": "b_06", "label": "Break into Two",
       "timeline_range_frames": [0, 11755]},
      {"id": "b_07", "label": "Catalyst",
       "timeline_range_frames": [11755, 64203]}
    ]
  }

Usage:
  py make_act_sidecar.py \\
    --xml "../../xml exports/...FULL_REMAP.xml" \\
    --beats-manifest "../sidecars/actII_beats_manifest.json" \\
    --asset-map "E:/open-post-stack/derivative media/_index/asset_map.json" \\
    --seed-v1-sidecars "../sidecars/_archive/v1_per_beat" \\
    --out "../sidecars/actII.sidecar.json"
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from pathlib import Path
from typing import Optional

# Reuse the per-beat extractor's primitives
sys.path.insert(0, str(Path(__file__).resolve().parent))
from make_beat_sidecar import (
    DEFAULT_FRAME_RATE,
    _extract_all_clipitems,
    _resolve_sentinels_and_orphans,
    _scope_to_beat,
    _detect_audio_spines,
    _make_annotation,
    _inherit_from_prior,
    _index_prior_annotations,
    _scene_for_timeline,
    _load_asset_map_inv,
)


SCHEMA_VERSION = 2


def _beat_for_timeline(tls_frames: int, beats: list[dict]) -> Optional[str]:
    """Return beat id whose timeline_range_frames contains tls_frames."""
    if tls_frames is None:
        return None
    for b in beats:
        s, e = b["timeline_range_frames"]
        if s <= tls_frames < e:
            return b["id"]
    return None


def _load_seed_v1_sidecars(seed_dir: Optional[Path]) -> dict:
    """Walk a directory of v1 per-beat sidecars; return dict
    {beat_id: v1_sidecar}. Used once for the v1 → v2 migration."""
    seeds: dict = {}
    if not seed_dir or not seed_dir.exists():
        return seeds
    for p in sorted(seed_dir.glob("actII_*.sidecar.json")):
        try:
            sc = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        bid = sc.get("beat_id")
        if bid:
            seeds[bid] = sc
    return seeds


def build_act_sidecar(
    xml_path: Path,
    beats_manifest: dict,
    frame_rate: float,
    asset_map: Optional[Path],
    prior_sidecar: Optional[Path],
    seed_v1_sidecars: Optional[Path],
) -> dict:
    asset_map_inv = _load_asset_map_inv(asset_map)
    all_cis = _extract_all_clipitems(xml_path, asset_map_inv)
    resolved, all_orphans = _resolve_sentinels_and_orphans(all_cis)

    act_start, act_end = beats_manifest["timeline_range_frames"]
    in_act = _scope_to_beat(resolved, act_start, act_end)
    spine_ids = _detect_audio_spines(in_act)

    # Include: V1 video, all audio tracks, AND V2+ video clipitems with a real
    # pathurl (= real b-roll media). V2+ clipitems WITHOUT a pathurl are
    # graphics placeholders (lower-thirds, generators) and stay excluded
    # (managed Premiere-side via pymiere, not in the LLM editorial loop).
    # Earlier behavior dropped all V2+ unconditionally; that was
    # safe when V2+ only held graphics, but broke once direct-XML b-roll
    # inserts started landing on V3+.
    in_act = [
        ci for ci in in_act
        if ci.get("track") == "V1"
        or (ci.get("track") or "").startswith("A")
        or ((ci.get("track") or "").startswith("V") and ci.get("pathurl"))
    ]

    # Sort all clipitems by (timeline_start, track, source_in, clipitem_id)
    # for stable, document-order numbering across the entire Act.
    def sort_key(ci):
        return (
            ci["timeline_start_frames"] or 0,
            0 if ci.get("track") == "V1" else (1 if (ci.get("track") or "").startswith("V") else 2),
            ci["source_in_frames"] or 0,
            ci["track"] or "",
            ci["clipitem_id"] or "",
        )
    in_act_sorted = sorted(in_act, key=sort_key)

    # Load prior v2 sidecar OR seed v1 sidecars (mutually exclusive use cases,
    # but both supported for safety: seeds take precedence for beat-level
    # scenes; prior takes precedence for annotation editorial fields).
    prior = None
    if prior_sidecar and prior_sidecar.exists():
        prior = json.loads(prior_sidecar.read_text(encoding="utf-8"))
    seeds = _load_seed_v1_sidecars(seed_v1_sidecars)

    # Beats: take from manifest; if a seed v1 sidecar exists for a beat,
    # inherit its scenes + boundary_anchors. If prior v2 exists, prior's
    # scenes for that beat override seeds.
    beats_out = []
    for b in beats_manifest["beats"]:
        bid = b["id"]
        bs, be = b["timeline_range_frames"]
        seconds_range = [round(bs / frame_rate, 3), round(be / frame_rate, 3)]
        beat_block = {
            "id": bid,
            "label": b.get("label") or "(beat name TBD)",
            "timeline_range_frames": [bs, be],
            "timeline_range_seconds": seconds_range,
            "scenes": [],
        }
        # Seed from v1 per-beat (migration only)
        if bid in seeds:
            v1 = seeds[bid]
            beat_block["scenes"] = v1.get("scenes", [])
            if v1.get("boundary_anchors"):
                beat_block["boundary_anchors"] = v1["boundary_anchors"]
        # Prior v2 overrides seed
        if prior:
            prior_beat = next((pb for pb in prior.get("beats", []) if pb.get("id") == bid), None)
            if prior_beat:
                if prior_beat.get("scenes"):
                    beat_block["scenes"] = prior_beat["scenes"]
                if prior_beat.get("boundary_anchors"):
                    beat_block["boundary_anchors"] = prior_beat["boundary_anchors"]
                if prior_beat.get("label") and prior_beat["label"] != "(beat name TBD)":
                    beat_block["label"] = prior_beat["label"]
        beats_out.append(beat_block)

    # Build a flat prior annotation index across BOTH v2 prior (act-scoped)
    # AND v1 seeds (per-beat). v2 prior takes precedence on key collisions.
    prior_ann_idx: dict = {}
    for v1 in seeds.values():
        prior_ann_idx.update(_index_prior_annotations(v1))
    if prior:
        prior_ann_idx.update(_index_prior_annotations(prior))

    # Clip_id numbering. Continue from prior v2's max, or 0 if first run.
    # Seeds don't drive numbering — they're per-beat and will collide if
    # treated as authoritative; instead, we'll honor seed clip_ids by reuse
    # via content key match below.
    def _max_prefix(d: dict, prefix: str) -> int:
        pat = re.compile(rf"^{prefix}(\d+)$")
        highest = -1
        for ann in d.values():
            cid = (ann or {}).get("clip_id") or ""
            m = pat.match(cid)
            if m:
                highest = max(highest, int(m.group(1)))
        return highest

    c_next = _max_prefix(prior_ann_idx, "c") + 1
    a_next = _max_prefix(prior_ann_idx, "a") + 1
    taken_c = {int(re.match(r"^c(\d+)$", (a or {}).get("clip_id") or "").group(1))
               for a in prior_ann_idx.values()
               if re.match(r"^c(\d+)$", (a or {}).get("clip_id") or "")}
    taken_a = {int(re.match(r"^a(\d+)$", (a or {}).get("clip_id") or "").group(1))
               for a in prior_ann_idx.values()
               if re.match(r"^a(\d+)$", (a or {}).get("clip_id") or "")}

    # Build annotations
    annotations = []
    for ci in in_act_sorted:
        track = ci.get("track") or ""
        # Determine prefix
        if track.startswith("V"):
            # Any video track that made it past the filter is real video
            # (V1 or V2+ b-roll). Same 'c' namespace; track field disambiguates.
            prefix = "c"
        elif track.startswith("A"):
            prefix = "a"
        else:
            continue  # only graphics with no pathurl would reach here; filtered above

        # Try clip_id reuse from prior by content key
        ktuple = (
            ci.get("asset_id"),
            ci["source_in_frames"], ci["source_out_frames"],
            ci["timeline_start_frames"], ci["track"],
        )
        prior_ann = prior_ann_idx.get(ktuple)
        prior_cid = (prior_ann or {}).get("clip_id") or ""
        if prior_cid.startswith(prefix) and prior_cid[1:].isdigit():
            cid = prior_cid
        else:
            if prefix == "c":
                while c_next in taken_c:
                    c_next += 1
                cid = f"c{c_next:04d}"
                taken_c.add(c_next)
                c_next += 1
            else:
                while a_next in taken_a:
                    a_next += 1
                cid = f"a{a_next:04d}"
                taken_a.add(a_next)
                a_next += 1

        ann = _make_annotation(
            ci, cid, is_audio_spine=(ci["clipitem_id"] in spine_ids),
        )
        ann = _inherit_from_prior(ann, prior_ann_idx)

        # Assign beat by timeline range
        ann["beat"] = _beat_for_timeline(
            ann["key"]["timeline_start_frames"], beats_manifest["beats"],
        )

        # Assign scene by walking THAT beat's scenes
        if ann["beat"]:
            beat_block = next(b for b in beats_out if b["id"] == ann["beat"])
            ann["scene"] = _scene_for_timeline(
                ann["key"]["timeline_start_frames"], beat_block.get("scenes", []),
            )
        annotations.append(ann)

    xml_sha = hashlib.sha256(xml_path.read_bytes()).hexdigest()

    return {
        "schema_version": SCHEMA_VERSION,
        "act_id": beats_manifest.get("act_id", "act"),
        "label": beats_manifest.get("label") or beats_manifest.get("act_id", "Act"),
        "xml_source": str(xml_path),
        "xml_sha256": xml_sha,
        "frame_rate": frame_rate,
        "timeline_range_frames": beats_manifest["timeline_range_frames"],
        "timeline_range_seconds": [
            round(beats_manifest["timeline_range_frames"][0] / frame_rate, 3),
            round(beats_manifest["timeline_range_frames"][1] / frame_rate, 3),
        ],
        "generated_by": "make_act_sidecar.py",
        "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "prior_sidecar": str(prior_sidecar) if prior_sidecar else None,
        "seed_v1_sidecars": str(seed_v1_sidecars) if seed_v1_sidecars else None,
        "beats": beats_out,
        "annotations": annotations,
        "_counts": {
            "n_beats": len(beats_out),
            "n_annotations": len(annotations),
            "n_v1": sum(1 for a in annotations if a["key"]["track"] == "V1"),
            "n_v_higher": sum(1 for a in annotations if (a["key"]["track"] or "").startswith("V") and a["key"]["track"] != "V1"),
            "n_audio": sum(1 for a in annotations if (a["key"]["track"] or "").startswith("A")),
            "n_audio_spine": sum(1 for a in annotations if a.get("audio_spine")),
            "n_unresolved_asset_id": sum(1 for a in annotations if a["key"]["asset_id"] is None),
            "n_unassigned_beat": sum(1 for a in annotations if a.get("beat") is None),
            "n_unassigned_scene": sum(1 for a in annotations if a.get("scene") is None),
            "n_inherited_editorial": sum(
                1 for a in annotations
                if (a.get("rationale") is not None
                    or a.get("lower_third") is not None
                    or a.get("location_title") is not None
                    or a.get("date_tracker") is not None)
            ),
            "n_orphans_in_xml": len(all_orphans),
        },
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--xml", required=True)
    ap.add_argument("--beats-manifest", required=True)
    ap.add_argument("--frame-rate", type=float, default=DEFAULT_FRAME_RATE)
    ap.add_argument("--asset-map", default=None)
    ap.add_argument("--prior-sidecar", default=None)
    ap.add_argument("--seed-v1-sidecars", default=None,
                    help="directory of v1 per-beat sidecars to seed from (migration use)")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    beats_manifest = json.loads(Path(args.beats_manifest).read_text(encoding="utf-8"))

    sc = build_act_sidecar(
        xml_path=Path(args.xml),
        beats_manifest=beats_manifest,
        frame_rate=args.frame_rate,
        asset_map=Path(args.asset_map) if args.asset_map else None,
        prior_sidecar=Path(args.prior_sidecar) if args.prior_sidecar else None,
        seed_v1_sidecars=Path(args.seed_v1_sidecars) if args.seed_v1_sidecars else None,
    )

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    # Safe write: verify after writing (mount truncation defense)
    payload = json.dumps(sc, indent=2, ensure_ascii=False).encode("utf-8")
    expected_sha = __import__("hashlib").sha256(payload).hexdigest()
    for attempt in range(3):
        out.write_bytes(payload)
        actual = out.read_bytes()
        if __import__("hashlib").sha256(actual).hexdigest() == expected_sha:
            break
        print(f"  WARN: write verify failed on attempt {attempt+1}, retrying", file=sys.stderr)
        __import__("time").sleep(0.5 * (attempt + 1))
    else:
        raise RuntimeError(f"failed to write {out} after 3 attempts (mount corruption)")

    c = sc["_counts"]
    print(f"act:                {sc['act_id']} ({sc['label']})")
    print(f"beats:              {c['n_beats']}  ({', '.join(b['id'] for b in sc['beats'])})")
    print(f"annotations:        {c['n_annotations']}  (V1={c['n_v1']}, V2+={c.get('n_v_higher', 0)}, audio={c['n_audio']})")
    print(f"audio_spine:        {c['n_audio_spine']}")
    print(f"unassigned beat:    {c['n_unassigned_beat']}")
    print(f"unassigned scene:   {c['n_unassigned_scene']}  (expected: clips in beats with no scenes defined yet)")
    print(f"inherited editorial:{c['n_inherited_editorial']}")
    print(f"orphans in XML:     {c['n_orphans_in_xml']}")
    print(f"output:             {out}")
    return 0 if c["n_unresolved_asset_id"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
