#!/usr/bin/env python3
"""face_queries.py — Editorial queries against the projected `frame_face` table.

Reads `indexes/editorial_catalog.sqlite` (built by `dataset/_scripts/build_editor_db.py`).
Subcommands:

  by-person      assets where a named person appears, ranked by face count
  co-appearance  assets where two named people both appear
  on-screen      timeline of who's on screen for a specific asset
  screen-time    estimated screen-time per person (assumes 1 keyframe = 7 sec)
  solo           assets where ONLY one named person appears (no other tagged faces)
  status         coverage stats

Names resolve through the same alias logic used by build_face_index.py —
typing `michelino`, `mike sunseri`, or `p_michelino_sunseri` all work.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from build_face_index import _load_humans_full, _build_alias_map, _resolve_name  # noqa: E402
from _paths import INDEXES_DIR  # noqa: E402

DB = INDEXES_DIR / "editorial_catalog.sqlite"
KEYFRAME_INTERVAL_SEC = 7  # SigLIP keyframe cadence; used for screen-time estimation


def _open() -> sqlite3.Connection:
    return sqlite3.connect(f"file:{DB}?mode=ro", uri=True)


def _resolve(name: str) -> str | None:
    humans_full = _load_humans_full()
    alias_map = _build_alias_map(humans_full)
    cands = _resolve_name(name, alias_map)
    if len(cands) == 1:
        return next(iter(cands))
    if len(cands) == 0:
        print(f"unknown name '{name}'", file=sys.stderr)
        return None
    print(f"'{name}' matches {len(cands)} people — try a fuller name:", file=sys.stderr)
    humans = {p["id"]: p.get("canonical_name", "") for p in humans_full}
    for pid in sorted(cands):
        print(f"  {pid}  ({humans.get(pid, '')})", file=sys.stderr)
    return None


def _canonical(con: sqlite3.Connection, pid: str) -> str:
    humans_full = _load_humans_full()
    for p in humans_full:
        if p.get("id") == pid:
            return p.get("canonical_name") or pid
    return pid


# ---------------- by-person ----------------

def cmd_by_person(args: argparse.Namespace) -> None:
    pid = _resolve(args.name)
    if not pid:
        sys.exit(2)
    con = _open()
    print(f"=== assets containing {_canonical(con, pid)} ({pid}) ===\n")
    rows = list(con.execute("""
        SELECT a.asset_id, COUNT(*) n_faces,
               COALESCE(a.shoot_label, a.category_name, '?') shoot,
               a.asset_type, a.semantic_subject, a.duration_sec
        FROM frame_face ff JOIN asset a ON ff.asset_id = a.asset_id
        WHERE ff.p_id = ?
        GROUP BY a.asset_id ORDER BY n_faces DESC LIMIT ?
    """, (pid, args.limit)))
    print(f"  total assets: {len(rows)}  (showing top {args.limit if len(rows) > args.limit else len(rows)})\n")
    print(f"  {'asset_id':>12} {'faces':>5} {'dur':>5} {'shoot':<26} {'type':<10}  subject")
    for r in rows:
        aid, n, shoot, atype, subj, dur = r
        print(f"  {aid[:12]:>12} {n:5d} {(dur or 0):5.0f} {(shoot or '?')[:26]:26s} {(atype or '?')[:10]:10s}  {(subj or '')[:60]}")


# ---------------- co-appearance ----------------

def cmd_co_appearance(args: argparse.Namespace) -> None:
    pid_a = _resolve(args.name_a)
    pid_b = _resolve(args.name_b)
    if not pid_a or not pid_b:
        sys.exit(2)
    con = _open()
    print(f"=== assets where both {_canonical(con, pid_a)} and {_canonical(con, pid_b)} appear ===\n")
    rows = list(con.execute("""
        SELECT a.asset_id,
               COUNT(DISTINCT CASE WHEN ff.p_id=? THEN ff.frame_idx END) n_a,
               COUNT(DISTINCT CASE WHEN ff.p_id=? THEN ff.frame_idx END) n_b,
               COALESCE(a.shoot_label, a.category_name, '?') shoot,
               a.asset_type, a.semantic_subject
        FROM frame_face ff JOIN asset a ON ff.asset_id = a.asset_id
        WHERE ff.p_id IN (?, ?)
        GROUP BY a.asset_id
        HAVING n_a > 0 AND n_b > 0
        ORDER BY (n_a + n_b) DESC LIMIT ?
    """, (pid_a, pid_b, pid_a, pid_b, args.limit)))
    print(f"  co-appearance assets: {len(rows)}\n")
    print(f"  {'asset_id':>12} {'A':>4} {'B':>4} {'shoot':<26} {'type':<10}  subject")
    for r in rows:
        aid, n_a, n_b, shoot, atype, subj = r
        print(f"  {aid[:12]:>12} {n_a:4d} {n_b:4d} {(shoot or '?')[:26]:26s} {(atype or '?')[:10]:10s}  {(subj or '')[:60]}")


# ---------------- on-screen timeline ----------------

def cmd_on_screen(args: argparse.Namespace) -> None:
    con = _open()
    a = con.execute(
        "SELECT asset_id, shoot_label, asset_type, semantic_subject, duration_sec FROM asset WHERE asset_id LIKE ?",
        (args.asset_id + "%",),
    ).fetchone()
    if not a:
        print(f"no asset matching '{args.asset_id}'", file=sys.stderr)
        sys.exit(2)
    aid, shoot, atype, subj, dur = a
    print(f"=== on-screen timeline: {aid[:12]} ({atype or '?'}, {(shoot or '?')}, {dur or 0:.0f}s) ===")
    if subj: print(f"  subject: {subj[:120]}")
    print()
    rows = list(con.execute("""
        SELECT ff.frame_time_sec, ff.p_id, ff.det_score
        FROM frame_face ff WHERE ff.asset_id=?
        ORDER BY ff.frame_time_sec, ff.p_id
    """, (aid,)))
    if not rows:
        print("  no named faces in this asset.")
        return
    humans_full = _load_humans_full()
    humans = {p["id"]: p.get("canonical_name", "") for p in humans_full}
    print(f"  {'time':>7}  {'person':<28}  score")
    for ts, pid, score in rows:
        print(f"  {ts or 0:7.1f}  {humans.get(pid, pid):28s}  {score:.2f}")


# ---------------- screen-time ----------------

def cmd_screen_time(args: argparse.Namespace) -> None:
    con = _open()
    humans_full = _load_humans_full()
    humans = {p["id"]: p.get("canonical_name", "") for p in humans_full}
    rows = list(con.execute("""
        SELECT p_id, COUNT(DISTINCT (asset_id || ':' || CAST(frame_idx AS TEXT))) n_unique_frames,
               COUNT(DISTINCT asset_id) n_assets
        FROM frame_face GROUP BY p_id ORDER BY n_unique_frames DESC
    """))
    print(f"=== estimated screen-time (1 keyframe = {KEYFRAME_INTERVAL_SEC}s sample) ===\n")
    print(f"  {'minutes':>9}  {'frames':>7}  {'assets':>6}  person")
    for pid, frames, assets in rows:
        mins = frames * KEYFRAME_INTERVAL_SEC / 60.0
        print(f"  {mins:9.1f}  {frames:7d}  {assets:6d}  {humans.get(pid, pid)}")


# ---------------- solo ----------------

def cmd_solo(args: argparse.Namespace) -> None:
    pid = _resolve(args.name)
    if not pid: sys.exit(2)
    con = _open()
    print(f"=== assets where ONLY {_canonical(con, pid)} appears (no other named faces) ===\n")
    rows = list(con.execute("""
        SELECT a.asset_id, COUNT(*) n_faces,
               COALESCE(a.shoot_label, a.category_name, '?') shoot,
               a.asset_type, a.semantic_subject
        FROM frame_face ff JOIN asset a ON ff.asset_id = a.asset_id
        WHERE ff.p_id = ?
          AND NOT EXISTS (
              SELECT 1 FROM frame_face ff2
              WHERE ff2.asset_id = ff.asset_id AND ff2.p_id != ?
          )
        GROUP BY a.asset_id ORDER BY n_faces DESC LIMIT ?
    """, (pid, pid, args.limit)))
    print(f"  solo assets: {len(rows)}\n")
    print(f"  {'asset_id':>12} {'faces':>5} {'shoot':<26} {'type':<10}  subject")
    for r in rows:
        aid, n, shoot, atype, subj = r
        print(f"  {aid[:12]:>12} {n:5d} {(shoot or '?')[:26]:26s} {(atype or '?')[:10]:10s}  {(subj or '')[:60]}")


# ---------------- status ----------------

def cmd_status(args: argparse.Namespace) -> None:
    con = _open()
    n_rows = con.execute("SELECT COUNT(*) FROM frame_face").fetchone()[0]
    n_assets = con.execute("SELECT COUNT(DISTINCT asset_id) FROM frame_face").fetchone()[0]
    n_people = con.execute("SELECT COUNT(DISTINCT p_id) FROM frame_face").fetchone()[0]
    print(f"=== frame_face coverage in editorial_catalog.sqlite ===")
    print(f"  rows:    {n_rows}")
    print(f"  assets:  {n_assets}")
    print(f"  people:  {n_people}")
    humans_full = _load_humans_full()
    humans = {p["id"]: p.get("canonical_name", "") for p in humans_full}
    print(f"\n  top 15 by row count:")
    for r in con.execute(
        "SELECT p_id, COUNT(*) c, COUNT(DISTINCT asset_id) a FROM frame_face GROUP BY p_id ORDER BY c DESC LIMIT 15"
    ):
        print(f"    {r[1]:6d} rows  {r[2]:4d} assets  {humans.get(r[0], r[0])}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p1 = sub.add_parser("by-person", help="assets where a person appears")
    p1.add_argument("name", help="canonical name, alias, or p_id (e.g. 'michelino', 'kit', 'cb')")
    p1.add_argument("--limit", type=int, default=30)
    p1.set_defaults(func=cmd_by_person)

    p2 = sub.add_parser("co-appearance", help="assets where two people both appear")
    p2.add_argument("name_a"); p2.add_argument("name_b")
    p2.add_argument("--limit", type=int, default=30)
    p2.set_defaults(func=cmd_co_appearance)

    p3 = sub.add_parser("on-screen", help="timeline of named faces in one asset")
    p3.add_argument("asset_id", help="full or prefix asset_id (e.g. first 12 chars)")
    p3.set_defaults(func=cmd_on_screen)

    p4 = sub.add_parser("screen-time", help="estimated screen-time per person")
    p4.set_defaults(func=cmd_screen_time)

    p5 = sub.add_parser("solo", help="assets where ONLY one named person appears")
    p5.add_argument("name"); p5.add_argument("--limit", type=int, default=30)
    p5.set_defaults(func=cmd_solo)

    p6 = sub.add_parser("status", help="frame_face coverage stats")
    p6.set_defaults(func=cmd_status)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
