"""Filters over `editorial_catalog.sqlite` for restricting similarity searches.

Two flavours:

- `asset_allowlist()` returns just the `set[asset_id]` — used to pre-filter
  vector candidates before scoring.
- `search_broll()` returns full asset rows for an editor-facing b-roll lookup.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Iterable, Optional

from ._paths import editorial_catalog_sqlite_path


def asset_allowlist(
    *,
    catalog_db: Optional[Path] = None,
    bucket: Optional[str] = None,
    asset_type: Optional[str] = None,
    record_kind: Optional[str] = "video",
    shoot_date_from: Optional[str] = None,  # YYYY-MM-DD inclusive
    shoot_date_to: Optional[str] = None,  # YYYY-MM-DD inclusive
    place_id: Optional[str] = None,
    person_ids: Optional[Iterable[str]] = None,
    exclude_assets: Optional[Iterable[str]] = None,
    camera_movement: Optional[str] = None,
    shot_size: Optional[str] = None,
) -> set[str]:
    """Return the set of asset_ids matching all provided filters.

    None / empty filter args mean unrestricted (no clause added). `record_kind`
    defaults to 'video' since SigLIP indexes only video assets; pass None to
    permit any kind. Returned set is suitable for intersecting against a chunk
    candidate list before vector scoring.
    """
    db = catalog_db or editorial_catalog_sqlite_path()
    con = sqlite3.connect(str(db))
    clauses: list[str] = []
    params: list = []
    if record_kind:
        clauses.append("a.record_kind = ?")
        params.append(record_kind)
    if bucket:
        clauses.append("LOWER(a.bucket) = LOWER(?)")
        params.append(bucket)
    if asset_type:
        clauses.append("LOWER(a.asset_type) = LOWER(?)")
        params.append(asset_type)
    if shoot_date_from:
        clauses.append("a.shoot_date >= ?")
        params.append(shoot_date_from)
    if shoot_date_to:
        clauses.append("a.shoot_date <= ?")
        params.append(shoot_date_to)
    if place_id:
        clauses.append(
            "EXISTS (SELECT 1 FROM asset_place ap "
            "WHERE ap.asset_id = a.asset_id AND ap.pl_id = ?)"
        )
        params.append(place_id)
    if person_ids:
        pids = list(person_ids)
        placeholders = ",".join("?" * len(pids))
        clauses.append(
            f"EXISTS (SELECT 1 FROM person_appearance pa "
            f"WHERE pa.asset_id = a.asset_id AND pa.p_id IN ({placeholders}))"
        )
        params.extend(pids)
    if camera_movement:
        clauses.append(
            "EXISTS (SELECT 1 FROM asset_semantic_chunk sc "
            "WHERE sc.asset_id = a.asset_id AND LOWER(sc.camera_movement) = LOWER(?))"
        )
        params.append(camera_movement)
    if shot_size:
        clauses.append(
            "EXISTS (SELECT 1 FROM asset_semantic_chunk sc "
            "WHERE sc.asset_id = a.asset_id AND UPPER(sc.camera_shot_size) = UPPER(?))"
        )
        params.append(shot_size)
    where = " AND ".join(clauses) if clauses else "1=1"
    sql = f"SELECT a.asset_id FROM asset a WHERE {where}"
    rows = con.execute(sql, params).fetchall()
    con.close()
    out = {r[0] for r in rows}
    if exclude_assets:
        out -= set(exclude_assets)
    return out


def search_broll(
    *,
    place_id: Optional[str] = None,
    location_like: Optional[str] = None,
    limit: int = 50,
    catalog_db: Optional[Path] = None,
) -> list[dict]:
    """B-roll candidates by normalized place and/or semantic location text.

    `place_id` is matched against `asset_place.pl_id`. `location_like` is a
    case-insensitive substring match against `asset.semantic_location` and
    `asset_semantic_chunk.setting_location`.
    """
    db = catalog_db or editorial_catalog_sqlite_path()
    con = sqlite3.connect(str(db))
    con.row_factory = sqlite3.Row
    clauses = [
        "a.record_kind = 'video'",
        "(LOWER(a.asset_type) IN ('b_roll', 'broll', 'b-roll')"
        " OR LOWER(a.bucket) LIKE '%broll%'"
        " OR LOWER(COALESCE(a.semantic_editorial_notes, '')) LIKE '%b-roll%'"
        " OR a.asset_id IN (SELECT asset_id FROM asset_semantic_chunk"
        "     WHERE LOWER(COALESCE(editorial_notes,'')) LIKE '%b-roll%'"
        "        OR LOWER(COALESCE(action,'')) LIKE '%b-roll%'))",
    ]
    params: list = []
    if place_id:
        clauses.append(
            "EXISTS (SELECT 1 FROM asset_place ap "
            "WHERE ap.asset_id = a.asset_id AND ap.pl_id = ?)"
        )
        params.append(place_id)
    if location_like:
        clauses.append(
            "(a.semantic_location LIKE ? OR EXISTS ("
            " SELECT 1 FROM asset_semantic_chunk c"
            " WHERE c.asset_id = a.asset_id AND c.setting_location LIKE ?))"
        )
        pat = f"%{location_like}%"
        params.extend([pat, pat])
    sql = f"""
        SELECT DISTINCT a.asset_id, a.filename, a.source_path, a.duration_sec,
               a.semantic_location, a.semantic_subject, a.shoot_date
          FROM asset a
         WHERE {' AND '.join(clauses)}
         ORDER BY a.shoot_date DESC, a.asset_id
         LIMIT ?
    """
    params.append(limit)
    rows = [dict(r) for r in con.execute(sql, params).fetchall()]
    con.close()
    return rows
