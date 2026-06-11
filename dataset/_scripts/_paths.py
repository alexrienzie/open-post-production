"""Shared filesystem locations + per-asset path resolvers for the inference-side ingest
scripts (INGEST.md Phases C / E / F / G).

Derivative media on the workspace SSD mirrors the source-path tree under `open-post-stack/derivative
media/`: each asset's proxy and WAV land in `<shoot-folder>/<source-filename>`
(with `.R3D` -> `.mp4` rewriting for raw camera output). Transcripts stage flat
under `derivative media/_transcript staging/` for promotion into the
canonical catalog.
"""
from __future__ import annotations

from pathlib import Path

WORKSPACE_ROOT = Path(__file__).resolve().parent.parent.parent
DATASET_ROOT = WORKSPACE_ROOT / "dataset"
VIDEO_CATALOG = DATASET_ROOT / "assets" / "video"
AUDIO_CATALOG = DATASET_ROOT / "assets" / "audio"
STILLS_CATALOG = DATASET_ROOT / "assets" / "stills"
TRANSCRIPT_CATALOG = DATASET_ROOT / "assets" / "transcripts"

INDEXES_DIR = WORKSPACE_ROOT / "indexes"
EMBEDDINGS_DB = INDEXES_DIR / "clip_and_still_embeddings.sqlite"

DERIVATIVE_MEDIA = WORKSPACE_ROOT / "derivative media"
ASSET_MAP = DERIVATIVE_MEDIA / "_index" / "asset_map.json"
TRANSCRIPT_STAGING = DERIVATIVE_MEDIA / "_transcript staging"
PROXY_CHUNKS_DIR = DERIVATIVE_MEDIA / "_proxy chunks"
SIGLIP_KEYFRAMES_DIR = DERIVATIVE_MEDIA / "_siglip keyframes"

FACE_EMBEDDINGS_DB = INDEXES_DIR / "face_embeddings.sqlite"
FACE_EXEMPLARS_DIR = DERIVATIVE_MEDIA / "_face exemplars"
PEOPLE_REGISTRY = DATASET_ROOT / "people" / "people.json"

AUDIO_EVENTS_DB = INDEXES_DIR / "audio_events.sqlite"
AUDIO_FINGERPRINT_DB = INDEXES_DIR / "audio_fingerprints.sqlite"

# Legacy per-layer stores — retired in the catalog-JSON refactor (signals now
# live on per-asset catalog JSON; see indexes/indexes_README.md). Kept only so
# archival readers (ocr/pilot.py, qa/run_layer_qa.py)
# import cleanly; they fail at file-open unless you restore a legacy store.
OCR_DB = INDEXES_DIR / "ocr.sqlite"
SHOTS_DB = INDEXES_DIR / "shots.sqlite"
SHOT_QUALITY_DB = INDEXES_DIR / "shot_quality.sqlite"
AUDIO_QUALITY_DB = INDEXES_DIR / "audio_quality.sqlite"
CLIP_FAISS_INDEX = INDEXES_DIR / "clip_embeddings.faiss"
CLIP_FAISS_META = INDEXES_DIR / "clip_embeddings.faiss.meta.json"

# The 6 builders below now write their signals directly to per-asset catalog
# JSON via `_catalog_layer_io.update_layer()`. The standalone SQLite stores
# (`shots.sqlite`, `shot_quality.sqlite`, `audio_quality.sqlite`, `ocr.sqlite`,
# `dense_captions.sqlite`, `still_quality.sqlite`) were retired in the
# Catalog-JSON refactor — see `indexes/indexes_README.md`.
# `dataset/_scripts/migrate_indexes_to_catalog.py`. Run logs go to per-run JSON
# files under `_runs/ingest_pipeline/<layer>/`.

RUNS_DIR = DATASET_ROOT / "_runs" / "ingest_pipeline"


def iter_catalog_jsons(catalog_dir: Path, suffix: str):
    """Glob `*<suffix>` under a catalog dir, skipping macOS AppleDouble (`._*`)
    sidecars that exFAT volumes accumulate on every write. Use this in place of
    `Path.glob("*.json")` in any catalog reader."""
    for p in catalog_dir.glob(f"*{suffix}"):
        if p.name.startswith("._"):
            continue
        yield p


def open_sqlite_ro(path):
    """Open a sqlite DB in read-only mode, with an exFAT-safe fallback.

    On the workspace SSD's exFAT volume, a WAL-mode DB can fail .execute() with
    "disk I/O error" when opened via `?mode=ro` because the filesystem can't
    create the -shm file. Probe with sqlite_master; if the probe fails, fall
    back to an rw connection (still effectively read-only at the call site)."""
    import sqlite3
    if not path.exists():
        return None
    try:
        c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        c.execute("SELECT name FROM sqlite_master LIMIT 1").fetchone()
        return c
    except sqlite3.OperationalError:
        return sqlite3.connect(str(path))

_SOURCE_ROOT_WIN = "D:\\Project\\"


def _norm_source(source_path: str) -> str:
    sp = (source_path or "").replace("\\", "/")
    prefix = _SOURCE_ROOT_WIN.replace("\\", "/")
    if sp.startswith(prefix):
        return sp[len(prefix):]
    if len(sp) >= 3 and sp[1:3] == ":/":
        return sp[3:]
    return sp.lstrip("/")


def derivative_relative(source_path: str) -> Path:
    """Path inside `derivative media/` mirroring the catalog source_path tree."""
    rel = _norm_source(source_path)
    if not rel:
        raise ValueError("source_path is empty")
    return Path(rel)


def proxy_output_path(record: dict) -> Path:
    """Absolute path where the H.264 proxy MP4 should land for this record."""
    rel = derivative_relative(record.get("source_path") or "")
    if rel.suffix.lower() == ".r3d":
        rel = rel.with_suffix(".mp4")
    return DERIVATIVE_MEDIA / rel


def wav_output_path(record: dict) -> Path:
    """Absolute path where the 16 kHz mono WAV should land for this record."""
    rel = derivative_relative(record.get("source_path") or "")
    return DERIVATIVE_MEDIA / rel.with_suffix(".wav")


def transcript_staging_path(asset_id: str) -> Path:
    return TRANSCRIPT_STAGING / f"{asset_id}.transcript.json"


def workspace_tilde(path: Path) -> str:
    """Render an absolute workspace path as `~/<relative>` for catalog writes,
    matching the convention documented in CLAUDE.md."""
    try:
        return "~/" + path.relative_to(WORKSPACE_ROOT).as_posix()
    except ValueError:
        return str(path)


def resolve_proxy_via_asset_map(asset_id: str, kind: str = "video_video_proxy") -> Path | None:
    """Best-effort lookup using `derivative media/_index/asset_map.json`. Returns
    None if asset_map is missing or the entry/slot isn't present. Re-reads each
    call to avoid stale-cache surprises across long runs."""
    import json
    if not ASSET_MAP.is_file():
        return None
    data = json.loads(ASSET_MAP.read_text(encoding="utf-8"))
    rec = (data.get("entries") or {}).get(asset_id)
    if not rec:
        return None
    slot = rec.get(kind) or rec.get("video_video_proxy")
    if not slot or not slot.get("relative_path"):
        return None
    return DERIVATIVE_MEDIA / Path(slot["relative_path"].replace("\\", "/"))
