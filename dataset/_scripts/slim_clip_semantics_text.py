"""Drop Gemini text payloads from clip_and_still_embeddings.sqlite (vectors only).

After `extract_semantic_summaries_to_catalog.py`, editorial semantics live in
catalog JSON. This script NULLs `response_json` / `response_raw`
on `semantic_chunks` and `semantic_stills`, keeping chunk registry rows for SigLIP
joins (`clip_embeddings.chunk_id`).

Usage:
  python _scripts/slim_clip_semantics_text.py           # dry-run
  python _scripts/slim_clip_semantics_text.py --apply   # mutate DB

Back up indexes/clip_and_still_embeddings.sqlite before --apply.
"""

from __future__ import annotations

import argparse
import sqlite3

from workspace_paths import clip_and_still_embeddings_sqlite_path

DB = clip_and_still_embeddings_sqlite_path()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="Write NULLs (default: dry-run).")
    args = ap.parse_args()

    if not DB.exists():
        print(f"ERROR: missing {DB}")
        return 1

    conn = sqlite3.connect(str(DB))
    cur = conn.cursor()
    n_chunks = cur.execute(
        "SELECT COUNT(*) FROM semantic_chunks WHERE response_json IS NOT NULL"
    ).fetchone()[0]
    n_stills = cur.execute(
        "SELECT COUNT(*) FROM semantic_stills WHERE response_json IS NOT NULL"
    ).fetchone()[0]
    emb = cur.execute("SELECT COUNT(*) FROM clip_embeddings").fetchone()[0]
    print(f"semantic_chunks with JSON: {n_chunks}")
    print(f"semantic_stills with JSON: {n_stills}")
    print(f"clip_embeddings (kept): {emb}")

    if not args.apply:
        print("Dry-run — pass --apply to NULL gemini text columns.")
        conn.close()
        return 0

    cur.execute(
        "UPDATE semantic_chunks SET response_json = NULL, response_raw = NULL "
        "WHERE response_json IS NOT NULL OR response_raw IS NOT NULL"
    )
    cur.execute(
        "UPDATE semantic_stills SET response_json = NULL, response_raw = NULL "
        "WHERE response_json IS NOT NULL OR response_raw IS NOT NULL"
    )
    conn.commit()
    conn.execute("VACUUM")
    conn.close()
    print("Done. Re-run sync_embeddings_flags_from_db.py if needed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
