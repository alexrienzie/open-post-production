"""Shared filesystem locations for the open-post-stack workspace.

`dataset/` holds canonical JSON catalogs; `indexes/` (repo sibling) holds
machine-generated SQLite files: `editorial_catalog.sqlite` (denormalized
editorial join surface) and `clip_and_still_embeddings.sqlite` (SigLIP frame
vectors + chunk registry for joins).
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path


def dataset_root() -> Path:
    """Catalog tree root (parent of `_scripts/`)."""
    return Path(__file__).resolve().parent.parent


def repo_root() -> Path:
    """Workspace root (`open-post-stack/`), parent of `dataset/`."""
    return dataset_root().parent


def indexes_dir() -> Path:
    """Directory for SQLite sidecars (sibling of `dataset/`)."""
    return repo_root() / "indexes"


def editorial_catalog_sqlite_path() -> Path:
    """Denormalized catalog join surface (`build_editor_db.py`)."""
    return indexes_dir() / "editorial_catalog.sqlite"


def clip_and_still_embeddings_sqlite_path() -> Path:
    """SigLIP embeddings + per-chunk registry (`semantic_chunks` / `clip_embeddings`)."""
    return indexes_dir() / "clip_and_still_embeddings.sqlite"


def clip_semantics_and_vectors_sqlite_path() -> Path:
    """Deprecated alias — use `clip_and_still_embeddings_sqlite_path()`."""
    return clip_and_still_embeddings_sqlite_path()


def transcript_rolling_embeddings_sqlite_path() -> Path:
    """Rolling-window transcript text embeddings (`embed_transcript_rolling_windows.py`)."""
    return indexes_dir() / "transcript_rolling_embeddings.sqlite"


def derivative_media_root() -> Path:
    return repo_root() / "derivative media"


def asset_map_path() -> Path:
    return derivative_media_root() / "_index" / "asset_map.json"


def legacy_proxy_videos_dir() -> Path:
    """Flat hash-named proxies (retired on the workspace; may still exist during migration)."""
    return derivative_media_root() / "proxy videos"


def placeholders_dir() -> Path:
    """Synthetic placeholder MP4s (text cards, slates)."""
    d = derivative_media_root() / "_placeholders"
    d.mkdir(parents=True, exist_ok=True)
    return d


@lru_cache(maxsize=1)
def _asset_map_entries() -> dict[str, dict]:
    path = asset_map_path()
    if not path.is_file():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return data.get("entries") or {}


def resolve_proxy_path(asset_id: str, kind: str = "video_video_proxy") -> Path | None:
    """Resolve catalog asset_id to an on-disk proxy path, or None."""
    rec = _asset_map_entries().get(asset_id)
    if rec:
        slot = rec.get(kind) or rec.get("video_video_proxy")
        if slot and slot.get("relative_path"):
            return derivative_media_root() / Path(slot["relative_path"])
    legacy = legacy_proxy_videos_dir() / f"{asset_id}.mp4"
    if legacy.is_file():
        return legacy
    return None


def placeholder_proxy_path(clip_id: str) -> Path:
    return placeholders_dir() / f"placeholder_{clip_id}.mp4"


if __name__ == "__main__":
    e = editorial_catalog_sqlite_path()
    r = clip_and_still_embeddings_sqlite_path()
    t = transcript_rolling_embeddings_sqlite_path()
    print(e)
    print(r)
    print(t)
    print("editorial_catalog exists:", e.exists())
    print("clip_and_still_embeddings exists:", r.exists())
