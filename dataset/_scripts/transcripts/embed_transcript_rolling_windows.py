"""
Build rolling-window transcript text embeddings into `indexes/transcript_rolling_embeddings.sqlite`.

Reads `assets/transcripts/*.transcript.json`, builds a synthetic per-asset document
(`Speaker: text\n` lines sorted by time), slices overlapping time windows, embeds with
`sentence-transformers`, and stores float32 vectors plus char/time metadata.
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import sqlite3
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import numpy as np

# `dataset/_scripts` on sys.path when run as `python _scripts/transcripts/embed_transcript_rolling_windows.py`
_SCRIPTS = Path(__file__).resolve().parent
_DATASET = _SCRIPTS.parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))  # shared modules live at _scripts root
from workspace_paths import transcript_rolling_embeddings_sqlite_path  # noqa: E402


@dataclass(frozen=True)
class SegmentLine:
    start_sec: float
    end_sec: float
    char_start: int
    char_end: int
    line: str


def _speaker_label(seg: dict[str, Any], speakers_raw: dict[str, Any]) -> str:
    sp = seg.get("speaker")
    if isinstance(sp, str) and sp.strip():
        return sp.strip()
    raw_id = seg.get("speaker_raw")
    if isinstance(raw_id, str) and raw_id in speakers_raw:
        ent = speakers_raw[raw_id]
        if isinstance(ent, dict):
            name = ent.get("name")
            if isinstance(name, str) and name.strip():
                return name.strip()
    return "unknown"


def build_global_document(
    asset_id: str,
    segments: list[dict[str, Any]],
    speakers_raw: dict[str, Any],
) -> tuple[str, list[SegmentLine]]:
    rows: list[tuple[float, float, str]] = []
    for seg in segments:
        if not isinstance(seg, dict):
            continue
        try:
            a = float(seg["start_sec"])
            b = float(seg["end_sec"])
        except (KeyError, TypeError, ValueError):
            continue
        text = seg.get("text")
        if not isinstance(text, str) or not text.strip():
            continue
        label = _speaker_label(seg, speakers_raw)
        line = f"{label}: {text.strip()}\n"
        rows.append((a, b, line))
    rows.sort(key=lambda r: r[0])

    blocks: list[SegmentLine] = []
    pos = 0
    parts: list[str] = []
    for a, b, line in rows:
        char_start = pos
        parts.append(line)
        pos += len(line)
        char_end = pos
        blocks.append(SegmentLine(start_sec=a, end_sec=b, char_start=char_start, char_end=char_end, line=line))
    return "".join(parts), blocks


def iter_time_windows(
    duration_sec: float,
    window_sec: float,
    overlap_sec: float,
) -> Iterable[tuple[float, float]]:
    if duration_sec <= 0 or window_sec <= 0:
        return
    step = max(window_sec - overlap_sec, 1e-6)
    t = 0.0
    while t < duration_sec:
        ws = t
        we = min(ws + window_sec, duration_sec)
        yield ws, we
        t += step
        if we >= duration_sec:
            break


def window_char_span(blocks: list[SegmentLine], ws: float, we: float) -> tuple[int, int] | None:
    c0: int | None = None
    c1: int | None = None
    for b in blocks:
        if b.start_sec < we and b.end_sec > ws:
            if c0 is None or b.char_start < c0:
                c0 = b.char_start
            if c1 is None or b.char_end > c1:
                c1 = b.char_end
    if c0 is None or c1 is None:
        return None
    return c0, c1


def init_db(path: Path, *, reset: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        if reset:
            conn.execute("DROP TABLE IF EXISTS transcript_window_embedding")
            conn.execute("DROP TABLE IF EXISTS embedding_run")
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS embedding_run (
                run_id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                model_name TEXT NOT NULL,
                window_sec REAL NOT NULL,
                overlap_sec REAL NOT NULL,
                line_template_version INTEGER NOT NULL,
                max_chars INTEGER NOT NULL,
                embed_batch_size INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS transcript_window_embedding (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id INTEGER NOT NULL REFERENCES embedding_run(run_id),
                asset_id TEXT NOT NULL,
                window_anchor_ms INTEGER NOT NULL,
                window_start_sec REAL NOT NULL,
                window_end_sec REAL NOT NULL,
                char_start INTEGER NOT NULL,
                char_end INTEGER NOT NULL,
                text_hash TEXT NOT NULL,
                text_preview TEXT NOT NULL,
                embedding_dim INTEGER NOT NULL,
                vector_blob BLOB NOT NULL,
                UNIQUE(run_id, asset_id, window_anchor_ms)
            );
            """
        )
        conn.commit()
    finally:
        # WAL/shm clean close (avoid bindfs .fuse_hidden* on indexes/).
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception:
            pass
        conn.close()
def insert_run(
    conn: sqlite3.Connection,
    *,
    model_name: str,
    window_sec: float,
    overlap_sec: float,
    line_template_version: int,
    max_chars: int,
    embed_batch_size: int,
) -> int:
    conn.execute(
        """
        INSERT INTO embedding_run (
            created_at, model_name, window_sec, overlap_sec,
            line_template_version, max_chars, embed_batch_size
        ) VALUES (datetime('now'), ?, ?, ?, ?, ?, ?)
        """,
        (model_name, window_sec, overlap_sec, line_template_version, max_chars, embed_batch_size),
    )
    conn.commit()
    rid = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
    return rid


def _set_windows_process_priority(mode: str) -> None:
    if sys.platform != "win32" or mode == "normal":
        return
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.SetPriorityClass.argtypes = (ctypes.c_void_p, ctypes.c_uint32)
    k32.SetPriorityClass.restype = ctypes.c_int
    k32.GetCurrentProcess.restype = ctypes.c_void_p
    # https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-setpriorityclass
    below_normal = 0x0000_4000
    idle = 0x0000_0040
    flag = idle if mode == "idle" else below_normal
    h = k32.GetCurrentProcess()
    if not k32.SetPriorityClass(h, flag):
        err = ctypes.get_last_error()
        print(
            f"warning: SetPriorityClass failed (winerror={err}); continuing at normal priority",
            file=sys.stderr,
        )


def load_model(model_name: str, *, torch_threads: int | None) -> Any:
    import torch
    from sentence_transformers import SentenceTransformer

    if torch_threads is not None:
        n = max(1, int(torch_threads))
        torch.set_num_threads(n)
        try:
            torch.set_num_interop_threads(1)
        except RuntimeError:
            pass
    return SentenceTransformer(model_name)


def embedding_dim(model: Any) -> int:
    fn = getattr(model, "get_embedding_dimension", None)
    if callable(fn):
        return int(fn())
    return int(model.get_sentence_embedding_dimension())


def discover_transcripts(root: Path) -> list[Path]:
    d = root / "assets" / "catalog" / "transcripts"
    return sorted(d.glob("*.transcript.json"))


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dataset-root", type=Path, default=_DATASET, help="Path to `dataset/` (default: parent of _scripts/)")
    p.add_argument("--output", type=Path, default=None, help="Override SQLite output path")
    p.add_argument("--model", type=str, default="sentence-transformers/all-MiniLM-L6-v2")
    p.add_argument("--window-sec", type=float, default=90.0)
    p.add_argument("--overlap-sec", type=float, default=45.0)
    p.add_argument("--max-chars", type=int, default=12000)
    p.add_argument("--embed-batch-size", type=int, default=16)
    p.add_argument(
        "--torch-threads",
        type=int,
        default=None,
        metavar="N",
        help="Cap PyTorch CPU threads (lower = gentler; e.g. 2–4 on a busy desktop).",
    )
    p.add_argument(
        "--sleep-after-batch",
        type=float,
        default=0.0,
        metavar="SEC",
        help="Pause after each embedding batch (reduces sustained CPU; 0 = off).",
    )
    p.add_argument(
        "--process-priority",
        choices=("normal", "below-normal", "idle"),
        default="normal",
        help="Windows process priority only (no-op on other OS). idle is lightest; below-normal suits background runs.",
    )
    p.add_argument("--dry-run", action="store_true", help="Parse windows only; no model / no DB writes")
    p.add_argument("--reset", action="store_true", help="Drop embedding tables before writing")
    p.add_argument("--max-assets", type=int, default=None)
    p.add_argument("--asset-id", type=str, default=None, help="Process a single asset id (hex stem)")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    root: Path = args.dataset_root.resolve()
    out: Path = (args.output or transcript_rolling_embeddings_sqlite_path()).resolve()
    sleep_after_batch = max(0.0, float(args.sleep_after_batch))
    window_sec = float(args.window_sec)
    overlap_sec = float(args.overlap_sec)

    paths = discover_transcripts(root)
    if args.asset_id:
        stem = args.asset_id.strip()
        want = root / "assets" / "catalog" / "transcripts" / f"{stem}.transcript.json"
        paths = [want] if want.exists() else []

    if args.max_assets is not None:
        paths = paths[: int(args.max_assets)]

    if args.dry_run:
        n_assets = 0
        n_windows = 0
        for tp in paths:
            try:
                data = json.loads(tp.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                continue
            aid = str(data.get("asset_id") or tp.stem.replace(".transcript", ""))
            segs = data.get("segments")
            if not isinstance(segs, list):
                continue
            sp_raw = data.get("speakers_raw")
            speakers_raw = sp_raw if isinstance(sp_raw, dict) else {}
            global_text, blocks = build_global_document(aid, segs, speakers_raw)
            if not blocks:
                continue
            dur = float(data.get("playback_duration_sec") or 0.0)
            dur = max(dur, max(b.end_sec for b in blocks))
            n_assets += 1
            for ws, we in iter_time_windows(dur, window_sec, overlap_sec):
                span = window_char_span(blocks, ws, we)
                if not span:
                    continue
                c0, c1 = span
                chunk = global_text[c0:c1].strip()
                if not chunk:
                    continue
                n_windows += 1
        print(f"dry-run: assets={n_assets} windows={n_windows} (no writes)")
        return 0

    if args.torch_threads is not None:
        n = str(max(1, int(args.torch_threads)))
        for k in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
            os.environ.setdefault(k, n)

    _set_windows_process_priority(str(args.process_priority))

    init_db(out, reset=bool(args.reset))
    conn = sqlite3.connect(out)
    try:
        run_id = insert_run(
            conn,
            model_name=args.model,
            window_sec=window_sec,
            overlap_sec=overlap_sec,
            line_template_version=1,
            max_chars=int(args.max_chars),
            embed_batch_size=int(args.embed_batch_size),
        )
        model = load_model(args.model, torch_threads=args.torch_threads)
        dim = embedding_dim(model)
        batch_records: list[tuple[Any, ...]] = []
        batch_texts: list[str] = []
        assets_done = 0
        windows_done = 0
        max_chars = int(args.max_chars)

        def flush() -> None:
            nonlocal batch_records, batch_texts, windows_done
            if not batch_texts:
                return
            emb = model.encode(
                batch_texts,
                batch_size=len(batch_texts),
                normalize_embeddings=True,
                show_progress_bar=False,
            )
            arr = np.asarray(emb, dtype=np.float32)
            if arr.ndim != 2 or arr.shape[1] != dim:
                raise RuntimeError(f"unexpected embedding shape {arr.shape}, expected (*,{dim})")
            for rec, row in zip(batch_records, arr):
                blob = row.tobytes()
                conn.execute(
                    """
                    INSERT OR REPLACE INTO transcript_window_embedding (
                        run_id, asset_id, window_anchor_ms,
                        window_start_sec, window_end_sec,
                        char_start, char_end,
                        text_hash, text_preview,
                        embedding_dim, vector_blob
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    rec + (dim, blob),
                )
            windows_done += len(batch_texts)
            conn.commit()
            batch_records = []
            batch_texts = []
            if sleep_after_batch:
                time.sleep(sleep_after_batch)

        for tp in paths:
            try:
                data = json.loads(tp.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                continue
            aid = str(data.get("asset_id") or tp.stem.replace(".transcript", ""))
            segs = data.get("segments")
            if not isinstance(segs, list):
                continue
            sp_raw = data.get("speakers_raw")
            speakers_raw = sp_raw if isinstance(sp_raw, dict) else {}
            global_text, blocks = build_global_document(aid, segs, speakers_raw)
            if not blocks:
                continue
            dur = float(data.get("playback_duration_sec") or 0.0)
            dur = max(dur, max(b.end_sec for b in blocks))

            assets_done += 1
            for ws, we in iter_time_windows(dur, window_sec, overlap_sec):
                span = window_char_span(blocks, ws, we)
                if not span:
                    continue
                c0, c1 = span
                text = global_text[c0:c1].strip()
                if not text:
                    continue
                if len(text) > max_chars:
                    text = text[:max_chars]
                h = hashlib.sha256(text.encode("utf-8")).hexdigest()
                preview = text[:500]
                anchor_ms = int(ws * 1000)
                rec = (run_id, aid, anchor_ms, ws, we, c0, c1, h, preview)
                batch_records.append(rec)
                batch_texts.append(text)
                if len(batch_texts) >= int(args.embed_batch_size):
                    flush()

        flush()
    finally:
        # WAL/shm clean close (avoid bindfs .fuse_hidden* on indexes/).
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception:
            pass
        conn.close()
    print(
        f"done: assets={assets_done} windows={windows_done} out={out} "
        f"dim={dim} model={args.model}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
