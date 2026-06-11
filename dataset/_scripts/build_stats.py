"""
Roll up root STATS.json from MANIFEST.json + light scans of asset catalogs.

Run after `build_indexes.py` so MANIFEST counts and index cardinalities stay
aligned. Uses atomic replace on write.

Usage:
  python _scripts/build_stats.py
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _atomic_write_json(path: Path, data: dict) -> None:
    text = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _ffprobe_duration_sec(rec: dict) -> float:
    fp = rec.get("ffprobe")
    if isinstance(fp, dict):
        v = fp.get("duration_sec")
        if isinstance(v, (int, float)):
            return float(v)
    return 0.0


def _filesize_bytes(rec: dict) -> int:
    v = rec.get("filesize_bytes")
    if isinstance(v, bool):
        return 0
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        return int(v)
    return 0


def _rollup_media() -> tuple[float, float, int]:
    """Returns (video_hours, audio_hours, total_size_bytes video+audio+stills)."""
    video_dir = ROOT / "assets/video"
    audio_dir = ROOT / "assets/audio"
    still_dir = ROOT / "assets/stills"
    v_sec = 0.0
    a_sec = 0.0
    size_b = 0

    for d in (video_dir, audio_dir, still_dir):
        if not d.is_dir():
            continue
        for p in d.glob("*.json"):
            try:
                rec = _load_json(p)
            except (OSError, json.JSONDecodeError):
                continue
            size_b += _filesize_bytes(rec)
            if d is video_dir:
                v_sec += _ffprobe_duration_sec(rec)
            elif d is audio_dir:
                a_sec += _ffprobe_duration_sec(rec)

    return v_sec / 3600.0, a_sec / 3600.0, size_b


def _manifest_counts(manifest: dict) -> dict[str, int]:
    out: dict[str, int] = {}
    for c in manifest.get("catalogs") or []:
        if isinstance(c, dict) and c.get("id"):
            out[str(c["id"])] = int(c.get("record_count") or 0)
    return out


def _schema_versions_block(manifest: dict, case_schema: int) -> dict[str, int]:
    """Path-keyed schema versions (STATS.json historical shape)."""
    msv = manifest.get("schema_versions") or {}
    return {
        "assets/video": int(msv.get("video", 0)),
        "assets/audio": int(msv.get("audio", 0)),
        "assets/stills": int(msv.get("still", 0)),
        "assets/transcripts": int(msv.get("transcript", 0)),
        "people/people.json": int(msv.get("people_registry", 0)),
        "organizations/orgs.json": int(msv.get("orgs_registry", 0)),
        "documents/case": case_schema,
        "documents/press/articles": int(msv.get("article", 0)),
        "documents/press/comments": int(msv.get("comment", 0)),
        "documents/press/social_posts": int(msv.get("social_post", 0)),
        "places/places.json": int(msv.get("places_registry", 0)),
    }


def _transcript_rolling_embedding_stats() -> dict[str, int | bool]:
    """Counts from `indexes/transcript_rolling_embeddings.sqlite` when present."""
    db = ROOT.parent / "indexes" / "transcript_rolling_embeddings.sqlite"
    out: dict[str, int | bool] = {"sqlite_exists": db.exists()}
    if not db.exists():
        out["window_rows"] = 0
        out["embedding_runs"] = 0
        return out
    conn = sqlite3.connect(db)
    try:
        out["window_rows"] = int(
            conn.execute("SELECT COUNT(*) FROM transcript_window_embedding").fetchone()[0]
        )
        out["embedding_runs"] = int(conn.execute("SELECT COUNT(*) FROM embedding_run").fetchone()[0])
    except sqlite3.Error:
        out["window_rows"] = 0
        out["embedding_runs"] = 0
    finally:
        conn.close()
    return out


def _case_sample_schema_version() -> int:
    # Public tree ships court filings only; default schema version is 1.
    return 1


def build_stats() -> dict:
    manifest_path = ROOT / "MANIFEST.json"
    if not manifest_path.exists():
        raise FileNotFoundError(
            "MANIFEST.json missing — run `python _scripts/build_indexes.py` first."
        )
    manifest = _load_json(manifest_path)
    counts = _manifest_counts(manifest)
    people = _load_json(ROOT / "people/people.json")
    orgs = _load_json(ROOT / "organizations/orgs.json")
    p_meta = people.get("_meta") or {}
    o_meta = orgs.get("_meta") or {}

    video_n = counts.get("video", 0)
    audio_n = counts.get("audio", 0)
    still_n = counts.get("still", 0)
    trans_n = counts.get("transcript", 0)
    v_h, a_h, size_b = _rollup_media()

    idx = manifest.get("indexes") or {}

    stats: dict = {
        "workspace_version": manifest.get("workspace_version", "2026-05-04"),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "schema_versions": _schema_versions_block(manifest, _case_sample_schema_version()),
        "registry_meta_versions": {
            "people": str(p_meta.get("registry_version") or ""),
            "orgs": str(o_meta.get("registry_version") or ""),
        },
        "totals": {
            "assets": {
                "video": video_n,
                "audio": audio_n,
                "stills": still_n,
                "all_assets": video_n + audio_n + still_n,
                "transcripts": trans_n,
                "video_runtime_hours": round(v_h, 2),
                "audio_runtime_hours": round(a_h, 2),
                "total_runtime_hours": round(v_h + a_h, 2),
                "total_size_gb": round(size_b / (1024**3), 2),
            },
            "people": {
                "total": int(p_meta.get("total_count") or len(people.get("people") or [])),
                "registry_version": str(p_meta.get("registry_version") or ""),
                "by_confidence": dict(p_meta.get("by_confidence") or {}),
            },
            "orgs": {
                "total": int(o_meta.get("total_count") or len(orgs.get("organizations") or [])),
                "registry_version": str(o_meta.get("registry_version") or ""),
                "by_confidence": dict(o_meta.get("by_confidence") or {}),
            },
            "documents_press": {
                "articles": counts.get("article", 0),
                "social_posts": counts.get("social_post", 0),
                "comments": counts.get("comment", 0),
            },
            "transcript_rolling_embeddings": _transcript_rolling_embedding_stats(),
        },
    }
    return stats


def main() -> int:
    try:
        stats = build_stats()
    except FileNotFoundError as e:
        print(str(e), file=sys.stderr)
        return 1
    _atomic_write_json(ROOT / "STATS.json", stats)
    t = stats["totals"]
    te = t.get("transcript_rolling_embeddings") or {}
    print(
        "STATS.json written.",
        f"assets video={t['assets']['video']} audio={t['assets']['audio']} "
        f"transcripts={t['assets']['transcripts']}",
        f"transcript_embed_windows={te.get('window_rows', 0)}",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
