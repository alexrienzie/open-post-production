#!/usr/bin/env python3
"""SigLIP-So400m keyframe embeddings — production pass over the full catalog.

Reads chunk list (asset_id, upload_path, duration_sec, etc.) from the same
results_production.db that the Gemini run populated. Adds clip_embeddings
rows: 1 frame per 7 sec from each chunk → 1152-dim L2-normalised vectors.

Fully local: FFmpeg keyframe extraction + sentence-transformers SigLIP on MPS.
No API, no network. Can run in parallel with the Vertex Gemini cleanup.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import sqlite3
import struct
import subprocess
import sys
import time
import traceback
from contextlib import closing
from pathlib import Path
from typing import Any, Iterable, List


# --------------------------------------------------------------------------- paths

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from _paths import (
    EMBEDDINGS_DB, DERIVATIVE_MEDIA, SIGLIP_KEYFRAMES_DIR, RUNS_DIR,
)

SCRIPT_DIR = Path(__file__).resolve().parent
DATA_DIR = RUNS_DIR / "production_run"
DB_PATH = EMBEDDINGS_DB
LOG_PATH = DATA_DIR / "pilot_siglip_production.log"

KEYFRAMES_DIR = SIGLIP_KEYFRAMES_DIR
FFMPEG = "/opt/homebrew/bin/ffmpeg"
FFPROBE = "/opt/homebrew/bin/ffprobe"

# --------------------------------------------------------------------------- config

SIGLIP_MODEL = "google/siglip-so400m-patch14-384"
SIGLIP_DIM = 1152
KEYFRAME_INTERVAL_SEC = 7
SIGLIP_BATCH = 32

# --------------------------------------------------------------------------- schema

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS semantic_chunks (
    chunk_id              TEXT PRIMARY KEY,
    parent_asset_id       TEXT NOT NULL,
    chunk_idx             INTEGER NOT NULL,
    is_chunked            INTEGER NOT NULL,
    bucket                TEXT,
    duration_sec          REAL,
    upload_path           TEXT NOT NULL,
    chunk_start_sec       REAL,
    chunk_end_sec         REAL,
    camera_id             TEXT,
    shoot_label           TEXT,
    category_name         TEXT,
    model          TEXT,
    gemini_file_id        TEXT,
    response_raw   TEXT,
    response_json  TEXT,
    gemini_input_tokens   INTEGER,
    gemini_output_tokens  INTEGER,
    gemini_cost_usd       REAL,
    label_status         TEXT,
    gemini_started_at     TEXT,
    gemini_completed_at   TEXT,
    error                 TEXT
);

CREATE TABLE IF NOT EXISTS clip_embeddings (
    embedding_pk      INTEGER PRIMARY KEY AUTOINCREMENT,
    chunk_id          TEXT NOT NULL,
    frame_idx         INTEGER NOT NULL,
    timestamp_sec     REAL NOT NULL,
    keyframe_path     TEXT,
    embedding_model   TEXT,
    vector_dim        INTEGER,
    vector_blob       BLOB,
    pulled_at         TEXT,
    UNIQUE(chunk_id, frame_idx)
);

CREATE INDEX IF NOT EXISTS idx_chunk ON clip_embeddings(chunk_id);
CREATE INDEX IF NOT EXISTS idx_status ON semantic_chunks(label_status);
CREATE INDEX IF NOT EXISTS idx_emb_pending ON clip_embeddings(vector_blob) WHERE vector_blob IS NULL;
"""

# --------------------------------------------------------------------------- logging

def setup_logging() -> logging.Logger:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("pilot")
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)sZ %(levelname)s %(message)s")
    fmt.converter = time.gmtime
    fh = logging.FileHandler(LOG_PATH)
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(fh)
    logger.addHandler(sh)
    return logger


LOG = setup_logging()

# --------------------------------------------------------------------------- helpers

def now_utc() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def db_connect() -> sqlite3.Connection:
    """WAL mode so this can co-exist with a live Gemini cleanup pass."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=30.0)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute("PRAGMA busy_timeout=30000")
    con.executescript(SCHEMA_SQL)
    return con


def assert_env_ready() -> None:
    if not Path(FFMPEG).exists():
        sys.exit(f"ffmpeg not found at {FFMPEG}")
    if not DB_PATH.exists():
        sys.exit(f"production DB missing: {DB_PATH}")
    if not DERIVATIVE_MEDIA.exists():
        sys.exit(f"derivative media not available: {DERIVATIVE_MEDIA} — remount the workspace SSD")

# --------------------------------------------------------------------------- extract-keyframes

def cmd_extract_keyframes(args: argparse.Namespace) -> None:
    """For every chunk in semantic_chunks (4,963 rows), extract 1 frame per 7 sec
    via FFmpeg. Idempotent — skips any chunk that already has clip_embeddings rows.
    """
    assert_env_ready()
    KEYFRAMES_DIR.mkdir(parents=True, exist_ok=True)
    with closing(db_connect()) as con:
        chunks = con.execute(
            "SELECT chunk_id, upload_path FROM semantic_chunks ORDER BY duration_sec ASC"
        ).fetchall()
        if args.limit:
            chunks = chunks[: args.limit]
        LOG.info("extract-keyframes: %d chunks to process", len(chunks))
        for ch in chunks:
            existing = con.execute(
                "SELECT COUNT(*) FROM clip_embeddings WHERE chunk_id = ?",
                (ch["chunk_id"],),
            ).fetchone()[0]
            if existing > 0:
                LOG.info("extract-keyframes: skip %s (%d frames present)",
                         ch["chunk_id"], existing)
                continue
            src = Path(ch["upload_path"])
            if not src.exists():
                LOG.error("extract-keyframes: missing source %s", src)
                continue
            _extract_chunk_keyframes(con, ch["chunk_id"], src)
            con.commit()
    LOG.info("extract-keyframes done")


def _extract_chunk_keyframes(con: sqlite3.Connection, chunk_id: str, src: Path) -> None:
    pattern = KEYFRAMES_DIR / f"{chunk_id}_%04d.jpg"
    LOG.info("extract-keyframes: %s", chunk_id)
    r = subprocess.run(
        [FFMPEG, "-nostdin", "-y", "-i", str(src),
         "-vf", f"fps=1/{KEYFRAME_INTERVAL_SEC},scale=512:-1", "-q:v", "2",
         "-strict", "unofficial",
         str(pattern)],
        check=False, capture_output=True,
    )
    if r.returncode != 0:
        # Most failures here mirror Vertex's INVALID_ARGUMENT: malformed proxy,
        # missing video stream, etc. Log and skip — don't kill the whole run.
        stderr_tail = (r.stderr or b"").decode("utf-8", errors="replace").splitlines()[-2:]
        LOG.error("ffmpeg failed (rc=%d) for %s: %s",
                  r.returncode, chunk_id, " | ".join(stderr_tail))
        return
    frames = sorted(KEYFRAMES_DIR.glob(f"{chunk_id}_*.jpg"))
    # Fallback for clips shorter than KEYFRAME_INTERVAL_SEC: fps=1/7 yields 0
    # frames on a <7s clip. Grab the first frame so even very short clips have
    # at least one SigLIP embedding.
    if not frames:
        fallback = KEYFRAMES_DIR / f"{chunk_id}_0001.jpg"
        r2 = subprocess.run(
            [FFMPEG, "-nostdin", "-y", "-i", str(src),
             "-vf", "scale=512:-1", "-frames:v", "1", "-q:v", "2",
             "-strict", "unofficial", str(fallback)],
            check=False, capture_output=True,
        )
        if r2.returncode != 0:
            stderr_tail = (r2.stderr or b"").decode("utf-8", errors="replace").splitlines()[-2:]
            LOG.error("ffmpeg fallback failed (rc=%d) for %s: %s",
                      r2.returncode, chunk_id, " | ".join(stderr_tail))
            return
        frames = sorted(KEYFRAMES_DIR.glob(f"{chunk_id}_*.jpg"))
        LOG.info("  fallback (clip <%ds): extracted %d frame",
                 KEYFRAME_INTERVAL_SEC, len(frames))
    else:
        LOG.info("  extracted %d frames", len(frames))
    rows = []
    for f in frames:
        idx = int(f.stem.rsplit("_", 1)[-1]) - 1   # ffmpeg's %04d starts at 1
        rows.append((chunk_id, idx, idx * KEYFRAME_INTERVAL_SEC, str(f), now_utc()))
    con.executemany(
        "INSERT OR IGNORE INTO clip_embeddings "
        "(chunk_id, frame_idx, timestamp_sec, keyframe_path, pulled_at) "
        "VALUES (?, ?, ?, ?, ?)",
        rows,
    )

# --------------------------------------------------------------------------- embed-keyframes

def cmd_embed_keyframes(args: argparse.Namespace) -> None:
    """Direct HF transformers loading — sentence-transformers' auto-loader
    fails on SigLIP's vision/text-split config (no top-level hidden_size).
    """
    from PIL import Image
    from transformers import AutoProcessor, AutoModel
    import torch

    device = "mps" if torch.backends.mps.is_available() else "cpu"
    LOG.info("embed-keyframes: loading SigLIP on %s", device)
    processor = AutoProcessor.from_pretrained(SIGLIP_MODEL)
    model = AutoModel.from_pretrained(SIGLIP_MODEL).to(device).eval()
    LOG.info("model loaded: %s (image dim=%d)", SIGLIP_MODEL, SIGLIP_DIM)

    with closing(db_connect()) as con:
        pending = con.execute(
            "SELECT embedding_pk, keyframe_path FROM clip_embeddings "
            "WHERE vector_blob IS NULL ORDER BY embedding_pk"
        ).fetchall()
        if not pending:
            LOG.info("embed-keyframes: nothing to do")
            return
        LOG.info("embed-keyframes: %d frames pending", len(pending))
        done = 0
        for batch_start in range(0, len(pending), SIGLIP_BATCH):
            batch = pending[batch_start:batch_start + SIGLIP_BATCH]
            paths = [Path(r["keyframe_path"]) for r in batch]
            missing = [p for p in paths if not p.exists()]
            if missing:
                LOG.error("missing keyframes (skipping batch): %s", missing[:3])
                continue
            images = [Image.open(p).convert("RGB") for p in paths]
            try:
                with torch.no_grad():
                    inputs = processor(images=images, return_tensors="pt").to(device)
                    feats = model.get_image_features(**inputs)
                    # L2-normalise for cosine similarity (matches Marengo's pattern).
                    feats = feats / feats.norm(p=2, dim=-1, keepdim=True)
                vectors = feats.cpu().numpy()
            finally:
                for img in images:
                    img.close()
            updates = []
            for r, vec in zip(batch, vectors):
                if len(vec) != SIGLIP_DIM:
                    sys.exit(f"unexpected SigLIP dim: {len(vec)}")
                blob = struct.pack(f"<{SIGLIP_DIM}f", *vec.astype("float32"))
                updates.append((SIGLIP_MODEL, SIGLIP_DIM, blob, now_utc(),
                                r["embedding_pk"]))
            con.executemany(
                "UPDATE clip_embeddings SET embedding_model = ?, vector_dim = ?, "
                "vector_blob = ?, pulled_at = ? WHERE embedding_pk = ?",
                updates,
            )
            con.commit()
            done += len(updates)
            LOG.info("  embedded %d / %d", done, len(pending))
    LOG.info("embed-keyframes done")

# --------------------------------------------------------------------------- status

def cmd_status(args: argparse.Namespace) -> None:
    with closing(db_connect()) as con:
        total_chunks = con.execute("SELECT COUNT(*) FROM semantic_chunks").fetchone()[0]
        chunks_with_keyframes = con.execute(
            "SELECT COUNT(DISTINCT chunk_id) FROM clip_embeddings"
        ).fetchone()[0]
        kf_total = con.execute("SELECT COUNT(*) FROM clip_embeddings").fetchone()[0]
        emb_total = con.execute(
            "SELECT COUNT(*) FROM clip_embeddings WHERE vector_blob IS NOT NULL"
        ).fetchone()[0]
    print("=== SigLIP Production Status ===")
    print(f"Total chunks (from Gemini run): {total_chunks}")
    print(f"Chunks with keyframes:          {chunks_with_keyframes} / {total_chunks}")
    print(f"Keyframes extracted:            {kf_total}")
    print(f"Embeddings stored:              {emb_total} / {kf_total} "
          f"({(emb_total/kf_total*100) if kf_total else 0:.1f}%)")
    print(f"  model: {SIGLIP_MODEL}")
    print(f"  dim:   {SIGLIP_DIM}")

# --------------------------------------------------------------------------- run-all

def cmd_run_all(args: argparse.Namespace) -> None:
    cmd_extract_keyframes(args)
    cmd_embed_keyframes(args)
    cmd_status(args)

# --------------------------------------------------------------------------- argparse

def _add_limit_arg(p):
    p.add_argument("--limit", type=int, default=None,
                   help="Process at most N chunks (smoke test)")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_extract = sub.add_parser("extract-keyframes")
    _add_limit_arg(p_extract)
    p_extract.set_defaults(func=cmd_extract_keyframes)

    p_embed = sub.add_parser("embed-keyframes")
    _add_limit_arg(p_embed)
    p_embed.set_defaults(func=cmd_embed_keyframes)

    sub.add_parser("status").set_defaults(func=cmd_status)

    p_all = sub.add_parser("run-all")
    _add_limit_arg(p_all)
    p_all.set_defaults(func=cmd_run_all)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
