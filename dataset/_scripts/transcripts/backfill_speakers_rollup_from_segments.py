#!/usr/bin/env python3
"""
Rebuild top-level `speakers[]` rollup from `segments` + `speakers_raw`.

Why:
Some transcripts have diarized `segments` (with `speaker_raw` GUIDs) and optional
`segments[].speaker` (`p_*`) but an empty or stale `speakers[]` array. Downstream
tools expect one row per diarization cluster with `segment_count`, durations,
and `p_id`.

Logic (per transcript):
- Union of GUIDs from `speakers_raw` keys and `segments[].speaker_raw`.
- Aggregate per GUID: segment_count, total_duration_sec, first_seen_sec from segments.
- `p_id`: duration-weighted majority over `segments[].speaker` values that look like
  `p_*`. If no segment has a `p_*` for that GUID, fall back to the existing
  `speakers[]` row's `p_id` for that GUID (preserve prior resolve when segments
  were not stamped).
- `label_raw` / `is_stub` from `speakers_raw[guid]` when present; otherwise
  `label_raw: null`, `is_stub: true`.

Writes atomically; idempotent for stable inputs.

Usage:
  python _scripts/transcripts/backfill_speakers_rollup_from_segments.py --dry-run
  python _scripts/transcripts/backfill_speakers_rollup_from_segments.py
  python _scripts/transcripts/backfill_speakers_rollup_from_segments.py --only-human-linked
  python _scripts/transcripts/backfill_speakers_rollup_from_segments.py --only-asset-ids-file ids.txt
"""
from __future__ import annotations

import argparse
import json
import os
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
TRANSCRIPTS_DIR = ROOT / "assets" / "catalog" / "transcripts"
CLIP_MANIFEST = ROOT / "assets" / "catalog" / "human_transcripts" / "clip_segments_manifest.jsonl"
AUDIT_DIR = ROOT / "_audit"


def now_run_id() -> str:
    return datetime.now(timezone.utc).strftime("speakers_rollup_backfill_%Y%m%dT%H%M%SZ")


def atomic_write_json(path: Path, data: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def append_jsonl(path: Path, rec: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _is_pid(s: Any) -> bool:
    return isinstance(s, str) and s.startswith("p_") and len(s) > 2


def load_asset_ids_file(path: Path) -> set[str]:
    out: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        out.add(line)
    return out


def load_human_linked_asset_ids() -> set[str]:
    if not CLIP_MANIFEST.exists():
        return set()
    out: set[str] = set()
    with CLIP_MANIFEST.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                o = json.loads(line)
            except json.JSONDecodeError:
                continue
            aid = o.get("asset_id")
            if isinstance(aid, str) and aid:
                out.add(aid)
    return out


def existing_p_id_by_guid(rec: dict) -> dict[str, str | None]:
    out: dict[str, str | None] = {}
    for row in rec.get("speakers") or []:
        sid = row.get("speaker_id")
        if isinstance(sid, str) and sid:
            out[sid] = row.get("p_id")
    return out


def build_speakers_rollup(rec: dict) -> list[dict[str, Any]]:
    speakers_raw = rec.get("speakers_raw") or {}
    if not isinstance(speakers_raw, dict):
        speakers_raw = {}

    segments = rec.get("segments") or []
    prev_pid = existing_p_id_by_guid(rec)

    dur_by_guid: dict[str, float] = defaultdict(float)
    count_by_guid: dict[str, int] = defaultdict(int)
    first_by_guid: dict[str, float] = {}
    pid_dur: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))

    for seg in segments:
        guid = seg.get("speaker_raw")
        if not guid:
            continue
        s0, s1 = seg.get("start_sec"), seg.get("end_sec")
        if s0 is None or s1 is None:
            continue
        dur = max(0.0, float(s1) - float(s0))
        dur_by_guid[guid] += dur
        count_by_guid[guid] += 1
        if guid not in first_by_guid:
            first_by_guid[guid] = float(s0)
        sp = seg.get("speaker")
        if _is_pid(sp):
            pid_dur[guid][sp] += dur

    all_guids = set(speakers_raw.keys()) | set(dur_by_guid.keys())

    rollup: list[dict[str, Any]] = []
    for guid in sorted(all_guids, key=lambda g: first_by_guid.get(g, 1e18)):
        raw = speakers_raw.get(guid)
        if isinstance(raw, dict):
            label_raw = raw.get("name")
            is_stub = raw.get("is_stub", True)
        else:
            label_raw = None
            is_stub = True

        pids = pid_dur.get(guid) or {}
        p_id: str | None
        if pids:
            p_id = max(pids.items(), key=lambda kv: (kv[1], kv[0]))[0]
        else:
            prev = prev_pid.get(guid)
            p_id = prev if isinstance(prev, str) and _is_pid(prev) else None

        rollup.append(
            {
                "speaker_id": guid,
                "p_id": p_id,
                "label_raw": label_raw,
                "is_stub": bool(is_stub),
                "segment_count": int(count_by_guid.get(guid, 0)),
                "total_duration_sec": round(float(dur_by_guid.get(guid, 0.0)), 3),
                "first_seen_sec": round(float(first_by_guid.get(guid, 0.0)), 3),
            }
        )

    return rollup


def rollups_equivalent(a: list[Any], b: list[Any]) -> bool:
    if len(a) != len(b):
        return False
    keys = ("speaker_id", "p_id", "label_raw", "is_stub", "segment_count", "total_duration_sec", "first_seen_sec")
    for x, y in zip(a, b):
        if not isinstance(x, dict) or not isinstance(y, dict):
            return False
        for k in keys:
            if x.get(k) != y.get(k):
                return False
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description="Backfill speakers[] rollup from segments + speakers_raw.")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=0, help="Process at most N transcript files (debug).")
    ap.add_argument("--only-human-linked", action="store_true", help="Only assets in clip_segments_manifest.jsonl.")
    ap.add_argument("--only-asset-ids-file", type=Path, default=None)
    args = ap.parse_args()

    run_id = now_run_id()
    audit_path = AUDIT_DIR / f"{run_id}.jsonl"

    restrict: set[str] | None = None
    if args.only_asset_ids_file:
        restrict = load_asset_ids_file(Path(args.only_asset_ids_file))
        if not restrict:
            print("No asset ids loaded from file.", file=__import__("sys").stderr)
            return 1
    elif args.only_human_linked:
        restrict = load_human_linked_asset_ids()

    files = sorted(TRANSCRIPTS_DIR.glob("*.transcript.json"))
    n_seen = n_changed = n_error = 0

    for p in files:
        if args.limit and n_seen >= args.limit:
            break
        aid = p.stem.replace(".transcript", "")
        if restrict is not None and aid not in restrict:
            continue
        n_seen += 1
        try:
            rec = json.loads(p.read_text(encoding="utf-8"))
        except Exception as e:
            n_error += 1
            append_jsonl(audit_path, {"run_id": run_id, "asset_id": aid, "ok": False, "error": str(e)})
            continue

        old = rec.get("speakers") or []
        new = build_speakers_rollup(rec)
        if rollups_equivalent(old, new):
            append_jsonl(
                audit_path,
                {"run_id": run_id, "asset_id": aid, "ok": True, "changed": False},
            )
            continue

        n_changed += 1
        rec["speakers"] = new
        diff = {
            "run_id": run_id,
            "asset_id": aid,
            "ok": True,
            "changed": True,
            "old_rows": len(old),
            "new_rows": len(new),
        }
        append_jsonl(audit_path, diff)

        if not args.dry_run:
            atomic_write_json(p, rec)

    print(
        json.dumps(
            {
                "run_id": run_id,
                "dry_run": bool(args.dry_run),
                "examined": n_seen,
                "updated": n_changed,
                "errors": n_error,
                "audit_log": str(audit_path.relative_to(ROOT)),
            },
            indent=2,
        )
    )
    return 0 if n_error == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
