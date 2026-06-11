"""Per-chunk mean SigLIP vector cache.

Builds `indexes/_cache/clip_chunk_means.npy` (vectors) + `clip_chunk_ids.npy`
(parallel chunk_id array) by reading every per-frame vector from
`clip_and_still_embeddings.sqlite` and mean-pooling per `chunk_id`.

Cache is invalidated when the embeddings DB mtime is newer than the .npy files.
Subsequent loads mmap the .npy and finish in <100ms.

Vectors are stored as float32 and are NOT L2-normalized (mean of unit vectors
is not unit). Cosine queries normalize on the hot path.
"""

from __future__ import annotations

import sqlite3
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np

from ._paths import cache_dir, clip_and_still_embeddings_sqlite_path

_VECTORS_FILE = "clip_chunk_means.npy"
_IDS_FILE = "clip_chunk_ids.npy"


@dataclass(frozen=True)
class ChunkMeanStore:
    """In-memory view of the per-chunk mean SigLIP vectors."""

    vectors: np.ndarray  # (N, dim) float32, NOT L2-normalized
    chunk_ids: np.ndarray  # (N,) object dtype
    dim: int

    @property
    def n(self) -> int:
        return int(self.vectors.shape[0])

    def index_of(self, chunk_id: str) -> Optional[int]:
        m = np.where(self.chunk_ids == chunk_id)[0]
        return int(m[0]) if m.size else None

    def restrict_by_asset(
        self, asset_to_chunks: dict, asset_allowlist: set
    ) -> np.ndarray:
        """Return row indices for chunks whose parent asset is in allowlist."""
        allowed_chunks: set = set()
        for aid in asset_allowlist:
            for cid in asset_to_chunks.get(aid, ()):
                allowed_chunks.add(cid)
        if not allowed_chunks:
            return np.zeros((0,), dtype=np.int64)
        mask = np.array([cid in allowed_chunks for cid in self.chunk_ids], dtype=bool)
        return np.nonzero(mask)[0]


def _blob_to_vec(blob: bytes, dim: int):
    n = len(blob) // 4
    if dim and n != dim:
        return None
    return np.array(struct.unpack(f"{n}f", blob), dtype=np.float32)


def _is_cache_fresh(db_path: Path, vec_path: Path, ids_path: Path) -> bool:
    if not (vec_path.exists() and ids_path.exists()):
        return False
    db_mtime = db_path.stat().st_mtime
    return vec_path.stat().st_mtime >= db_mtime and ids_path.stat().st_mtime >= db_mtime


def build_chunk_mean_store(
    db_path: Optional[Path] = None,
    out_dir: Optional[Path] = None,
    *,
    verbose: bool = False,
) -> ChunkMeanStore:
    """Read every per-frame vector, mean-pool per chunk, persist to .npy files."""
    db = db_path or clip_and_still_embeddings_sqlite_path()
    out = out_dir or cache_dir()
    out.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(db))
    rows = con.execute(
        "SELECT chunk_id, vector_dim, vector_blob FROM clip_embeddings "
        "WHERE vector_blob IS NOT NULL"
    ).fetchall()
    con.close()
    if verbose:
        print(f"[store] decoded {len(rows):,} per-frame vectors from {db.name}")
    by_chunk: dict = {}
    seen_dim: Optional[int] = None
    for cid, dim, blob in rows:
        v = _blob_to_vec(blob, dim or 0)
        if v is None:
            continue
        if seen_dim is None:
            seen_dim = int(v.shape[0])
        elif int(v.shape[0]) != seen_dim:
            raise ValueError(
                f"Vector dim mismatch: expected {seen_dim}, got {v.shape[0]} for chunk {cid}"
            )
        by_chunk.setdefault(cid, []).append(v)
    if not by_chunk:
        raise RuntimeError(f"No vectors found in {db}")
    chunk_ids = sorted(by_chunk.keys())
    means = np.stack(
        [np.mean(np.stack(by_chunk[c], axis=0), axis=0) for c in chunk_ids], axis=0
    ).astype(np.float32)
    vec_path = out / _VECTORS_FILE
    ids_path = out / _IDS_FILE
    _atomic_npy_save(vec_path, means)
    _atomic_npy_save(ids_path, np.array(chunk_ids, dtype=object))
    if verbose:
        print(
            f"[store] wrote {means.shape[0]:,} mean vectors "
            f"(dim={means.shape[1]}, {means.nbytes / 1e6:.1f} MB) -> {vec_path.name}"
        )
    return ChunkMeanStore(
        vectors=means,
        chunk_ids=np.array(chunk_ids, dtype=object),
        dim=int(means.shape[1]),
    )


def _atomic_npy_save(path: Path, arr: np.ndarray) -> None:
    # np.save auto-appends ".npy" if the filename doesn't already end in it.
    # Use an explicit file handle to keep our chosen tmp name literal.
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("wb") as fh:
        if arr.dtype == object:
            np.save(fh, arr, allow_pickle=True)
        else:
            np.save(fh, arr)
    tmp.replace(path)


def load_chunk_mean_store(
    *,
    db_path: Optional[Path] = None,
    out_dir: Optional[Path] = None,
    force_rebuild: bool = False,
    mmap: bool = True,
) -> ChunkMeanStore:
    """Return store; build cache if stale or missing."""
    db = db_path or clip_and_still_embeddings_sqlite_path()
    out = out_dir or cache_dir()
    vec = out / _VECTORS_FILE
    ids = out / _IDS_FILE
    if force_rebuild or not _is_cache_fresh(db, vec, ids):
        return build_chunk_mean_store(db_path=db, out_dir=out)
    arr = np.load(vec, mmap_mode="r" if mmap else None)
    chunk_ids = np.load(ids, allow_pickle=True)
    if arr.shape[0] != chunk_ids.shape[0]:
        return build_chunk_mean_store(db_path=db, out_dir=out)
    return ChunkMeanStore(vectors=arr, chunk_ids=chunk_ids, dim=int(arr.shape[1]))


def cosine_topk(
    query: np.ndarray,
    matrix: np.ndarray,
    k: int,
    *,
    candidate_idx: Optional[np.ndarray] = None,
):
    """Return (indices_into_matrix, scores) for top-k by cosine similarity.

    Args:
        query: (D,) vector. Will be L2-normalized.
        matrix: (N, D) candidate vectors. Per-row norm is computed.
        k: top-K to return.
        candidate_idx: optional (M,) int indices to restrict scoring to a subset.
    """
    q = np.asarray(query, dtype=np.float32).reshape(-1)
    qn = float(np.linalg.norm(q))
    q = q / max(qn, 1e-12)
    if candidate_idx is not None and candidate_idx.size == 0:
        return np.zeros((0,), dtype=np.int64), np.zeros((0,), dtype=np.float32)
    sub = matrix if candidate_idx is None else matrix[candidate_idx]
    norms = np.linalg.norm(sub, axis=1)
    norms = np.clip(norms, 1e-12, None)
    scores = (sub @ q) / norms
    k_eff = min(k, scores.shape[0])
    if k_eff <= 0:
        return np.zeros((0,), dtype=np.int64), np.zeros((0,), dtype=np.float32)
    part = np.argpartition(-scores, k_eff - 1)[:k_eff]
    order = part[np.argsort(-scores[part])]
    if candidate_idx is None:
        return order, scores[order]
    return candidate_idx[order], scores[order]
