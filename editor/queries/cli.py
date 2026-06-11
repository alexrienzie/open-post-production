"""Argparse dispatcher for editor.queries — `py editor/queries/retrieval.py <cmd>`.

Subcommands:
  broll               SQL b-roll filter by place or location-text
  search-transcript   FTS5 keyword search on segment text
  similar-chunk       SigLIP visual similarity from chunk_id or asset_id
  similar-text        SigLIP visual similarity from a text query
  similar-transcript  MiniLM text similarity over transcript windows
  build-cache         Rebuild the per-chunk mean-vector .npy cache
"""

from __future__ import annotations

import argparse
import sys
from typing import Iterable

from .filters import search_broll
from .transcript import find_similar_transcript_windows, search_transcript_fts

_ML_EXTRAS_HINT = (
    "this command needs the ML extras (pip install numpy; visual search also "
    "wants torch + the SigLIP weights). FTS and SQL commands run on stdlib alone."
)


def _trunc_text(s: str, n: int) -> str:
    s = (s or "").replace("\n", " ")
    return s if len(s) <= n else s[: n - 1] + "..."


def _print_rows(rows):
    for i, r in enumerate(rows, 1):
        parts = [f"{i:2d}."]
        for k in (
            "score",
            "asset_id",
            "chunk_id",
            "filename",
            "semantic_location",
            "semantic_subject",
            "shoot_date",
        ):
            v = r.get(k)
            if v is None:
                continue
            if isinstance(v, float):
                parts.append(f"{k}={v:.3f}")
            else:
                parts.append(f"{k}={v}")
        if "window_start_sec" in r and "window_end_sec" in r:
            parts.append(f"t={r['window_start_sec']:.1f}-{r['window_end_sec']:.1f}s")
        elif "start_sec" in r and "end_sec" in r:
            parts.append(f"t={r['start_sec']:.1f}-{r['end_sec']:.1f}s")
        if r.get("text_preview"):
            parts.append('"' + _trunc_text(r["text_preview"], 80) + '"')
        elif r.get("text"):
            parts.append('"' + _trunc_text(r["text"], 80) + '"')
        print(" ".join(parts))


def _add_filter_args(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--bucket",
        help="restrict to asset.bucket=... (e.g. in_house_other, in_house_priority_ht, third_party)",
    )
    p.add_argument(
        "--asset-type",
        help="restrict to asset.asset_type=... (e.g. b_roll, verite, recreate, timelapse, interview)",
    )
    p.add_argument("--shoot-date-from", help="YYYY-MM-DD inclusive lower bound")
    p.add_argument("--shoot-date-to", help="YYYY-MM-DD inclusive upper bound")
    p.add_argument("--place", dest="place_id", help="restrict to asset_place.pl_id=...")
    p.add_argument(
        "--person",
        dest="person_ids",
        action="append",
        default=[],
        help="restrict to person_appearance.p_id=...; repeatable",
    )
    p.add_argument(
        "--exclude-asset",
        dest="exclude_assets",
        action="append",
        default=[],
        help="exclude an asset_id from results; repeatable",
    )
    p.add_argument(
        "--camera-movement",
        help="restrict by asset_semantic_chunk.camera_movement (e.g. static, handheld, pan, mixed, pull_out)",
    )
    p.add_argument(
        "--shot-size",
        help="restrict by asset_semantic_chunk.camera_shot_size (e.g. WS, MS, CU)",
    )


def _filter_kwargs(ns: argparse.Namespace) -> dict:
    return {
        "bucket": getattr(ns, "bucket", None),
        "asset_type": getattr(ns, "asset_type", None),
        "shoot_date_from": getattr(ns, "shoot_date_from", None),
        "shoot_date_to": getattr(ns, "shoot_date_to", None),
        "place_id": getattr(ns, "place_id", None),
        "person_ids": getattr(ns, "person_ids", None) or None,
        "exclude_assets": getattr(ns, "exclude_assets", None) or None,
        "camera_movement": getattr(ns, "camera_movement", None),
        "shot_size": getattr(ns, "shot_size", None),
    }


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="editor.queries", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_b = sub.add_parser("broll", help="search b-roll by place or location text")
    p_b.add_argument("--place", dest="place_id")
    p_b.add_argument("--location-like")
    p_b.add_argument("--limit", type=int, default=30)

    p_f = sub.add_parser("search-transcript", help="FTS5 keyword search on segment text")
    p_f.add_argument("--query", required=True)
    p_f.add_argument("--limit", type=int, default=30)

    p_v = sub.add_parser("similar-chunk", help="SigLIP chunk similarity")
    p_v.add_argument("--chunk-id")
    p_v.add_argument("--asset-id")
    p_v.add_argument("--top-k", type=int, default=20)
    _add_filter_args(p_v)

    p_st = sub.add_parser("similar-text", help="SigLIP text->chunk similarity")
    p_st.add_argument("--text", required=True)
    p_st.add_argument("--top-k", type=int, default=20)
    p_st.add_argument(
        "--device",
        default="cpu",
        choices=["cpu", "cuda", "auto"],
        help="encoder device (default: cpu)",
    )
    _add_filter_args(p_st)

    p_t = sub.add_parser("similar-transcript", help="MiniLM transcript-window similarity")
    p_t.add_argument("--text", required=True)
    p_t.add_argument("--top-k", type=int, default=25)

    p_c = sub.add_parser("build-cache", help="rebuild per-chunk mean-vector cache")
    p_c.add_argument(
        "--force",
        action="store_true",
        help="force rebuild even if cache appears fresh",
    )
    p_c.add_argument("--verbose", action="store_true")

    return ap


def main(argv=None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)

    if args.cmd == "broll":
        rows = search_broll(
            place_id=args.place_id,
            location_like=args.location_like,
            limit=args.limit,
        )
    elif args.cmd == "search-transcript":
        rows = search_transcript_fts(args.query, limit=args.limit)
    elif args.cmd == "similar-chunk":
        if not args.chunk_id and not args.asset_id:
            print("Need --chunk-id or --asset-id", file=sys.stderr)
            return 2
        try:
            from .visual import find_visually_similar
        except ImportError:
            print(f"similar-chunk: {_ML_EXTRAS_HINT}", file=sys.stderr)
            return 2
        rows = find_visually_similar(
            chunk_id=args.chunk_id,
            asset_id=args.asset_id,
            top_k=args.top_k,
            **_filter_kwargs(args),
        )
    elif args.cmd == "similar-text":
        try:
            from .encoder import SigLIPEncoder
            from .visual import find_visually_similar_by_text
        except ImportError:
            print(f"similar-text: {_ML_EXTRAS_HINT}", file=sys.stderr)
            return 2

        enc = SigLIPEncoder(device=args.device)
        rows = find_visually_similar_by_text(
            args.text,
            top_k=args.top_k,
            encoder=enc,
            **_filter_kwargs(args),
        )
    elif args.cmd == "similar-transcript":
        rows = find_similar_transcript_windows(args.text, top_k=args.top_k)
    elif args.cmd == "build-cache":
        try:
            from .store import build_chunk_mean_store, load_chunk_mean_store
        except ImportError:
            print(f"build-cache: {_ML_EXTRAS_HINT}", file=sys.stderr)
            return 2
        if args.force:
            store = build_chunk_mean_store(verbose=args.verbose)
        else:
            store = load_chunk_mean_store(force_rebuild=False)
        print(f"chunk_mean_store: n={store.n} dim={store.dim}")
        return 0
    else:  # pragma: no cover
        print(f"unknown command: {args.cmd}", file=sys.stderr)
        return 2

    _print_rows(rows)
    print(f"\n({len(rows)} results)")
    return 0
