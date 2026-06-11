"""Transcript-side queries: FTS5 keyword + MiniLM rolling-window similarity."""

from __future__ import annotations

import sqlite3
import struct
from pathlib import Path
from typing import Optional

from ._paths import editorial_catalog_sqlite_path, transcript_rolling_embeddings_sqlite_path


def _blob_to_f32(blob: bytes, dim: int):
    import numpy as np

    n = len(blob) // 4
    if dim and n != dim:
        return None
    return np.array(struct.unpack(f"{n}f", blob), dtype=np.float32)


def search_transcript_fts(
    query: str,
    *,
    limit: int = 40,
    catalog_db: Optional[Path] = None,
) -> list[dict]:
    """FTS5 keyword search on `segment_fts`.

    Requires the FTS5 virtual table built by `apply_sqlite_perf_indexes.py`
    (or the equivalent rebuild path). Returns empty list if absent.
    """
    db = catalog_db or editorial_catalog_sqlite_path()
    con = sqlite3.connect(str(db))
    con.row_factory = sqlite3.Row
    try:
        con.execute("SELECT 1 FROM segment_fts LIMIT 1")
    except sqlite3.OperationalError:
        con.close()
        return []
    # Quote tokens for FTS5 phrase safety, after stripping embedded quotes.
    tokens = [w.replace('"', "") for w in query.split()]
    q = " ".join(f'"{w}"' for w in tokens if w.strip())
    if not q:
        con.close()
        return []
    rows = con.execute(
        """
        SELECT s.asset_id, s.seg_idx, s.start_sec, s.end_sec, s.text,
               bm25(segment_fts) AS rank
          FROM segment_fts f
          JOIN segment s ON s.asset_id = f.asset_id AND s.seg_idx = f.seg_idx
         WHERE segment_fts MATCH ?
         ORDER BY rank
         LIMIT ?
        """,
        (q, limit),
    ).fetchall()
    con.close()
    return [dict(r) for r in rows]


def find_similar_transcript_windows(
    text: str,
    *,
    top_k: int = 25,
    embed_db: Optional[Path] = None,
) -> list[dict]:
    """Embed query text with the latest rolling-run model; rank windows by cosine."""
    import numpy as np

    embed_db = embed_db or transcript_rolling_embeddings_sqlite_path()
    con = sqlite3.connect(str(embed_db))
    run = con.execute(
        "SELECT run_id, model_name FROM embedding_run ORDER BY run_id DESC LIMIT 1"
    ).fetchone()
    if not run:
        con.close()
        return []
    run_id, model_name = run
    rows = con.execute(
        """
        SELECT asset_id, window_anchor_ms, window_start_sec, window_end_sec,
               text_preview, embedding_dim, vector_blob
          FROM transcript_window_embedding
         WHERE run_id = ?
        """,
        (run_id,),
    ).fetchall()
    con.close()
    if not rows:
        return []

    from sentence_transformers import SentenceTransformer

    model = SentenceTransformer(model_name)
    q = np.asarray(
        model.encode([text], normalize_embeddings=True)[0], dtype=np.float32
    )

    meta: list[dict] = []
    mat: list[np.ndarray] = []
    for asset_id, anchor_ms, ws, we, preview, dim, blob in rows:
        v = _blob_to_f32(blob, dim)
        if v is None:
            continue
        n = float(np.linalg.norm(v))
        if n < 1e-12:
            continue
        mat.append(v / n)
        meta.append(
            {
                "asset_id": asset_id,
                "window_anchor_ms": anchor_ms,
                "window_start_sec": ws,
                "window_end_sec": we,
                "text_preview": preview,
            }
        )
    if not mat:
        return []
    M = np.stack(mat, axis=0)
    scores = M @ q  # both unit, dot == cosine
    k_eff = min(top_k, scores.shape[0])
    part = np.argpartition(-scores, k_eff - 1)[:k_eff]
    order = part[np.argsort(-scores[part])]
    out: list[dict] = []
    for i in order:
        m = dict(meta[int(i)])
        m["score"] = float(scores[int(i)])
        out.append(m)
    return out
