"""Shared catalog link graph for human clip manifest assets (video/audio JSON edges)."""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from _lib.linked_assets import neighbor_target_ids  # noqa: E402
VIDEO_DIR = ROOT / "assets" / "catalog" / "video"
AUDIO_DIR = ROOT / "assets" / "catalog" / "audio"
CLIP_MANIFEST = ROOT / "assets" / "catalog" / "human_transcripts" / "clip_segments_manifest.jsonl"

_json_cache: dict[Path, dict | None] = {}


def load_manifest_asset_ids() -> set[str]:
    out: set[str] = set()
    if not CLIP_MANIFEST.exists():
        return out
    for line in CLIP_MANIFEST.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            o = json.loads(line)
        except json.JSONDecodeError:
            continue
        aid = o.get("asset_id")
        if isinstance(aid, str) and aid:
            out.add(aid)
    return out


def catalog_kind(aid: str) -> str | None:
    v = (VIDEO_DIR / f"{aid}.video.json").is_file()
    a = (AUDIO_DIR / f"{aid}.audio.json").is_file()
    if v and a:
        return "both"
    if v:
        return "video"
    if a:
        return "audio"
    return None


def _load_catalog(path: Path) -> dict | None:
    if path in _json_cache:
        return _json_cache[path]
    try:
        _json_cache[path] = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        _json_cache[path] = None
    return _json_cache[path]


def linked_neighbors(aid: str) -> set[str]:
    n: set[str] = set()
    vp = VIDEO_DIR / f"{aid}.video.json"
    ap = AUDIO_DIR / f"{aid}.audio.json"
    if vp.is_file():
        v = _load_catalog(vp)
        if v:
            n |= neighbor_target_ids(v)
    if ap.is_file():
        a = _load_catalog(ap)
        if a:
            n |= neighbor_target_ids(a)
    return n


def is_symmetric_neighbor(a: str, b: str) -> bool:
    return b in linked_neighbors(a) and a in linked_neighbors(b)


def component_from_seed(seed: str) -> set[str]:
    comp: set[str] = set()
    stack = [seed]
    seen: set[str] = set()
    while stack:
        aid = stack.pop()
        if aid in seen:
            continue
        seen.add(aid)
        if catalog_kind(aid):
            comp.add(aid)
        for nb in linked_neighbors(aid):
            if nb not in seen:
                stack.append(nb)
    return comp


def discover_components(manifest_ids: set[str]) -> list[set[str]]:
    covered: set[str] = set()
    out: list[set[str]] = []
    for seed in sorted(manifest_ids):
        if seed in covered:
            continue
        c = component_from_seed(seed)
        covered |= c
        out.append(c)
    return out


def manifest_rows_by_asset() -> dict[str, list[dict[str, Any]]]:
    by: dict[str, list[dict[str, Any]]] = defaultdict(list)
    if not CLIP_MANIFEST.exists():
        return {}
    for line in CLIP_MANIFEST.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            o = json.loads(line)
        except json.JSONDecodeError:
            continue
        aid = o.get("asset_id")
        if isinstance(aid, str) and aid:
            by[aid].append(o)
    return dict(by)
