"""Reconcile catalog `embeddings` flags with `clip_and_still_embeddings.sqlite`.

Replaces the legacy `{ text_uri, image_uri, model }` shape with reconciliation flags:

- Catalog **video / audio / still** rows: `{ "semantic": bool, "vector": bool }`

- Video/audio: Gemini chunk row (status=done) ⇒ `semantic`; ≥1 SigLIP row for any chunk of that parent ⇒ `vector`.
- Stills: `semantic_stills` / `still_embeddings` on `asset_id`.

Transcript JSON does **not** carry `embeddings` (use the sibling video/audio/still row for the same `asset_id`).

Requires `clip_and_still_embeddings.sqlite` under the workspace `indexes/` folder. Idempotent atomic writes.

Usage:
  python _scripts/sync_embeddings_flags_from_db.py
"""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

from workspace_paths import clip_and_still_embeddings_sqlite_path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = clip_and_still_embeddings_sqlite_path()


def atomic_write_json(path: Path, data: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def load_sets(conn: sqlite3.Connection) -> tuple[set[str], set[str], set[str], set[str]]:
    cur = conn.cursor()
    sem_v = {
        r[0]
        for r in cur.execute(
            "SELECT DISTINCT parent_asset_id FROM semantic_chunks WHERE label_status = 'done'"
        )
    }
    vec_v = {
        r[0]
        for r in cur.execute(
            """
            SELECT DISTINCT g.parent_asset_id
              FROM semantic_chunks g
              INNER JOIN clip_embeddings c ON c.chunk_id = g.chunk_id
            """
        )
    }
    sem_s = {
        r[0]
        for r in cur.execute(
            "SELECT DISTINCT asset_id FROM semantic_stills WHERE label_status = 'done'"
        )
    }
    vec_s = {r[0] for r in cur.execute("SELECT DISTINCT asset_id FROM still_embeddings")}
    return sem_v, vec_v, sem_s, vec_s


def norm_embeddings(r: dict, hs: bool, hv: bool) -> bool:
    """Return True if record changed."""
    emb = r.get("embeddings")
    new_block = {"semantic": hs, "vector": hv}
    if emb == new_block:
        return False
    r["embeddings"] = new_block
    return True


def sweep_dir(
    d: Path,
    *,
    label: str,
    id_key: str,
    semantic: set[str],
    vector: set[str],
) -> tuple[int, int]:
    updated = skipped = 0
    for p in d.glob("*.json"):
        try:
            r = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        aid = r.get(id_key)
        if not aid:
            continue
        hs = aid in semantic
        hv = aid in vector
        if norm_embeddings(r, hs, hv):
            atomic_write_json(p, r)
            updated += 1
        else:
            skipped += 1
    print(f"{label}: updated={updated} unchanged={skipped}")
    return updated, skipped


def main() -> int:
    if not DB_PATH.exists():
        print(f"ERROR: missing {DB_PATH.relative_to(ROOT)} — cannot reconcile flags.")
        return 1

    conn = sqlite3.connect(str(DB_PATH))
    try:
        sem_v, vec_v, sem_s, vec_s = load_sets(conn)
    finally:
        # WAL/shm clean close (avoid bindfs .fuse_hidden* on indexes/).
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception:
            pass
        conn.close()
    print(
        f"DB: semantic_video={len(sem_v)} vector_video={len(vec_v)} "
        f"semantic_still={len(sem_s)} vector_still={len(vec_s)}"
    )

    sweep_dir(
        ROOT / "assets/video",
        label="video",
        id_key="asset_id",
        semantic=sem_v,
        vector=vec_v,
    )
    sweep_dir(
        ROOT / "assets/audio",
        label="audio",
        id_key="asset_id",
        semantic=sem_v,
        vector=vec_v,
    )
    sweep_dir(
        ROOT / "assets/stills",
        label="still",
        id_key="asset_id",
        semantic=sem_s,
        vector=vec_s,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
