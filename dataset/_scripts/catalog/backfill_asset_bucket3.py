"""
Backfill asset tags on catalog assets (video/audio/stills):

- `asset_classifications` (video + audio + stills):
    { "bucket": third_party | in_house_priority_ht | in_house_other,
      "type": b_roll | interview | timelapse | archival | third_party | verite | court_recordings }
  Replaces legacy `asset_bucket` on video/audio (key removed). Stills keep `asset_bucket` and also get `asset_classifications` for the same bucket + path-derived type.

- `asset_bucket` (stills only) — same three values as before (single string); kept alongside `asset_classifications`.

- `human_transcript` (video + audio only) — true iff asset_id appears in index.jsonl
  (canonical HTR roster match; supersedes legacy ingest-only human_transcript_pdf)

Removes legacy keys: asset_bucket3, human_transcript_pdf, has_transcript (AV only).

Idempotent; atomic writes.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from _lib.asset_classifications import (  # noqa: E402
    build_asset_classifications,
    classify_asset_bucket,
    load_human_transcript_asset_ids,
)

ASSET_DIRS: dict[str, Path] = {
    "video": ROOT / "assets/video",
    "audio": ROOT / "assets/audio",
    "still": ROOT / "assets/stills",
}

FIELD_ASSET_CLASSIFICATIONS = "asset_classifications"
FIELD_BUCKET = "asset_bucket"
FIELD_HUMAN = "human_transcript"
LEGACY_BUCKET = "asset_bucket3"
LEGACY_HUMAN = "human_transcript_pdf"
LEGACY_HAS_TRANSCRIPT = "has_transcript"

AV_KINDS = frozenset({"video", "audio"})


def atomic_write_json(path: Path, obj: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    human_ids = load_human_transcript_asset_ids(ROOT)
    print(f"human_roster_unique_asset_ids={len(human_ids)}")

    changed = 0
    total = 0
    by_value: dict[str, int] = {"third_party": 0, "in_house_priority_ht": 0, "in_house_other": 0}
    missing_source_path = 0
    ht_true_video = 0
    ht_true_audio = 0
    consistency_bad = 0
    third_party_with_ht = 0

    for kind, d in ASSET_DIRS.items():
        if not d.exists():
            continue
        for p in d.glob(f"*.{kind}.json"):
            total += 1
            obj = json.loads(p.read_text(encoding="utf-8"))
            asset_id = obj.get("asset_id") or ""
            source_path = obj.get("source_path") or ""
            if not source_path:
                missing_source_path += 1

            value = classify_asset_bucket(str(asset_id), str(source_path), human_ids)
            by_value[value] = by_value.get(value, 0) + 1

            dirty = False
            if LEGACY_BUCKET in obj:
                del obj[LEGACY_BUCKET]
                dirty = True

            if kind in AV_KINDS:
                ac = build_asset_classifications(str(asset_id), str(source_path), human_ids)
                if obj.get(FIELD_ASSET_CLASSIFICATIONS) != ac:
                    obj[FIELD_ASSET_CLASSIFICATIONS] = ac
                    dirty = True
                if FIELD_BUCKET in obj:
                    del obj[FIELD_BUCKET]
                    dirty = True
            else:
                ac = build_asset_classifications(str(asset_id), str(source_path), human_ids)
                if obj.get(FIELD_ASSET_CLASSIFICATIONS) != ac:
                    obj[FIELD_ASSET_CLASSIFICATIONS] = ac
                    dirty = True
                if obj.get(FIELD_BUCKET) != value:
                    obj[FIELD_BUCKET] = value
                    dirty = True

            if kind in AV_KINDS:
                ht = str(asset_id) in human_ids
                if ht and kind == "video":
                    ht_true_video += 1
                if ht and kind == "audio":
                    ht_true_audio += 1

                if LEGACY_HUMAN in obj:
                    del obj[LEGACY_HUMAN]
                    dirty = True
                if LEGACY_HAS_TRANSCRIPT in obj:
                    del obj[LEGACY_HAS_TRANSCRIPT]
                    dirty = True
                if obj.get(FIELD_HUMAN) != ht:
                    obj[FIELD_HUMAN] = ht
                    dirty = True

                if value == "third_party" and ht:
                    third_party_with_ht += 1
                if (value == "in_house_priority_ht" and not ht) or (value == "in_house_other" and ht):
                    consistency_bad += 1

            if dirty:
                changed += 1
                if not args.dry_run:
                    atomic_write_json(p, obj)

    print(f"assets_total={total}")
    print(f"assets_changed={changed}")
    print(f"missing_source_path={missing_source_path}")
    print(f"human_transcript_true_video={ht_true_video}")
    print(f"human_transcript_true_audio={ht_true_audio}")
    print(f"av_third_party_with_human_transcript={third_party_with_ht}")
    print(f"av_bucket_human_tag_mismatches={consistency_bad}")
    for k in ("third_party", "in_house_priority_ht", "in_house_other"):
        print(f"{k}={by_value.get(k, 0)}")


if __name__ == "__main__":
    main()
