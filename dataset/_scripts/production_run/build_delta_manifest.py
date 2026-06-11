#!/usr/bin/env python3
"""Build a delta production_manifest.json from a list of asset_ids + the video
catalog under `dataset/assets/`.

Output schema matches the existing production_manifest.json so that
label_videos_vertex.py:cmd_prepare can consume it as-is. Run via:

    python3 build_delta_manifest.py \
        --asset-ids /path/to/added_ready_for_proxy.txt \
        --out <RUNS_DIR>/production_run/production_manifest_v6_delta.json

Bucket derivation mirrors the empirical distribution of the v3 manifest:
- category_name -> broll/timelapse/pov_phone if it matches the known set
- shoot_label "interview" or "call" + duration -> interview_long/mid/call_short
- everything else -> verite
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from _paths import VIDEO_CATALOG, DERIVATIVE_MEDIA, RUNS_DIR, resolve_proxy_via_asset_map

DATA_DIR = RUNS_DIR / "production_run"
DEFAULT_CATALOG_DIR = VIDEO_CATALOG
DEFAULT_ASSET_IDS = DATA_DIR / "added_ready_for_proxy.txt"
DEFAULT_OUT = DATA_DIR / "production_manifest_v6_delta.json"
DEFAULT_PROXIES_DIR = DERIVATIVE_MEDIA

CHUNK_THRESHOLD_SEC = 55 * 60

BROLL_CATEGORIES = {
    "<b-roll-folder-2>", "<b-roll-folder-3>",
    "<b-roll-folder-4>", "<b-roll-folder-5>", "<b-roll-folder-6>",
}


def derive_bucket(camera_id: str | None, category_name: str | None,
                  shoot_label: str | None, duration_sec: float) -> str:
    if category_name == "<b-roll-folder-1>":
        return "timelapse"
    if category_name in BROLL_CATEGORIES:
        return "broll"
    if category_name == "Insta + Phone Dumps":
        return "pov_phone"

    label = (shoot_label or "").lower()
    is_interview = "interview" in label or " int" in label or label.endswith(" int")
    is_call = "call" in label

    if duration_sec >= 30 * 60:
        if is_interview:
            return "interview_long"
        if is_call:
            return "call_short"
        return "verite"
    if duration_sec >= 10 * 60:
        if is_interview:
            return "interview_mid"
        if is_call:
            return "call_short"
    return "verite"


def build_clip_entry(catalog_json: dict) -> dict:
    pm = catalog_json.get("path_metadata", {}) or {}
    ff = catalog_json.get("ffprobe", {}) or {}
    duration_sec = float(ff.get("duration_sec") or 0.0)
    return {
        "asset_id": catalog_json["asset_id"],
        "filename": catalog_json.get("filename"),
        "source_path": catalog_json.get("source_path"),
        "duration_sec": duration_sec,
        "duration_min": round(duration_sec / 60.0, 2),
        "fps": ff.get("fps"),
        "width": ff.get("width"),
        "height": ff.get("height"),
        "codec": ff.get("codec"),
        "camera_id": pm.get("camera_id"),
        "shoot_label": pm.get("shoot_label"),
        "category_name": pm.get("category_name"),
        "shoot_date": pm.get("shoot_date"),
        "has_machine_transcript": bool(catalog_json.get("has_machine_transcript", False)),
        "needs_chunking": duration_sec > CHUNK_THRESHOLD_SEC,
        "bucket": derive_bucket(pm.get("camera_id"), pm.get("category_name"),
                                pm.get("shoot_label"), duration_sec),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--asset-ids", type=Path, default=DEFAULT_ASSET_IDS,
                    help=f"file with one asset_id per line (default: {DEFAULT_ASSET_IDS})")
    ap.add_argument("--catalog-dir", type=Path, default=DEFAULT_CATALOG_DIR,
                    help=f"directory of {{asset_id}}.video.json (default: {DEFAULT_CATALOG_DIR})")
    ap.add_argument("--proxies-dir", type=Path, default=DEFAULT_PROXIES_DIR,
                    help="proxy mp4 directory; clips without a proxy are skipped")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT,
                    help=f"output manifest path (default: {DEFAULT_OUT})")
    ap.add_argument("--schema-source", type=str, default="sample automation v6 delta",
                    help="metadata field recorded in the manifest")
    ap.add_argument("--require-proxy", action="store_true", default=True,
                    help="(default) skip asset_ids without a proxy on disk")
    ap.add_argument("--no-require-proxy", dest="require_proxy", action="store_false",
                    help="include asset_ids even if their proxy is missing")
    args = ap.parse_args()

    asset_ids = [line.strip() for line in args.asset_ids.read_text().splitlines() if line.strip()]
    print(f"asset_ids in list:        {len(asset_ids)}", file=sys.stderr)

    clips: list[dict] = []
    skipped_no_catalog = []
    skipped_no_proxy = []
    skipped_no_duration = []

    for aid in asset_ids:
        cat_path = args.catalog_dir / f"{aid}.video.json"
        if not cat_path.exists():
            skipped_no_catalog.append(aid)
            continue
        j = json.loads(cat_path.read_text())
        clip = build_clip_entry(j)
        if not clip["duration_sec"]:
            skipped_no_duration.append(aid)
            continue
        if args.require_proxy:
            resolved = resolve_proxy_via_asset_map(aid)
            if resolved is None or not resolved.exists():
                skipped_no_proxy.append(aid)
                continue
        clips.append(clip)

    total_sec = sum(c["duration_sec"] for c in clips)
    long_clips = [c for c in clips if c["needs_chunking"]]
    long_sec = sum(c["duration_sec"] for c in long_clips)
    bucket_counts = Counter(c["bucket"] for c in clips)

    manifest = {
        "generated_at": __import__("datetime").datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "schema_source": args.schema_source,
        "asset_ids_source": str(args.asset_ids),
        "proxy_source_dir": str(args.proxies_dir),
        "total_clips": len(clips),
        "skipped_no_proxy": len(skipped_no_proxy),
        "skipped_no_catalog": len(skipped_no_catalog),
        "skipped_no_duration": len(skipped_no_duration),
        "total_duration_sec": round(total_sec, 2),
        "total_duration_hrs": round(total_sec / 3600.0, 2),
        "clips_needing_chunking": len(long_clips),
        "long_clip_total_hrs": round(long_sec / 3600.0, 2),
        "bucket_counts": dict(bucket_counts.most_common()),
        "clips": clips,
    }

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(manifest, indent=2))

    print(f"\nWrote {args.out}", file=sys.stderr)
    print(f"  total clips:          {len(clips)}", file=sys.stderr)
    print(f"  total duration:       {total_sec/3600:.2f} hr", file=sys.stderr)
    print(f"  needs chunking:       {len(long_clips)}  ({long_sec/3600:.2f} hr)", file=sys.stderr)
    print(f"  bucket distribution:  {dict(bucket_counts.most_common())}", file=sys.stderr)
    if skipped_no_proxy:
        print(f"  skipped (no proxy):   {len(skipped_no_proxy)}  e.g. {skipped_no_proxy[:3]}", file=sys.stderr)
    if skipped_no_catalog:
        print(f"  skipped (no catalog): {len(skipped_no_catalog)}  e.g. {skipped_no_catalog[:3]}", file=sys.stderr)
    if skipped_no_duration:
        print(f"  skipped (no ffprobe): {len(skipped_no_duration)}  e.g. {skipped_no_duration[:3]}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
