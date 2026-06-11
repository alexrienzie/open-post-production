#!/usr/bin/env python3
"""build_faiss.py — Corpus-scale visual similarity over SigLIP keyframes.

Reads all 1152-d L2-normalized SigLIP frame vectors from
indexes/clip_and_still_embeddings.sqlite, builds an HNSW FAISS index, persists
to disk + a small metadata JSON that maps FAISS position → (embedding_pk,
chunk_id, frame_idx, timestamp_sec, parent_asset_id).

Subcommands:
  build        Rebuild index from scratch
  query-frame  Query nearest K frames given an embedding_pk, image path, or
               raw vector (debug helper)
  status       Index stats
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import struct
import sys
import time
import warnings
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from _paths import (  # noqa: E402
    CLIP_FAISS_INDEX, CLIP_FAISS_META, INDEXES_DIR,
)

warnings.filterwarnings("ignore", message="urllib3 v2 only supports OpenSSL")

EMBEDDINGS_DB = INDEXES_DIR / "clip_and_still_embeddings.sqlite"
EMBED_DIM = 1152
HNSW_M = 32                # neighbors per node (16-32 typical; higher = denser graph, slower build, better recall)
HNSW_EF_CONSTRUCTION = 200  # build-time accuracy (40-200 typical; higher = better quality, slower build)
HNSW_EF_SEARCH = 64         # query-time accuracy (16-128 typical; higher = better recall, slower query)


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _load_vectors_with_meta() -> tuple[np.ndarray, list[dict]]:
    """Pull every clip_embeddings row + join to semantic_chunks for parent context.
    Returns (vectors[N, 1152], meta[N]) in stable order matching FAISS positions."""
    con = sqlite3.connect(f"file:{EMBEDDINGS_DB}?mode=ro", uri=True)
    rows = list(con.execute("""
        SELECT ce.embedding_pk, ce.chunk_id, ce.frame_idx, ce.timestamp_sec,
               ce.vector_blob, gc.parent_asset_id, gc.chunk_start_sec
        FROM clip_embeddings ce
        JOIN semantic_chunks gc ON ce.chunk_id = gc.chunk_id
        ORDER BY ce.embedding_pk
    """))
    n = len(rows)
    print(f"  loading {n} embeddings from clip_embeddings...")
    vecs = np.empty((n, EMBED_DIM), dtype=np.float32)
    meta: list[dict] = []
    for i, r in enumerate(rows):
        emb_pk, chunk_id, frame_idx, ts, blob, parent_aid, chunk_start = r
        vecs[i] = np.array(struct.unpack(f"<{EMBED_DIM}f", blob), dtype=np.float32)
        meta.append({
            "embedding_pk": emb_pk,
            "chunk_id": chunk_id,
            "frame_idx": frame_idx,
            "timestamp_sec": ts,                                       # within-chunk
            "parent_asset_id": parent_aid,
            "abs_time_sec": (chunk_start or 0.0) + ts,                  # absolute in parent
        })
    # Re-normalize defensively — vectors were stored normalized but stale rounding can drift
    norms = np.linalg.norm(vecs, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    vecs = vecs / norms
    return vecs, meta


# ---------------- build ----------------

def cmd_build(args: argparse.Namespace) -> None:
    import faiss

    vecs, meta = _load_vectors_with_meta()
    n, d = vecs.shape
    print(f"  building HNSW index (M={HNSW_M}, ef_construction={HNSW_EF_CONSTRUCTION}) ...")
    t0 = time.time()
    index = faiss.IndexHNSWFlat(d, HNSW_M, faiss.METRIC_INNER_PRODUCT)
    index.hnsw.efConstruction = HNSW_EF_CONSTRUCTION
    index.hnsw.efSearch = HNSW_EF_SEARCH
    index.add(vecs)
    build_sec = time.time() - t0
    print(f"  built in {build_sec:.1f}s  ({n/build_sec:.0f} vectors/s)")

    CLIP_FAISS_INDEX.parent.mkdir(parents=True, exist_ok=True)
    faiss.write_index(index, str(CLIP_FAISS_INDEX))
    CLIP_FAISS_META.write_text(json.dumps({
        "schema_version": 1,
        "built_at": now_iso(),
        "n_vectors": n,
        "embed_dim": d,
        "index_type": "IndexHNSWFlat",
        "hnsw_m": HNSW_M,
        "hnsw_ef_construction": HNSW_EF_CONSTRUCTION,
        "hnsw_ef_search_default": HNSW_EF_SEARCH,
        "metric": "inner_product",   # equivalent to cosine on L2-normalized vectors
        "vectors_normalized": True,
        "position_to_meta": meta,
    }, indent=2, default=str))

    sz_idx = CLIP_FAISS_INDEX.stat().st_size / 1e6
    sz_meta = CLIP_FAISS_META.stat().st_size / 1e6
    print(f"  index → {CLIP_FAISS_INDEX} ({sz_idx:.1f} MB)")
    print(f"  meta  → {CLIP_FAISS_META} ({sz_meta:.1f} MB)")


# ---------------- query helpers ----------------

def _load_index_and_meta():
    import faiss
    if not CLIP_FAISS_INDEX.exists() or not CLIP_FAISS_META.exists():
        print(f"index missing — run `build` first", file=sys.stderr)
        sys.exit(2)
    index = faiss.read_index(str(CLIP_FAISS_INDEX))
    meta = json.loads(CLIP_FAISS_META.read_text())
    return index, meta


def _vector_for_embedding_pk(emb_pk: int) -> np.ndarray | None:
    con = sqlite3.connect(f"file:{EMBEDDINGS_DB}?mode=ro", uri=True)
    r = con.execute("SELECT vector_blob FROM clip_embeddings WHERE embedding_pk=?", (emb_pk,)).fetchone()
    if not r:
        return None
    v = np.array(struct.unpack(f"<{EMBED_DIM}f", r[0]), dtype=np.float32)
    n = np.linalg.norm(v)
    return v / (n if n > 0 else 1.0)


def cmd_query_frame(args: argparse.Namespace) -> None:
    """Query nearest-K frames given an embedding_pk (debug). For image-based query
    we'd need to run SigLIP on the image first — not yet wired."""
    index, meta = _load_index_and_meta()
    qv = _vector_for_embedding_pk(args.embedding_pk)
    if qv is None:
        print(f"no clip_embeddings row with embedding_pk={args.embedding_pk}", file=sys.stderr)
        sys.exit(2)
    D, I = index.search(qv.reshape(1, -1), args.k)
    pos2meta = meta["position_to_meta"]
    print(f"=== top {args.k} neighbors of embedding_pk={args.embedding_pk} ===")
    print(f"  {'sim':>5}  {'parent_aid':>14}  {'chunk_id':>14}  abs_t")
    for sim, pos in zip(D[0], I[0]):
        m = pos2meta[pos]
        print(f"  {sim:5.3f}  {m['parent_asset_id'][:14]:>14}  {m['chunk_id'][:14]:>14}  {m['abs_time_sec']:.1f}s")


def _load_siglip_model(device: str = "mps"):
    """Lazy-load SigLIP-So400m for image-in queries.
    Matches the model used by `production_run/siglip_embed_keyframes.py`."""
    from transformers import AutoModel, AutoProcessor
    import torch
    model_id = "google/siglip-so400m-patch14-384"
    print(f"  loading SigLIP {model_id} on {device}...", file=sys.stderr)
    processor = AutoProcessor.from_pretrained(model_id)
    model = AutoModel.from_pretrained(model_id, torch_dtype=torch.float32).to(device).eval()
    return model, processor


def cmd_query_image(args: argparse.Namespace) -> None:
    """Encode an arbitrary image via SigLIP and query the FAISS index.

    Use case: 'paste a screengrab, find moments in the corpus that look like
    this.' Loads SigLIP once per process. First-run downloads ~3.5 GB of
    weights from HuggingFace.
    """
    import torch
    from PIL import Image

    img_path = Path(args.image)
    if not img_path.exists():
        print(f"image not found: {img_path}", file=sys.stderr)
        sys.exit(2)

    index, meta = _load_index_and_meta()
    model, processor = _load_siglip_model(device=args.device)

    img = Image.open(img_path).convert("RGB")
    with torch.no_grad():
        inputs = processor(images=img, return_tensors="pt").to(args.device)
        emb = model.get_image_features(**inputs).cpu().numpy().astype("float32")
    # L2-normalize so dot-product == cosine (matches build_faiss)
    n = np.linalg.norm(emb, axis=1, keepdims=True)
    emb = emb / np.where(n == 0, 1.0, n)

    D, I = index.search(emb, args.k)
    pos2meta = meta["position_to_meta"]
    print(f"\n=== top {args.k} matches for {img_path.name} ===")
    print(f"  {'sim':>5}  {'parent_aid':>14}  {'chunk_id':>14}  abs_t")
    # Optional: filter to one match per asset (default: keep up to one rank-1 per asset)
    seen_assets: set[str] = set()
    n_shown = 0
    for sim, pos in zip(D[0], I[0]):
        m = pos2meta[pos]
        aid = m["parent_asset_id"]
        if args.distinct_assets and aid in seen_assets:
            continue
        seen_assets.add(aid)
        n_shown += 1
        print(f"  {sim:5.3f}  {aid[:14]:>14}  {m['chunk_id'][:14]:>14}  {m['abs_time_sec']:.1f}s")
        if n_shown >= args.k:
            break


# ---------------- status ----------------

def cmd_status(args: argparse.Namespace) -> None:
    if not CLIP_FAISS_META.exists():
        print(f"No index at {CLIP_FAISS_INDEX} — run `build` first.")
        return
    meta = json.loads(CLIP_FAISS_META.read_text())
    sz_idx = CLIP_FAISS_INDEX.stat().st_size / 1e6 if CLIP_FAISS_INDEX.exists() else 0.0
    sz_meta = CLIP_FAISS_META.stat().st_size / 1e6
    print(f"=== FAISS clip embeddings index ===")
    print(f"  built_at:    {meta.get('built_at')}")
    print(f"  vectors:     {meta.get('n_vectors')}")
    print(f"  dim:         {meta.get('embed_dim')}")
    print(f"  index type:  {meta.get('index_type')}")
    print(f"  HNSW M / efC: {meta.get('hnsw_m')} / {meta.get('hnsw_ef_construction')}")
    print(f"  file sizes:  index={sz_idx:.1f} MB, meta={sz_meta:.1f} MB")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_build = sub.add_parser("build", help="Build HNSW index from clip_embeddings")
    p_build.set_defaults(func=cmd_build)

    p_q = sub.add_parser("query-frame", help="Smoke: query nearest-K for a given embedding_pk")
    p_q.add_argument("embedding_pk", type=int)
    p_q.add_argument("-k", type=int, default=10)
    p_q.set_defaults(func=cmd_query_frame)

    p_qi = sub.add_parser("query-image", help="Image-in: encode an image via SigLIP and find similar moments")
    p_qi.add_argument("image", help="path to query image (JPG/PNG)")
    p_qi.add_argument("-k", type=int, default=10, help="top-K matches to return")
    p_qi.add_argument("--device", default="mps", choices=["mps", "cpu", "cuda"])
    p_qi.add_argument("--distinct-assets", action="store_true",
                      help="Show at most one match per asset (collapse near-duplicates)")
    p_qi.set_defaults(func=cmd_query_image)

    p_status = sub.add_parser("status", help="Index stats")
    p_status.set_defaults(func=cmd_status)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
