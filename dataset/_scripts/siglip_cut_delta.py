#!/usr/bin/env python3
"""siglip_cut_delta.py — SigLIP cosine-delta at cut boundaries.

A dataset-side primitive. Given two `(asset_id, timestamp_sec)` points
(outgoing cut frame, incoming cut frame), look up the nearest SigLIP keyframe
on each side and return the cosine distance between the two 1152-d vectors.

Low cosine distance ≈ visually continuous (e.g. two adjacent shots in the same
location, similar framing) → a "clean" cut by visual flow.
High cosine distance ≈ abrupt visual change → a "hard" cut.

Editorial use: pair with `sidecar_cut_eval.py` (editor side, out of scope here)
to give each annotation in a sidecar a `siglip_delta` score alongside the
existing `mid_word_in/out` flags. Editors can then surface cuts that are both
mid-word AND visually jarring as highest-priority review items.

Reuses:
  - `indexes/clip_embeddings.faiss` — 1152-d SigLIP vectors over keyframes
  - `indexes/clip_embeddings.faiss.meta.json` — position → (asset, abs_time_sec)
  - The FAISS-meta `abs_time_sec` field already accounts for chunked-asset
    `chunk_start_sec` offsets, so we don't need to re-join `semantic_chunks`.

Subcommands (smoke / debug):
  probe <out_asset>:<out_sec> <in_asset>:<in_sec>   — single cut, JSON result
  nearest <asset>:<sec>                              — nearest keyframe lookup
  status                                             — index counts + coverage

Python API:
  from siglip_cut_delta import SigLIPCutIndex
  idx = SigLIPCutIndex.load_default()
  out = idx.compute_cut_delta(asset_out, ts_out_sec, asset_in, ts_in_sec)
"""
from __future__ import annotations

import argparse
import bisect
import json
import math
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _paths import CLIP_FAISS_INDEX, CLIP_FAISS_META  # noqa: E402


@dataclass(frozen=True)
class Keyframe:
    asset_id: str
    abs_time_sec: float
    faiss_position: int   # row index into the FAISS HNSW index
    embedding_pk: int     # FK into clip_and_still_embeddings.sqlite::clip_embeddings
    chunk_id: str
    frame_idx: int


class SigLIPCutIndex:
    """Per-asset bisect-indexed keyframe lookup over the FAISS meta map, plus
    lazy vector reconstruction from the FAISS index file.

    Loaded once per process; ~36 MB of metadata in RAM + ~566 MB FAISS index
    mmap'd (FAISS handles the mmap internally on read_index)."""

    def __init__(self, meta_path: Path, index_path: Path):
        import faiss  # local import — heavy
        self._index = faiss.read_index(str(index_path))
        meta = json.loads(meta_path.read_text())
        positions = meta.get("position_to_meta", [])
        # Build per-asset sorted lists of (abs_time_sec, Keyframe).
        per_asset: dict[str, list[Keyframe]] = {}
        for pos, m in enumerate(positions):
            aid = m["parent_asset_id"]
            kf = Keyframe(
                asset_id=aid,
                abs_time_sec=float(m["abs_time_sec"]),
                faiss_position=pos,
                embedding_pk=int(m["embedding_pk"]),
                chunk_id=str(m["chunk_id"]),
                frame_idx=int(m["frame_idx"]),
            )
            per_asset.setdefault(aid, []).append(kf)
        # Sort each asset's keyframes by time
        for aid in per_asset:
            per_asset[aid].sort(key=lambda kf: kf.abs_time_sec)
        self._by_asset = per_asset
        # Parallel sorted-time array per asset for bisect lookup
        self._time_arr = {
            aid: [kf.abs_time_sec for kf in kfs]
            for aid, kfs in per_asset.items()
        }
        self.n_assets = len(per_asset)
        self.n_keyframes = len(positions)

    # ------------------------------------------- factory

    @classmethod
    def load_default(cls) -> "SigLIPCutIndex":
        return cls(CLIP_FAISS_META, CLIP_FAISS_INDEX)

    # ------------------------------------------- lookups

    def nearest_keyframe(self, asset_id: str, ts_sec: float) -> Keyframe | None:
        """Binary-search the per-asset sorted keyframe list. Returns the
        keyframe closest in time, regardless of side. Returns None if the
        asset has no SigLIP coverage."""
        kfs = self._by_asset.get(asset_id)
        if not kfs:
            return None
        times = self._time_arr[asset_id]
        # bisect_left gives the insertion point; check both neighbors
        i = bisect.bisect_left(times, ts_sec)
        candidates: list[Keyframe] = []
        if i < len(kfs):
            candidates.append(kfs[i])
        if i > 0:
            candidates.append(kfs[i - 1])
        return min(candidates, key=lambda kf: abs(kf.abs_time_sec - ts_sec))

    def vector_at(self, position: int) -> np.ndarray:
        """Reconstruct the L2-normalized 1152-d vector at FAISS position."""
        v = self._index.reconstruct(int(position))
        return np.asarray(v, dtype=np.float32)

    # ------------------------------------------- main API

    def compute_cut_delta(
        self,
        asset_out: str, ts_out_sec: float,
        asset_in: str, ts_in_sec: float,
    ) -> dict:
        """Cosine distance between nearest keyframe on each side of a cut.

        Returns a dict shaped for direct insertion into a sidecar annotation:
          {
            "ok": True,
            "cosine_similarity": 0.91,   # -1..1, 1=identical
            "cosine_distance": 0.09,     # 1 - cos
            "out": {"asset_id", "abs_time_sec", "offset_sec", "embedding_pk"},
            "in":  {"asset_id", "abs_time_sec", "offset_sec", "embedding_pk"},
            "same_asset": True/False,
            "interpretation": "clean" | "soft" | "hard"  (informational)
          }
        Sets ok=False (with a `reason` field) if either side has no keyframe.
        """
        kf_out = self.nearest_keyframe(asset_out, ts_out_sec)
        kf_in = self.nearest_keyframe(asset_in, ts_in_sec)
        if kf_out is None or kf_in is None:
            return {
                "ok": False,
                "reason": (
                    "no_siglip_coverage_out" if kf_out is None
                    else "no_siglip_coverage_in"
                ),
                "out_asset": asset_out, "out_ts_sec": ts_out_sec,
                "in_asset": asset_in, "in_ts_sec": ts_in_sec,
            }
        v_out = self.vector_at(kf_out.faiss_position)
        v_in = self.vector_at(kf_in.faiss_position)
        # Vectors are L2-normalized at write time, but re-normalize defensively
        # in case FAISS reconstruction drifted (HNSW does not).
        n_out = np.linalg.norm(v_out) or 1.0
        n_in = np.linalg.norm(v_in) or 1.0
        cos = float(np.dot(v_out / n_out, v_in / n_in))
        cos = max(-1.0, min(1.0, cos))
        dist = 1.0 - cos
        # Coarse interpretation buckets — calibrated against the smoke run
        # below. Editors can re-bin.
        if cos >= 0.85:
            interp = "clean"     # near-identical framing; rare in cuts, common in same-take ramps
        elif cos >= 0.55:
            interp = "soft"      # related shots; same location or subject
        else:
            interp = "hard"      # abrupt change; cross-shoot, different framing/subject
        return {
            "ok": True,
            "cosine_similarity": cos,
            "cosine_distance": dist,
            "out": {
                "asset_id": kf_out.asset_id,
                "abs_time_sec": kf_out.abs_time_sec,
                "offset_sec": abs(kf_out.abs_time_sec - ts_out_sec),
                "embedding_pk": kf_out.embedding_pk,
            },
            "in": {
                "asset_id": kf_in.asset_id,
                "abs_time_sec": kf_in.abs_time_sec,
                "offset_sec": abs(kf_in.abs_time_sec - ts_in_sec),
                "embedding_pk": kf_in.embedding_pk,
            },
            "same_asset": asset_out == asset_in,
            "interpretation": interp,
        }

    # ------------------------------------------- bulk

    def compute_many(self, cuts: Iterable[dict]) -> list[dict]:
        """Convenience: apply compute_cut_delta over a sequence of
        {asset_out, ts_out_sec, asset_in, ts_in_sec, ...} dicts. Echoes extra
        fields back into each result for caller correlation (e.g. clip_id)."""
        out: list[dict] = []
        for c in cuts:
            r = self.compute_cut_delta(
                c["asset_out"], float(c["ts_out_sec"]),
                c["asset_in"], float(c["ts_in_sec"]),
            )
            # Pass through any caller-supplied keys we don't own
            for k in c:
                if k not in ("asset_out", "ts_out_sec", "asset_in", "ts_in_sec"):
                    r[k] = c[k]
            out.append(r)
        return out


# ----------------------------------------------- CLI


def _parse_asset_ts(token: str) -> tuple[str, float]:
    """Parse `<asset_id>:<float_seconds>` (asset_id is sha256 hex, no colons)."""
    if ":" not in token:
        raise ValueError(f"expected '<asset_id>:<sec>', got: {token!r}")
    aid, sec = token.split(":", 1)
    return aid.strip(), float(sec)


def cmd_probe(args: argparse.Namespace) -> None:
    asset_out, ts_out = _parse_asset_ts(args.out)
    asset_in, ts_in = _parse_asset_ts(args.inn)
    idx = SigLIPCutIndex.load_default()
    print(f"# loaded index: {idx.n_assets:,} assets, {idx.n_keyframes:,} keyframes")
    res = idx.compute_cut_delta(asset_out, ts_out, asset_in, ts_in)
    print(json.dumps(res, indent=2))


def cmd_nearest(args: argparse.Namespace) -> None:
    asset, ts = _parse_asset_ts(args.point)
    idx = SigLIPCutIndex.load_default()
    kf = idx.nearest_keyframe(asset, ts)
    if kf is None:
        print(json.dumps({"ok": False, "reason": "no_siglip_coverage"}))
        return
    print(json.dumps({
        "ok": True,
        "asset_id": kf.asset_id,
        "requested_sec": ts,
        "abs_time_sec": kf.abs_time_sec,
        "offset_sec": abs(kf.abs_time_sec - ts),
        "faiss_position": kf.faiss_position,
        "embedding_pk": kf.embedding_pk,
        "chunk_id": kf.chunk_id,
        "frame_idx": kf.frame_idx,
    }, indent=2))


def cmd_status(args: argparse.Namespace) -> None:
    print(f"=== siglip_cut_delta status ===")
    print(f"  faiss index: {CLIP_FAISS_INDEX}")
    print(f"  faiss meta:  {CLIP_FAISS_META}")
    if not CLIP_FAISS_INDEX.exists() or not CLIP_FAISS_META.exists():
        print("  (index or meta missing — build via dataset/_scripts/faiss_index/build_faiss.py)")
        return
    idx = SigLIPCutIndex.load_default()
    print(f"  loaded: {idx.n_assets:,} assets, {idx.n_keyframes:,} keyframes")
    # Show coverage distribution
    sizes = [len(v) for v in idx._by_asset.values()]
    sizes.sort()
    print(f"  per-asset keyframes — min: {sizes[0]}  median: {sizes[len(sizes)//2]}  "
          f"max: {sizes[-1]}  mean: {sum(sizes)/len(sizes):.1f}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("probe", help="single cut: print cosine distance + metadata")
    sp.add_argument("out", help="outgoing point as '<asset_id>:<sec>'")
    sp.add_argument("inn", metavar="in", help="incoming point as '<asset_id>:<sec>'")
    sp.set_defaults(func=cmd_probe)

    sp = sub.add_parser("nearest", help="find nearest SigLIP keyframe to a point")
    sp.add_argument("point", help="'<asset_id>:<sec>'")
    sp.set_defaults(func=cmd_nearest)

    sp = sub.add_parser("status", help="index coverage stats")
    sp.set_defaults(func=cmd_status)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
