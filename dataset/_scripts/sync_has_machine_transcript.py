"""Set machine_transcript on video + audio from transcript file presence.

Idempotent. Does not run in rebuild_all — run after new transcripts land:
  python _scripts/sync_has_machine_transcript.py
"""
from __future__ import annotations

import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def atomic_write_json(path: Path, data: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def transcribed_asset_ids() -> set[str]:
    out: set[str] = set()
    d = ROOT / "assets/transcripts"
    for p in d.glob("*.json"):
        stem = p.stem
        if stem.endswith(".transcript"):
            out.add(stem[: -len(".transcript")])
        else:
            out.add(stem)
    return out


def main() -> None:
    ids = transcribed_asset_ids()
    print(f"transcript files -> {len(ids)} asset_ids")

    for d, label in (
        (ROOT / "assets/video", "video"),
        (ROOT / "assets/audio", "audio"),
    ):
        updated = skipped = 0
        true_after = 0
        for p in d.glob("*.json"):
            try:
                r = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                continue
            aid = r.get("asset_id")
            new_val = bool(aid and aid in ids)
            has_legacy = "has_machine_transcript" in r
            cur = r.get("machine_transcript")
            if cur is None and has_legacy:
                cur = r["has_machine_transcript"]
            unchanged = cur is not None and bool(cur) == new_val and not has_legacy
            if unchanged:
                skipped += 1
                if new_val:
                    true_after += 1
                continue
            r["machine_transcript"] = new_val
            r.pop("has_machine_transcript", None)
            atomic_write_json(p, r)
            updated += 1
            if new_val:
                true_after += 1
        print(f"{label}: updated={updated} skipped_unchanged={skipped} true_now={true_after}")


if __name__ == "__main__":
    main()
