"""editor.queries — read-only editorial queries over indexes/ and dataset/.

Public API (see queries_README.md for full contract):

    from editor.queries import (
        # SQL filters (single-layer)
        search_broll,
        search_transcript_fts,
        asset_allowlist,

        # Vector similarity (single-layer)
        find_visually_similar,
        find_visually_similar_by_text,
        find_similar_transcript_windows,

        # Multi-layer composition
        find_soundbites_with_face,
        find_broll_with_quality,
        find_dense_caption_matches,
        find_funny_moments_on_camera,
        find_bib_appearances,
        find_quotes_about_topic,

        # Cache / encoder
        load_chunk_mean_store,
        SigLIPEncoder,
    )
"""

from .compose import (
    find_bib_appearances,
    find_broll_v2,
    find_broll_with_quality,
    find_dense_caption_matches,
    find_funny_moments_on_camera,
    find_quotes_about_topic,
    find_soundbites_with_face,
)
from .encoder import SigLIPEncoder
from .filters import asset_allowlist, search_broll
from .store import ChunkMeanStore, load_chunk_mean_store
from .transcript import find_similar_transcript_windows, search_transcript_fts
from .usage import (
    annotate_usage,
    filter_unused,
    find_act_exports,
    is_used,
    used_assets,
)
from .visual import find_visually_similar, find_visually_similar_by_text

__all__ = [
    "SigLIPEncoder",
    "ChunkMeanStore",
    "asset_allowlist",
    "search_broll",
    "search_transcript_fts",
    "find_visually_similar",
    "find_visually_similar_by_text",
    "find_similar_transcript_windows",
    "load_chunk_mean_store",
    # Multi-layer composition
    "find_soundbites_with_face",
    "find_broll_with_quality",
    "find_broll_v2",
    "find_dense_caption_matches",
    "find_funny_moments_on_camera",
    "find_bib_appearances",
    "find_quotes_about_topic",
    # Cross-Act "already-used" dedup
    "used_assets",
    "is_used",
    "filter_unused",
    "annotate_usage",
    "find_act_exports",
]
