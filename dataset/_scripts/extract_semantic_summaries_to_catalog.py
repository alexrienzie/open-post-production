"""Copy Gemini semantics from clip_and_still_embeddings.sqlite into catalog JSON.

Writes `asset_semantic_summary` on matching **video** / **still** (and **audio**
when no sibling video row exists). Idempotent atomic writes.

Requires `response_json` still present in `clip_and_still_embeddings.sqlite`
(run **before** `slim_clip_semantics_text.py` on a fresh ingest).

After this script, rebuild indexes:
  python _scripts/build_editor_db.py

Usage:
  python _scripts/extract_semantic_summaries_to_catalog.py
  python _scripts/extract_semantic_summaries_to_catalog.py --dry-run
  python _scripts/extract_semantic_summaries_to_catalog.py --limit 20
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
from collections import defaultdict
from pathlib import Path

from semantic_catalog import (
    build_asset_semantic_summary,
    chunk_from_gemini_response,
    summaries_equal,
)
from workspace_paths import clip_and_still_embeddings_sqlite_path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = clip_and_still_embeddings_sqlite_path()
VIDEO_DIR = ROOT / "assets/video"
AUDIO_DIR = ROOT / "assets/audio"
STILL_DIR = ROOT / "assets/stills"


def atomic_write_json(path: Path, data: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def catalog_path_for_asset(asset_id: str) -> Path | None:
    vp = VIDEO_DIR / f"{asset_id}.video.json"
    if vp.exists():
        return vp
    ap = AUDIO_DIR / f"{asset_id}.audio.json"
    if ap.exists():
        return ap
    sp = STILL_DIR / f"{asset_id}.still.json"
    if sp.exists():
        return sp
    return None


def load_video_chunk_groups(conn: sqlite3.Connection) -> dict[str, list[dict]]:
    cur = conn.cursor()
    rows = cur.execute(
        """
        SELECT parent_asset_id, chunk_id, chunk_idx,
               chunk_start_sec, chunk_end_sec, model, response_json
          FROM semantic_chunks
         WHERE label_status = 'done' AND response_json IS NOT NULL
         ORDER BY parent_asset_id, chunk_idx ASC
        """
    ).fetchall()
    groups: dict[str, list[dict]] = defaultdict(list)
    for parent_id, chunk_id, chunk_idx, start_sec, end_sec, model, resp in rows:
        ch = chunk_from_gemini_response(
            chunk_id=chunk_id,
            chunk_idx=int(chunk_idx),
            start_sec=float(start_sec) if start_sec is not None else None,
            end_sec=float(end_sec) if end_sec is not None else None,
            model=model,
            response_json=resp,
        )
        if ch:
            groups[parent_id].append(ch)
    return groups


def load_still_chunks(conn: sqlite3.Connection) -> dict[str, list[dict]]:
    cur = conn.cursor()
    rows = cur.execute(
        """
        SELECT asset_id, asset_id, 0, NULL, NULL, model, response_json
          FROM semantic_stills
         WHERE label_status = 'done' AND response_json IS NOT NULL
        """
    ).fetchall()
    out: dict[str, list[dict]] = {}
    for asset_id, chunk_id, chunk_idx, start_sec, end_sec, model, resp in rows:
        ch = chunk_from_gemini_response(
            chunk_id=chunk_id,
            chunk_idx=0,
            start_sec=None,
            end_sec=None,
            model=model,
            response_json=resp,
        )
        if ch:
            out[asset_id] = [ch]
    return out


def apply_summary(
    path: Path,
    summary: dict,
    *,
    dry_run: bool,
) -> str:
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return "read_error"
    prev = record.get("asset_semantic_summary")
    if summaries_equal(prev, summary):
        return "unchanged"
    if dry_run:
        return "would_update"
    record["asset_semantic_summary"] = summary
    atomic_write_json(path, record)
    return "updated"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="Report changes without writing.")
    ap.add_argument("--limit", type=int, default=0, help="Max assets to write (0 = all).")
    args = ap.parse_args()

    if not DB_PATH.exists():
        print(f"ERROR: missing {DB_PATH}")
        return 1

    conn = sqlite3.connect(str(DB_PATH))
    try:
        video_groups = load_video_chunk_groups(conn)
        still_groups = load_still_chunks(conn)
    finally:
        # WAL/shm clean close (avoid bindfs .fuse_hidden* on indexes/).
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception:
            pass
        conn.close()
    counts: dict[str, int] = defaultdict(int)
    n_written = 0

    all_groups: list[tuple[str, list[dict]]] = list(video_groups.items()) + [
        (k, v) for k, v in still_groups.items() if k not in video_groups
    ]

    for asset_id, chunks in all_groups:
        if args.limit and n_written >= args.limit:
            break
        path = catalog_path_for_asset(asset_id)
        if path is None:
            counts["no_catalog"] += 1
            continue
        summary = build_asset_semantic_summary(chunks)
        if not summary:
            counts["empty"] += 1
            continue
        status = apply_summary(path, summary, dry_run=args.dry_run)
        counts[status] += 1
        if status in ("updated", "would_update"):
            n_written += 1

    print(
        f"video+still groups: video={len(video_groups)} still_only="
        f"{len([k for k in still_groups if k not in video_groups])}"
    )
    for k in sorted(counts):
        print(f"  {k}: {counts[k]}")
    if args.dry_run:
        print("(dry-run — no files written)")
    else:
        print("Next: python _scripts/build_editor_db.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
