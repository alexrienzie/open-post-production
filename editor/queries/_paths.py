"""Path bootstrap for the queries package.

`dataset/_scripts/` is not on the import path by default (the repo is a flat-script
project, not a package). This module fixes that so submodules can import the
canonical `workspace_paths` helpers, and adds derived cache-dir helpers used here.
"""

from __future__ import annotations

import sys
from pathlib import Path

# editor/queries/<this> -> editor/queries -> editor -> open-post-stack
_REPO = Path(__file__).resolve().parents[2]
_DATASET_SCRIPTS = _REPO / "dataset" / "_scripts"
if str(_DATASET_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_DATASET_SCRIPTS))

from workspace_paths import (  # noqa: E402
    clip_and_still_embeddings_sqlite_path,
    editorial_catalog_sqlite_path,
    indexes_dir,
    transcript_rolling_embeddings_sqlite_path,
)


def repo_root() -> Path:
    return _REPO


def cache_dir() -> Path:
    """Local cache root for derived artifacts (mean-vector .npy, HF weights)."""
    d = indexes_dir() / "_cache"
    d.mkdir(parents=True, exist_ok=True)
    return d


def hf_cache_dir() -> Path:
    """HuggingFace cache directory for SigLIP weights."""
    d = cache_dir() / "hf"
    d.mkdir(parents=True, exist_ok=True)
    return d


__all__ = [
    "clip_and_still_embeddings_sqlite_path",
    "editorial_catalog_sqlite_path",
    "transcript_rolling_embeddings_sqlite_path",
    "indexes_dir",
    "repo_root",
    "cache_dir",
    "hf_cache_dir",
]
