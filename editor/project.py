"""Project paths and format defaults for the editor utilities."""

from __future__ import annotations

import sys
from pathlib import Path

from pydantic import BaseModel


class ProjectConfig(BaseModel):
    """Editorial project format for previews and Premiere XML handoff."""

    frame_rate: float = 23.976
    width: int = 1280
    height: int = 720
    audio_rate: int = 48000
    audio_channels: int = 2


def editor_root() -> Path:
    """Return the editor workspace root."""
    return Path(__file__).resolve().parent


def workspace_root() -> Path:
    """Return the shared open-post-stack workspace root."""
    return editor_root().parent


def _dataset_scripts_on_path() -> None:
    scripts = workspace_root() / "dataset" / "_scripts"
    s = str(scripts)
    if s not in sys.path:
        sys.path.insert(0, s)


def default_proxies_dir() -> Path:
    """Directory for synthetic placeholders (not catalog shoot-folder proxies)."""
    _dataset_scripts_on_path()
    from workspace_paths import placeholders_dir  # noqa: E402

    return placeholders_dir()


def resolve_proxy_path(asset_id: str, kind: str = "video_video_proxy") -> Path | None:
    """Resolve a catalog asset_id to an on-disk proxy file."""
    _dataset_scripts_on_path()
    from workspace_paths import resolve_proxy_path as _resolve  # noqa: E402

    return _resolve(asset_id, kind=kind)


def placeholder_proxy_path(clip_id: str) -> Path:
    _dataset_scripts_on_path()
    from workspace_paths import placeholder_proxy_path as _path  # noqa: E402

    return _path(clip_id)


def default_output_dir() -> Path:
    """Preview renders from the archived ffmpeg pipeline."""
    return editor_root() / "_archive" / "render_to_mp4" / "outputs"


def indexes_dir() -> Path:
    return workspace_root() / "indexes"


def default_clip_and_still_embeddings_sqlite_path() -> Path:
    return indexes_dir() / "clip_and_still_embeddings.sqlite"


def default_clip_semantics_and_vectors_sqlite_path() -> Path:
    """Deprecated alias."""
    return default_clip_and_still_embeddings_sqlite_path()


def resources_dir() -> Path:
    return editor_root() / "story" / "_resources"
