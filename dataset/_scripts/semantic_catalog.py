"""Shared helpers: Gemini semantics → catalog `asset_semantic_summary`."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

CATALOG_VIDEO = "assets/video"
CATALOG_AUDIO = "assets/audio"
CATALOG_STILLS = "assets/stills"

GEMINI_CHUNK_FIELDS = (
    "subject",
    "action",
    "setting",
    "camera",
    "audio_character",
    "emotional_tone",
    "editorial_notes",
    "key_moments",
)


def _pick(d: dict | None, key: str) -> Any:
    if not isinstance(d, dict):
        return None
    v = d.get(key)
    if v in ("", None, [], {}):
        return None
    return v


def chunk_from_gemini_response(
    *,
    chunk_id: str,
    chunk_idx: int,
    start_sec: float | None,
    end_sec: float | None,
    model: str | None,
    response_json: str | None,
) -> dict | None:
    if not response_json:
        return None
    try:
        sem = json.loads(response_json)
    except Exception:
        return None
    if not isinstance(sem, dict):
        return None
    out: dict[str, Any] = {
        "chunk_id": chunk_id,
        "chunk_idx": chunk_idx,
        "start_sec": start_sec,
        "end_sec": end_sec,
    }
    if model:
        out["model"] = model
    for key in GEMINI_CHUNK_FIELDS:
        v = _pick(sem, key)
        if v is not None:
            out[key] = v
    if len(out) <= 4:
        return None
    return out


def build_asset_semantic_summary(
    chunks: list[dict],
    *,
    source: str = "clip_and_still_embeddings.sqlite",
    extracted_at: str | None = None,
) -> dict | None:
    if not chunks:
        return None
    models = sorted({c["model"] for c in chunks if c.get("model")})
    summary: dict[str, Any] = {
        "source": source,
        "extracted_at": extracted_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "chunks": chunks,
    }
    if len(models) == 1:
        summary["model"] = models[0]
    elif models:
        summary["models"] = models
    return summary


def semantic_projection_from_record(record: dict) -> tuple[str | None, str | None, str | None]:
    """First-chunk headline fields for SQLite / filters."""
    sm = record.get("asset_semantic_summary")
    if not isinstance(sm, dict):
        return None, None, None
    chunks = sm.get("chunks")
    if not isinstance(chunks, list) or not chunks:
        return None, None, None
    c0 = chunks[0] if isinstance(chunks[0], dict) else {}
    setting = c0.get("setting")
    loc = setting.get("location") if isinstance(setting, dict) else None
    subject = c0.get("subject") if isinstance(c0.get("subject"), str) else None
    notes = c0.get("editorial_notes") if isinstance(c0.get("editorial_notes"), str) else None
    return loc, subject, notes


def summaries_equal(a: dict | None, b: dict | None) -> bool:
    return json.dumps(a, sort_keys=True, ensure_ascii=False) == json.dumps(
        b, sort_keys=True, ensure_ascii=False
    )


def _setting_field(setting: Any, key: str) -> str | None:
    if not isinstance(setting, dict):
        return None
    v = setting.get(key)
    return v if isinstance(v, str) and v.strip() else None


def _camera_field(camera: Any, key: str) -> str | None:
    if not isinstance(camera, dict):
        return None
    v = camera.get(key)
    return v if isinstance(v, str) and v.strip() else None


def chunk_sql_row(asset_id: str, chunk: dict) -> tuple | None:
    """Row tuple for `asset_semantic_chunk` INSERT."""
    chunk_id = chunk.get("chunk_id")
    if not chunk_id:
        return None
    setting = chunk.get("setting")
    camera = chunk.get("camera")
    return (
        asset_id,
        chunk_id,
        int(chunk.get("chunk_idx") or 0),
        chunk.get("start_sec"),
        chunk.get("end_sec"),
        chunk.get("model"),
        chunk.get("subject") if isinstance(chunk.get("subject"), str) else None,
        chunk.get("action") if isinstance(chunk.get("action"), str) else None,
        _setting_field(setting, "location"),
        _setting_field(setting, "time_of_day"),
        _setting_field(setting, "weather"),
        _camera_field(camera, "shot_size"),
        _camera_field(camera, "movement"),
        _camera_field(camera, "perspective"),
        chunk.get("audio_character") if isinstance(chunk.get("audio_character"), str) else None,
        chunk.get("emotional_tone") if isinstance(chunk.get("emotional_tone"), str) else None,
        chunk.get("editorial_notes") if isinstance(chunk.get("editorial_notes"), str) else None,
    )


def key_moment_sql_rows(asset_id: str, chunk: dict) -> list[tuple]:
    """Rows for `asset_semantic_key_moment` INSERT."""
    chunk_id = chunk.get("chunk_id")
    if not chunk_id:
        return []
    moments = chunk.get("key_moments")
    if not isinstance(moments, list):
        return []
    out: list[tuple] = []
    for idx, m in enumerate(moments):
        if not isinstance(m, dict):
            continue
        ts = m.get("timestamp_sec")
        if ts is None:
            continue
        desc = m.get("description")
        out.append(
            (
                asset_id,
                chunk_id,
                idx,
                float(ts),
                desc if isinstance(desc, str) else None,
            )
        )
    return out


def iter_semantic_from_record(asset_id: str, record: dict) -> tuple[list[tuple], list[tuple]]:
    """Return (chunk_rows, key_moment_rows) for SQLite load."""
    sm = record.get("asset_semantic_summary")
    if not isinstance(sm, dict):
        return [], []
    chunks = sm.get("chunks")
    if not isinstance(chunks, list):
        return [], []
    chunk_rows: list[tuple] = []
    km_rows: list[tuple] = []
    for ch in chunks:
        if not isinstance(ch, dict):
            continue
        row = chunk_sql_row(asset_id, ch)
        if row:
            chunk_rows.append(row)
            km_rows.extend(key_moment_sql_rows(asset_id, ch))
    return chunk_rows, km_rows


def chunks_for_overlap_lookup(record: dict) -> list[dict]:
    """Normalized chunk list for editor overlap helpers (catalog JSON)."""
    sm = record.get("asset_semantic_summary")
    if not isinstance(sm, dict):
        return []
    chunks = sm.get("chunks")
    if not isinstance(chunks, list):
        return []
    out: list[dict] = []
    for ch in chunks:
        if not isinstance(ch, dict):
            continue
        out.append({
            "chunk_idx": int(ch.get("chunk_idx") or 0),
            "chunk_start_sec": float(ch["start_sec"]) if ch.get("start_sec") is not None else 0.0,
            "chunk_end_sec": float(ch["end_sec"]) if ch.get("end_sec") is not None else 0.0,
            "subject": ch.get("subject"),
            "action": ch.get("action"),
            "setting": ch.get("setting"),
            "camera": ch.get("camera"),
            "audio_character": ch.get("audio_character"),
            "emotional_tone": ch.get("emotional_tone"),
            "editorial_notes": ch.get("editorial_notes"),
            "key_moments": ch.get("key_moments"),
        })
    return out


def key_moments_in_window(
    chunk: dict,
    window_in: float,
    window_out: float,
    *,
    pad_sec: float = 3.0,
) -> list[dict]:
    """`key_moments` entries whose timestamp falls in [window_in, window_out]."""
    moments = chunk.get("key_moments")
    if not isinstance(moments, list):
        return []
    out: list[dict] = []
    for m in moments:
        if not isinstance(m, dict):
            continue
        ts = m.get("timestamp_sec")
        if ts is None:
            continue
        t = float(ts)
        if window_in <= t <= window_out:
            out.append({
                "timestamp_sec": t,
                "description": m.get("description"),
            })
    return out


def suggest_span_from_key_moments(
    chunk: dict,
    window_in: float,
    window_out: float,
    *,
    pad_before: float = 2.0,
    pad_after: float = 8.0,
) -> dict | None:
    """Pick best key_moment inside the clip window and return a suggested sub-span."""
    in_window = key_moments_in_window(chunk, window_in, window_out)
    if not in_window:
        return None
    pick = in_window[0]
    t = float(pick["timestamp_sec"])
    chunk_end = float(chunk.get("chunk_end_sec") or window_out)
    start = max(window_in, t - pad_before)
    end = min(window_out, chunk_end, t + pad_after)
    if end <= start:
        end = min(window_out, t + pad_after)
    return {
        "start_sec": round(start, 3),
        "end_sec": round(end, 3),
        "anchor_timestamp_sec": t,
        "description": pick.get("description"),
        "reason": "key_moment_in_clip_window",
    }


def apply_gemini_annotation_fields(
    target: dict,
    chunk: dict | None,
    *,
    window_in: float | None = None,
    window_out: float | None = None,
) -> None:
    """Write rich Gemini fields onto a sidecar annotation or beat clip dict."""
    if not chunk:
        return
    if chunk.get("subject"):
        target["chunk_subject"] = chunk["subject"]
    if chunk.get("action"):
        target["chunk_action"] = chunk["action"]
    notes = chunk.get("editorial_notes")
    if isinstance(notes, str) and notes.strip():
        target["chunk_editorial_notes"] = notes
    tone = chunk.get("emotional_tone")
    if isinstance(tone, str) and tone.strip():
        target["chunk_emotional_tone"] = tone
    audio = chunk.get("audio_character")
    if isinstance(audio, str) and audio.strip():
        target["chunk_audio_character"] = audio
    setting = chunk.get("setting")
    if isinstance(setting, dict) and setting:
        target["chunk_setting"] = setting
        loc = setting.get("location")
        if isinstance(loc, str) and loc.strip():
            target["chunk_setting_location"] = loc
    camera = chunk.get("camera")
    if isinstance(camera, dict) and camera:
        target["chunk_camera"] = camera

    if window_in is not None and window_out is not None:
        km = key_moments_in_window(chunk, window_in, window_out)
        if km:
            target["chunk_key_moments"] = km
        span = suggest_span_from_key_moments(chunk, window_in, window_out)
        if span:
            target["chunk_suggested_span"] = span
    else:
        moments = chunk.get("key_moments")
        if isinstance(moments, list) and moments:
            target["chunk_key_moments"] = moments


def still_subject_action(record: dict) -> tuple[str | None, str | None]:
    chunks = chunks_for_overlap_lookup(record)
    if not chunks:
        return None, None
    c0 = chunks[0]
    subj = c0.get("subject") if isinstance(c0.get("subject"), str) else None
    act = c0.get("action") if isinstance(c0.get("action"), str) else None
    return subj, act


def catalog_media_path(dataset_root: Path, asset_id: str) -> Path | None:
    """Resolve video / audio / still catalog JSON for an asset_id."""
    for sub, suffix in (
        (CATALOG_VIDEO, ".video.json"),
        (CATALOG_AUDIO, ".audio.json"),
        (CATALOG_STILLS, ".still.json"),
    ):
        p = dataset_root / sub / f"{asset_id}{suffix}"
        if p.exists():
            return p
    return None


def read_catalog_record(dataset_root: Path, asset_id: str) -> dict | None:
    path = catalog_media_path(dataset_root, asset_id)
    if path is None:
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def load_chunks_by_asset_ids(dataset_root: Path, asset_ids: set[str]) -> dict[str, list[dict]]:
    """asset_id → overlap-ready chunk dicts from catalog `asset_semantic_summary`."""
    out: dict[str, list[dict]] = {}
    for aid in asset_ids:
        rec = read_catalog_record(dataset_root, aid)
        if not rec:
            continue
        chunks = chunks_for_overlap_lookup(rec)
        if chunks:
            out[aid] = chunks
    return out


def chunk_to_response_json(ch: dict) -> str | None:
    """Serialize catalog chunk fields to a Gemini-shaped JSON string."""
    if not isinstance(ch, dict):
        return None
    payload = {k: ch[k] for k in GEMINI_CHUNK_FIELDS if k in ch}
    if not payload:
        return None
    return json.dumps(payload, ensure_ascii=False)


def fetch_chunk_rows_from_catalog(
    dataset_root: Path, asset_ids: list[str]
) -> dict[str, list[dict]]:
    """Crosscheck-compatible rows: chunk_idx, times, response_json."""
    out: dict[str, list[dict]] = {}
    for aid in asset_ids:
        rec = read_catalog_record(dataset_root, aid)
        if not rec:
            continue
        sm = rec.get("asset_semantic_summary")
        if not isinstance(sm, dict):
            continue
        chunks = sm.get("chunks")
        if not isinstance(chunks, list):
            continue
        rows: list[dict] = []
        for ch in chunks:
            if not isinstance(ch, dict):
                continue
            js = chunk_to_response_json(ch)
            if not js:
                continue
            rows.append({
                "chunk_idx": int(ch.get("chunk_idx") or 0),
                "chunk_start_sec": ch.get("start_sec"),
                "chunk_end_sec": ch.get("end_sec"),
                "response_json": js,
            })
        if rows:
            rows.sort(key=lambda r: r["chunk_idx"])
            out[aid] = rows
    return out


def load_overlap_chunks_all_video_still(dataset_root: Path) -> dict[str, list[dict]]:
    """Load semantics for every catalog row that has `asset_semantic_summary`."""
    out: dict[str, list[dict]] = {}
    for sub in (CATALOG_VIDEO, CATALOG_AUDIO, CATALOG_STILLS):
        d = dataset_root / sub
        if not d.exists():
            continue
        for p in d.glob("*.json"):
            try:
                rec = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                continue
            aid = rec.get("asset_id") or p.stem.split(".")[0]
            chunks = chunks_for_overlap_lookup(rec)
            if chunks:
                out[aid] = chunks
    return out
