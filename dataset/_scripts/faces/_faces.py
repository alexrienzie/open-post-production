"""Shared helpers for the face index pipeline (INGEST.md candidate Phase K /
the face layer).

Encapsulates InsightFace model loading, the SQLite schema, frame fetching from
proxies via ffmpeg, and embedding (de)serialization.
"""
from __future__ import annotations

import io
import os
import struct
import subprocess
import sqlite3
import sys
import warnings
from pathlib import Path
from typing import Iterable

import numpy as np

# `_paths.py` lives one dir up; the sibling face scripts import this module
# directly so `sys.path[0]` already points at faces/. Insert mac/ for _paths.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from _paths import (  # noqa: E402
    FACE_EMBEDDINGS_DB, FACE_EXEMPLARS_DIR, DERIVATIVE_MEDIA, RUNS_DIR,
    VIDEO_CATALOG, AUDIO_CATALOG, STILLS_CATALOG, INDEXES_DIR,
)

# Silence urllib3-LibreSSL noise from the system Python (cosmetic)
warnings.filterwarnings("ignore", message="urllib3 v2 only supports OpenSSL")

FFMPEG = "/opt/homebrew/bin/ffmpeg"
INSIGHTFACE_PACK = "buffalo_l"   # SCRFD-10G detector + ArcFace R100 embedder, 280 MB
EMBEDDING_DIM = 512
DET_SIZE = (640, 640)


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS face_detection (
    face_pk        INTEGER PRIMARY KEY AUTOINCREMENT,
    asset_id       TEXT NOT NULL,
    record_kind    TEXT NOT NULL,          -- 'video' | 'still' | 'audio'(unused)
    chunk_id       TEXT,                   -- semantic_chunks.chunk_id for video; NULL for stills
    frame_idx      INTEGER NOT NULL,       -- 0 for stills; SigLIP keyframe idx within chunk for video
    frame_time_sec REAL NOT NULL,          -- absolute time within parent asset; 0 for stills
    bbox_json      TEXT NOT NULL,          -- [x, y, w, h] absolute pixels
    landmarks_json TEXT,                   -- 5-point face landmarks, list of [x,y]
    det_score      REAL NOT NULL,
    embedding      BLOB NOT NULL,          -- struct.pack('<512f', ...), 2048 bytes
    cluster_id     INTEGER,                -- assigned post-clustering, nullable
    p_id           TEXT,                   -- linked manually or by seed match, nullable
    identified_via TEXT,                   -- 'seed_match' | 'cluster_label' | 'manual'
    detected_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS face_det_asset ON face_detection(asset_id, frame_idx);
CREATE INDEX IF NOT EXISTS face_det_p_id  ON face_detection(p_id) WHERE p_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS face_det_cluster ON face_detection(cluster_id) WHERE cluster_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS face_processed_frame (
    chunk_id    TEXT NOT NULL,             -- 'still:<aid>' for stills, gemini chunk_id for video
    frame_idx   INTEGER NOT NULL,
    asset_id    TEXT NOT NULL,
    record_kind TEXT NOT NULL,
    n_faces     INTEGER NOT NULL,
    processed_at TEXT NOT NULL,
    PRIMARY KEY (chunk_id, frame_idx)
);
CREATE INDEX IF NOT EXISTS face_proc_asset ON face_processed_frame(asset_id);

CREATE TABLE IF NOT EXISTS face_cluster (
    cluster_id     INTEGER PRIMARY KEY,
    centroid       BLOB,                   -- mean embedding, 2048 bytes
    n_faces        INTEGER,
    p_id           TEXT,                   -- nullable until labeled
    label_source   TEXT,                   -- 'auto_seed' | 'manual' | NULL
    label_at       TEXT,
    notes          TEXT
);

CREATE TABLE IF NOT EXISTS face_exemplar (
    p_id           TEXT NOT NULL,
    exemplar_idx   INTEGER NOT NULL,
    embedding      BLOB NOT NULL,
    source         TEXT,                   -- 'asset:<aid>:frame:<n>' or 'manual:<filename>'
    added_at       TEXT,
    PRIMARY KEY (p_id, exemplar_idx)
);

CREATE TABLE IF NOT EXISTS face_run (
    run_pk      INTEGER PRIMARY KEY AUTOINCREMENT,
    phase       TEXT NOT NULL,             -- 'stills' | 'video' | 'seed' | 'cluster' | 'label'
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    args_json   TEXT,
    summary_json TEXT
);
"""


def open_db(path: Path = FACE_EMBEDDINGS_DB) -> sqlite3.Connection:
    """Open (and initialize schema on) the face embeddings DB. Idempotent
    ALTERs cover the chunk_id column added for video keyframe joins."""
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path))
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.executescript(SCHEMA_SQL)
    # Idempotent migration: older face_detection rows lack chunk_id.
    cols = {row[1] for row in con.execute("PRAGMA table_info(face_detection)")}
    if "chunk_id" not in cols:
        con.execute("ALTER TABLE face_detection ADD COLUMN chunk_id TEXT")
        con.execute("CREATE INDEX IF NOT EXISTS face_det_chunk ON face_detection(chunk_id, frame_idx)")
    con.commit()
    return con


def pack_embedding(vec: np.ndarray) -> bytes:
    """L2-normalize then pack as little-endian 512 float32. ArcFace embeddings
    are already L2-normalized in practice but we re-normalize defensively."""
    v = vec.astype(np.float32, copy=False)
    n = np.linalg.norm(v)
    if n > 0:
        v = v / n
    return struct.pack(f"<{EMBEDDING_DIM}f", *v.tolist())


def unpack_embedding(blob: bytes) -> np.ndarray:
    return np.array(struct.unpack(f"<{EMBEDDING_DIM}f", blob), dtype=np.float32)


_FACE_APP = None


def get_face_app():
    """Lazy-init InsightFace FaceAnalysis. ~280 MB model download on first run,
    cached at ~/.insightface/models/buffalo_l/. Subsequent calls return the
    cached singleton."""
    global _FACE_APP
    if _FACE_APP is not None:
        return _FACE_APP
    # Defer import so `--help` is fast and CLI is usable without the heavy dep.
    from insightface.app import FaceAnalysis
    app = FaceAnalysis(name=INSIGHTFACE_PACK, providers=["CPUExecutionProvider"])
    app.prepare(ctx_id=0, det_size=DET_SIZE)
    _FACE_APP = app
    return app


RAW_EXTS = {".arw", ".dng", ".cr2", ".cr3", ".nef", ".raf", ".orf"}
FFMPEG_FALLBACK_EXTS = {".heic", ".heif"}


def load_image_bgr(path: Path) -> np.ndarray | None:
    """Load an image as BGR numpy array (InsightFace convention). Returns None
    on RAW or unreadable files. HEIC/HEIF route through ffmpeg pipe."""
    import cv2
    ext = path.suffix.lower()
    if ext in RAW_EXTS:
        return None  # would need rawpy / libraw; skipped from face index
    if ext in FFMPEG_FALLBACK_EXTS:
        cmd = [
            FFMPEG, "-hide_banner", "-loglevel", "error", "-nostdin",
            "-i", str(path),
            "-frames:v", "1", "-f", "image2pipe", "-vcodec", "mjpeg", "-",
        ]
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=30)
        except subprocess.TimeoutExpired:
            return None
        if proc.returncode != 0 or not proc.stdout:
            return None
        arr = np.frombuffer(proc.stdout, dtype=np.uint8)
        return cv2.imdecode(arr, cv2.IMREAD_COLOR)
    return cv2.imread(str(path))  # JPG / PNG / WEBP / TIF — cv2 handles natively


def extract_frame_bgr(proxy_path: Path, timestamp_sec: float) -> np.ndarray | None:
    """Seek into proxy with ffmpeg and decode a single frame to BGR numpy array.
    Stream raw RGB24 via pipe, no temp file. Returns None on failure."""
    cmd = [
        FFMPEG, "-hide_banner", "-loglevel", "error", "-nostdin",
        "-ss", f"{timestamp_sec:.3f}",
        "-i", str(proxy_path),
        "-frames:v", "1",
        "-f", "rawvideo",
        "-pix_fmt", "rgb24",
        "-",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=30)
    except subprocess.TimeoutExpired:
        return None
    if proc.returncode != 0 or not proc.stdout:
        return None
    # We don't know W/H ahead of time; probe via ffprobe? Cheaper: ask ffmpeg to
    # also print the resolution via -vf to stderr. Simpler: use ffprobe once per
    # asset (caller is responsible — see fetch_frame_for_clip_embedding for the
    # batched variant). For the one-off case here, fall back to imdecode:
    import cv2
    arr = np.frombuffer(proc.stdout, dtype=np.uint8)
    # If we got here we have raw RGB but unknown dims — fall through to imdecode
    # path: ask ffmpeg for a JPEG instead.
    cmd_jpg = [
        FFMPEG, "-hide_banner", "-loglevel", "error", "-nostdin",
        "-ss", f"{timestamp_sec:.3f}",
        "-i", str(proxy_path),
        "-frames:v", "1",
        "-f", "image2pipe", "-vcodec", "mjpeg",
        "-",
    ]
    try:
        proc = subprocess.run(cmd_jpg, capture_output=True, timeout=30)
    except subprocess.TimeoutExpired:
        return None
    if proc.returncode != 0 or not proc.stdout:
        return None
    arr = np.frombuffer(proc.stdout, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)  # BGR
    return img


def faces_to_rows(asset_id: str, record_kind: str, chunk_id: str | None,
                  frame_idx: int, frame_time_sec: float, faces,
                  now_iso: str) -> Iterable[tuple]:
    """Convert a list of InsightFace face objects into face_detection row tuples."""
    import json
    for f in faces:
        bbox = [int(x) for x in f.bbox.tolist()]
        landmarks = None
        if hasattr(f, "kps") and f.kps is not None:
            landmarks = [[float(x), float(y)] for x, y in f.kps.tolist()]
        yield (
            asset_id, record_kind, chunk_id, frame_idx, float(frame_time_sec),
            json.dumps(bbox), json.dumps(landmarks) if landmarks else None,
            float(f.det_score), pack_embedding(f.embedding),
            None, None, None, now_iso,
        )


INSERT_FACE_SQL = """
INSERT INTO face_detection
    (asset_id, record_kind, chunk_id, frame_idx, frame_time_sec,
     bbox_json, landmarks_json, det_score, embedding,
     cluster_id, p_id, identified_via, detected_at)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

INSERT_PROCESSED_SQL = """
INSERT OR REPLACE INTO face_processed_frame
    (chunk_id, frame_idx, asset_id, record_kind, n_faces, processed_at)
VALUES (?, ?, ?, ?, ?, ?)
"""


def extract_frame_at(proxy_path: Path, timestamp_sec: float,
                     timeout_sec: int = 30) -> np.ndarray | None:
    """Seek into a proxy via ffmpeg and decode a single frame as BGR numpy.
    Returns None on failure. Used by the video pipeline (P1)."""
    cmd = [
        FFMPEG, "-hide_banner", "-loglevel", "error", "-nostdin",
        "-ss", f"{timestamp_sec:.3f}",
        "-i", str(proxy_path),
        "-frames:v", "1",
        "-f", "image2pipe", "-vcodec", "mjpeg", "-",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout_sec)
    except subprocess.TimeoutExpired:
        return None
    if proc.returncode != 0 or not proc.stdout:
        return None
    import cv2
    arr = np.frombuffer(proc.stdout, dtype=np.uint8)
    return cv2.imdecode(arr, cv2.IMREAD_COLOR)
