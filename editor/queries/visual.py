"""Visual similarity queries over the per-chunk SigLIP mean-vector store.

Two entry points:

- `find_visually_similar(chunk_id=... | asset_id=...)` — seed from an existing
  chunk or asset; multi-chunk assets are averaged across all their chunks.
- `find_visually_similar_by_text(text=...)` — encode a natural-language query
  with the SigLIP text tower and rank candidate chunks.

Both accept the same allowlist filter args (bucket / asset_type /
shoot_date_range / place_id / person_ids / exclude_assets) to restrict the
candidate set before scoring. Filtering happens *before* the dot product, so
the cost scales with the filtered subset, not the full 5,600-chunk store.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Iterable, Optional

import numpy as np

from ._paths import clip_and_still_embeddings_sqlite_path, editorial_catalog_sqlite_path
from .encoder import SigLIPEncoder
from .filters import asset_allowlist
from .store import ChunkMeanStore, cosine_topk, load_chunk_mean_store


def _chunk_to_asset_map(embed_db: Path):
    """Return (chunk_id -> asset_id, asset_id -> [chunk_id...]) from semantic_chunks."""
    con = sqlite3.connect(str(embed_db))
    rows = con.execute(
        "SELECT chunk_id, parent_asset_id FROM semantic_chunks"
    ).fetchall()
    con.close()
    c2a = {}
    a2c = {}
    for cid, aid in rows:
        c2a[cid] = aid
        a2c.setdefault(aid, []).append(cid)
    return c2a, a2c



def _load_editor_notes(asset_ids):
    """Return {asset_id: {'editor_notes': [...], 'editor_tags': [...]}} from
    dataset/assets/editor_notes/{asset_id}_editor_notes.json.

    Missing files are silently skipped (returned dict has no entry for that
    asset_id). Malformed files raise — better to know than to silently lose
    editorial signal.
    """
    import json
    from ._paths import repo_root

    out = {}
    notes_dir = repo_root() / "dataset" / "assets" / "editor_notes"
    if not notes_dir.exists():
        return out
    for aid in asset_ids:
        p = notes_dir / f"{aid}_editor_notes.json"
        if not p.exists():
            continue
        body = json.loads(p.read_text(encoding="utf-8"))
        out[aid] = {
            "editor_notes": body.get("notes") or [],
            "editor_tags": body.get("tags") or [],
        }
    return out


def _enrich_results(
    *,
    embed_db: Path,
    catalog_db: Path,
    ranked_chunk_ids,
    scores,
):
    """One-shot enrichment of (chunk_id, score) -> full result dicts.

    Batches chunk and asset lookups into one query each instead of N+1.
    """
    if not ranked_chunk_ids:
        return []
    cph = ",".join("?" * len(ranked_chunk_ids))
    con = sqlite3.connect(str(embed_db))
    con.row_factory = sqlite3.Row
    chunk_rows = {
        r["chunk_id"]: dict(r)
        for r in con.execute(
            "SELECT chunk_id, parent_asset_id, chunk_start_sec, chunk_end_sec "
            "FROM semantic_chunks WHERE chunk_id IN (" + cph + ")",
            ranked_chunk_ids,
        ).fetchall()
    }
    con.close()
    asset_ids = sorted(
        {r["parent_asset_id"] for r in chunk_rows.values() if r.get("parent_asset_id")}
    )
    asset_rows = {}
    if asset_ids:
        aph = ",".join("?" * len(asset_ids))
        cat = sqlite3.connect(str(catalog_db))
        cat.row_factory = sqlite3.Row
        asset_rows = {
            r["asset_id"]: dict(r)
            for r in cat.execute(
                "SELECT asset_id, filename, semantic_subject, semantic_location, "
                "shoot_date, source_path, duration_sec "
                "FROM asset WHERE asset_id IN (" + aph + ")",
                asset_ids,
            ).fetchall()
        }
        cat.close()
    # Load per-asset editor_notes so the LLM editor sees human-flagged
    # findings (wobble warnings, "use src 25-31s," tags like 'avoid') alongside
    # the SigLIP score. Missing files are silently skipped.
    notes_by_asset = _load_editor_notes(asset_ids)

    out = []
    for cid, sc in zip(ranked_chunk_ids, scores):
        row = {"chunk_id": cid, "score": float(sc)}
        cr = chunk_rows.get(cid)
        if cr:
            row["asset_id"] = cr["parent_asset_id"]
            row["start_sec"] = cr["chunk_start_sec"]
            row["end_sec"] = cr["chunk_end_sec"]
            ar = asset_rows.get(cr["parent_asset_id"])
            if ar:
                row["filename"] = ar["filename"]
                row["semantic_subject"] = ar["semantic_subject"]
                row["semantic_location"] = ar["semantic_location"]
                row["shoot_date"] = ar["shoot_date"]
            ed = notes_by_asset.get(cr["parent_asset_id"])
            if ed:
                row["editor_notes"] = ed["editor_notes"]
                row["editor_tags"] = ed["editor_tags"]
        out.append(row)
    return out


def _candidate_indices(
    store: ChunkMeanStore,
    *,
    chunk_to_asset,
    asset_to_chunks,
    catalog_db: Optional[Path],
    allowlist_kwargs: dict,
    exclude_chunk_ids: Iterable[str] = (),
):
    """Build row-index restriction for the store, or None if unrestricted."""
    excluded = set(exclude_chunk_ids)
    needs_allowlist = any(
        v for k, v in allowlist_kwargs.items() if k != "record_kind"
    )
    allowed_assets = None
    if needs_allowlist:
        allowed_assets = asset_allowlist(catalog_db=catalog_db, **allowlist_kwargs)
        if not allowed_assets:
            return np.zeros((0,), dtype=np.int64)

    if allowed_assets is None and not excluded:
        return None

    keep = []
    for i, cid in enumerate(store.chunk_ids):
        if cid in excluded:
            continue
        if allowed_assets is not None:
            aid = chunk_to_asset.get(cid)
            if aid is None or aid not in allowed_assets:
                continue
        keep.append(i)
    return np.asarray(keep, dtype=np.int64)


def find_visually_similar(
    *,
    chunk_id: Optional[str] = None,
    asset_id: Optional[str] = None,
    top_k: int = 20,
    embed_db: Optional[Path] = None,
    catalog_db: Optional[Path] = None,
    store: Optional[ChunkMeanStore] = None,
    bucket: Optional[str] = None,
    asset_type: Optional[str] = None,
    shoot_date_from: Optional[str] = None,
    shoot_date_to: Optional[str] = None,
    place_id: Optional[str] = None,
    person_ids: Optional[Iterable[str]] = None,
    exclude_assets: Optional[Iterable[str]] = None,
    camera_movement: Optional[str] = None,
    shot_size: Optional[str] = None,
):
    """Cosine similarity over per-chunk SigLIP mean vectors.

    Seeded by either chunk_id (single chunk) or asset_id (averages across all
    of that asset's chunks). Filter kwargs restrict the candidate pool before
    scoring.
    """
    if not chunk_id and not asset_id:
        raise ValueError("Provide either chunk_id or asset_id")

    embed_db = embed_db or clip_and_still_embeddings_sqlite_path()
    catalog_db = catalog_db or editorial_catalog_sqlite_path()
    store = store or load_chunk_mean_store(db_path=embed_db)
    chunk_to_asset, asset_to_chunks = _chunk_to_asset_map(embed_db)

    if chunk_id:
        idx = store.index_of(chunk_id)
        if idx is None:
            return []
        query_vec = store.vectors[idx].astype(np.float32)
        seed_chunk_ids = [chunk_id]
    else:
        chunks_for_asset = asset_to_chunks.get(asset_id or "", [])
        if not chunks_for_asset:
            return []
        idxs = [
            store.index_of(c)
            for c in chunks_for_asset
            if store.index_of(c) is not None
        ]
        if not idxs:
            return []
        query_vec = np.mean(
            np.asarray([store.vectors[i] for i in idxs], dtype=np.float32),
            axis=0,
        )
        seed_chunk_ids = list(chunks_for_asset)

    allowlist_kwargs = dict(
        bucket=bucket,
        asset_type=asset_type,
        record_kind="video",
        shoot_date_from=shoot_date_from,
        shoot_date_to=shoot_date_to,
        place_id=place_id,
        person_ids=list(person_ids) if person_ids else None,
        exclude_assets=list(exclude_assets) if exclude_assets else None,
        camera_movement=camera_movement,
        shot_size=shot_size,
    )
    cand = _candidate_indices(
        store,
        chunk_to_asset=chunk_to_asset,
        asset_to_chunks=asset_to_chunks,
        catalog_db=catalog_db,
        allowlist_kwargs=allowlist_kwargs,
        exclude_chunk_ids=seed_chunk_ids,
    )
    if cand is not None and cand.size == 0:
        return []

    # Drop the seed asset from candidates when seeded by asset_id, to avoid
    # trivial self-matches across other chunks of the same asset.
    if asset_id and cand is not None:
        keep_mask = np.array(
            [chunk_to_asset.get(store.chunk_ids[i]) != asset_id for i in cand],
            dtype=bool,
        )
        cand = cand[keep_mask]
    elif asset_id and cand is None:
        cand = np.array(
            [
                i
                for i, cid in enumerate(store.chunk_ids)
                if chunk_to_asset.get(cid) != asset_id
            ],
            dtype=np.int64,
        )

    ranked_idx, scores = cosine_topk(query_vec, store.vectors, top_k, candidate_idx=cand)
    if ranked_idx.size == 0:
        return []
    ranked_chunk_ids = [str(store.chunk_ids[int(i)]) for i in ranked_idx]
    return _enrich_results(
        embed_db=embed_db,
        catalog_db=catalog_db,
        ranked_chunk_ids=ranked_chunk_ids,
        scores=scores.tolist(),
    )


def find_visually_similar_by_text(
    text: str,
    *,
    top_k: int = 20,
    embed_db: Optional[Path] = None,
    catalog_db: Optional[Path] = None,
    store: Optional[ChunkMeanStore] = None,
    encoder: Optional[SigLIPEncoder] = None,
    bucket: Optional[str] = None,
    asset_type: Optional[str] = None,
    shoot_date_from: Optional[str] = None,
    shoot_date_to: Optional[str] = None,
    place_id: Optional[str] = None,
    person_ids: Optional[Iterable[str]] = None,
    exclude_assets: Optional[Iterable[str]] = None,
    camera_movement: Optional[str] = None,
    shot_size: Optional[str] = None,
):
    """Text -> top-K SigLIP-similar chunks.

    Encodes `text` with the SigLIP text tower and scores against per-chunk
    means. Filter kwargs are forwarded to asset_allowlist.
    """
    embed_db = embed_db or clip_and_still_embeddings_sqlite_path()
    catalog_db = catalog_db or editorial_catalog_sqlite_path()
    store = store or load_chunk_mean_store(db_path=embed_db)
    encoder = encoder or SigLIPEncoder()
    chunk_to_asset, asset_to_chunks = _chunk_to_asset_map(embed_db)

    q = encoder.encode_text(text).astype(np.float32)
    allowlist_kwargs = dict(
        bucket=bucket,
        asset_type=asset_type,
        record_kind="video",
        shoot_date_from=shoot_date_from,
        shoot_date_to=shoot_date_to,
        place_id=place_id,
        person_ids=list(person_ids) if person_ids else None,
        exclude_assets=list(exclude_assets) if exclude_assets else None,
        camera_movement=camera_movement,
        shot_size=shot_size,
    )
    cand = _candidate_indices(
        store,
        chunk_to_asset=chunk_to_asset,
        asset_to_chunks=asset_to_chunks,
        catalog_db=catalog_db,
        allowlist_kwargs=allowlist_kwargs,
    )
    if cand is not None and cand.size == 0:
        return []
    ranked_idx, scores = cosine_topk(q, store.vectors, top_k, candidate_idx=cand)
    if ranked_idx.size == 0:
        return []
    ranked_chunk_ids = [str(store.chunk_ids[int(i)]) for i in ranked_idx]
    return _enrich_results(
        embed_db=embed_db,
        catalog_db=catalog_db,
        ranked_chunk_ids=ranked_chunk_ids,
        scores=scores.tolist(),
    )
