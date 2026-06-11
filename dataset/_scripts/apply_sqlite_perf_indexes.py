#!/usr/bin/env python3
"""Apply hot-path indexes + segment FTS to existing SQLite sidecars (no full rebuild).

Usage:
  python apply_sqlite_perf_indexes.py
  python apply_sqlite_perf_indexes.py --catalog-only
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_editor_db import rebuild_segment_fts
from workspace_paths import (
    clip_and_still_embeddings_sqlite_path,
    editorial_catalog_sqlite_path,
    transcript_rolling_embeddings_sqlite_path,
)

CATALOG_DDL = """
CREATE INDEX IF NOT EXISTS idx_segment_time ON segment(asset_id, start_sec, end_sec);
CREATE INDEX IF NOT EXISTS idx_asset_type ON asset(asset_type);
CREATE INDEX IF NOT EXISTS idx_asset_bucket ON asset(bucket);

DROP TABLE IF EXISTS segment_fts;
CREATE VIRTUAL TABLE segment_fts USING fts5(
    text,
    asset_id UNINDEXED,
    seg_idx UNINDEXED,
    tokenize='porter unicode61'
);
"""

EMBED_DDL = """
CREATE INDEX IF NOT EXISTS idx_gemini_parent ON semantic_chunks(parent_asset_id);
CREATE INDEX IF NOT EXISTS idx_label_status ON semantic_chunks(label_status)
    WHERE label_status IS NOT NULL;
"""

TRANSCRIPT_DDL = """
CREATE INDEX IF NOT EXISTS idx_twe_asset ON transcript_window_embedding(asset_id);
CREATE INDEX IF NOT EXISTS idx_twe_run ON transcript_window_embedding(run_id, asset_id);
"""


def _apply(con: sqlite3.Connection, label: str, ddl: str, *, post=None) -> None:
    print(f"=== {label} ===")
    con.executescript(ddl)
    if post:
        n = post(con)
        print(f"  segment_fts rows: {n}")
    con.commit()
    print("  done")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--catalog-only", action="store_true")
    ap.add_argument("--embeddings-only", action="store_true")
    ap.add_argument("--transcript-only", action="store_true")
    args = ap.parse_args()
    all_three = not (args.catalog_only or args.embeddings_only or args.transcript_only)

    if all_three or args.catalog_only:
        cat = editorial_catalog_sqlite_path()
        if not cat.exists():
            print(f"MISSING: {cat}", file=sys.stderr)
            return 2
        con = sqlite3.connect(str(cat))
        _apply(con, cat.name, CATALOG_DDL, post=rebuild_segment_fts)
        # WAL/shm clean close (avoid bindfs .fuse_hidden* on indexes/).
        try:
            con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception:
            pass
        con.close()
    if all_three or args.embeddings_only:
        emb = clip_and_still_embeddings_sqlite_path()
        if emb.exists():
            con = sqlite3.connect(str(emb))
            _apply(con, emb.name, EMBED_DDL)
            # WAL/shm clean close (avoid bindfs .fuse_hidden* on indexes/).
            try:
                con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except Exception:
                pass
            con.close()
        else:
            print(f"SKIP (missing): {emb}")

    if all_three or args.transcript_only:
        tr = transcript_rolling_embeddings_sqlite_path()
        if tr.exists():
            con = sqlite3.connect(str(tr))
            _apply(con, tr.name, TRANSCRIPT_DDL)
            # WAL/shm clean close (avoid bindfs .fuse_hidden* on indexes/).
            try:
                con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except Exception:
                pass
            con.close()
        else:
            print(f"SKIP (missing): {tr}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
