#!/usr/bin/env python3
"""build_audio_events.py — Per-asset timed audio events via CLAP.

CLAP (Contrastive Language-Audio Pretraining) scores arbitrary English phrases
against an audio window. We use it to tag each WAV with a flat project
vocabulary of ~50 sound categories (wind, crowd cheering, footsteps, ...) so
editors can filter footage by what's *audible* in a clip — not just what was
described in the chunk-level Gemini summary.

This layer is the audio sibling of:
  - faces        — who's visible
  - shots        — where the visual cuts are
  - OCR          — what text is on screen
  - shot_quality — is the shot usable
  - audio_quality — is the audio usable (asset-level)

Subcommands:
  pilot   Phase A — 30 stratified clips, both engines, comparison report
  run     Phase B — full corpus, single engine, windowed sampling
  status  Coverage + tag distribution
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sqlite3
import sys
import time
import warnings
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from _paths import (  # noqa: E402
    AUDIO_EVENTS_DB, AUDIO_QUALITY_DB, DERIVATIVE_MEDIA, VIDEO_CATALOG,
    AUDIO_CATALOG, SHOTS_DB, WORKSPACE_ROOT, RUNS_DIR, derivative_relative,
)

# Reuse the audio-quality WAV resolver — same shape, different DSP.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "audio_quality"))

warnings.filterwarnings("ignore", message="urllib3 v2 only supports OpenSSL")
warnings.filterwarnings("ignore", category=UserWarning)

# Default analysis window per query.
WINDOW_SEC = 10.0
# For the full run: stride between windows in seconds. 7-sec stride with 10-sec
# window gives ~3-sec overlap, so a 60-sec asset yields ~9 query windows.
DEFAULT_STRIDE_SEC = 7.0
# Default confidence threshold for persisting (tag, score) rows. Tunable; the
# pilot will recommend a value based on the engine pick.
DEFAULT_MIN_SCORE = 0.25
# Top-K tags retained per window (after threshold).
TOP_K = 5


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS audio_event (
    event_pk      INTEGER PRIMARY KEY AUTOINCREMENT,
    asset_id      TEXT NOT NULL,
    record_kind   TEXT NOT NULL,
    window_start_sec REAL NOT NULL,
    window_end_sec   REAL NOT NULL,
    tag           TEXT NOT NULL,
    theme         TEXT NOT NULL,
    score         REAL NOT NULL,
    rank_in_win   INTEGER NOT NULL,
    engine        TEXT NOT NULL,
    detected_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ae_asset ON audio_event(asset_id);
CREATE INDEX IF NOT EXISTS ae_tag ON audio_event(tag);
CREATE INDEX IF NOT EXISTS ae_theme ON audio_event(theme);
CREATE INDEX IF NOT EXISTS ae_asset_time
    ON audio_event(asset_id, window_start_sec);

CREATE TABLE IF NOT EXISTS audio_event_processed (
    asset_id     TEXT NOT NULL,
    engine       TEXT NOT NULL,
    n_windows    INTEGER NOT NULL,
    n_hits       INTEGER NOT NULL,
    success      INTEGER NOT NULL,
    processed_at TEXT NOT NULL,
    PRIMARY KEY (asset_id, engine)
);

CREATE TABLE IF NOT EXISTS audio_event_run (
    run_pk        INTEGER PRIMARY KEY AUTOINCREMENT,
    phase         TEXT NOT NULL,
    engine        TEXT,
    started_at    TEXT NOT NULL,
    finished_at   TEXT,
    args_json     TEXT,
    summary_json  TEXT
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def open_db() -> sqlite3.Connection:
    AUDIO_EVENTS_DB.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(AUDIO_EVENTS_DB))
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.executescript(SCHEMA_SQL)
    con.commit()
    return con


# -------------------------------------------------------- catalog walk

def _resolve_wav_path(catalog_record: dict) -> Path | None:
    """Mirror of audio_quality's _resolve_wav_path. Kept inline to avoid the
    audio_quality module's heavy imports (numpy DSP etc.)."""
    ae = catalog_record.get("audio_extract") or {}
    p = ae.get("path") or ""
    if p:
        if p.startswith("~/"):
            cand = WORKSPACE_ROOT / p[2:]
            if cand.exists():
                return cand
        elif p.startswith("/"):
            cand = Path(p)
            if cand.exists():
                return cand
    sp = catalog_record.get("source_path") or ""
    if sp:
        try:
            rel = derivative_relative(sp)
        except ValueError:
            return None
        cand = DERIVATIVE_MEDIA / rel.with_suffix(".wav")
        if cand.exists():
            return cand
    return None


def _iter_catalog_assets():
    """Yield (kind, record, wav_path) for every catalog asset with a WAV on
    disk. Skips assets without an audio_extract block or with missing files."""
    for cat_dir, suffix, kind in (
        (VIDEO_CATALOG, ".video.json", "video"),
        (AUDIO_CATALOG, ".audio.json", "audio"),
    ):
        for f in cat_dir.glob(f"*{suffix}"):
            if f.name.startswith("._"): continue  # macOS AppleDouble sidecar
            try:
                d = json.loads(f.read_text())
            except Exception:
                continue
            if not d.get("audio_extract"):
                continue
            wav = _resolve_wav_path(d)
            if wav is None:
                continue
            yield kind, d, wav


# -------------------------------------------------------- pilot

def _flatview(d: dict) -> dict:
    """Pull the fields we use for stratification + the report context from
    their actual nested locations in the catalog record."""
    pm = d.get("path_metadata") or {}
    ac = d.get("asset_classifications") or {}
    s = d.get("asset_semantic_summary") or {}
    chunks = s.get("chunks") or []
    ch0 = chunks[0] if chunks else {}
    setting = ch0.get("setting") or {}
    return {
        "asset_id": d.get("asset_id"),
        "shoot_label": pm.get("shoot_label") or "",
        "category_name": pm.get("category_name") or "",
        "camera_id": pm.get("camera_id") or "",
        "asset_type": ac.get("type") or "",
        "bucket": ac.get("bucket") or "",
        "subject": (ch0.get("subject") or "")[:120],
        "setting_location": (setting.get("location") or "")[:120] if isinstance(setting, dict) else "",
    }


PILOT_STRATA = [
    # (label, catalog filter applied to _flatview(record), target count)
    ("race",      lambda v: any(k in v["shoot_label"] for k in ["<race-shoot-a>", "<race-shoot-b>", "<race-shoot-c>", "Race"]), 5),
    ("interview", lambda v: v["asset_type"] == "interview", 5),
    ("press_news_pod", lambda v: any(k in v["category_name"] for k in ["News", "Podcast", "Press", "Other Clips"])
                                or any(k in v["shoot_label"] for k in ["Podcast", "News", "Press"])
                                or v["bucket"] == "third_party", 5),
    ("aerial_drone",   lambda v: "DJI" in v["camera_id"] or "Aeriel" in v["shoot_label"]
                                  or "drone" in v["subject"].lower() or "drone" in v["setting_location"].lower(), 5),
    ("outdoor_broll",  lambda v: v["asset_type"] in ("b_roll", "broll", "verite"), 5),
    ("misc",      lambda v: True, 5),
]


def _stratified_pilot_pick(rng: random.Random, n_per_stratum_max: int = 5) -> list[tuple[str, dict, Path, str]]:
    """Pick a stratified sample. Walks the catalog once, buckets by stratum,
    samples within each bucket. Returns (kind, record, wav_path, stratum) tuples."""
    pools: dict[str, list[tuple[str, dict, Path]]] = {s[0]: [] for s in PILOT_STRATA}
    used_aids: set[str] = set()
    for kind, rec, wav in _iter_catalog_assets():
        aid = rec.get("asset_id")
        if not aid or aid in used_aids:
            continue
        view = _flatview(rec)
        for label, predicate, _target in PILOT_STRATA:
            try:
                if predicate(view):
                    pools[label].append((kind, rec, wav))
                    used_aids.add(aid)
                    break
            except Exception:
                continue
    picks: list[tuple[str, dict, Path, str]] = []
    for label, _pred, target in PILOT_STRATA:
        bucket = pools[label]
        rng.shuffle(bucket)
        for kind, rec, wav in bucket[:target]:
            picks.append((kind, rec, wav, label))
    return picks


def _midpoint_window(wav: Path) -> tuple[float, float] | None:
    """Return (start_sec, dur_sec) for a WINDOW_SEC-long window centered on the
    asset midpoint. For assets shorter than WINDOW_SEC, take the whole thing."""
    from _audio_events import asset_duration_sec
    d = asset_duration_sec(wav)
    if d is None:
        return None
    if d <= WINDOW_SEC:
        return (0.0, max(0.5, d))
    start = max(0.0, (d - WINDOW_SEC) / 2.0)
    return (start, WINDOW_SEC)


def cmd_pilot(args: argparse.Namespace) -> None:
    from _audio_events import (
        load_engine, decode_window, score_tags,
        all_tags, tag_to_theme, WINDOW_SEC as _WIN,
    )
    con = open_db()
    cur = con.execute(
        "INSERT INTO audio_event_run (phase, started_at, args_json) VALUES (?, ?, ?)",
        ("pilot", now_iso(), json.dumps(vars(args), default=str)),
    )
    run_pk = cur.lastrowid
    con.commit()

    print(f"=== audio_events PILOT | run_pk={run_pk} | {now_iso()} ===")

    rng = random.Random(args.seed)
    picks = _stratified_pilot_pick(rng)
    print(f"  stratified pool: {len(picks)} clips across {len({p[3] for p in picks})} strata")
    if args.limit:
        picks = picks[: args.limit]
        print(f"  --limit: {len(picks)}")
    if not picks:
        print("nothing to pilot.")
        return

    tags = all_tags()
    theme_map = tag_to_theme()
    print(f"  vocabulary: {len(tags)} tags across {len(set(theme_map.values()))} themes")

    engines_to_run = args.engines.split(",") if args.engines else ["laion_clap", "ms_clap"]
    print(f"  engines: {engines_to_run}")

    # Load engines once (slow: pulls model checkpoints on first run)
    loaded: dict[str, object] = {}
    for name in engines_to_run:
        t0 = time.time()
        print(f"  loading engine {name} ...")
        loaded[name] = load_engine(name)
        print(f"    loaded in {time.time() - t0:.1f}s")

    # Pre-decode each pilot clip into the per-engine sample-rate variants
    rows_for_report: list[dict] = []
    for i, (kind, rec, wav, label) in enumerate(picks):
        aid = rec.get("asset_id") or ""
        win = _midpoint_window(wav)
        if win is None:
            print(f"[{i+1:02d}/{len(picks)}] {aid[:12]} {label} -- no duration; skip")
            continue
        start, dur = win
        per_engine: dict[str, list[tuple[str, float]]] = {}
        timings: dict[str, float] = {}
        for ename, engine in loaded.items():
            samples = decode_window(wav, start, dur, engine.sample_rate)
            if samples is None or samples.size == 0:
                per_engine[ename] = []
                timings[ename] = 0.0
                continue
            t0 = time.time()
            ranked = score_tags(engine, samples, tags)
            timings[ename] = time.time() - t0
            per_engine[ename] = ranked[:TOP_K]
        view = _flatview(rec)
        rows_for_report.append({
            "asset_id": aid,
            "stratum": label,
            "wav_path": str(wav),
            "shoot_label": view["shoot_label"],
            "category_name": view["category_name"],
            "camera_id": view["camera_id"],
            "asset_type": view["asset_type"],
            "bucket": view["bucket"],
            "semantic_subject": view["subject"],
            "setting_location": view["setting_location"],
            "window_start_sec": start,
            "window_end_sec": start + dur,
            "engines": per_engine,
            "timings_sec": timings,
        })
        snip = "; ".join(
            f"{ename}: {', '.join(f'{t}({s:.2f})' for t,s in ranked[:3])}"
            for ename, ranked in per_engine.items()
        )
        print(f"[{i+1:02d}/{len(picks)}] {aid[:12]} {label} t=[{start:.0f}-{start+dur:.0f}s] -> {snip}")

    # Write report under dataset/_runs/ingest_pipeline/audio_events/
    report_dir = RUNS_DIR / "audio_events"
    report_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    report_path = report_dir / f"pilot_{ts}.md"
    json_path = report_dir / f"pilot_{ts}.json"
    _write_pilot_report(report_path, json_path, rows_for_report, engines_to_run, theme_map, run_pk)
    print(f"\nreport: {report_path}")
    print(f"  json: {json_path}")

    con.execute(
        "UPDATE audio_event_run SET finished_at=?, summary_json=? WHERE run_pk=?",
        (now_iso(), json.dumps({
            "n_clips": len(rows_for_report),
            "engines": engines_to_run,
            "report_path": str(report_path),
        }), run_pk),
    )
    con.commit()


def _write_pilot_report(
    md_path: Path, json_path: Path,
    rows: list[dict], engines: list[str],
    theme_map: dict[str, str], run_pk: int,
) -> None:
    """Markdown side-by-side report: per clip, per-engine top-K with scores +
    catalog context. JSON is the raw structured dump."""
    json_path.write_text(json.dumps({"run_pk": run_pk, "rows": rows}, indent=2))

    lines: list[str] = []
    lines.append(f"# Audio-events pilot — run {run_pk} ({now_iso()})")
    lines.append("")
    lines.append(f"Engines: {', '.join(engines)}  |  Clips: {len(rows)}  |  "
                 f"Window: {WINDOW_SEC:.0f} sec (asset midpoint)")
    lines.append(f"Vocabulary: {len({t for r in rows for ranked in r['engines'].values() for t,_ in ranked})}"
                 f" tags in top-K (of full vocab; CLAP scores all tags every query)")
    lines.append("")
    lines.append("Below: for each pilot clip, the top-5 tags per engine sorted by cosine similarity. "
                 "Eyeball each row against the catalog context (`shoot_label`, `semantic_subject`) "
                 "to judge precision. Pick an engine and a confidence threshold for the full run.")
    lines.append("")

    # Sort by stratum then asset_id for readability
    rows = sorted(rows, key=lambda r: (r["stratum"], r["asset_id"]))
    for r in rows:
        lines.append(f"## {r['stratum']}  ·  `{r['asset_id'][:12]}`")
        lines.append("")
        lines.append(f"- **shoot:** {r.get('shoot_label') or '—'}  ·  "
                     f"**category:** {r.get('category_name') or '—'}  ·  "
                     f"**type:** {r.get('asset_type') or '—'}  ·  "
                     f"**camera:** {r.get('camera_id') or '—'}  ·  "
                     f"**bucket:** {r.get('bucket') or '—'}")
        if r.get('semantic_subject'):
            lines.append(f"- **gemini subject:** {r['semantic_subject']}")
        if r.get('setting_location'):
            lines.append(f"- **gemini setting:** {r['setting_location']}")
        lines.append(f"- **window:** {r['window_start_sec']:.1f} → {r['window_end_sec']:.1f} sec")
        lines.append(f"- **wav:** `{r['wav_path']}`")
        lines.append("")
        # Engine table
        lines.append("| Rank | " + " | ".join(
            f"{e} (score)" for e in engines
        ) + " |")
        lines.append("|" + "---|" * (len(engines) + 1))
        for rank in range(TOP_K):
            cells = [f"{rank + 1}"]
            for e in engines:
                ranked = r["engines"].get(e) or []
                if rank < len(ranked):
                    tag, score = ranked[rank]
                    theme = theme_map.get(tag, "?")
                    cells.append(f"{tag} *[{theme}]* ({score:.2f})")
                else:
                    cells.append("—")
            lines.append("| " + " | ".join(cells) + " |")
        # Timings
        lines.append("")
        timings = r.get("timings_sec") or {}
        lines.append("Latency: " + ", ".join(f"`{e}`: {timings.get(e, 0):.2f}s" for e in engines))
        lines.append("")
        lines.append("---")
        lines.append("")

    md_path.write_text("\n".join(lines))


# -------------------------------------------------------- status

def cmd_status(args: argparse.Namespace) -> None:
    if not AUDIO_EVENTS_DB.exists():
        print("(audio_events.sqlite not yet created — run `pilot` or `run` first)")
        return
    con = sqlite3.connect(str(AUDIO_EVENTS_DB))
    print(f"=== audio_events status | {AUDIO_EVENTS_DB} ===")
    n_events = con.execute("SELECT COUNT(*) FROM audio_event").fetchone()[0]
    n_assets = con.execute("SELECT COUNT(DISTINCT asset_id) FROM audio_event").fetchone()[0]
    print(f"  events: {n_events:,} across {n_assets:,} assets")
    print(f"  by engine:")
    for engine, c in con.execute(
        "SELECT engine, COUNT(*) FROM audio_event GROUP BY engine"
    ).fetchall():
        print(f"    {engine}: {c:,}")
    print(f"  top tags overall:")
    for tag, c in con.execute(
        "SELECT tag, COUNT(*) FROM audio_event GROUP BY tag ORDER BY 2 DESC LIMIT 15"
    ).fetchall():
        print(f"    {tag}: {c:,}")
    print(f"  runs:")
    for run_pk, phase, engine, started_at, finished_at in con.execute(
        "SELECT run_pk, phase, engine, started_at, finished_at FROM audio_event_run "
        "ORDER BY run_pk DESC LIMIT 10"
    ).fetchall():
        print(f"    run {run_pk}: phase={phase} engine={engine} {started_at} → {finished_at or '(in progress)'}")
    con.close()


# -------------------------------------------------------- run (full corpus)

# Per-engine min-score thresholds calibrated from the pilot:
#   LAION-CLAP: p75 ≈ 0.18 — narrower band; keep top-quartile
#   MS-CLAP:    p75 ≈ 0.31 — wider band, higher floor; keep top-quartile post-vocab-cleanup
ENGINE_DEFAULT_MIN_SCORE = {
    "laion_clap": 0.18,
    "ms_clap":    0.30,
}


def _asset_shot_windows(con_shots: sqlite3.Connection | None,
                        asset_id: str,
                        asset_duration_sec: float) -> list[tuple[float, float]]:
    """Build a list of (start_sec, dur_sec) sampling windows for an asset.

    Strategy: shot-aware sampling, mirroring OCR.
      - If we have shot boundaries for this asset:
          * 1 sample at each shot midpoint (if shot >= 2 sec)
          * Additional samples every 15 sec inside shots >= 30 sec
      - If we don't (no shots row), fall back to:
          * asset < 30s   → 1 window centered
          * 30s ≤ a < 300s → 3 windows at 25/50/75 %
          * else            → window every 30 sec
    """
    rows: list[tuple[float, float]] = []
    if con_shots is not None:
        shots = con_shots.execute(
            "SELECT start_sec, end_sec FROM shots WHERE asset_id=? ORDER BY shot_idx",
            (asset_id,),
        ).fetchall()
        if shots:
            for s_start, s_end in shots:
                s_dur = max(0.0, float(s_end) - float(s_start))
                if s_dur < 2.0:
                    continue
                mid = float(s_start) + s_dur / 2.0
                rows.append((max(0.0, mid - WINDOW_SEC / 2.0), WINDOW_SEC))
                if s_dur >= 30.0:
                    # Extra interior samples every 15 sec, skipping the midpoint
                    t = float(s_start) + 7.5
                    while t + WINDOW_SEC <= float(s_end):
                        if abs(t + WINDOW_SEC / 2.0 - mid) > 7.5:
                            rows.append((t, WINDOW_SEC))
                        t += 15.0
            return rows
    # Fallback path
    d = asset_duration_sec
    if d < 30.0:
        return [(max(0.0, (d - WINDOW_SEC) / 2.0), min(WINDOW_SEC, max(0.5, d)))]
    if d < 300.0:
        return [
            (max(0.0, d * 0.25 - WINDOW_SEC / 2.0), WINDOW_SEC),
            (max(0.0, d * 0.50 - WINDOW_SEC / 2.0), WINDOW_SEC),
            (max(0.0, d * 0.75 - WINDOW_SEC / 2.0), WINDOW_SEC),
        ]
    # Long: stride every 30 sec
    out: list[tuple[float, float]] = []
    t = 0.0
    while t + WINDOW_SEC <= d:
        out.append((t, WINDOW_SEC))
        t += 30.0
    return out


def _is_silent_asset(con_aq: sqlite3.Connection | None, asset_id: str) -> bool:
    """Skip assets that audio_quality already flagged as fully silent — no
    point running CLAP on dead air."""
    if con_aq is None:
        return False
    r = con_aq.execute(
        "SELECT is_silent FROM audio_quality WHERE asset_id=?", (asset_id,),
    ).fetchone()
    if r is None:
        return False
    return bool(r[0])


def cmd_run(args: argparse.Namespace) -> None:
    from _audio_events import (
        load_engine, decode_window, score_tags, all_tags, tag_to_theme,
        asset_duration_sec,
    )
    con = open_db()
    cur = con.execute(
        "INSERT INTO audio_event_run (phase, engine, started_at, args_json) "
        "VALUES (?, ?, ?, ?)",
        ("run", ",".join(args.engines.split(",")), now_iso(),
         json.dumps(vars(args), default=str)),
    )
    run_pk = cur.lastrowid
    con.commit()

    print(f"=== audio_events RUN | run_pk={run_pk} | {now_iso()} ===")

    engines_to_run = args.engines.split(",")
    print(f"  engines: {engines_to_run}")
    thresholds = {}
    for e in engines_to_run:
        t = getattr(args, f"min_score_{e}", None)
        if t is None:
            t = ENGINE_DEFAULT_MIN_SCORE.get(e, 0.25)
        thresholds[e] = float(t)
    print(f"  thresholds: {thresholds}")

    # Attach shots + audio_quality as read-only sources.
    # On the workspace SSD's exFAT volume, opening a WAL-mode DB with ?mode=ro can later
    # fail ("disk I/O error") on .execute() because the FS can't write the
    # -shm file. Probe with sqlite_master, fall back to rw — only used for
    # SELECTs either way.
    def _open_ro(path):
        if not path.exists():
            return None
        try:
            c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            c.execute("SELECT name FROM sqlite_master LIMIT 1").fetchone()
            return c
        except sqlite3.OperationalError:
            return sqlite3.connect(str(path))
    con_shots = _open_ro(SHOTS_DB)
    con_aq = _open_ro(AUDIO_QUALITY_DB)
    print(f"  shots db: {'attached' if con_shots else 'not present'}")
    print(f"  audio_quality db: {'attached' if con_aq else 'not present'}")

    # Build work list
    print("  walking catalog for assets with WAVs...")
    work: list[tuple[str, str, Path]] = []
    skipped_silent = 0
    skipped_already = 0
    for kind, rec, wav in _iter_catalog_assets():
        aid = rec.get("asset_id")
        if not aid:
            continue
        already_done = con.execute(
            "SELECT engine FROM audio_event_processed WHERE asset_id=? AND success=1",
            (aid,),
        ).fetchall()
        done_engines = {r[0] for r in already_done}
        if all(e in done_engines for e in engines_to_run):
            skipped_already += 1
            continue
        if _is_silent_asset(con_aq, aid):
            skipped_silent += 1
            # Record silent skip so we don't re-check
            for e in engines_to_run:
                if e not in done_engines:
                    con.execute(
                        "INSERT OR REPLACE INTO audio_event_processed "
                        "(asset_id, engine, n_windows, n_hits, success, processed_at) "
                        "VALUES (?, ?, 0, 0, 1, ?)",
                        (aid, e, now_iso()),
                    )
            continue
        work.append((aid, kind, wav))
    con.commit()
    print(f"  already processed (all engines): {skipped_already}")
    print(f"  skipped (is_silent in audio_quality): {skipped_silent}")
    print(f"  effective work: {len(work)}")
    if args.limit:
        work = work[: args.limit]
        print(f"  --limit: {len(work)}")
    if not work:
        print("nothing to do.")
        return

    # Load engines (slow on first call due to model load)
    loaded = {}
    for name in engines_to_run:
        t0 = time.time()
        print(f"  loading engine {name} ...")
        loaded[name] = load_engine(name)
        print(f"    loaded in {time.time() - t0:.1f}s")

    tags = all_tags()
    theme_map = tag_to_theme()
    print(f"  vocab: {len(tags)} tags after cleanup")

    t_start = time.time()
    n_events = 0
    n_windows_total = 0
    for i, (aid, kind, wav) in enumerate(work):
        # Duration: ffprobe once (cheap)
        dur = asset_duration_sec(wav) or 0.0
        if dur < 1.0:
            for e in engines_to_run:
                con.execute(
                    "INSERT OR REPLACE INTO audio_event_processed "
                    "(asset_id, engine, n_windows, n_hits, success, processed_at) "
                    "VALUES (?, ?, 0, 0, 1, ?)",
                    (aid, e, now_iso()),
                )
            continue
        windows = _asset_shot_windows(con_shots, aid, dur)
        if not windows:
            continue

        per_engine_hits: dict[str, int] = {e: 0 for e in engines_to_run}
        per_engine_windows: dict[str, int] = {e: 0 for e in engines_to_run}
        for w_start, w_dur in windows:
            for ename, engine in loaded.items():
                # Skip if this asset is already done for this engine
                done = con.execute(
                    "SELECT 1 FROM audio_event_processed WHERE asset_id=? AND engine=? AND success=1",
                    (aid, ename),
                ).fetchone()
                if done:
                    continue
                samples = decode_window(wav, w_start, w_dur, engine.sample_rate)
                if samples is None or samples.size == 0:
                    continue
                ranked = score_tags(engine, samples, tags)
                kept = [(t, s) for t, s in ranked[:TOP_K] if s >= thresholds[ename]]
                w_end = w_start + w_dur
                rank = 0
                for t, s in kept:
                    rank += 1
                    con.execute(
                        "INSERT INTO audio_event "
                        "(asset_id, record_kind, window_start_sec, window_end_sec, "
                        " tag, theme, score, rank_in_win, engine, detected_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (aid, kind, w_start, w_end, t, theme_map.get(t, "?"),
                         float(s), rank, ename, now_iso()),
                    )
                    per_engine_hits[ename] += 1
                per_engine_windows[ename] += 1

        # Mark asset processed per engine
        for e in engines_to_run:
            if e in loaded:
                con.execute(
                    "INSERT OR REPLACE INTO audio_event_processed "
                    "(asset_id, engine, n_windows, n_hits, success, processed_at) "
                    "VALUES (?, ?, ?, ?, 1, ?)",
                    (aid, e, per_engine_windows[e], per_engine_hits[e], now_iso()),
                )
        con.commit()
        n_events += sum(per_engine_hits.values())
        n_windows_total += sum(per_engine_windows.values())
        elapsed = time.time() - t_start
        rate = (i + 1) / elapsed if elapsed > 0 else 0.0
        eta_min = (len(work) - i - 1) / rate / 60.0 if rate > 0 else 0.0
        print(f"[{i+1:>5}/{len(work)}] {aid[:12]} dur={dur:6.1f}s wins={len(windows):>3}  "
              f"hits=[{','.join(f'{e}:{per_engine_hits[e]}' for e in engines_to_run)}]  "
              f"elapsed={elapsed/60:.1f}m  rate={rate*60:.1f}/min  ETA={eta_min:.0f}m")

    if con_shots: con_shots.close()
    if con_aq: con_aq.close()

    con.execute(
        "UPDATE audio_event_run SET finished_at=?, summary_json=? WHERE run_pk=?",
        (now_iso(), json.dumps({
            "n_assets": len(work),
            "n_windows": n_windows_total,
            "n_events": n_events,
            "engines": engines_to_run,
            "thresholds": thresholds,
        }), run_pk),
    )
    con.commit()
    con.close()
    print(f"\nrun complete: {n_events:,} events across {len(work):,} assets in {(time.time()-t_start)/60:.1f}m")


# -------------------------------------------------------- main

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("pilot", help="Stratified pilot — both engines, MD report")
    sp.add_argument("--seed", type=int, default=20260524)
    sp.add_argument("--limit", type=int, help="cap clip count")
    sp.add_argument("--engines", default="laion_clap,ms_clap",
                    help="comma-list of engines to run")
    sp.set_defaults(func=cmd_pilot)

    sp = sub.add_parser("run", help="Full corpus pass — shot-aware windowing, dual-engine")
    sp.add_argument("--engines", default="laion_clap,ms_clap",
                    help="comma-list of engines to run")
    sp.add_argument("--limit", type=int)
    sp.add_argument("--min-score-laion_clap", type=float,
                    default=ENGINE_DEFAULT_MIN_SCORE["laion_clap"],
                    help="min cosine score to persist a LAION-CLAP tag")
    sp.add_argument("--min-score-ms_clap", type=float,
                    default=ENGINE_DEFAULT_MIN_SCORE["ms_clap"],
                    help="min cosine score to persist a MS-CLAP tag")
    sp.set_defaults(func=cmd_run)

    sp = sub.add_parser("status", help="Coverage + tag distribution")
    sp.set_defaults(func=cmd_status)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
