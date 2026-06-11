#!/usr/bin/env python3
"""build_audio_fingerprint.py — Cross-modal audio↔video linker via chromaprint.

Production recorders (lavalier WAV, bag-recorder WAV) often capture the same
acoustic event as a camera's onboard mic. When a take exists in both modes,
linking the two assets unlocks editorial moves like:

  - Replace lossy camera audio with the clean recorder track in the same cut
  - Promote diarized transcripts from the recorder side to the camera side
  - Surface "which camera angles cover this interview line?" queries

This layer is the audio sibling of the visual-similarity FAISS index:
both build proximity graphs across the corpus, just over different modalities.

Pipeline:

  fingerprint  Phase J.1 — chromaprint over every WAV (video + audio catalog),
               persists uint32 hash arrays to audio_fingerprints.sqlite
  match        Phase J.2 — pairwise bit-Hamming similarity over candidate
               pairs (heuristic prefilter by shoot folder + date), persists
               proposals
  apply        Phase J.3 — write proposals above threshold into catalog records
               via the existing `linked_audio_asset_ids[]` /
               `linked_video_asset_ids[]` slots (legacy schema)
  status       Coverage summary

Schema add note: the existing `linked_*` slots are a generic association list.
A future schema add (`audio_source` / `audio_backup_of` relation types) would
let editors distinguish "this is the clean recorder backup" from "this is
a different mic that happens to overlap." Not done here; left for the apply
step's caller to decide.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from _paths import (  # noqa: E402
    AUDIO_FINGERPRINT_DB, DERIVATIVE_MEDIA, VIDEO_CATALOG, AUDIO_CATALOG,
    WORKSPACE_ROOT, RUNS_DIR, derivative_relative,
)

DEFAULT_MATCH_THRESHOLD = 0.55   # bit-Hamming similarity floor (>>0.5 = signal)
DEFAULT_APPLY_THRESHOLD = 0.65   # higher floor for writing to catalog records
DEFAULT_PREFILTER_FLOOR = 0.30   # quick exact-equality probe; below = skip full match
TOP_K_PROPOSALS_PER_VIDEO = 3    # at most 3 audio matches per video


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS fingerprint (
    asset_id      TEXT PRIMARY KEY,
    record_kind   TEXT NOT NULL,              -- 'video' or 'audio'
    wav_path      TEXT NOT NULL,
    duration_sec  REAL,
    n_hashes      INTEGER NOT NULL,
    fp_blob       BLOB NOT NULL,              -- uint32[] as packed bytes
    shoot_label   TEXT,
    shoot_date    TEXT,
    camera_id     TEXT,
    computed_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS fp_kind  ON fingerprint(record_kind);
CREATE INDEX IF NOT EXISTS fp_shoot ON fingerprint(shoot_label);
CREATE INDEX IF NOT EXISTS fp_date  ON fingerprint(shoot_date);

CREATE TABLE IF NOT EXISTS link_proposal (
    proposal_pk        INTEGER PRIMARY KEY AUTOINCREMENT,
    video_asset_id     TEXT NOT NULL,
    audio_asset_id     TEXT NOT NULL,
    raw_match_score    REAL NOT NULL,         -- bit-Hamming similarity
    offset_frames      INTEGER,               -- best alignment offset
    path_overlap_score REAL NOT NULL,         -- heuristic [0..1]
    combined_score     REAL NOT NULL,         -- raw_match_score + path_overlap_score weighting
    candidate_rank     INTEGER NOT NULL,      -- rank within video_asset_id's matches
    proposed_at        TEXT NOT NULL,
    UNIQUE (video_asset_id, audio_asset_id)
);
CREATE INDEX IF NOT EXISTS lp_video ON link_proposal(video_asset_id);
CREATE INDEX IF NOT EXISTS lp_audio ON link_proposal(audio_asset_id);
CREATE INDEX IF NOT EXISTS lp_score ON link_proposal(combined_score);

CREATE TABLE IF NOT EXISTS fp_processed (
    asset_id     TEXT PRIMARY KEY,
    success      INTEGER NOT NULL,
    processed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS applied_link (
    applied_pk         INTEGER PRIMARY KEY AUTOINCREMENT,
    video_asset_id     TEXT NOT NULL,
    audio_asset_id     TEXT NOT NULL,
    raw_match_score    REAL,
    combined_score     REAL,
    applied_at         TEXT NOT NULL,
    applied_run_pk     INTEGER,
    UNIQUE (video_asset_id, audio_asset_id)
);
CREATE INDEX IF NOT EXISTS al_video ON applied_link(video_asset_id);
CREATE INDEX IF NOT EXISTS al_audio ON applied_link(audio_asset_id);

CREATE TABLE IF NOT EXISTS fp_run (
    run_pk       INTEGER PRIMARY KEY AUTOINCREMENT,
    phase        TEXT NOT NULL,                 -- 'fingerprint' | 'match' | 'apply'
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    args_json    TEXT,
    summary_json TEXT
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def open_db() -> sqlite3.Connection:
    AUDIO_FINGERPRINT_DB.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(AUDIO_FINGERPRINT_DB))
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.executescript(SCHEMA_SQL)
    con.commit()
    return con


# ----------------------------------------------------------- catalog walk

def _resolve_wav_path(rec: dict) -> Path | None:
    ae = rec.get("audio_extract") or {}
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
    sp = rec.get("source_path") or ""
    if sp:
        try:
            rel = derivative_relative(sp)
        except ValueError:
            return None
        cand = DERIVATIVE_MEDIA / rel.with_suffix(".wav")
        if cand.exists():
            return cand
    return None


def _flatview(rec: dict) -> dict:
    pm = rec.get("path_metadata") or {}
    return {
        "asset_id": rec.get("asset_id"),
        "shoot_label": pm.get("shoot_label") or "",
        "shoot_date": pm.get("shoot_date") or "",
        "camera_id": pm.get("camera_id") or "",
    }


def _iter_catalog_assets():
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


# ----------------------------------------------------------- fingerprint

def cmd_fingerprint(args: argparse.Namespace) -> None:
    from _fingerprint import fingerprint_wav, pack_fp
    con = open_db()
    cur = con.execute(
        "INSERT INTO fp_run (phase, started_at, args_json) VALUES (?, ?, ?)",
        ("fingerprint", now_iso(), json.dumps(vars(args), default=str)),
    )
    run_pk = cur.lastrowid
    con.commit()

    print(f"=== fingerprint | run_pk={run_pk} | {now_iso()} ===")
    processed = {r[0] for r in con.execute(
        "SELECT asset_id FROM fp_processed WHERE success=1")}

    work = []
    for kind, rec, wav in _iter_catalog_assets():
        aid = rec.get("asset_id")
        if not aid or aid in processed:
            continue
        work.append((aid, kind, rec, wav))
    print(f"  already processed: {len(processed)}  effective: {len(work)}")
    if args.limit:
        work = work[: args.limit]
        print(f"  --limit: {len(work)}")
    if not work:
        return

    t0 = time.time()
    n_ok = 0; n_err = 0
    for i, (aid, kind, rec, wav) in enumerate(work):
        v = _flatview(rec)
        res = fingerprint_wav(wav, length_sec=args.length)
        if res is None:
            con.execute(
                "INSERT OR REPLACE INTO fp_processed (asset_id, success, processed_at) "
                "VALUES (?, 0, ?)", (aid, now_iso()),
            )
            n_err += 1
        else:
            dur, hashes = res
            con.execute(
                "INSERT OR REPLACE INTO fingerprint (asset_id, record_kind, wav_path, "
                "duration_sec, n_hashes, fp_blob, shoot_label, shoot_date, camera_id, "
                "computed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (aid, kind, str(wav), dur, len(hashes), pack_fp(hashes),
                 v["shoot_label"], v["shoot_date"], v["camera_id"], now_iso()),
            )
            con.execute(
                "INSERT OR REPLACE INTO fp_processed (asset_id, success, processed_at) "
                "VALUES (?, 1, ?)", (aid, now_iso()),
            )
            n_ok += 1
        if (i + 1) % 100 == 0:
            con.commit()
            elapsed = time.time() - t0
            rate = (i + 1) / elapsed
            eta = (len(work) - i - 1) / rate / 60.0 if rate > 0 else 0.0
            print(f"  [{i+1:>5}/{len(work)}] ok={n_ok} err={n_err} "
                  f"rate={rate*60:.0f}/min ETA={eta:.0f}m")
    con.commit()
    con.execute(
        "UPDATE fp_run SET finished_at=?, summary_json=? WHERE run_pk=?",
        (now_iso(), json.dumps({"ok": n_ok, "err": n_err}), run_pk),
    )
    con.commit()
    print(f"\nfingerprint complete: ok={n_ok} err={n_err} in {(time.time()-t0)/60:.1f}m")


# ----------------------------------------------------------- match

def _path_overlap_score(v_view: tuple, a_view: tuple) -> float:
    """Heuristic boost from catalog metadata. Returns [0..1].
    Tuple shape: (shoot_label, shoot_date, camera_id)"""
    v_sl, v_sd, v_cam = v_view
    a_sl, a_sd, a_cam = a_view
    s = 0.0
    if v_sl and a_sl and v_sl == a_sl:
        s += 0.50
    elif v_sl and a_sl and (v_sl in a_sl or a_sl in v_sl):
        s += 0.25
    if v_sd and a_sd and v_sd == a_sd:
        s += 0.40
    elif v_sd and a_sd:
        # Same year-month
        if v_sd[:7] == a_sd[:7]:
            s += 0.10
    if v_cam and a_cam and v_cam.split("_")[0] == a_cam.split("_")[0]:
        s += 0.10
    return min(1.0, s)


def cmd_match(args: argparse.Namespace) -> None:
    from _fingerprint import unpack_fp, match_fingerprints, quick_prefilter_score
    con = open_db()
    cur = con.execute(
        "INSERT INTO fp_run (phase, started_at, args_json) VALUES (?, ?, ?)",
        ("match", now_iso(), json.dumps(vars(args), default=str)),
    )
    run_pk = cur.lastrowid
    con.commit()

    print(f"=== match | run_pk={run_pk} | {now_iso()} ===")

    # Pull all fingerprints into memory (a few MB)
    print("  loading fingerprints...")
    videos: list[dict] = []
    audios: list[dict] = []
    for row in con.execute(
        "SELECT asset_id, record_kind, shoot_label, shoot_date, camera_id, "
        "duration_sec, n_hashes, fp_blob FROM fingerprint"
    ):
        rec = {
            "asset_id": row[0], "record_kind": row[1],
            "shoot_label": row[2] or "", "shoot_date": row[3] or "",
            "camera_id": row[4] or "", "duration_sec": row[5],
            "n_hashes": row[6], "fp": unpack_fp(row[7]),
        }
        if row[1] == "video":
            videos.append(rec)
        else:
            audios.append(rec)
    print(f"  videos: {len(videos)}  audios: {len(audios)}")
    if not videos or not audios:
        print("  nothing to match.")
        return

    if args.limit_videos:
        videos = videos[: args.limit_videos]
        print(f"  --limit-videos: {len(videos)}")

    # Wipe existing proposals so re-runs are clean (cheap; small table)
    con.execute("DELETE FROM link_proposal")
    con.commit()

    t0 = time.time()
    n_compared = 0
    n_proposals = 0
    n_quick_skip = 0
    for vi, v in enumerate(videos):
        v_view = (v["shoot_label"], v["shoot_date"], v["camera_id"])
        # Build candidate audio set: same shoot OR same date OR same shoot-label
        # base (e.g. 2024-9-2_<shoot> matches 2024-9-2_*). Cuts the search space
        # without missing cross-day backups.
        cands: list[tuple[float, dict]] = []
        for a in audios:
            a_view = (a["shoot_label"], a["shoot_date"], a["camera_id"])
            path_overlap = _path_overlap_score(v_view, a_view)
            if path_overlap < 0.30:
                # No catalog overlap signal → skip full chromaprint match
                continue
            n_compared += 1
            # Quick prefilter on the first ~16 hashes
            if v["n_hashes"] >= 16 and a["n_hashes"] >= 16:
                quick = quick_prefilter_score(v["fp"], a["fp"], n_probe=16)
                if quick < DEFAULT_PREFILTER_FLOOR:
                    n_quick_skip += 1
                    continue
            sim, off = match_fingerprints(
                v["fp"], a["fp"],
                max_slide=args.max_slide_frames,
            )
            if sim < args.match_threshold:
                continue
            combined = 0.7 * sim + 0.3 * path_overlap
            cands.append((combined, sim, off, path_overlap, a))
        # Keep top-K per video
        cands.sort(key=lambda c: c[0], reverse=True)
        for rank, (combined, sim, off, path_overlap, a) in enumerate(cands[:TOP_K_PROPOSALS_PER_VIDEO], start=1):
            con.execute(
                "INSERT OR REPLACE INTO link_proposal "
                "(video_asset_id, audio_asset_id, raw_match_score, offset_frames, "
                " path_overlap_score, combined_score, candidate_rank, proposed_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (v["asset_id"], a["asset_id"], float(sim), int(off),
                 float(path_overlap), float(combined), rank, now_iso()),
            )
            n_proposals += 1
        if (vi + 1) % 200 == 0:
            con.commit()
            elapsed = time.time() - t0
            rate = (vi + 1) / elapsed
            print(f"  [{vi+1:>5}/{len(videos)}] proposals={n_proposals} "
                  f"compared={n_compared} quick_skip={n_quick_skip} "
                  f"rate={rate*60:.0f}/min")
    con.commit()
    con.execute(
        "UPDATE fp_run SET finished_at=?, summary_json=? WHERE run_pk=?",
        (now_iso(), json.dumps({
            "n_videos": len(videos),
            "n_audios": len(audios),
            "n_compared": n_compared,
            "n_quick_skip": n_quick_skip,
            "n_proposals": n_proposals,
            "match_threshold": args.match_threshold,
        }), run_pk),
    )
    con.commit()
    print(f"\nmatch complete: {n_proposals} proposals over {n_compared} compared pairs "
          f"({n_quick_skip} prefilter-skipped) in {(time.time()-t0)/60:.1f}m")


# ----------------------------------------------------------- apply (stub)

def _atomic_write_json(path: Path, data: dict) -> None:
    """tmp.write + os.replace. Safe on native FS (the workspace SSD directly, not bindfs)."""
    import os
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    os.replace(tmp, path)


def cmd_apply(args: argparse.Namespace) -> None:
    """Write high-confidence chromaprint proposals into catalog `linked_assets`.

    Uses the PC-side typed-relation convention:
      - Audio side: linked_assets.video[].link_kind = "audio_video_transcript"
      - Video side: linked_assets.audio[].link_kind = "audio_video_reverse"
      - Both:        established_by = "chromaprint_pairwise_match"
      - Both:        carry raw_match_score / combined_score / offset_frames
                     for future score-gated query filters

    Idempotent: if the target_asset_id is already in the linked_assets slot
    (by any prior establisher), we skip the catalog write but still record the
    chromaprint confirmation in applied_link for traceability.
    """
    con = open_db()
    cur = con.execute(
        "INSERT INTO fp_run (phase, started_at, args_json) VALUES (?, ?, ?)",
        ("apply", now_iso(), json.dumps(vars(args), default=str)),
    )
    run_pk = cur.lastrowid
    con.commit()

    print(f"=== apply | run_pk={run_pk} | {now_iso()} ===")
    print(f"  threshold: {args.apply_threshold}  dry-run: {args.dry_run}")

    rows = con.execute(
        "SELECT video_asset_id, audio_asset_id, raw_match_score, path_overlap_score, "
        "combined_score, offset_frames FROM link_proposal "
        "WHERE combined_score >= ? AND candidate_rank=1 "
        "ORDER BY combined_score DESC", (args.apply_threshold,),
    ).fetchall()
    print(f"  proposals to consider: {len(rows)}")
    if args.limit:
        rows = rows[: args.limit]
        print(f"  --limit: {len(rows)}")
    if not rows:
        return

    n_wrote_both = 0
    n_wrote_one = 0
    n_skipped_already = 0
    n_missing_file = 0
    n_errors = 0
    for vid_aid, aud_aid, raw, path_s, comb, off in rows:
        vf = next(VIDEO_CATALOG.glob(f"{vid_aid}*.video.json"), None)
        af = next(AUDIO_CATALOG.glob(f"{aud_aid}*.audio.json"), None)
        if vf is None or af is None:
            n_missing_file += 1
            continue
        try:
            vdata = json.loads(vf.read_text())
            adata = json.loads(af.read_text())
        except Exception:
            n_errors += 1
            continue

        # Existing-link check
        v_links = (vdata.get("linked_assets") or {}).get("audio") or []
        a_links = (adata.get("linked_assets") or {}).get("video") or []
        v_has = any(
            isinstance(l, dict) and l.get("target_asset_id") == aud_aid for l in v_links
        )
        a_has = any(
            isinstance(l, dict) and l.get("target_asset_id") == vid_aid for l in a_links
        )

        if v_has and a_has:
            n_skipped_already += 1
            # Still record in applied_link so we can answer "which proposals did chromaprint confirm"
            con.execute(
                "INSERT OR IGNORE INTO applied_link (video_asset_id, audio_asset_id, "
                "raw_match_score, combined_score, applied_at, applied_run_pk) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (vid_aid, aud_aid, float(raw), float(comb), now_iso(), run_pk),
            )
            continue

        v_link_obj = {
            "target_asset_id": aud_aid,
            "link_kind": "audio_video_reverse",
            "established_by": "chromaprint_pairwise_match",
            "raw_match_score": round(float(raw), 4),
            "combined_score": round(float(comb), 4),
            "offset_frames": int(off),
        }
        a_link_obj = {
            "target_asset_id": vid_aid,
            "link_kind": "audio_video_transcript",
            "established_by": "chromaprint_pairwise_match",
            "raw_match_score": round(float(raw), 4),
            "combined_score": round(float(comb), 4),
            "offset_frames": int(off),
        }

        wrote_v = False; wrote_a = False
        if not v_has:
            vdata.setdefault("linked_assets", {}).setdefault("audio", []).append(v_link_obj)
            wrote_v = True
        if not a_has:
            adata.setdefault("linked_assets", {}).setdefault("video", []).append(a_link_obj)
            wrote_a = True

        if args.dry_run:
            if wrote_v and wrote_a:
                n_wrote_both += 1
            elif wrote_v or wrote_a:
                n_wrote_one += 1
            continue

        try:
            if wrote_v:
                _atomic_write_json(vf, vdata)
            if wrote_a:
                _atomic_write_json(af, adata)
        except Exception as e:
            n_errors += 1
            print(f"  ERROR writing pair {vid_aid[:8]} / {aud_aid[:8]}: {e}")
            continue

        con.execute(
            "INSERT OR REPLACE INTO applied_link (video_asset_id, audio_asset_id, "
            "raw_match_score, combined_score, applied_at, applied_run_pk) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (vid_aid, aud_aid, float(raw), float(comb), now_iso(), run_pk),
        )
        if wrote_v and wrote_a:
            n_wrote_both += 1
        else:
            n_wrote_one += 1
        if (n_wrote_both + n_wrote_one) % 25 == 0:
            con.commit()

    con.commit()
    con.execute(
        "UPDATE fp_run SET finished_at=?, summary_json=? WHERE run_pk=?",
        (now_iso(), json.dumps({
            "wrote_both_sides": n_wrote_both,
            "wrote_one_side": n_wrote_one,
            "skipped_already_linked": n_skipped_already,
            "missing_file": n_missing_file,
            "errors": n_errors,
            "apply_threshold": args.apply_threshold,
            "dry_run": args.dry_run,
        }), run_pk),
    )
    con.commit()
    mode = "(DRY RUN — no files written)" if args.dry_run else "(WROTE)"
    print(f"\napply complete {mode}: "
          f"both_sides={n_wrote_both} one_side={n_wrote_one} "
          f"skipped_already={n_skipped_already} missing_file={n_missing_file} errors={n_errors}")


# ----------------------------------------------------------- status

def cmd_status(args: argparse.Namespace) -> None:
    if not AUDIO_FINGERPRINT_DB.exists():
        print("(audio_fingerprints.sqlite not yet created)")
        return
    con = sqlite3.connect(str(AUDIO_FINGERPRINT_DB))
    print(f"=== audio_fingerprint status ===")
    print(f"  db: {AUDIO_FINGERPRINT_DB}")
    n_fp_v = con.execute(
        "SELECT COUNT(*) FROM fingerprint WHERE record_kind='video'").fetchone()[0]
    n_fp_a = con.execute(
        "SELECT COUNT(*) FROM fingerprint WHERE record_kind='audio'").fetchone()[0]
    n_proc = con.execute(
        "SELECT COUNT(*) FROM fp_processed WHERE success=1").fetchone()[0]
    print(f"  fingerprints: video {n_fp_v:,}  audio {n_fp_a:,}  processed_ok {n_proc:,}")
    n_lp = con.execute("SELECT COUNT(*) FROM link_proposal").fetchone()[0]
    print(f"  link proposals: {n_lp:,}")
    if n_lp:
        rows = con.execute(
            "SELECT video_asset_id, audio_asset_id, raw_match_score, path_overlap_score, "
            "combined_score FROM link_proposal "
            "WHERE candidate_rank=1 "
            "ORDER BY combined_score DESC LIMIT 20"
        ).fetchall()
        print(f"  top-20 (rank-1) proposals:")
        for r in rows:
            print(f"    {r[0][:12]} ←→ {r[1][:12]}  raw={r[2]:.3f}  path={r[3]:.2f}  combined={r[4]:.3f}")
    print(f"  runs:")
    for run_pk, phase, started_at, finished_at, summary_json in con.execute(
        "SELECT run_pk, phase, started_at, finished_at, summary_json FROM fp_run "
        "ORDER BY run_pk DESC LIMIT 6"
    ).fetchall():
        s = summary_json or ""
        print(f"    run {run_pk}: {phase} {started_at} → {finished_at or '(in progress)'}  {s[:120]}")
    con.close()


# ----------------------------------------------------------- main

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("fingerprint", help="Compute chromaprint for every WAV")
    sp.add_argument("--limit", type=int)
    sp.add_argument("--length", type=int, default=0,
                    help="seconds to fingerprint (0 = full file)")
    sp.set_defaults(func=cmd_fingerprint)

    sp = sub.add_parser("match", help="Pairwise match video↔audio fingerprints")
    sp.add_argument("--match-threshold", type=float, default=DEFAULT_MATCH_THRESHOLD)
    sp.add_argument("--limit-videos", type=int,
                    help="cap videos processed (smoke / debug)")
    sp.add_argument("--max-slide-frames", type=int, default=0,
                    help="cap sliding-window search (0 = full)")
    sp.set_defaults(func=cmd_match)

    sp = sub.add_parser("apply", help="Write high-conf proposals into catalog linked_assets")
    sp.add_argument("--apply-threshold", type=float, default=DEFAULT_APPLY_THRESHOLD,
                    help=f"min combined_score to apply (default {DEFAULT_APPLY_THRESHOLD})")
    sp.add_argument("--limit", type=int, help="cap proposals applied (smoke / debug)")
    sp.add_argument("--dry-run", action="store_true",
                    help="report what would be written; do NOT touch catalog files")
    sp.set_defaults(func=cmd_apply)

    sp = sub.add_parser("status", help="Coverage + recent runs + top proposals")
    sp.set_defaults(func=cmd_status)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
