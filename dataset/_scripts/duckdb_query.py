#!/usr/bin/env python3
"""Run a SQL query across attached index DBs via DuckDB (optional dependency).

Usage:
  python duckdb_query.py "SELECT COUNT(*) FROM cat.asset"
  python duckdb_query.py --file indexes/duckdb_unified.sql
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from workspace_paths import (
    clip_and_still_embeddings_sqlite_path,
    editorial_catalog_sqlite_path,
    indexes_dir,
    transcript_rolling_embeddings_sqlite_path,
)


def _attach_sql() -> str:
    cat = editorial_catalog_sqlite_path()
    emb = clip_and_still_embeddings_sqlite_path()
    tr = transcript_rolling_embeddings_sqlite_path()
    parts = [f"ATTACH '{cat}' AS cat (TYPE SQLITE, READ_ONLY);"]
    if emb.exists():
        parts.append(f"ATTACH '{emb}' AS emb (TYPE SQLITE, READ_ONLY);")
    if tr.exists():
        parts.append(f"ATTACH '{tr}' AS tr (TYPE SQLITE, READ_ONLY);")
    return "\n".join(parts)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("sql", nargs="?", help="SQL to execute (default: show attach + table list)")
    ap.add_argument("--file", type=Path, help="SQL file (e.g. indexes/duckdb_unified.sql)")
    args = ap.parse_args()

    try:
        import duckdb
    except ImportError:
        print("duckdb not installed. pip install duckdb", file=sys.stderr)
        print(f"Attach paths manually from {indexes_dir() / 'duckdb_unified.sql'}", file=sys.stderr)
        return 2

    con = duckdb.connect()
    con.execute(_attach_sql())

    if args.file:
        sql = args.file.read_text(encoding="utf-8")
    elif args.sql:
        sql = args.sql
    else:
        sql = """
        SELECT table_schema, table_name
          FROM information_schema.tables
         WHERE table_schema IN ('cat', 'emb', 'tr')
         ORDER BY 1, 2
         LIMIT 50
        """

    rows = con.execute(sql).fetchall()
    cols = [d[0] for d in con.description] if con.description else []
    if cols:
        print("\t".join(cols))
    for row in rows:
        print("\t".join(str(c) for c in row))
    return 0


if __name__ == "__main__":
    sys.exit(main())
