#!/usr/bin/env python3
"""Append or update a per-asset editor_notes entry.

Resolves the right asset_id (multiple shoots reuse C-numbers), reads any
existing notes file, deduplicates against existing entries of the same
type+text substance, appends/updates, unions tags, and writes the file
back. Writer; lives in dataset/_scripts/ per the dataset-writes vs queries
boundary.

Usage:
  py dataset/_scripts/append_editor_note.py \\
      --asset-id <sha256> \\
      --type stability \\
      --text "Wobble extends past t=8s; use src 25-31 for stable middle window" \\
      --tag wobble_at_start --tag static_middle_only \\
      --session <scene_session_tag>

  # Resolve by filename + shoot date when you don't have the asset_id handy:
  py dataset/_scripts/append_editor_note.py \\
      --filename C0150.MP4 --shoot-date 2025-08-16 \\
      --type framing --text "..."

Schema and tag vocabulary: dataset/assets/editor_notes/_schema.md
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sqlite3
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))
from workspace_paths import dataset_root, editorial_catalog_sqlite_path  # noqa: E402


def _notes_dir():
    return dataset_root() / "assets" / "catalog" / "editor_notes"


def _resolve_asset(asset_id=None, filename=None, shoot_date=None):
    if asset_id:
        return asset_id
    if not (filename and shoot_date):
        raise SystemExit("Provide --asset-id, OR both --filename and --shoot-date")
    con = sqlite3.connect(str(editorial_catalog_sqlite_path()))
    rows = con.execute(
        "SELECT asset_id FROM asset WHERE filename = ? AND shoot_date = ?",
        (filename, shoot_date),
    ).fetchall()
    con.close()
    if not rows:
        raise SystemExit(f"No asset matches filename={filename} shoot_date={shoot_date}")
    if len(rows) > 1:
        raise SystemExit(
            f"Ambiguous: {len(rows)} assets match filename={filename} shoot_date={shoot_date}. "
            f"Specify --asset-id directly."
        )
    return rows[0][0]


def _lookup_filename_and_date(asset_id):
    con = sqlite3.connect(str(editorial_catalog_sqlite_path()))
    r = con.execute("SELECT filename, shoot_date FROM asset WHERE asset_id = ?", (asset_id,)).fetchone()
    con.close()
    if not r:
        raise SystemExit(f"asset_id not in catalog: {asset_id}")
    return r[0], r[1]


def _is_duplicate(existing_notes, new_type, new_text):
    """Same type AND substantial text overlap = duplicate. Skip in that case."""
    new_words = set(new_text.lower().split())
    for n in existing_notes:
        if n.get("type") != new_type:
            continue
        old_words = set((n.get("text") or "").lower().split())
        if not old_words:
            continue
        overlap = len(new_words & old_words) / max(len(new_words | old_words), 1)
        if overlap >= 0.7:  # 70% word overlap = same finding
            return n
    return None


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--asset-id")
    ap.add_argument("--filename")
    ap.add_argument("--shoot-date")
    ap.add_argument("--type", required=True,
                    choices=["stability", "framing", "audio", "subject", "pacing", "usage", "avoid", "prefer"])
    ap.add_argument("--text", required=True)
    ap.add_argument("--tag", action="append", default=[], help="repeatable")
    ap.add_argument("--session", help="optional session label")
    ap.add_argument("--date", help="YYYY-MM-DD; defaults to today UTC")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    asset_id = _resolve_asset(args.asset_id, args.filename, args.shoot_date)
    filename, shoot_date = _lookup_filename_and_date(asset_id)
    date_str = args.date or dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")

    notes_dir = _notes_dir()
    notes_dir.mkdir(parents=True, exist_ok=True)
    path = notes_dir / f"{asset_id}_editor_notes.json"

    if path.exists():
        body = json.loads(path.read_text(encoding="utf-8"))
    else:
        body = {
            "asset_id": asset_id,
            "filename": filename,
            "shoot_date": shoot_date,
            "notes": [],
            "tags": [],
        }

    existing_notes = body.get("notes", [])
    dup = _is_duplicate(existing_notes, args.type, args.text)
    if dup:
        print(f"[dedup] existing note of type={args.type} with >=70% word overlap found; replacing")
        print(f"        old: {dup.get('text','')[:120]}")
        print(f"        new: {args.text[:120]}")
        # Update in place
        dup["date"] = date_str
        dup["text"] = args.text
        if args.session:
            dup["session"] = args.session
    else:
        entry = {"date": date_str, "type": args.type, "text": args.text}
        if args.session:
            entry["session"] = args.session
        existing_notes.append(entry)
        body["notes"] = existing_notes

    # Union tags
    if args.tag:
        existing_tags = set(body.get("tags") or [])
        existing_tags.update(args.tag)
        body["tags"] = sorted(existing_tags)

    out = json.dumps(body, indent=2) + "\n"
    print(f"[asset]   {filename} ({asset_id[:12]}..) shoot_date={shoot_date}")
    print(f"[file]    {path}")
    print(f"[notes]   {len(body['notes'])} total ({'replaced' if dup else 'appended'})")
    print(f"[tags]    {body['tags']}")
    if args.dry_run:
        print("[dry-run] not writing")
        print(out)
        return 0
    path.write_text(out, encoding="utf-8")
    print(f"[ok] wrote {len(out)} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
