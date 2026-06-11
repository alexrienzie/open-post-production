#!/usr/bin/env python3
"""Agent-facing retrieval over editorial_catalog + embedding sidecars.

Thin facade: import this for the public query API, or run it as a script for
the CLI. See `queries_README.md` for the full contract.

Examples:
  py editor/queries/retrieval.py broll --place pl_jenny_lake_ranger_station
  py editor/queries/retrieval.py similar-chunk --asset-id <asset> --top-k 25
  py editor/queries/retrieval.py similar-text  --text "ranger station, golden hour"
  py editor/queries/retrieval.py similar-transcript --text "search and rescue"
  py editor/queries/retrieval.py build-cache --verbose
"""

from __future__ import annotations

# Allow `py editor/queries/retrieval.py ...` to work even though we use
# relative imports below. When invoked as a script, Python sets
# __package__ to None and parents are not on sys.path; fix that here.
if __name__ == "__main__" and (__package__ in (None, "")):
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    __package__ = "editor.queries"

from .encoder import SigLIPEncoder  # noqa: E402
from .filters import asset_allowlist, search_broll  # noqa: E402
from .store import ChunkMeanStore, load_chunk_mean_store  # noqa: E402
from .transcript import find_similar_transcript_windows, search_transcript_fts  # noqa: E402
from .visual import find_visually_similar, find_visually_similar_by_text  # noqa: E402

__all__ = [
    "search_broll",
    "search_transcript_fts",
    "asset_allowlist",
    "find_visually_similar",
    "find_visually_similar_by_text",
    "find_similar_transcript_windows",
    "load_chunk_mean_store",
    "ChunkMeanStore",
    "SigLIPEncoder",
]


def _main() -> int:
    from .cli import main

    return main()


if __name__ == "__main__":
    raise SystemExit(_main())
