#!/usr/bin/env python3
"""classify_audio_role.py — Tag chromaprint-applied links by production-audio role.

For each chromaprint-`apply`'d link in `audio_fingerprints.sqlite::applied_link`,
classify the audio path as one of:
  - audio_source  — clean production-recorder audio (DJI Mic lavalier, RED .RDC
                    XLR-input audio, Zoom/Sony PCM/Tentacle Track recorders, or
                    anything under an `Audio/` folder in the source tree)
  - audio_overlap — coincidental acoustic match without production-source signal
                    (default if no recognizable production-audio pattern fits)

Two writes per pair:
  1. UPDATE `applied_link` with new `audio_role` + `audio_role_reason` columns
  2. UPDATE catalog `linked_assets[*]` for the link object with `audio_role` +
     `audio_role_reason` fields, atomic-write per JSON

Idempotent. Re-run safe. Path classifier is the source of truth — re-running
re-classifies all rows from scratch (so editing the classifier and re-running
flows through to catalog).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from _paths import AUDIO_FINGERPRINT_DB, VIDEO_CATALOG, AUDIO_CATALOG  # noqa: E402

# Production-audio path patterns. Order matters: most-specific first.
_DJI_LAVALIER = re.compile(r"DJI_\d+_\d{8}_\d{6}\.WAV$", re.IGNORECASE)
_RED_RDC     = re.compile(r"\.RDC[/\\][^/\\]+\.wav$", re.IGNORECASE)
_TENTACLE    = re.compile(r"Tentacle\s*Track", re.IGNORECASE)
_ZOOM        = re.compile(r"ZOOM\d+", re.IGNORECASE)
_SONY_PCM    = re.compile(r"C\d{4}[^/\\]*\.(wav|mp3)$", re.IGNORECASE)
_AUDIO_FOLDER = re.compile(r"[/\\]Audio[/\\]", re.IGNORECASE)


def classify(wav_path: str) -> tuple[str, str]:
    """Return (role, reason). `role` ∈ {'audio_source','audio_overlap'}."""
    if not wav_path:
        return "audio_overlap", "no path"
    if _DJI_LAVALIER.search(wav_path):
        return "audio_source", "DJI Mic 2 wireless lavalier"
    if _RED_RDC.search(wav_path):
        return "audio_source", "RED .RDC bundle XLR-input audio"
    if _TENTACLE.search(wav_path):
        return "audio_source", "Tentacle Track production bag recorder"
    if _ZOOM.search(wav_path):
        return "audio_source", "Zoom handheld recorder"
    if _SONY_PCM.search(wav_path) and _AUDIO_FOLDER.search(wav_path):
        return "audio_source", "Sony PCM handheld recorder in Audio/ folder"
    if _AUDIO_FOLDER.search(wav_path):
        return "audio_source", "production audio folder (unrecognized recorder)"
    return "audio_overlap", "no production-audio signal in path"


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _add_db_columns(con: sqlite3.Connection) -> None:
    cols = {r[1] for r in con.execute("PRAGMA table_info(applied_link)").fetchall()}
    if "audio_role" not in cols:
        con.execute("ALTER TABLE applied_link ADD COLUMN audio_role TEXT")
    if "audio_role_reason" not in cols:
        con.execute("ALTER TABLE applied_link ADD COLUMN audio_role_reason TEXT")
    if "audio_role_classified_at" not in cols:
        con.execute("ALTER TABLE applied_link ADD COLUMN audio_role_classified_at TEXT")
    con.execute("CREATE INDEX IF NOT EXISTS al_audio_role ON applied_link(audio_role)")
    con.commit()


def _atomic_write_json(path: Path, data: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    os.replace(tmp, path)


def _classify_catalog_link(rec: dict, target_aid: str, kind: str,
                           role: str, reason: str) -> bool:
    """Update the matching link object in rec['linked_assets'][kind].
    `kind` ∈ {'audio','video'}. Returns True if modified."""
    la = rec.get("linked_assets") or {}
    slot = la.get(kind) or []
    modified = False
    for link in slot:
        if not isinstance(link, dict):
            continue
        if link.get("target_asset_id") != target_aid:
            continue
        if link.get("established_by") != "chromaprint_pairwise_match":
            continue  # only tag chromaprint-derived links; leave others alone
        # Only mark as modified if values actually change (idempotent)
        if link.get("audio_role") != role or link.get("audio_role_reason") != reason:
            link["audio_role"] = role
            link["audio_role_reason"] = reason
            modified = True
    return modified


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would be changed; do NOT touch DB or catalog")
    args = ap.parse_args()

    print(f"=== classify_audio_role | {now_iso()} ===")
    print(f"  dry-run: {args.dry_run}")

    con = sqlite3.connect(str(AUDIO_FINGERPRINT_DB))
    if not args.dry_run:
        _add_db_columns(con)

    rows = con.execute("""
        SELECT al.video_asset_id, al.audio_asset_id, fp.wav_path
        FROM applied_link al JOIN fingerprint fp ON fp.asset_id = al.audio_asset_id
    """).fetchall()
    print(f"  applied_link rows to classify: {len(rows):,}")

    classifications: list[tuple[str, str, str, str]] = []
    from collections import Counter
    reasons = Counter()
    for v_aid, a_aid, wp in rows:
        role, reason = classify(wp or "")
        classifications.append((v_aid, a_aid, role, reason))
        reasons[(role, reason)] += 1
    print("\n  classification breakdown:")
    for (role, reason), n in reasons.most_common():
        print(f"    {n:>4}  {role:<14}  {reason}")

    if args.dry_run:
        print("\n(DRY RUN — no DB updates, no catalog writes)")
        return

    # Step 1: persist to applied_link
    ts = now_iso()
    con.executemany(
        "UPDATE applied_link SET audio_role=?, audio_role_reason=?, audio_role_classified_at=? "
        "WHERE video_asset_id=? AND audio_asset_id=?",
        [(role, reason, ts, v, a) for (v, a, role, reason) in classifications],
    )
    con.commit()
    print(f"\n  applied_link table: {len(classifications):,} rows updated")

    # Step 2: write back to catalog JSONs (both video + audio sides)
    n_video_modified = 0
    n_audio_modified = 0
    n_errors = 0
    for v_aid, a_aid, role, reason in classifications:
        for cat_dir, target_aid, link_aid, kind in (
            (VIDEO_CATALOG, v_aid, a_aid, "audio"),
            (AUDIO_CATALOG, a_aid, v_aid, "video"),
        ):
            f = next(cat_dir.glob(f"{target_aid}*.json"), None)
            if not f:
                n_errors += 1
                continue
            try:
                rec = json.loads(f.read_text())
            except Exception:
                n_errors += 1
                continue
            if _classify_catalog_link(rec, link_aid, kind, role, reason):
                try:
                    _atomic_write_json(f, rec)
                    if kind == "audio":
                        n_video_modified += 1
                    else:
                        n_audio_modified += 1
                except Exception as e:
                    n_errors += 1
                    print(f"  [error writing {f.name}]: {e}", file=sys.stderr)

    print(f"\n  catalog file mutations:")
    print(f"    video JSONs updated: {n_video_modified:,}")
    print(f"    audio JSONs updated: {n_audio_modified:,}")
    print(f"    errors: {n_errors}")
    print(f"\nDone. Re-run `build_editor_db.py` if the link-shape change should flow into editorial_catalog.")


if __name__ == "__main__":
    main()
