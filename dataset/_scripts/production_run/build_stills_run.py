#!/usr/bin/env python3
"""Sample/production stills run — Gemini Pro editorial descriptions + SigLIP embeddings.

Mirrors the video pipeline in label_videos_vertex.py and
siglip_embed_keyframes.py, minus all video-only machinery
(proxy encode, long-clip chunking, keyframe extraction). The still IS the frame.

Subcommands:
  coverage   walk the mirror-SSD volumes, hash-match catalog stills,
             write coverage_stills.json
  prepare    INSERT OR IGNORE rows into semantic_stills (status='pending') from
             coverage report
  describe   AI Studio Gemini 2.5 Pro per-image describe via inline bytes
  embed      SigLIP-So400m → 1152-dim, struct-packed into still_embeddings
  export     dump JSONs to data/descriptions_stills/
  status     summary
  run-all    coverage → prepare → describe → embed → export → status

Auth: GEMINI_API_KEY env var (already in ~/.zshrc). No GCS hop — image bytes
go inline with the request, much simpler than the Vertex video path.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sqlite3
import struct
import sys
import threading
import time
import traceback
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import closing
from io import BytesIO
from pathlib import Path
from typing import List, Optional

from pydantic import BaseModel

# --------------------------------------------------------------------------- paths

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from _paths import EMBEDDINGS_DB, STILLS_CATALOG, RUNS_DIR

SCRIPT_DIR = Path(__file__).resolve().parent
DATA_DIR = RUNS_DIR / "production_run"
DB_PATH = EMBEDDINGS_DB
DESCRIPTIONS_DIR = DATA_DIR / "descriptions_stills"
LOG_PATH = DATA_DIR / "stills_run.log"
COVERAGE_PATH = DATA_DIR / "coverage_stills.json"

CATALOG_DIR = STILLS_CATALOG

SSDS = [
    ("Backup-1", "/Volumes/Backup-1"),  # your mirror-SSD volume names
    ("Backup-2", "/Volumes/Backup-2"),
    ("Backup-3", "/Volumes/Backup-3"),
]

SKIP_DIRS = {
    "$RECYCLE.BIN", "System Volume Information",
    ".Spotlight-V100", ".fseventsd", ".TemporaryItems", ".Trashes",
    ".DocumentRevisions-V100",
    "AI_Proxies",
}

IMAGE_EXTS = {".jpg", ".jpeg", ".heic", ".heif", ".png", ".tif", ".tiff"}

HEAD_TAIL = 1_000_000  # matches make_proxies/verify_ssd_match partial_hash

# --------------------------------------------------------------------------- config

GEMINI_MODEL = "gemini-2.5-pro"
# Vertex AI fallback for when AI Studio Tier 1 daily quota (1000 req/day) is hit.
GCP_PROJECT = os.environ.get("GCP_PROJECT", "<your-gcp-project-id>")
VERTEX_LOCATION = "us-central1"
GEMINI_PRICE_TIER_THRESHOLD = 200_000
GEMINI_INPUT_LO = 1.25 / 1_000_000
GEMINI_INPUT_HI = 2.50 / 1_000_000
GEMINI_OUTPUT_LO = 10.00 / 1_000_000
GEMINI_OUTPUT_HI = 15.00 / 1_000_000

# AI Studio inline-bytes limit; stills are typically 5–25MB JPEG, well under.
INLINE_BYTES_LIMIT = 20 * 1024 * 1024

SIGLIP_MODEL = "google/siglip-so400m-patch14-384"
SIGLIP_DIM = 1152

PEGASUS_STILL_PROMPT = """You are analyzing a single still image from a documentary feature about ultrarunning and mountaineering in Grand Teton National Park, Wyoming. The film follows athletes preparing for and attempting fastest-known-time records on technical alpine routes. Stills include landscape photography, athlete portraits, behind-the-scenes production stills, archival snapshots, and casual phone photos.

Analyze this image and output ONLY valid JSON, no markdown fences, no commentary, matching this exact schema:

{
  "subject": "Who or what is in the frame. Name people if recognizable from context. Use 'unknown person' or 'multiple people' if ambiguous. For non-people, describe the subject (e.g., 'Grand Teton ridgeline at sunrise', 'a runner's hands taping shoes').",
  "description": "What the image shows, 2-4 sentences. Be specific about composition, action frozen in the frame, mood, and visual events. Avoid generic phrases like 'a person stands'.",
  "setting": {
    "location": "Interior or exterior. If exterior and recognizable, name the location (mountains, ridge, summit, trail, town). If interior, describe the space (living room, vehicle, restaurant).",
    "time_of_day": "One of: golden_hour | blue_hour | midday | overcast | night | indoor | unknown",
    "weather": "One of: clear | overcast | snow | rain | mixed | indoor_na"
  },
  "camera": {
    "shot_size": "One of: ECU | CU | MCU | MS | MWS | WS | EWS | aerial | mixed",
    "perspective": "One of: eye_level | low_angle | high_angle | overhead | POV"
  },
  "emotional_tone": "1-3 words capturing energy and mood (e.g., 'contemplative', 'high-energy action', 'tense', 'meditative landscape').",
  "editorial_notes": "1-2 sentences about editorial value. Is this opening-worthy, transitional, B-roll filler, archival, etc.? What kind of cut would this still slot into?"
}

Output ONLY the JSON object."""


class StillSetting(BaseModel):
    location: str
    time_of_day: str
    weather: str


class StillCamera(BaseModel):
    shot_size: str
    perspective: str


class StillDescription(BaseModel):
    subject: str
    description: str
    setting: StillSetting
    camera: StillCamera
    emotional_tone: str
    editorial_notes: str


StillDescription.model_rebuild()

# --------------------------------------------------------------------------- schema

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS semantic_stills (
    asset_id              TEXT PRIMARY KEY,
    upload_path           TEXT NOT NULL,
    filename              TEXT,
    filesize_bytes        INTEGER,
    mime_type             TEXT,
    width                 INTEGER,
    height                INTEGER,
    camera_id             TEXT,
    shoot_label           TEXT,
    category_name         TEXT,
    shoot_date            TEXT,
    linked_video_asset_id TEXT,
    model          TEXT,
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

CREATE INDEX IF NOT EXISTS idx_stills_status ON semantic_stills(label_status);

CREATE TABLE IF NOT EXISTS still_embeddings (
    asset_id          TEXT PRIMARY KEY,
    embedding_model   TEXT,
    vector_dim        INTEGER,
    vector_blob       BLOB,
    pulled_at         TEXT
);
"""

# --------------------------------------------------------------------------- logging

def setup_logging() -> logging.Logger:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("stills")
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
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=30.0)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute("PRAGMA busy_timeout=30000")
    con.executescript(SCHEMA_SQL)
    return con


def gemini_cost_usd(input_tokens: int, output_tokens: int) -> float:
    over = input_tokens > GEMINI_PRICE_TIER_THRESHOLD
    in_rate = GEMINI_INPUT_HI if over else GEMINI_INPUT_LO
    out_rate = GEMINI_OUTPUT_HI if over else GEMINI_OUTPUT_LO
    return input_tokens * in_rate + output_tokens * out_rate


def mime_for_filename(name: str) -> str:
    n = name.lower()
    if n.endswith((".jpg", ".jpeg")):
        return "image/jpeg"
    if n.endswith((".heic", ".heif")):
        return "image/heic"
    if n.endswith(".png"):
        return "image/png"
    if n.endswith((".tif", ".tiff")):
        return "image/tiff"
    return "application/octet-stream"


def partial_hash(path: str, size: int) -> str:
    """Same algorithm as verify_ssd_match.py — catalog asset_id IS this hash."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        if size <= 2 * HEAD_TAIL:
            while b := f.read(1 << 20):
                h.update(b)
        else:
            h.update(f.read(HEAD_TAIL))
            f.seek(-HEAD_TAIL, os.SEEK_END)
            h.update(f.read(HEAD_TAIL))
    h.update(size.to_bytes(8, "big"))
    return h.hexdigest()


def load_catalog() -> List[dict]:
    return [json.loads(p.read_text())
            for p in sorted(CATALOG_DIR.glob("*.still.json"))
            if not p.name.startswith("._")]

# --------------------------------------------------------------------------- coverage

def cmd_coverage(args: argparse.Namespace) -> None:
    """Walk the SSDs, match catalog stills by hash with basename+filesize fallback.

    Writes coverage_stills.json mapping asset_id → resolved local path.
    """
    catalog = load_catalog()
    LOG.info("coverage: %d catalog stills", len(catalog))

    # Phase 1 — walk + hash. Hashing 1,243+ images on three SSDs is IO-bound;
    # parallelism by drive is enough. We ONLY hash files whose basename+size
    # matches a catalog entry, so we don't pay to hash unrelated images.
    catalog_by_bn = defaultdict(list)
    for asset in catalog:
        bn = (asset.get("filename") or "").lower()
        catalog_by_bn[bn].append(asset)

    hash_index: dict[str, dict] = {}
    bn_index: dict[str, list] = defaultdict(list)
    visited = 0

    LOG.info("phase 1: walk SSDs (only hashing files matching catalog basenames)")
    t0 = time.time()
    for label, root in SSDS:
        if not os.path.isdir(root):
            LOG.warning("  %s NOT MOUNTED at %s", label, root)
            continue
        n_seen = n_matched = 0
        for dp, dn, fn in os.walk(root, followlinks=False):
            dn[:] = [d for d in dn if d not in SKIP_DIRS]
            for f in fn:
                ext = os.path.splitext(f)[1].lower()
                if ext not in IMAGE_EXTS:
                    continue
                n_seen += 1
                bn_lower = f.lower()
                if bn_lower not in catalog_by_bn:
                    continue
                fp = os.path.join(dp, f)
                try:
                    st = os.stat(fp)
                except OSError:
                    continue
                bn_index[bn_lower].append({
                    "ssd": label, "path": fp, "size": st.st_size,
                })
                # Hash only on real candidates (size in any catalog match,
                # within 1% tolerance OR ≤5MB delta).
                size_match = any(
                    abs(st.st_size - (a.get("filesize_bytes") or 0)) / max(a.get("filesize_bytes") or 1, 1) < 0.01
                    or abs(st.st_size - (a.get("filesize_bytes") or 0)) < 5_000_000
                    for a in catalog_by_bn[bn_lower]
                )
                if not size_match:
                    continue
                try:
                    h = partial_hash(fp, st.st_size)
                except OSError:
                    continue
                hash_index[h] = {"ssd": label, "path": fp, "size": st.st_size}
                n_matched += 1
        visited += n_seen
        LOG.info("  %s: %d images seen, %d hashed candidates", label, n_seen, n_matched)
    LOG.info("phase 1 done in %.1fs (%d images visited, %d hashed)",
             time.time() - t0, visited, len(hash_index))

    LOG.info("phase 2: match catalog → SSD")
    resolved = []
    fallback = []
    unresolved = []
    for asset in catalog:
        aid = asset["asset_id"]
        bn = (asset.get("filename") or "").lower()
        sz = asset.get("filesize_bytes") or 0
        rec = {
            "asset_id": aid,
            "filename": asset.get("filename"),
            "filesize_bytes": sz,
            "linked_video_asset_id": asset.get("linked_video_asset_id"),
            "path_metadata": asset.get("path_metadata") or {},
            "exif": asset.get("exif") or {},
        }
        if aid in hash_index:
            info = hash_index[aid]
            rec.update({"resolved_path": info["path"], "ssd": info["ssd"],
                        "match_method": "hash"})
            resolved.append(rec)
            continue
        # fallback: basename + size within 1% (or 5MB)
        cands = bn_index.get(bn, [])
        good = [c for c in cands
                if sz and (abs(c["size"] - sz) / max(sz, 1) < 0.01
                           or abs(c["size"] - sz) < 5_000_000)]
        if not good:
            rec["match_method"] = "unresolved"
            unresolved.append(rec)
            continue
        if len(good) == 1:
            pick = good[0]
            method = "fallback_single"
        else:
            shoot = (asset.get("path_metadata") or {}).get("shoot_label") or ""
            overlap = [c for c in good if shoot and shoot in c["path"]]
            if overlap:
                pick = overlap[0]; method = "fallback_folder_overlap"
            else:
                good.sort(key=lambda c: abs(c["size"] - sz))
                pick = good[0]; method = "fallback_closest_size"
        rec.update({"resolved_path": pick["path"], "ssd": pick["ssd"],
                    "match_method": method})
        fallback.append(rec)

    report = {
        "generated_at": now_utc(),
        "catalog_total": len(catalog),
        "resolved_hash": len(resolved),
        "resolved_fallback": len(fallback),
        "unresolved": len(unresolved),
        "by_ssd": dict(_count_by(resolved + fallback, "ssd")),
        "stills": resolved + fallback,
        "unresolved_assets": [
            {"asset_id": r["asset_id"], "filename": r["filename"],
             "filesize_bytes": r["filesize_bytes"],
             "shoot_label": r["path_metadata"].get("shoot_label"),
             "category_name": r["path_metadata"].get("category_name")}
            for r in unresolved
        ],
    }
    COVERAGE_PATH.write_text(json.dumps(report, indent=2))
    LOG.info("coverage written → %s", COVERAGE_PATH)
    LOG.info("  resolved: %d (%d hash, %d fallback)",
             len(resolved) + len(fallback), len(resolved), len(fallback))
    LOG.info("  unresolved: %d", len(unresolved))
    for ssd, n in report["by_ssd"].items():
        LOG.info("    %s: %d", ssd, n)


def _count_by(items, key):
    out = defaultdict(int)
    for it in items:
        out[it.get(key)] += 1
    return out

# --------------------------------------------------------------------------- prepare

def cmd_prepare(args: argparse.Namespace) -> None:
    if not COVERAGE_PATH.exists():
        sys.exit(f"coverage report missing — run `coverage` first: {COVERAGE_PATH}")
    report = json.loads(COVERAGE_PATH.read_text())
    LOG.info("prepare: %d resolved stills in coverage report",
             len(report["stills"]))
    inserted = 0
    with closing(db_connect()) as con:
        for s in report["stills"]:
            pm = s.get("path_metadata") or {}
            ex = s.get("exif") or {}
            row = {
                "asset_id": s["asset_id"],
                "upload_path": s["resolved_path"],
                "filename": s.get("filename"),
                "filesize_bytes": s.get("filesize_bytes"),
                "mime_type": mime_for_filename(s.get("filename") or ""),
                "width": ex.get("width"),
                "height": ex.get("height"),
                "camera_id": pm.get("camera_id"),
                "shoot_label": pm.get("shoot_label"),
                "category_name": pm.get("category_name"),
                "shoot_date": pm.get("shoot_date"),
                "linked_video_asset_id": s.get("linked_video_asset_id"),
                "model": GEMINI_MODEL,
                "label_status": "pending",
            }
            cols = ", ".join(row.keys())
            ph = ", ".join("?" * len(row))
            cur = con.execute(
                f"INSERT OR IGNORE INTO semantic_stills ({cols}) VALUES ({ph})",
                tuple(row.values()),
            )
            inserted += cur.rowcount
        con.commit()
        total = con.execute("SELECT COUNT(*) FROM semantic_stills").fetchone()[0]
    LOG.info("prepare done: inserted=%d total_rows=%d", inserted, total)

# --------------------------------------------------------------------------- describe

_progress_lock = threading.Lock()


def cmd_describe(args: argparse.Namespace) -> None:
    """Concurrent describe pass — inline image bytes, no upload.

    Two backends:
      - AI Studio (default): GEMINI_API_KEY auth, generative-language quota.
      - Vertex (--vertex):   ADC auth via gcloud, separate Vertex quota.
    """
    use_vertex = bool(getattr(args, "vertex", False))
    if use_vertex:
        adc = Path.home() / ".config" / "gcloud" / "application_default_credentials.json"
        if not adc.exists():
            sys.exit("ADC missing — run: gcloud auth application-default login")
        api_key = None
    else:
        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key:
            sys.exit("GEMINI_API_KEY not set — `source ~/.zshrc`")
    from google import genai

    with closing(db_connect()) as con:
        rows = [dict(r) for r in con.execute(
            "SELECT asset_id, upload_path, filename, mime_type, filesize_bytes "
            "FROM semantic_stills "
            "WHERE label_status IN ('pending', 'failed', 'generating') "
            "ORDER BY filesize_bytes ASC"
        ).fetchall()]
        already = con.execute(
            "SELECT COALESCE(SUM(gemini_cost_usd), 0) FROM semantic_stills "
            "WHERE label_status='done'"
        ).fetchone()[0]
    if args.limit:
        rows = rows[: args.limit]
    if not rows:
        LOG.info("describe: nothing to do")
        return

    workers = max(1, args.workers)
    backend = "Vertex" if use_vertex else "AI Studio"
    LOG.info("describe (%s): %d stills pending, %d workers, $%.4f already spent",
             backend, len(rows), workers, already)

    state = {"running_cost": 0.0, "done": 0, "failed": 0}

    def worker(s):
        thread_name = threading.current_thread().name
        if use_vertex:
            client = genai.Client(vertexai=True, project=GCP_PROJECT,
                                  location=VERTEX_LOCATION)
        else:
            client = genai.Client(api_key=api_key)
        con = db_connect()
        try:
            try:
                cost = _describe_one(con, client, s, thread_name)
                with _progress_lock:
                    state["running_cost"] += cost
                    state["done"] += 1
                    LOG.info("[%s] running cost: $%.4f  (%d done, %d failed of %d)",
                             thread_name, state["running_cost"],
                             state["done"], state["failed"], len(rows))
            except Exception as e:
                tb = traceback.format_exc()
                LOG.error("[%s] still %s failed: %s", thread_name, s["asset_id"][:12], e)
                con.execute(
                    "UPDATE semantic_stills SET label_status='failed', error=? "
                    "WHERE asset_id=?",
                    (tb, s["asset_id"]),
                )
                con.commit()
                with _progress_lock:
                    state["failed"] += 1
        finally:
            con.close()

    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="w") as ex:
        futures = [ex.submit(worker, s) for s in rows]
        try:
            for _ in as_completed(futures):
                pass
        except KeyboardInterrupt:
            LOG.warning("interrupted; cancelling")
            for f in futures:
                f.cancel()
            raise
    LOG.info("describe done: %d done, %d failed", state["done"], state["failed"])


def _read_image_bytes(path: Path, mime: str) -> tuple[bytes, str]:
    """Return JPEG bytes and 'image/jpeg' mime. HEIC and big TIFF/PNG are
    converted/recompressed locally so the request stays under 20MB and the
    mime is uniform."""
    from PIL import Image
    if mime == "image/heic":
        try:
            import pillow_heif  # noqa: F401
            pillow_heif.register_heif_opener()
        except ImportError:
            raise RuntimeError("pillow-heif not installed; required for HEIC")
    if mime == "image/jpeg" and path.stat().st_size <= INLINE_BYTES_LIMIT:
        return path.read_bytes(), "image/jpeg"
    img = Image.open(path)
    img.load()
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    # Cap longest edge to keep request small; SigLIP handles full res itself.
    max_edge = 2048
    if max(img.size) > max_edge:
        img.thumbnail((max_edge, max_edge), Image.LANCZOS)
    buf = BytesIO()
    img.save(buf, format="JPEG", quality=92, optimize=True)
    img.close()
    return buf.getvalue(), "image/jpeg"


def _describe_one(con: sqlite3.Connection, client, s: dict, tag: str = "") -> float:
    from google.genai import types
    asset_id = s["asset_id"]
    src = Path(s["upload_path"])
    if not src.exists():
        raise FileNotFoundError(f"missing source: {src}")
    LOG.info("[%s] describe %s (%s, %.1fMB)",
             tag, asset_id[:12], src.name,
             (s.get("filesize_bytes") or 0) / 1e6)

    con.execute(
        "UPDATE semantic_stills SET label_status='generating', "
        "gemini_started_at=?, error=NULL WHERE asset_id=?",
        (now_utc(), asset_id),
    )
    con.commit()

    img_bytes, send_mime = _read_image_bytes(src, s.get("mime_type") or "image/jpeg")
    image_part = types.Part.from_bytes(data=img_bytes, mime_type=send_mime)

    response = _generate_with_retry(client, image_part, tag=tag)
    raw_text = response.text
    parsed = json.loads(raw_text)
    usage = response.usage_metadata
    in_tok = usage.prompt_token_count
    out_tok = usage.candidates_token_count
    cost = gemini_cost_usd(in_tok, out_tok)
    LOG.info("[%s]   tokens in=%d out=%d cost=$%.4f  (%s)",
             tag, in_tok, out_tok, cost, asset_id[:12])

    con.execute(
        "UPDATE semantic_stills SET label_status='done', "
        "response_raw=?, response_json=?, "
        "gemini_input_tokens=?, gemini_output_tokens=?, gemini_cost_usd=?, "
        "gemini_completed_at=?, error=NULL WHERE asset_id=?",
        (raw_text, json.dumps(parsed), in_tok, out_tok, cost, now_utc(), asset_id),
    )
    con.commit()
    return cost


def _generate_with_retry(client, image_part, max_attempts: int = 6, tag: str = ""):
    delay = 5.0
    for attempt in range(1, max_attempts + 1):
        try:
            return client.models.generate_content(
                model=GEMINI_MODEL,
                contents=[image_part, PEGASUS_STILL_PROMPT],
                config={
                    "response_mime_type": "application/json",
                    "response_schema": StillDescription,
                },
            )
        except Exception as e:
            msg = str(e).lower()
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

# --------------------------------------------------------------------------- embed

def cmd_embed(args: argparse.Namespace) -> None:
    """SigLIP-So400m image embeddings, struct-packed into still_embeddings."""
    from PIL import Image
    from transformers import AutoProcessor, AutoModel
    import torch
    try:
        import pillow_heif
        pillow_heif.register_heif_opener()
    except ImportError:
        LOG.warning("pillow-heif not installed; HEIC stills will fail")

    device = "mps" if torch.backends.mps.is_available() else "cpu"
    LOG.info("embed: loading SigLIP on %s", device)
    processor = AutoProcessor.from_pretrained(SIGLIP_MODEL)
    model = AutoModel.from_pretrained(SIGLIP_MODEL).to(device).eval()
    LOG.info("model loaded: %s (image dim=%d)", SIGLIP_MODEL, SIGLIP_DIM)

    with closing(db_connect()) as con:
        rows = [dict(r) for r in con.execute(
            "SELECT s.asset_id, s.upload_path "
            "FROM semantic_stills s "
            "LEFT JOIN still_embeddings e ON e.asset_id = s.asset_id "
            "WHERE e.asset_id IS NULL "
            "ORDER BY s.filesize_bytes ASC"
        ).fetchall()]
        if args.limit:
            rows = rows[: args.limit]
        if not rows:
            LOG.info("embed: nothing to do")
            return
        LOG.info("embed: %d stills pending", len(rows))

        BATCH = 16
        done = failed = 0
        for batch_start in range(0, len(rows), BATCH):
            batch = rows[batch_start:batch_start + BATCH]
            paths = [Path(r["upload_path"]) for r in batch]
            images = []
            keep = []
            for r, p in zip(batch, paths):
                if not p.exists():
                    LOG.error("missing source: %s", p)
                    failed += 1
                    continue
                try:
                    img = Image.open(p)
                    img.load()
                    if img.mode != "RGB":
                        img = img.convert("RGB")
                    images.append(img)
                    keep.append(r)
                except Exception as e:
                    LOG.error("PIL open failed for %s: %s", p, e)
                    failed += 1
            if not images:
                continue
            try:
                with torch.no_grad():
                    inputs = processor(images=images, return_tensors="pt").to(device)
                    feats = model.get_image_features(**inputs)
                    feats = feats / feats.norm(p=2, dim=-1, keepdim=True)
                vectors = feats.cpu().numpy()
            finally:
                for img in images:
                    img.close()
            updates = []
            for r, vec in zip(keep, vectors):
                if len(vec) != SIGLIP_DIM:
                    sys.exit(f"unexpected SigLIP dim: {len(vec)}")
                blob = struct.pack(f"<{SIGLIP_DIM}f", *vec.astype("float32"))
                updates.append((r["asset_id"], SIGLIP_MODEL, SIGLIP_DIM,
                                blob, now_utc()))
            con.executemany(
                "INSERT OR REPLACE INTO still_embeddings "
                "(asset_id, embedding_model, vector_dim, vector_blob, pulled_at) "
                "VALUES (?, ?, ?, ?, ?)",
                updates,
            )
            con.commit()
            done += len(updates)
            LOG.info("  embedded %d / %d (failed %d)", done, len(rows), failed)
    LOG.info("embed done: %d done, %d failed", done, failed)

# --------------------------------------------------------------------------- export

def cmd_export(args: argparse.Namespace) -> None:
    DESCRIPTIONS_DIR.mkdir(parents=True, exist_ok=True)
    n = 0
    with closing(db_connect()) as con:
        for r in con.execute(
            "SELECT asset_id, response_json FROM semantic_stills "
            "WHERE label_status='done' AND response_json IS NOT NULL"
        ):
            out = DESCRIPTIONS_DIR / f"{r['asset_id']}.json"
            out.write_text(r["response_json"])
            n += 1
    LOG.info("export wrote %d files to %s", n, DESCRIPTIONS_DIR)

# --------------------------------------------------------------------------- status

def cmd_status(args: argparse.Namespace) -> None:
    with closing(db_connect()) as con:
        total = con.execute("SELECT COUNT(*) FROM semantic_stills").fetchone()[0]
        by_status = dict(con.execute(
            "SELECT label_status, COUNT(*) FROM semantic_stills GROUP BY label_status"
        ).fetchall())
        cost_row = con.execute(
            "SELECT COALESCE(SUM(gemini_cost_usd),0), "
            "COALESCE(SUM(gemini_input_tokens),0), "
            "COALESCE(SUM(gemini_output_tokens),0), COUNT(*) "
            "FROM semantic_stills WHERE label_status='done'"
        ).fetchone()
        total_cost, total_in, total_out, n_done = cost_row
        avg_cost = (total_cost / n_done) if n_done else 0.0
        emb_n = con.execute("SELECT COUNT(*) FROM still_embeddings "
                            "WHERE vector_blob IS NOT NULL").fetchone()[0]
        failures = con.execute(
            "SELECT asset_id, error FROM semantic_stills WHERE label_status='failed'"
        ).fetchall()
    print("=== Stills Run Status ===")
    print(f"Total stills: {total}")
    for st in ("pending", "generating", "done", "failed"):
        n = by_status.get(st, 0)
        if n:
            print(f"  {st:12s} {n}")
    print()
    print(f"Total Gemini cost:    ${total_cost:.4f}")
    print(f"  Input tokens:       {total_in:,}")
    print(f"  Output tokens:      {total_out:,}")
    print(f"  Avg cost/still:     ${avg_cost:.4f}")
    print(f"SigLIP embeddings:    {emb_n} / {total}")
    if failures:
        print()
        print(f"Failures ({len(failures)}):")
        for f in failures[:20]:
            first_line = (f["error"] or "").splitlines()[-1] if f["error"] else "(no error)"
            print(f"  {f['asset_id'][:24]}: {first_line[:100]}")
        if len(failures) > 20:
            print(f"  ... and {len(failures) - 20} more")

# --------------------------------------------------------------------------- run-all

def cmd_run_all(args: argparse.Namespace) -> None:
    if not COVERAGE_PATH.exists():
        cmd_coverage(args)
    cmd_prepare(args)
    cmd_describe(args)
    cmd_embed(args)
    cmd_export(args)
    cmd_status(args)

# --------------------------------------------------------------------------- argparse

def _add_limit_arg(p):
    p.add_argument("--limit", type=int, default=None, help="cap N rows (smoke)")


def _add_describe_args(p):
    _add_limit_arg(p)
    p.add_argument("--workers", type=int, default=8,
                   help="concurrent describe workers (default 8)")
    p.add_argument("--vertex", action="store_true",
                   help="use Vertex AI (ADC auth) instead of AI Studio (API key)")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("coverage").set_defaults(func=cmd_coverage)
    sub.add_parser("prepare").set_defaults(func=cmd_prepare)

    p_describe = sub.add_parser("describe")
    _add_describe_args(p_describe)
    p_describe.set_defaults(func=cmd_describe)

    p_embed = sub.add_parser("embed")
    _add_limit_arg(p_embed)
    p_embed.set_defaults(func=cmd_embed)

    sub.add_parser("export").set_defaults(func=cmd_export)
    sub.add_parser("status").set_defaults(func=cmd_status)

    p_all = sub.add_parser("run-all")
    _add_describe_args(p_all)
    p_all.set_defaults(func=cmd_run_all)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
