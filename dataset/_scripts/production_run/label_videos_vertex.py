#!/usr/bin/env python3
"""Gemini 2.5 Pro production pass via Vertex AI (Gemini Enterprise Agent
Platform). Same DB and descriptions as the AI Studio production script —
this just swaps the API path to bypass the AI Studio Tier 1 1000-RPD cap.

Per-chunk flow:
  1. upload local proxy file to GCS bucket (gs://your-gcs-bucket-...)
  2. call generate_content via Vertex with the gs:// URI
  3. delete the GCS blob (lifecycle rule on bucket also catches stragglers)

Auth: Application Default Credentials. Run `gcloud auth application-default
login` once before invoking. No API key.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import sqlite3
import struct
import subprocess
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import closing
from pathlib import Path
from typing import Any, Iterable, List

from pydantic import BaseModel

# --------------------------------------------------------------------------- paths

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from _paths import (
    EMBEDDINGS_DB, DERIVATIVE_MEDIA, PROXY_CHUNKS_DIR, RUNS_DIR,
    resolve_proxy_via_asset_map,
)

SCRIPT_DIR = Path(__file__).resolve().parent
DATA_DIR = RUNS_DIR / "production_run"
DB_PATH = EMBEDDINGS_DB
DESCRIPTIONS_DIR = DATA_DIR / "descriptions_production"
LOG_PATH = DATA_DIR / "pilot_production.log"
FINDINGS_PATH = DATA_DIR / "findings_production.md"
PRODUCTION_MANIFEST = DATA_DIR / "production_manifest.json"

LONG_CHUNKS_DIR = PROXY_CHUNKS_DIR
FFMPEG = "/opt/homebrew/bin/ffmpeg"
FFPROBE = "/opt/homebrew/bin/ffprobe"


def _proxy_for(asset_id: str) -> Path | None:
    """Resolve catalog asset_id to its on-disk proxy via asset_map.json."""
    return resolve_proxy_via_asset_map(asset_id)

# Long clips are ffmpeg-stream-copied to ~50-min segments. Stays under Gemini's
# 1-hour default-resolution upload cap with margin.
CHUNK_SEGMENT_SEC = 50 * 60

# --------------------------------------------------------------------------- config

GEMINI_MODEL = "gemini-2.5-pro"
GCP_PROJECT = os.environ.get("GCP_PROJECT", "<your-gcp-project-id>")
VERTEX_LOCATION = "us-central1"
GCS_BUCKET = "your-gcs-bucket"

# Verified 2026-05-05 from https://ai.google.dev/gemini-api/docs/pricing
GEMINI_PRICE_TIER_THRESHOLD = 200_000
GEMINI_INPUT_LO = 1.25 / 1_000_000   # USD per token, prompt <= 200K
GEMINI_INPUT_HI = 2.50 / 1_000_000   # prompt > 200K
GEMINI_OUTPUT_LO = 10.00 / 1_000_000
GEMINI_OUTPUT_HI = 15.00 / 1_000_000

PEGASUS_PROMPT = """You are analyzing a video clip from a documentary feature about ultrarunning and mountaineering in Grand Teton National Park, Wyoming. The film follows athletes preparing for and attempting fastest-known-time records on technical alpine routes. Footage includes interviews, verite athlete training and racing, B-roll landscapes, drone aerials, timelapses, and casual phone-call recordings.

Analyze this clip and output ONLY valid JSON, no markdown fences, no commentary, matching this exact schema:

{
  "subject": "Who or what is on screen. Name people if recognizable from context. Use 'unknown person' or 'multiple people' if ambiguous. For non-people, describe the subject (e.g., 'Grand Teton ridgeline at sunrise', 'a runner's hands taping shoes').",
  "action": "Chronological description of what happens in the clip, 2-4 sentences. Be specific about physical actions, dialogue topics, and visual events. Avoid generic phrases like 'a person walks'.",
  "setting": {
    "location": "Interior or exterior. If exterior and recognizable, name the location (mountains, ridge, summit, trail, town). If interior, describe the space (living room, vehicle, restaurant).",
    "time_of_day": "One of: golden_hour | blue_hour | midday | overcast | night | indoor | unknown",
    "weather": "One of: clear | overcast | snow | rain | mixed | indoor_na"
  },
  "camera": {
    "shot_size": "One of: ECU | CU | MCU | MS | MWS | WS | EWS | aerial | mixed",
    "movement": "One of: static | handheld | gimbal | drone | pan | tilt | dolly | whip | push_in | pull_out | mixed",
    "perspective": "One of: eye_level | low_angle | high_angle | overhead | POV"
  },
  "audio_character": "If dialogue, summarize the conversation topic in 1 sentence. If sync sound, describe (footsteps, wind, breathing). If silent or music-only, note that.",
  "emotional_tone": "1-3 words capturing energy and mood (e.g., 'contemplative', 'high-energy action', 'tense argument', 'meditative landscape').",
  "editorial_notes": "1-2 sentences about editorial value. Is this opening-worthy, transitional, beat-driven, B-roll filler, etc.? What kind of cut would this clip slot into?",
  "key_moments": [
    {"timestamp_sec": 12.5, "description": "1-sentence description of a cuttable beat"}
  ]
}

For key_moments, identify 3-7 distinct beats spread across the clip's duration. Timestamps must be within the actual clip duration. For very short clips (<30s), 1-2 moments is sufficient. For long clips (interviews, calls), aim for 6-10 moments highlighting topic shifts and emotional inflection points.

Output ONLY the JSON object."""

# Pydantic models enforce response shape via google-genai's response_schema.
# Field types mirror the Pegasus prompt; values stay as strings so Gemini can
# return any of the pipe-separated options without rejection.
class Setting(BaseModel):
    location: str
    time_of_day: str
    weather: str


class Camera(BaseModel):
    shot_size: str
    movement: str
    perspective: str


class KeyMoment(BaseModel):
    timestamp_sec: float
    description: str


class ClipDescription(BaseModel):
    subject: str
    action: str
    setting: Setting
    camera: Camera
    audio_character: str
    emotional_tone: str
    editorial_notes: str
    key_moments: List[KeyMoment]


# `from __future__ import annotations` turns these into forward references;
# rebuild so google-genai gets a fully resolved schema at call time.
ClipDescription.model_rebuild()

# --------------------------------------------------------------------------- schema

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS semantic_chunks (
    chunk_id              TEXT PRIMARY KEY,
    parent_asset_id       TEXT NOT NULL,
    chunk_idx             INTEGER NOT NULL,
    is_chunked            INTEGER NOT NULL,
    bucket                TEXT,
    duration_sec          REAL,
    upload_path           TEXT NOT NULL,
    chunk_start_sec       REAL,
    chunk_end_sec         REAL,
    camera_id             TEXT,
    shoot_label           TEXT,
    category_name         TEXT,
    shoot_date            TEXT,
    model          TEXT,
    gemini_file_id        TEXT,
    response_raw   TEXT,
    response_json  TEXT,
    gemini_input_tokens   INTEGER,
    gemini_output_tokens  INTEGER,
    gemini_cost_usd       REAL,
    label_status         TEXT,
    gemini_started_at     TEXT,
    gemini_completed_at   TEXT,
    error                 TEXT
);

CREATE INDEX IF NOT EXISTS idx_status ON semantic_chunks(label_status);
"""

# --------------------------------------------------------------------------- logging

def setup_logging() -> logging.Logger:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("pilot")
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)sZ %(levelname)s %(message)s")
    fmt.converter = time.gmtime
    fh = logging.FileHandler(LOG_PATH)
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(fh)
    logger.addHandler(sh)
    return logger


LOG = setup_logging()

# --------------------------------------------------------------------------- helpers

def now_utc() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def db_connect() -> sqlite3.Connection:
    """Open a SQLite connection in WAL mode so worker threads can write concurrently."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=30.0)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute("PRAGMA busy_timeout=30000")
    con.executescript(SCHEMA_SQL)
    return con


def ffprobe_duration(path: Path) -> float:
    out = subprocess.run(
        [FFPROBE, "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        check=True, capture_output=True, text=True,
    )
    return float(out.stdout.strip())


def gemini_chunk_id(asset_id: str, idx: int, is_chunked: bool) -> str:
    """Match TL pilot chunk_id format so compare-step joins work."""
    return f"{asset_id}_chunk{idx}" if is_chunked else f"{asset_id}_whole"


def gemini_cost_usd(input_tokens: int, output_tokens: int) -> float:
    """Apply tiered pricing: prompts >200K input tokens cost 2x."""
    over = input_tokens > GEMINI_PRICE_TIER_THRESHOLD
    in_rate = GEMINI_INPUT_HI if over else GEMINI_INPUT_LO
    out_rate = GEMINI_OUTPUT_HI if over else GEMINI_OUTPUT_LO
    return input_tokens * in_rate + output_tokens * out_rate


def assert_env_ready(need_gemini: bool = False, need_proxies: bool = False) -> None:
    if need_gemini:
        # Vertex uses ADC, not an API key. Check the credentials file exists.
        adc = Path.home() / ".config" / "gcloud" / "application_default_credentials.json"
        if not adc.exists():
            sys.exit("ADC not found — run: gcloud auth application-default login")
    if not Path(FFMPEG).exists():
        sys.exit(f"ffmpeg not found at {FFMPEG}")
    if not PRODUCTION_MANIFEST.exists():
        sys.exit(f"Production manifest missing: {PRODUCTION_MANIFEST}")
    if need_proxies and not DERIVATIVE_MEDIA.exists():
        sys.exit(f"derivative media not available: {DERIVATIVE_MEDIA} — remount the workspace SSD")

# --------------------------------------------------------------------------- prepare

def cmd_prepare(args: argparse.Namespace) -> None:
    """Read production manifest, populate semantic_chunks rows. Idempotent.

    For long clips (>55 min), expects pre-chunked files in LONG_CHUNKS_DIR;
    skip those with a warning if chunk-long hasn't been run yet.
    """
    assert_env_ready(need_proxies=False)
    manifest = json.loads(PRODUCTION_MANIFEST.read_text())
    LOG.info("manifest loaded: %d clips, %.1f hrs",
             manifest["total_clips"], manifest["total_duration_hrs"])

    inserted = chunks_missing = 0
    with closing(db_connect()) as con:
        for clip in manifest["clips"]:
            asset_id = clip["asset_id"]
            if clip["needs_chunking"]:
                chunk_files = sorted(LONG_CHUNKS_DIR.glob(f"{asset_id}_chunk*.mp4"))
                if not chunk_files:
                    chunks_missing += 1
                    continue
                cumulative = 0.0
                for idx, chunk_path in enumerate(chunk_files):
                    dur = ffprobe_duration(chunk_path)
                    chunk_id = gemini_chunk_id(asset_id, idx, is_chunked=True)
                    row = _prepare_row(clip, chunk_id, idx, True,
                                       str(chunk_path), cumulative,
                                       cumulative + dur, dur)
                    inserted += _insert_chunk(con, row)
                    cumulative += dur
            else:
                proxy = _proxy_for(asset_id)
                if proxy is None:
                    LOG.warning("  no asset_map entry for %s — skipping", asset_id)
                    continue
                chunk_id = gemini_chunk_id(asset_id, 0, is_chunked=False)
                row = _prepare_row(clip, chunk_id, 0, False, str(proxy),
                                   0.0, clip["duration_sec"], clip["duration_sec"])
                inserted += _insert_chunk(con, row)
        con.commit()
        total = con.execute("SELECT COUNT(*) FROM semantic_chunks").fetchone()[0]
    LOG.info("prepare done: inserted=%d total_rows=%d", inserted, total)
    if chunks_missing:
        LOG.warning("%d long clips skipped (run chunk-long first)", chunks_missing)


def _prepare_row(clip: dict, chunk_id: str, chunk_idx: int, is_chunked: bool,
                 upload_path: str, start: float, end: float, duration: float) -> dict:
    return {
        "chunk_id": chunk_id,
        "parent_asset_id": clip["asset_id"],
        "chunk_idx": chunk_idx,
        "is_chunked": int(is_chunked),
        "bucket": clip.get("bucket"),
        "duration_sec": duration,
        "upload_path": upload_path,
        "chunk_start_sec": start,
        "chunk_end_sec": end,
        "camera_id": clip.get("camera_id"),
        "shoot_label": clip.get("shoot_label"),
        "category_name": clip.get("category_name"),
        "shoot_date": clip.get("shoot_date"),
        "model": GEMINI_MODEL,
        "label_status": "pending",
    }


def _insert_chunk(con: sqlite3.Connection, row: dict) -> int:
    cols = ", ".join(row.keys())
    placeholders = ", ".join("?" * len(row))
    cur = con.execute(
        f"INSERT OR IGNORE INTO semantic_chunks ({cols}) VALUES ({placeholders})",
        tuple(row.values()),
    )
    return cur.rowcount

# --------------------------------------------------------------------------- chunk-long

def cmd_chunk_long(args: argparse.Namespace) -> None:
    """FFmpeg stream-copy the 27 long-clip proxies into LONG_CHUNKS_DIR.

    Idempotent — skips assets that already have chunk files. Stream copy is
    bandwidth-bound (no re-encode), runs in seconds-per-clip on SSD.
    """
    assert_env_ready(need_proxies=True)
    LONG_CHUNKS_DIR.mkdir(parents=True, exist_ok=True)
    manifest = json.loads(PRODUCTION_MANIFEST.read_text())
    long_clips = [c for c in manifest["clips"] if c["needs_chunking"]]
    LOG.info("chunk-long: %d clips to chunk into %s", len(long_clips), LONG_CHUNKS_DIR)

    chunked = skipped = 0
    for clip in long_clips:
        aid = clip["asset_id"]
        existing = sorted(LONG_CHUNKS_DIR.glob(f"{aid}_chunk*.mp4"))
        if existing:
            LOG.info("  skip %s — %d chunks already present", aid[:12], len(existing))
            skipped += 1
            continue
        proxy = _proxy_for(aid)
        if proxy is None or not proxy.exists():
            LOG.error("  proxy missing for %s — skipping", aid)
            continue
        out_pattern = LONG_CHUNKS_DIR / f"{aid}_chunk%d.mp4"
        LOG.info("  chunking %s (%.1f min) → segments of %d sec",
                 aid[:12], clip["duration_min"], CHUNK_SEGMENT_SEC)
        subprocess.run(
            [FFMPEG, "-nostdin", "-y", "-i", str(proxy),
             "-c", "copy", "-map", "0",
             "-f", "segment", "-segment_time", str(CHUNK_SEGMENT_SEC),
             "-reset_timestamps", "1",
             str(out_pattern)],
            check=True, capture_output=True,
        )
        produced = sorted(LONG_CHUNKS_DIR.glob(f"{aid}_chunk*.mp4"))
        LOG.info("    → %d chunks", len(produced))
        chunked += 1
    LOG.info("chunk-long done: chunked=%d skipped(already-chunked)=%d", chunked, skipped)

# --------------------------------------------------------------------------- gemini-describe

_progress_lock = threading.Lock()


def cmd_gemini_describe(args: argparse.Namespace) -> None:
    """Concurrent Vertex describe pass. Each worker uploads to GCS, generates
    via gs:// URI, deletes the blob.
    """
    assert_env_ready(need_gemini=True, need_proxies=True)
    from google import genai
    from google.cloud import storage

    # Pull the work list once on the main thread.
    with closing(db_connect()) as con:
        rows = [dict(r) for r in con.execute(
            "SELECT chunk_id, upload_path, duration_sec FROM semantic_chunks "
            "WHERE label_status IN ('pending', 'failed', 'uploading', 'generating') "
            "ORDER BY duration_sec ASC"
        ).fetchall()]
        total_already_done = con.execute(
            "SELECT COALESCE(SUM(gemini_cost_usd), 0) FROM semantic_chunks "
            "WHERE label_status='done'"
        ).fetchone()[0]
    if args.limit:
        rows = rows[: args.limit]
    if not rows:
        LOG.info("gemini-describe: nothing to do")
        return

    workers = max(1, args.workers)
    LOG.info("vertex describe: %d chunks pending, %d workers, $%.4f already spent",
             len(rows), workers, total_already_done)
    LOG.info("vertex project=%s location=%s bucket=%s", GCP_PROJECT, VERTEX_LOCATION, GCS_BUCKET)

    state = {"running_cost": 0.0, "done": 0, "failed": 0}

    def worker(ch_dict):
        thread_name = threading.current_thread().name
        client = genai.Client(vertexai=True, project=GCP_PROJECT, location=VERTEX_LOCATION)
        storage_client = storage.Client(project=GCP_PROJECT)
        bucket = storage_client.bucket(GCS_BUCKET)
        con = db_connect()
        try:
            try:
                cost = _describe_one(con, client, bucket, ch_dict, thread_name)
                with _progress_lock:
                    state["running_cost"] += cost
                    state["done"] += 1
                    LOG.info("[%s] running cost: $%.4f  (%d done, %d failed of %d)",
                             thread_name, state["running_cost"],
                             state["done"], state["failed"], len(rows))
            except Exception as e:
                tb = traceback.format_exc()
                LOG.error("[%s] chunk %s failed: %s", thread_name, ch_dict["chunk_id"], e)
                con.execute(
                    "UPDATE semantic_chunks SET label_status='failed', error=? "
                    "WHERE chunk_id=?",
                    (tb, ch_dict["chunk_id"]),
                )
                con.commit()
                with _progress_lock:
                    state["failed"] += 1
        finally:
            con.close()

    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="w") as ex:
        futures = [ex.submit(worker, ch) for ch in rows]
        try:
            for _ in as_completed(futures):
                pass
        except KeyboardInterrupt:
            LOG.warning("interrupted; cancelling pending workers (in-flight will finish)")
            for f in futures:
                f.cancel()
            raise
    LOG.info("vertex describe done: %d done, %d failed", state["done"], state["failed"])


def _describe_one(con: sqlite3.Connection, client, bucket, ch: dict, tag: str = "") -> float:
    from google.genai import types
    chunk_id = ch["chunk_id"]
    src = Path(ch["upload_path"])
    if not src.exists():
        raise FileNotFoundError(f"missing source: {src}")
    LOG.info("[%s] describe %s (%s, %.1fs)", tag, chunk_id, src.name, ch["duration_sec"])

    con.execute(
        "UPDATE semantic_chunks SET label_status='uploading', "
        "gemini_started_at=?, error=NULL WHERE chunk_id=?",
        (now_utc(), chunk_id),
    )
    con.commit()

    blob_name = f"chunks/{chunk_id}.mp4"
    blob = bucket.blob(blob_name)
    blob.upload_from_filename(str(src), content_type="video/mp4")
    gcs_uri = f"gs://{bucket.name}/{blob_name}"

    con.execute(
        "UPDATE semantic_chunks SET label_status='generating', gemini_file_id=? "
        "WHERE chunk_id=?",
        (gcs_uri, chunk_id),
    )
    con.commit()

    try:
        video_part = types.Part.from_uri(file_uri=gcs_uri, mime_type="video/mp4")
        response = _generate_with_retry(client, video_part, tag=tag)
        raw_text = response.text
        parsed = json.loads(raw_text)
        usage = response.usage_metadata
        in_tok = usage.prompt_token_count
        out_tok = usage.candidates_token_count
        cost = gemini_cost_usd(in_tok, out_tok)
        LOG.info("[%s]   tokens in=%d out=%d cost=$%.4f  (%s)",
                 tag, in_tok, out_tok, cost, chunk_id[:12])

        con.execute(
            "UPDATE semantic_chunks SET label_status='done', "
            "response_raw=?, response_json=?, "
            "gemini_input_tokens=?, gemini_output_tokens=?, gemini_cost_usd=?, "
            "gemini_completed_at=?, error=NULL WHERE chunk_id=?",
            (raw_text, json.dumps(parsed), in_tok, out_tok, cost, now_utc(), chunk_id),
        )
        con.commit()
        return cost
    finally:
        # Always try to delete the blob, even if generate_content failed.
        # Bucket lifecycle (1 day) is the backstop for any straggler.
        try:
            blob.delete()
        except Exception as e:
            LOG.warning("[%s] blob.delete(%s) failed: %s", tag, blob_name, e)


def _generate_with_retry(client, video_part, max_attempts: int = 6, tag: str = ""):
    # Vertex per-region RPM caps are tight; back off harder than AI Studio.
    delay = 10.0
    for attempt in range(1, max_attempts + 1):
        try:
            return client.models.generate_content(
                model=GEMINI_MODEL,
                contents=[video_part, PEGASUS_PROMPT],
                config={
                    "response_mime_type": "application/json",
                    "response_schema": ClipDescription,
                },
            )
        except Exception as e:
            msg = str(e).lower()
            # Vertex says "resource exhausted"; AI Studio says "quota exceeded".
            # Match all the transient signals from both.
            transient = any(s in msg for s in (
                "rate", "quota", "exhausted", "429",
                "unavailable", "503", "500", "deadline", "timeout",
            ))
            if attempt == max_attempts or not transient:
                raise
            LOG.warning("[%s]   generate attempt %d failed (%s); sleep %.0fs",
                        tag, attempt, e, delay)
            time.sleep(delay)
            delay *= 2

# --------------------------------------------------------------------------- status

def cmd_status(args: argparse.Namespace) -> None:
    with closing(db_connect()) as con:
        total = con.execute("SELECT COUNT(*) FROM semantic_chunks").fetchone()[0]
        by_status = dict(con.execute(
            "SELECT label_status, COUNT(*) FROM semantic_chunks GROUP BY label_status"
        ).fetchall())
        cost_row = con.execute(
            "SELECT COALESCE(SUM(gemini_cost_usd),0), COALESCE(SUM(gemini_input_tokens),0), "
            "COALESCE(SUM(gemini_output_tokens),0), COUNT(*) "
            "FROM semantic_chunks WHERE label_status='done'"
        ).fetchone()
        total_cost, total_in, total_out, n_done = cost_row
        avg_cost = (total_cost / n_done) if n_done else 0.0
        failures = con.execute(
            "SELECT chunk_id, error FROM semantic_chunks WHERE label_status='failed'"
        ).fetchall()

    print("=== Gemini Production Status ===")
    print(f"Total chunks: {total}")
    for status in ("pending", "uploading", "generating", "done", "failed"):
        n = by_status.get(status, 0)
        if n:
            print(f"  {status:12s} {n}")
    print()
    print(f"Total Gemini cost:   ${total_cost:.4f}")
    print(f"  Input tokens:      {total_in:,}")
    print(f"  Output tokens:     {total_out:,}")
    print(f"  Avg cost/chunk:    ${avg_cost:.4f}")
    if failures:
        print()
        print(f"Failures ({len(failures)}):")
        for f in failures[:20]:
            first_line = (f["error"] or "").splitlines()[-1] if f["error"] else "(no error text)"
            print(f"  {f['chunk_id'][:24]}: {first_line[:100]}")
        if len(failures) > 20:
            print(f"  ... and {len(failures) - 20} more")

# --------------------------------------------------------------------------- export

def cmd_export(args: argparse.Namespace) -> None:
    DESCRIPTIONS_DIR.mkdir(parents=True, exist_ok=True)
    n = 0
    with closing(db_connect()) as con:
        for r in con.execute(
            "SELECT chunk_id, response_json FROM semantic_chunks "
            "WHERE label_status='done' AND response_json IS NOT NULL"
        ):
            out = DESCRIPTIONS_DIR / f"{r['chunk_id']}.json"
            out.write_text(r["response_json"])
            n += 1
    LOG.info("export wrote %d files to %s", n, DESCRIPTIONS_DIR)

# --------------------------------------------------------------------------- run-all

def cmd_run_all(args: argparse.Namespace) -> None:
    cmd_chunk_long(args)
    cmd_prepare(args)
    cmd_gemini_describe(args)
    cmd_status(args)

# --------------------------------------------------------------------------- argparse

def _add_describe_args(p):
    p.add_argument("--limit", type=int, default=None,
                   help="Process at most N pending chunks")
    p.add_argument("--workers", type=int, default=14,
                   help="Concurrent worker threads (default 14)")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("prepare").set_defaults(func=cmd_prepare)
    sub.add_parser("chunk-long").set_defaults(func=cmd_chunk_long)

    p_describe = sub.add_parser("gemini-describe")
    _add_describe_args(p_describe)
    p_describe.set_defaults(func=cmd_gemini_describe)

    sub.add_parser("status").set_defaults(func=cmd_status)
    sub.add_parser("export").set_defaults(func=cmd_export)

    p_all = sub.add_parser("run-all")
    _add_describe_args(p_all)
    p_all.set_defaults(func=cmd_run_all)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
