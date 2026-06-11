#!/usr/bin/env python3
"""rollback_chromaprint_apply.py — Undo the chromaprint `apply` pass.

Walks every catalog video + audio JSON, removes entries from `linked_assets`
where `established_by == "chromaprint_pairwise_match"`, and atomic-writes the
result. Idempotent — safe to re-run.

The chromaprint-derived links are tagged so they can be selectively removed
without touching links established by other pipelines (snippet match,
transcript similarity, manual annotation).

After running, also clears `applied_link` rows in `audio_fingerprints.sqlite`
so a future `apply` pass re-discovers them fresh.

Usage:
  python3 rollback_chromaprint_apply.py --dry-run     # preview
  python3 rollback_chromaprint_apply.py               # actually undo
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from _paths import (  # noqa: E402
    VIDEO_CATALOG, AUDIO_CATALOG, AUDIO_FINGERPRINT_DB,
)

CHROMAPRINT_ESTABLISHED_BY = "chromaprint_pairwise_match"


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _atomic_write_json(path: Path, data: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    os.replace(tmp, path)


def _scrub_record(rec: dict) -> tuple[bool, int]:
    """Remove chromaprint-established links from rec['linked_assets'].
    Returns (modified, n_removed)."""
    la = rec.get("linked_assets") or {}
    if not isinstance(la, dict):
        return False, 0
    n_removed = 0
    modified = False
    for kind in ("video", "audio", "stills"):
        slot = la.get(kind) or []
        if not isinstance(slot, list):
            continue
        before = len(slot)
        kept = [
            l for l in slot
            if not (isinstance(l, dict) and l.get("established_by") == CHROMAPRINT_ESTABLISHED_BY)
        ]
        if len(kept) != before:
            la[kind] = kept
            modified = True
            n_removed += before - len(kept)
    if modified:
        rec["linked_assets"] = la
    return modified, n_removed


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would be changed; do NOT touch catalog or DB")
    args = ap.parse_args()

    print(f"=== rollback_chromaprint_apply | {now_iso()} ===")
    print(f"  dry-run: {args.dry_run}")
    print(f"  establishd-by tag scrubbed: {CHROMAPRINT_ESTABLISHED_BY}")

    n_files = 0
    n_modified = 0
    n_total_removed = 0
    n_errors = 0

    for cat_dir, suffix in (
        (VIDEO_CATALOG, ".video.json"),
        (AUDIO_CATALOG, ".audio.json"),
    ):
        for f in cat_dir.glob(f"*{suffix}"):
            if f.name.startswith("._"): continue  # macOS AppleDouble sidecar
            n_files += 1
            try:
                rec = json.loads(f.read_text())
            except Exception as e:
                n_errors += 1
                continue
            modified, n_rm = _scrub_record(rec)
            if modified:
                n_modified += 1
                n_total_removed += n_rm
                if not args.dry_run:
                    try:
                        _atomic_write_json(f, rec)
                    except Exception as e:
                        n_errors += 1
                        print(f"  [error] writing {f.name}: {e}", file=sys.stderr)

    print(f"\n=== summary ===")
    print(f"  catalog files scanned: {n_files:,}")
    print(f"  files modified:        {n_modified:,}")
    print(f"  link objects removed:  {n_total_removed:,}")
    print(f"  errors:                {n_errors}")

    # Also clear applied_link rows so the trail is consistent
    if AUDIO_FINGERPRINT_DB.exists():
        if args.dry_run:
            con = sqlite3.connect(f"file:{AUDIO_FINGERPRINT_DB}?mode=ro", uri=True)
            n_db = con.execute("SELECT COUNT(*) FROM applied_link").fetchone()[0]
            print(f"  audio_fingerprints.sqlite::applied_link: {n_db:,} rows (would clear)")
        else:
            con = sqlite3.connect(str(AUDIO_FINGERPRINT_DB))
            n_db = con.execute("SELECT COUNT(*) FROM applied_link").fetchone()[0]
            con.execute("DELETE FROM applied_link")
            con.commit()
            con.close()
            print(f"  cleared applied_link: {n_db:,} rows")

    if args.dry_run:
        print(f"\n(DRY RUN — nothing written. Drop --dry-run to apply.)")
    else:
        print(f"\nRollback complete. Re-run `build_editor_db.py` to refresh the editor projection.")


if __name__ == "__main__":
    main()
