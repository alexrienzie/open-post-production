"""Shared catalog-JSON I/O for the 6 enrichment-layer builders (shots,
shot_quality, audio_quality, ocr, dense_captions, still_aesthetic).

Replaces the previous "each builder writes its own SQLite at indexes/<layer>.sqlite"
pattern. Now each builder reads/writes the per-asset catalog JSON directly
(`dataset/assets/{video,audio,stills}/{asset_id}.{kind}.json`), with one
top-level key per layer (e.g. `video.json["shots"] = {...}`).

The schema convention per layer (mirrors `migrate_indexes_to_catalog.py`):

  cat[layer_key] = {
    "processed_at": "<ISO8601>",
    "engine":       "...",       # model / detector name (optional)
    "params":       {...},       # detector params (optional)
    "items":        [{...}],     # per-row signal data (per-shot, per-frame, etc.)
    "metrics":      {...},       # per-asset scalar metrics (audio_quality, still_aesthetic)
    "processed_frames": [{...}], # ocr per-frame tracker (idempotency)
    "processed_shots":  [{...}], # dense_captions per-shot tracker
  }

The `audio_quality` block on video.json is nested under `audio_extract` (it's a
property of the extracted WAV, not the video itself).

Idempotency: builders check `is_layer_processed(asset_id, kind, layer_key)`
before processing. The check returns True iff the catalog JSON has the layer
key with a non-null `processed_at`.

Run logs: each builder calls `start_run_log(layer, args_dict)` at the top and
`finish_run_log(run_path, summary_dict)` at the bottom. Logs land at
`dataset/_runs/ingest_pipeline/<layer>/<timestamp>Z.run.json` — one file per run,
human-readable, queryable via `cat`/`jq`.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Add parent dir to path so we can import _paths.py
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _paths import VIDEO_CATALOG, AUDIO_CATALOG, STILLS_CATALOG, RUNS_DIR  # noqa: E402


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _catalog_path(asset_id: str, kind: str) -> Path:
    if kind == "video":
        return VIDEO_CATALOG / f"{asset_id}.video.json"
    if kind == "audio":
        return AUDIO_CATALOG / f"{asset_id}.audio.json"
    if kind == "still":
        return STILLS_CATALOG / f"{asset_id}.still.json"
    raise ValueError(f"unknown kind: {kind!r}")


def load_catalog(asset_id: str, kind: str) -> dict | None:
    """Load the per-asset catalog JSON. Returns None if the file doesn't exist."""
    p = _catalog_path(asset_id, kind)
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def _atomic_write(path: Path, data: dict) -> None:
    """Write JSON atomically: write to .tmp, then os.replace. Preserves trailing newline."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    text = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def update_layer(asset_id: str, kind: str, layer_key: str, block: dict) -> bool:
    """Merge a layer block into the per-asset catalog JSON. Atomic write.

    Special case: audio_quality on a video.json nests under audio_extract.

    Returns False if the catalog JSON doesn't exist (skipped); True on write.
    """
    p = _catalog_path(asset_id, kind)
    if not p.exists():
        return False
    cat = json.loads(p.read_text(encoding="utf-8"))
    if kind == "video" and layer_key == "audio_quality":
        ae = cat.get("audio_extract") or {}
        ae["audio_quality"] = block
        cat["audio_extract"] = ae
    else:
        cat[layer_key] = block
    _atomic_write(p, cat)
    return True


def is_layer_processed(asset_id: str, kind: str, layer_key: str) -> bool:
    """True if the catalog JSON has the layer key with a non-null `processed_at`."""
    cat = load_catalog(asset_id, kind)
    if cat is None:
        return False
    if kind == "video" and layer_key == "audio_quality":
        block = (cat.get("audio_extract") or {}).get("audio_quality") or {}
    else:
        block = cat.get(layer_key) or {}
    return bool(block.get("processed_at"))


def get_layer_block(asset_id: str, kind: str, layer_key: str) -> dict | None:
    """Read a layer block from the catalog JSON. Returns None if missing.

    Useful for incremental builders (OCR, dense_captions) that need to merge
    new per-frame results with existing per-frame results.
    """
    cat = load_catalog(asset_id, kind)
    if cat is None:
        return None
    if kind == "video" and layer_key == "audio_quality":
        return (cat.get("audio_extract") or {}).get("audio_quality")
    return cat.get(layer_key)


# ===== Run logs =====

def start_run_log(layer_name: str, args: dict[str, Any]) -> Path:
    """Create a per-run JSON log file at _runs/ingest_pipeline/<layer>/<timestamp>Z.run.json.

    The file holds: started_at, args, and a `summary` key populated by
    finish_run_log(). Returns the path for finish_run_log() to write to.
    """
    run_dir = RUNS_DIR / layer_name
    run_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = run_dir / f"{ts}.run.json"
    payload = {
        "layer": layer_name,
        "started_at": now_iso(),
        "args": args,
        "summary": None,
    }
    _atomic_write(path, payload)
    return path


def finish_run_log(run_path: Path, summary: dict[str, Any]) -> None:
    """Write the final summary into the run log file (started by start_run_log)."""
    if not run_path.exists():
        return
    payload = json.loads(run_path.read_text(encoding="utf-8"))
    payload["finished_at"] = now_iso()
    payload["summary"] = summary
    _atomic_write(run_path, payload)
