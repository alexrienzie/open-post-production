"""editor.queries.compose — multi-layer editorial query composition.

Composes the enrichment layers projected into `indexes/editorial_catalog.sqlite`
(frame_face, shot, shot_quality, frame_text, bib_hit, audio_event, audio_quality,
dense_caption, still_aesthetic, asset_place, person_appearance) and the key-quote
scored quotes / comedy fields in per-asset `dataset/assets/transcripts/*.json`.

The single-layer helpers in `filters.py`, `transcript.py`, `visual.py` still
do the heavy lifting per layer; this module composes them into multi-layer
editorial queries the editor (and an eventual LLM front) can call by name.

See `SCHEMA.md` for the full catalog of queryable signals.

Public API (returns lists of plain dicts — JSON-serializable for LLM hand-off):

  find_soundbites_with_face(p_id, ...)
    Top quote-scored soundbites where the speaker is on camera at quote-time
    AND the asset's audio passes a usability filter. Pivots on `key_quotes`
    in transcript JSONs; joins to `frame_face` + `audio_quality`.

  find_broll_with_quality(place_id=, location_like=, ...)
    B-roll shots at a place, filtered by `shot_quality` flags (in_focus,
    not setup/teardown, optionally aesthetic), ranked by sharpness.

  find_dense_caption_matches(caption_query, ...)
    Shots whose VLM `dense_caption.caption_text` mentions a phrase, optionally
    joined with face-presence and shot_quality gates.

  find_funny_moments_on_camera(p_id=None, ...)
    scored comedic moments where someone is on camera at moment-time AND audio
    is usable. Optional speaker / comedy_type / confidence filters.

  find_bib_appearances(bib_number=None, p_id=None, ...)
    Frames where a numeric bib is visible, optionally restricted by bib number
    or person, with shot_quality gates.

  find_quotes_about_topic(topic, ...)
    FTS5-keyword search on `segment.text` cross-referenced with key-quote
    scoring (if a matching key_quote exists), filtered by speaker / story_function.

Each function takes a `limit` argument (default 30); ordering is layer-specific
(soundbite_score for quotes, sharpness × duration for b-roll, etc.).
"""
from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

# --- workspace paths (cross-platform) ---
_QUERIES_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _QUERIES_DIR.parent.parent
EDITORIAL_DB = _REPO_ROOT / "indexes" / "editorial_catalog.sqlite"
TRANSCRIPTS_DIR = _REPO_ROOT / "dataset" / "assets" / "transcripts"
PEOPLE_REGISTRY = _REPO_ROOT / "dataset" / "people" / "people.json"


# ---------------- shared connection + people-name resolver ----------------

def _connect_ro() -> sqlite3.Connection:
    if not EDITORIAL_DB.exists():
        raise FileNotFoundError(f"editorial_catalog.sqlite not at {EDITORIAL_DB}")
    return sqlite3.connect(f"file:{EDITORIAL_DB}?mode=ro", uri=True)


_PEOPLE_CACHE: dict[str, str] | None = None


def _people_names() -> dict[str, str]:
    """{p_id: canonical_name}. Lazy-loaded once per process."""
    global _PEOPLE_CACHE
    if _PEOPLE_CACHE is not None:
        return _PEOPLE_CACHE
    if not PEOPLE_REGISTRY.exists():
        _PEOPLE_CACHE = {}
        return _PEOPLE_CACHE
    pd = json.loads(PEOPLE_REGISTRY.read_text(encoding="utf-8"))
    people = pd.get("people", pd) if isinstance(pd, dict) else pd
    out = {}
    for p in people:
        pid = p.get("id") or p.get("p_id")
        name = p.get("canonical_name", "")
        if pid:
            out[pid] = name
    _PEOPLE_CACHE = out
    return out


# ---------------- frame-face join helper ----------------

def _face_present_at(
    con: sqlite3.Connection, asset_id: str, p_id: str,
    start_sec: float, end_sec: float, tolerance_sec: float = 1.0,
) -> bool:
    """True if any `frame_face` row for (p_id) exists in [start_sec-tol, end_sec+tol]
    on this asset."""
    row = con.execute(
        "SELECT 1 FROM frame_face "
        "WHERE asset_id=? AND p_id=? "
        "AND frame_time_sec >= ? AND frame_time_sec <= ? LIMIT 1",
        (asset_id, p_id, start_sec - tolerance_sec, end_sec + tolerance_sec),
    ).fetchone()
    return row is not None


def _faces_at(
    con: sqlite3.Connection, asset_id: str,
    start_sec: float, end_sec: float, tolerance_sec: float = 1.0,
) -> list[str]:
    """Return distinct p_ids visible in the window."""
    rows = con.execute(
        "SELECT DISTINCT p_id FROM frame_face "
        "WHERE asset_id=? "
        "AND frame_time_sec >= ? AND frame_time_sec <= ?",
        (asset_id, start_sec - tolerance_sec, end_sec + tolerance_sec),
    ).fetchall()
    return [r[0] for r in rows if r[0]]


# ---------------- audio_quality gate ----------------

def _audio_quality_for(con: sqlite3.Connection, asset_id: str) -> dict | None:
    """Return dict of audio_quality fields for an asset, or None if not scored."""
    row = con.execute(
        "SELECT rms_dbfs, peak_dbfs, is_silent, is_quiet, is_clippy, is_windy, is_usable "
        "FROM audio_quality WHERE asset_id=?",
        (asset_id,),
    ).fetchone()
    if not row:
        return None
    return {
        "rms_dbfs": row[0], "peak_dbfs": row[1],
        "is_silent": row[2], "is_quiet": row[3],
        "is_clippy": row[4], "is_windy": row[5], "is_usable": row[6],
    }


# ---------------- shot lookup ----------------

def _shot_at(con: sqlite3.Connection, asset_id: str, t_sec: float) -> dict | None:
    """Find the shot containing timestamp t_sec, with its quality row joined.
    Returns None if no shot covers t_sec."""
    row = con.execute(
        "SELECT s.shot_idx, s.start_sec, s.end_sec, s.duration_sec, "
        "       sq.sharpness_score, sq.is_blurry, sq.is_in_focus, "
        "       sq.is_setup_or_teardown, sq.is_aesthetic, sq.aesthetic_score "
        "FROM shot s LEFT JOIN shot_quality sq "
        "  ON sq.asset_id=s.asset_id AND sq.shot_idx=s.shot_idx "
        "WHERE s.asset_id=? AND s.start_sec <= ? AND s.end_sec >= ? "
        "LIMIT 1",
        (asset_id, t_sec, t_sec),
    ).fetchone()
    if not row:
        return None
    return {
        "shot_idx": row[0], "start_sec": row[1], "end_sec": row[2],
        "duration_sec": row[3], "sharpness_score": row[4],
        "is_blurry": row[5], "is_in_focus": row[6],
        "is_setup_or_teardown": row[7],
        "is_aesthetic": row[8], "aesthetic_score": row[9],
    }


# ---------------- 1. soundbites with face on camera ----------------

def _parse_score(v: Any) -> float | None:
    """The enrichment's soundbite_quality.score is stored as int|str; coerce to float."""
    if v is None or v == "None" or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _is_truthy(v: Any) -> bool:
    """The enrichment's comedic.is_comedic is the string 'True'/'False'/'None'."""
    if v is None:
        return False
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    return str(v).strip().lower() in ("true", "1", "yes")


def find_soundbites_with_face(
    p_id: str,
    *,
    min_soundbite_score: float = 4.0,
    topic_contains: str | None = None,
    story_function: str | None = None,
    require_audio_usable: bool = True,
    require_not_windy: bool = True,
    asset_type: str | None = None,
    limit: int = 30,
) -> list[dict]:
    """Top quote-scored soundbites where speaker `p_id` is also on camera at
    the quote's timestamp AND audio passes the quality gate.

    Walks `dataset/assets/transcripts/*.json::analysis.key_quotes[]` (transcript-enrichment
    fields live there, not in editorial_catalog), joins to `frame_face` +
    `audio_quality` via SQL.

    Args:
      p_id: required, e.g. `p_michelino_sunseri`
      min_soundbite_score: skip key_quotes below this score. Score scale is
        1-5 (LLM rating; median 4). Default 4 keeps the top ~50%.
      topic_contains: substring match on the quote text (case-insensitive)
      story_function: filter to a specific story_function tag
        (setup / catalyst / theme_statement / character_reveal / etc.)
      require_audio_usable: drop assets where audio_quality.is_usable=0
      require_not_windy: drop assets where audio_quality.is_windy=1
      asset_type: restrict to a specific asset_type (e.g. `interview`)
      limit: max results

    Returns: list of dicts ranked by soundbite_score desc, each with:
      {asset_id, filename, shoot_label, asset_type,
       start_sec, end_sec, text, soundbite_score, soundbite_reasons,
       story_function, speaker_p_id, speaker_name,
       audio_quality: {...}, on_camera_p_ids: [...]}
    """
    con = _connect_ro()
    names = _people_names()
    speaker_name = names.get(p_id, p_id)

    # Pre-filter assets to those where p_id appears in person_appearance
    # (speaker present at all) AND match asset_type filter.
    where_clauses = ["pa.p_id = ?"]
    params: list = [p_id]
    if asset_type:
        where_clauses.append("a.asset_type = ?")
        params.append(asset_type)
    asset_rows = con.execute(
        "SELECT DISTINCT a.asset_id, a.filename, a.shoot_label, a.asset_type "
        "FROM person_appearance pa JOIN asset a ON pa.asset_id = a.asset_id "
        "WHERE " + " AND ".join(where_clauses),
        params,
    ).fetchall()
    asset_index = {r[0]: {"filename": r[1], "shoot_label": r[2], "asset_type": r[3]}
                   for r in asset_rows}

    topic_lower = (topic_contains or "").lower()
    results: list[dict] = []

    for aid, meta in asset_index.items():
        tx_path = TRANSCRIPTS_DIR / f"{aid}.transcript.json"
        if not tx_path.exists():
            continue
        try:
            tx = json.loads(tx_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        analysis = tx.get("analysis") or {}
        quotes = analysis.get("key_quotes") or []
        if not quotes:
            continue

        # Audio quality gate (per-asset)
        aq = _audio_quality_for(con, aid)
        if require_audio_usable and (not aq or not aq.get("is_usable")):
            continue
        if require_not_windy and aq and aq.get("is_windy"):
            continue

        for q in quotes:
            if q.get("speaker") != p_id:
                continue
            sb = q.get("soundbite_quality") or {}
            score = _parse_score(sb.get("score"))
            if score is None or score < min_soundbite_score:
                continue
            if story_function and q.get("story_function") != story_function:
                continue
            text = q.get("text") or ""
            if topic_lower and topic_lower not in text.lower():
                continue
            start_sec = q.get("start_sec")
            end_sec = q.get("end_sec")
            if start_sec is None or end_sec is None:
                continue
            # Face-on-camera check: p_id visible during quote window
            if not _face_present_at(con, aid, p_id, start_sec, end_sec):
                continue
            on_camera = _faces_at(con, aid, start_sec, end_sec)
            results.append({
                "asset_id": aid,
                "filename": meta["filename"],
                "shoot_label": meta["shoot_label"],
                "asset_type": meta["asset_type"],
                "start_sec": start_sec,
                "end_sec": end_sec,
                "text": text,
                "soundbite_score": score,
                "soundbite_reasons": sb.get("reasons"),
                "story_function": q.get("story_function"),
                "speaker_p_id": p_id,
                "speaker_name": speaker_name,
                "audio_quality": aq,
                "on_camera_p_ids": on_camera,
                "on_camera_names": [names.get(x, x) for x in on_camera],
            })

    con.close()
    results.sort(key=lambda r: -r["soundbite_score"])
    return results[:limit]


# ---------------- 2. b-roll at place with quality ----------------

def find_broll_with_quality(
    *,
    place_id: str | None = None,
    location_like: str | None = None,
    require_in_focus: bool = True,
    exclude_setup_or_teardown: bool = True,
    require_aesthetic: bool = False,
    min_duration_sec: float = 2.0,
    asset_type: str | None = "b_roll",
    limit: int = 30,
) -> list[dict]:
    """B-roll shots at a place, ranked by sharpness × duration, with editorial
    usability filters.

    At least one of `place_id` / `location_like` must be provided.

    Returns: list of dicts ranked by (is_aesthetic desc, sharpness desc, duration desc):
      {asset_id, filename, shoot_label, shot_idx, start_sec, end_sec, duration_sec,
       sharpness_score, is_blurry, is_in_focus, is_setup_or_teardown,
       is_aesthetic, aesthetic_score, semantic_subject, semantic_location, place_ids}
    """
    if not place_id and not location_like:
        raise ValueError("provide place_id or location_like")
    con = _connect_ro()

    where: list[str] = []
    params: list = []
    if place_id:
        where.append(
            "a.asset_id IN (SELECT asset_id FROM asset_place WHERE pl_id = ?)")
        params.append(place_id)
    if location_like:
        where.append("(a.semantic_location LIKE ?)")
        params.append(f"%{location_like}%")
    if asset_type:
        where.append("a.asset_type = ?")
        params.append(asset_type)

    sql = (
        "SELECT a.asset_id, a.filename, a.shoot_label, a.semantic_subject, "
        "       a.semantic_location, "
        "       s.shot_idx, s.start_sec, s.end_sec, s.duration_sec, "
        "       sq.sharpness_score, sq.is_blurry, sq.is_in_focus, "
        "       sq.is_setup_or_teardown, sq.is_aesthetic, sq.aesthetic_score "
        "FROM shot s "
        "JOIN asset a ON s.asset_id = a.asset_id "
        "LEFT JOIN shot_quality sq "
        "  ON sq.asset_id = s.asset_id AND sq.shot_idx = s.shot_idx "
        "WHERE a.record_kind='video' AND " + " AND ".join(where) + " "
        "AND s.duration_sec >= ? "
    )
    params.append(min_duration_sec)

    if require_in_focus:
        sql += " AND COALESCE(sq.is_in_focus, 0) = 1"
    if exclude_setup_or_teardown:
        sql += " AND COALESCE(sq.is_setup_or_teardown, 0) = 0"
    if require_aesthetic:
        sql += " AND COALESCE(sq.is_aesthetic, 0) = 1"

    sql += (
        " ORDER BY COALESCE(sq.is_aesthetic, 0) DESC, "
        "          COALESCE(sq.sharpness_score, 0) DESC, "
        "          s.duration_sec DESC "
        " LIMIT ?"
    )
    params.append(limit)

    rows = con.execute(sql, params).fetchall()
    out = []
    for r in rows:
        aid = r[0]
        place_ids_row = con.execute(
            "SELECT GROUP_CONCAT(pl_id) FROM asset_place WHERE asset_id=?",
            (aid,),
        ).fetchone()
        out.append({
            "asset_id": aid,
            "filename": r[1], "shoot_label": r[2],
            "semantic_subject": r[3], "semantic_location": r[4],
            "shot_idx": r[5], "start_sec": r[6], "end_sec": r[7],
            "duration_sec": r[8],
            "sharpness_score": r[9],
            "is_blurry": r[10], "is_in_focus": r[11],
            "is_setup_or_teardown": r[12],
            "is_aesthetic": r[13], "aesthetic_score": r[14],
            "place_ids": place_ids_row[0] if place_ids_row and place_ids_row[0] else None,
        })
    con.close()
    return out


# ---------------- 2b. find_broll_v2 — the rich b-roll picker ----------------

def find_broll_v2(
    *,
    place_id: str | None = None,
    location_like: str | None = None,
    caption_contains: str | None = None,
    subject_contains: str | None = None,
    similar_to_text: str | None = None,
    similar_to_asset_id: str | None = None,
    camera_movement: "str | list[str] | None" = None,
    exclude_people: bool = False,
    require_people_p_ids: list[str] | None = None,
    audio_state: str = "any",   # 'silent' / 'usable' / 'any'
    exclude_visible_text: bool = False,
    exclude_bibs: bool = False,
    require_in_focus: bool = True,
    exclude_setup_or_teardown: bool = True,
    min_aesthetic_score: float | None = None,
    min_duration_sec: float = 2.0,
    rank_by: str = "blended",   # 'aesthetic' / 'sharpness' / 'duration' / 'blended'
    diversify_by_shoot: bool = False,
    asset_type: str | None = "b_roll",
    siglip_candidate_pool: int = 200,
    limit: int = 30,
) -> list[dict]:
    """The rich b-roll picker — composes 5+ enrichment layers.

    Required: at least one of {place_id, location_like, caption_contains,
    similar_to_text, similar_to_asset_id}. Otherwise the query is unconstrained
    and would return every b-roll shot in the corpus.

    Filters (all optional unless flagged):
      Where the shot is / what it shows:
        place_id            asset_place.pl_id match
        location_like       case-insensitive substring of asset.semantic_location
        caption_contains    case-insensitive substring of dense_caption.caption_text
        subject_contains    case-insensitive substring of asset.semantic_subject
                            (e.g. "moving car", "driving" — what raw SQL had to do before)
        similar_to_text     SigLIP text→image: pre-filter candidates to top-N visually similar
                            assets (siglip_candidate_pool, default 200)
        similar_to_asset_id SigLIP image→image: pre-filter to top-N visually similar assets
        camera_movement     str or list of camera_movement tags to require (case-insensitive
                            substring), shot-aligned to overlapping asset_semantic_chunk.
                            e.g. "gimbal", ["gimbal","dolly","drone"] for smooth motion.
                            Values in corpus: handheld/static/mixed/pan/gimbal/drone/dolly/
                            push_in/pull_out/whip/tilt.

      Who's in it:
        exclude_people=True              drop shots where any face appears
        require_people_p_ids=["..."]     drop shots without ALL named people

      Audio:
        audio_state='silent'  asset has audio_quality.is_silent=1 (cuttable silent b-roll)
        audio_state='usable'  asset has audio_quality.is_usable=1 AND is_silent=0
        audio_state='any'     no filter (default)

      Visual cleanliness:
        exclude_visible_text=True       drop shots with any frame_text rows
        exclude_bibs=True               drop shots with any bib_hit rows
        require_in_focus=True (default) shot_quality.is_in_focus=1
        exclude_setup_or_teardown=True  shot_quality.is_setup_or_teardown=0
        min_aesthetic_score             shot_quality.aesthetic_score >= this (NIMA; ~4.05 = p85)
        min_duration_sec=2.0            shot.duration_sec >= this

    Ranking:
        rank_by='aesthetic'   ORDER BY aesthetic_score DESC, sharpness DESC, duration DESC
        rank_by='sharpness'   ORDER BY sharpness DESC, duration DESC
        rank_by='duration'    ORDER BY duration DESC, sharpness DESC
        rank_by='blended'     ORDER BY normalized combo (default)

      diversify_by_shoot=True  post-cap per shoot_label to ceil(limit / 5)

    Returns list of dicts ranked by the chosen criterion:
      {asset_id, shot_idx, start_sec, end_sec, duration_sec,
       sharpness_score, motion_score, is_in_focus, is_setup_or_teardown,
       is_aesthetic, aesthetic_score,
       dense_caption_text, on_camera_p_ids, on_camera_names,
       audio_state, has_visible_text, has_bib,
       semantic_subject, semantic_location, place_ids,
       shoot_label, shoot_date, asset_type,
       blended_score}
    """
    if not any([place_id, location_like, caption_contains, subject_contains,
                similar_to_text, similar_to_asset_id]):
        raise ValueError(
            "find_broll_v2: provide at least one of {place_id, location_like, "
            "caption_contains, subject_contains, similar_to_text, similar_to_asset_id}. "
            "(camera_movement is a refiner, not a sole anchor — it'd return every "
            "gimbal/dolly shot in the corpus.)"
        )
    if rank_by not in ("aesthetic", "sharpness", "duration", "blended"):
        raise ValueError(f"rank_by must be aesthetic/sharpness/duration/blended; got {rank_by!r}")
    if audio_state not in ("silent", "usable", "any"):
        raise ValueError(f"audio_state must be silent/usable/any; got {audio_state!r}")

    con = _connect_ro()
    names = _people_names()

    # ---- SigLIP pre-filter (if requested) → asset_id allowlist ----
    siglip_allowed_assets: set[str] | None = None
    if similar_to_text or similar_to_asset_id:
        # Local import to avoid heavy SigLIP load when not needed
        try:
            if similar_to_text:
                from .visual import find_visually_similar_by_text
                hits = find_visually_similar_by_text(
                    similar_to_text, top_k=siglip_candidate_pool)
            else:
                from .visual import find_visually_similar
                hits = find_visually_similar(
                    asset_id=similar_to_asset_id, top_k=siglip_candidate_pool)
            siglip_allowed_assets = {h["asset_id"] for h in hits if h.get("asset_id")}
            if not siglip_allowed_assets:
                con.close()
                return []
        except Exception as e:
            con.close()
            raise RuntimeError(f"SigLIP pre-filter failed: {e}") from e

    # ---- Build SQL ----
    where: list[str] = ["a.record_kind='video'", "s.duration_sec >= ?"]
    params: list = [min_duration_sec]

    if asset_type:
        where.append("a.asset_type = ?")
        params.append(asset_type)

    if place_id:
        where.append(
            "a.asset_id IN (SELECT asset_id FROM asset_place WHERE pl_id = ?)")
        params.append(place_id)

    if location_like:
        where.append("(a.semantic_location LIKE ?)")
        params.append(f"%{location_like}%")

    if caption_contains:
        where.append(
            "EXISTS (SELECT 1 FROM dense_caption dc "
            "WHERE dc.asset_id = s.asset_id AND dc.shot_idx = s.shot_idx "
            "AND dc.caption_text LIKE ?)")
        params.append(f"%{caption_contains}%")

    if subject_contains:
        where.append("a.semantic_subject LIKE ?")
        params.append(f"%{subject_contains}%")

    if camera_movement:
        movs = [camera_movement] if isinstance(camera_movement, str) else list(camera_movement)
        ors = " OR ".join("LOWER(amc.camera_movement) LIKE ?" for _ in movs)
        # shot-aligned: the shot overlaps a semantic chunk with the requested movement
        where.append(
            "EXISTS (SELECT 1 FROM asset_semantic_chunk amc "
            "WHERE amc.asset_id = s.asset_id "
            f"AND ({ors}) "
            "AND amc.start_sec <= s.end_sec AND amc.end_sec >= s.start_sec)")
        for m in movs:
            params.append(f"%{m.lower()}%")

    if siglip_allowed_assets is not None:
        # Inline placeholder list for set restriction
        ph = ",".join("?" * len(siglip_allowed_assets))
        where.append(f"a.asset_id IN ({ph})")
        params.extend(sorted(siglip_allowed_assets))

    if exclude_people:
        where.append(
            "NOT EXISTS (SELECT 1 FROM frame_face ff "
            "WHERE ff.asset_id = s.asset_id "
            "AND ff.frame_time_sec >= s.start_sec - 0.5 "
            "AND ff.frame_time_sec <= s.end_sec + 0.5)")
    if require_people_p_ids:
        for pid in require_people_p_ids:
            where.append(
                "EXISTS (SELECT 1 FROM frame_face ff "
                "WHERE ff.asset_id = s.asset_id AND ff.p_id = ? "
                "AND ff.frame_time_sec >= s.start_sec - 0.5 "
                "AND ff.frame_time_sec <= s.end_sec + 0.5)")
            params.append(pid)

    if audio_state == "silent":
        where.append(
            "EXISTS (SELECT 1 FROM audio_quality aq "
            "WHERE aq.asset_id = s.asset_id AND aq.is_silent = 1)")
    elif audio_state == "usable":
        where.append(
            "EXISTS (SELECT 1 FROM audio_quality aq "
            "WHERE aq.asset_id = s.asset_id "
            "AND aq.is_usable = 1 AND COALESCE(aq.is_silent, 0) = 0)")

    if exclude_visible_text:
        where.append(
            "NOT EXISTS (SELECT 1 FROM frame_text ft "
            "WHERE ft.asset_id = s.asset_id AND ft.shot_idx = s.shot_idx)")
    if exclude_bibs:
        where.append(
            "NOT EXISTS (SELECT 1 FROM bib_hit bh "
            "WHERE bh.asset_id = s.asset_id AND bh.shot_idx = s.shot_idx)")

    if require_in_focus:
        where.append("COALESCE(sq.is_in_focus, 0) = 1")
    if exclude_setup_or_teardown:
        where.append("COALESCE(sq.is_setup_or_teardown, 0) = 0")
    if min_aesthetic_score is not None:
        where.append("COALESCE(sq.aesthetic_score, 0) >= ?")
        params.append(min_aesthetic_score)

    # Ranking
    if rank_by == "aesthetic":
        order_by = (
            "COALESCE(sq.aesthetic_score, 0) DESC, "
            "COALESCE(sq.sharpness_score, 0) DESC, "
            "s.duration_sec DESC")
    elif rank_by == "sharpness":
        order_by = "COALESCE(sq.sharpness_score, 0) DESC, s.duration_sec DESC"
    elif rank_by == "duration":
        order_by = "s.duration_sec DESC, COALESCE(sq.sharpness_score, 0) DESC"
    else:  # blended
        # Normalize aesthetic (~4-7) + sharpness (~0-1000+) + duration (~0-60s)
        # into a rough 0..1ish score: aesthetic/10 + sharpness/1000 + duration/30
        order_by = (
            "(COALESCE(sq.aesthetic_score, 0) / 10.0 "
            "+ MIN(COALESCE(sq.sharpness_score, 0), 1000.0) / 1000.0 "
            "+ MIN(s.duration_sec, 30.0) / 30.0) DESC")

    # Pull more than `limit` if diversifying, so the post-filter has options
    pull = limit * 5 if diversify_by_shoot else limit
    sql = (
        "SELECT a.asset_id, a.filename, a.shoot_label, a.shoot_date, "
        "       a.asset_type, a.semantic_subject, a.semantic_location, "
        "       s.shot_idx, s.start_sec, s.end_sec, s.duration_sec, "
        "       sq.sharpness_score, sq.motion_score, "
        "       sq.is_in_focus, sq.is_setup_or_teardown, "
        "       sq.is_aesthetic, sq.aesthetic_score, "
        "       (SELECT amc.camera_movement FROM asset_semantic_chunk amc "
        "        WHERE amc.asset_id = s.asset_id "
        "        AND amc.start_sec <= s.end_sec AND amc.end_sec >= s.start_sec "
        "        ORDER BY amc.start_sec LIMIT 1) AS camera_movement "
        "FROM shot s "
        "JOIN asset a ON s.asset_id = a.asset_id "
        "LEFT JOIN shot_quality sq "
        "  ON sq.asset_id = s.asset_id AND sq.shot_idx = s.shot_idx "
        "WHERE " + " AND ".join(where) + " "
        f"ORDER BY {order_by} LIMIT ?"
    )
    params.append(pull)

    rows = con.execute(sql, params).fetchall()

    # Build enriched dicts with derived per-shot signals
    out: list[dict] = []
    for r in rows:
        (aid, filename, shoot_label, shoot_date, atype,
         sem_subj, sem_loc, sidx, ss, se, dur,
         sharp, motion, is_focus, is_setup, is_aes, aes_score, cam_move) = r

        # On-camera faces (only fetch if NOT excluded — saves a query per shot)
        if exclude_people:
            on_camera: list[str] = []
        else:
            on_camera = _faces_at(con, aid, ss, se, tolerance_sec=0.5)

        # Dense caption text (first match for this shot, if any)
        caption_row = con.execute(
            "SELECT caption_text FROM dense_caption "
            "WHERE asset_id=? AND shot_idx=? "
            "ORDER BY sample_pos LIMIT 1",
            (aid, sidx),
        ).fetchone()
        caption_text = caption_row[0] if caption_row else None

        # Audio state for this asset (single audio_quality row per asset)
        aq = _audio_quality_for(con, aid)
        if aq:
            if aq.get("is_silent"):
                this_audio = "silent"
            elif aq.get("is_usable"):
                this_audio = "usable"
            else:
                this_audio = "unusable"
        else:
            this_audio = "unknown"

        # Presence flags (cheap EXISTS checks)
        has_text = con.execute(
            "SELECT 1 FROM frame_text WHERE asset_id=? AND shot_idx=? LIMIT 1",
            (aid, sidx),
        ).fetchone() is not None
        has_bib = con.execute(
            "SELECT 1 FROM bib_hit WHERE asset_id=? AND shot_idx=? LIMIT 1",
            (aid, sidx),
        ).fetchone() is not None

        place_row = con.execute(
            "SELECT GROUP_CONCAT(pl_id) FROM asset_place WHERE asset_id=?",
            (aid,),
        ).fetchone()

        # Blended score (recomputed in Python for the response, matches SQL order)
        blended = (
            (aes_score or 0) / 10.0
            + min(sharp or 0, 1000.0) / 1000.0
            + min(dur or 0, 30.0) / 30.0
        )

        out.append({
            "asset_id": aid, "filename": filename,
            "shoot_label": shoot_label, "shoot_date": shoot_date,
            "asset_type": atype,
            "semantic_subject": sem_subj, "semantic_location": sem_loc,
            "place_ids": place_row[0] if place_row and place_row[0] else None,
            "shot_idx": sidx,
            "start_sec": ss, "end_sec": se, "duration_sec": dur,
            "sharpness_score": sharp, "motion_score": motion,
            "is_in_focus": is_focus, "is_setup_or_teardown": is_setup,
            "is_aesthetic": is_aes, "aesthetic_score": aes_score,
            "camera_movement": cam_move,
            "dense_caption_text": caption_text,
            "on_camera_p_ids": on_camera,
            "on_camera_names": [names.get(x, x) for x in on_camera],
            "audio_state": this_audio,
            "has_visible_text": has_text,
            "has_bib": has_bib,
            "blended_score": round(blended, 4),
        })

    con.close()

    # Diversify by shoot: cap per-shoot at ceil(limit / 5)
    if diversify_by_shoot:
        per_shoot_cap = max(1, -(-limit // 5))
        seen: dict[str, int] = {}
        diversified: list[dict] = []
        for r in out:
            sl = r.get("shoot_label") or "_unknown"
            if seen.get(sl, 0) >= per_shoot_cap:
                continue
            seen[sl] = seen.get(sl, 0) + 1
            diversified.append(r)
            if len(diversified) >= limit:
                break
        out = diversified
    else:
        out = out[:limit]

    return out


# ---------------- 3. dense_caption phrase search ----------------

def find_dense_caption_matches(
    caption_query: str,
    *,
    require_face_p_id: str | None = None,
    require_in_focus: bool = True,
    exclude_setup_or_teardown: bool = True,
    asset_type: str | None = None,
    limit: int = 30,
) -> list[dict]:
    """Find shots whose VLM `dense_caption.caption_text` contains a phrase,
    joined with shot_quality + optional face-presence gate.

    Returns: list of dicts ranked by (sharpness desc):
      {asset_id, shot_idx, frame_time_sec, caption_text, model_engine,
       shot_quality: {...}, on_camera_p_ids: [...]}
    """
    if not caption_query.strip():
        raise ValueError("caption_query required")
    con = _connect_ro()
    names = _people_names()

    sql = (
        "SELECT dc.asset_id, dc.shot_idx, dc.frame_time_sec, dc.caption_text, "
        "       dc.model_engine, dc.sample_pos, "
        "       a.filename, a.shoot_label, a.asset_type, "
        "       sq.sharpness_score, sq.is_in_focus, sq.is_setup_or_teardown, "
        "       sq.is_aesthetic, sq.aesthetic_score "
        "FROM dense_caption dc "
        "JOIN asset a ON a.asset_id = dc.asset_id "
        "LEFT JOIN shot_quality sq "
        "  ON sq.asset_id = dc.asset_id AND sq.shot_idx = dc.shot_idx "
        "WHERE dc.caption_text LIKE ? "
    )
    params: list = [f"%{caption_query}%"]

    if asset_type:
        sql += " AND a.asset_type = ?"
        params.append(asset_type)
    if require_in_focus:
        sql += " AND COALESCE(sq.is_in_focus, 0) = 1"
    if exclude_setup_or_teardown:
        sql += " AND COALESCE(sq.is_setup_or_teardown, 0) = 0"

    sql += " ORDER BY COALESCE(sq.sharpness_score, 0) DESC LIMIT ?"
    params.append(limit * 3 if require_face_p_id else limit)

    rows = con.execute(sql, params).fetchall()
    out: list[dict] = []
    for r in rows:
        aid, sidx, ft = r[0], r[1], r[2]
        on_camera = _faces_at(con, aid, ft, ft, tolerance_sec=1.0)
        if require_face_p_id and require_face_p_id not in on_camera:
            continue
        out.append({
            "asset_id": aid, "shot_idx": sidx, "frame_time_sec": ft,
            "caption_text": r[3], "model_engine": r[4], "sample_pos": r[5],
            "filename": r[6], "shoot_label": r[7], "asset_type": r[8],
            "shot_quality": {
                "sharpness_score": r[9],
                "is_in_focus": r[10], "is_setup_or_teardown": r[11],
                "is_aesthetic": r[12], "aesthetic_score": r[13],
            },
            "on_camera_p_ids": on_camera,
            "on_camera_names": [names.get(x, x) for x in on_camera],
        })
        if len(out) >= limit:
            break
    con.close()
    return out


# ---------------- 4. funny moments on camera ----------------

def find_funny_moments_on_camera(
    *,
    p_id: str | None = None,
    comedy_type: str | None = None,
    min_confidence: float = 0.5,
    require_audio_usable: bool = True,
    require_not_windy: bool = True,
    limit: int = 30,
) -> list[dict]:
    """scored comedic moments where someone is on camera at moment-time AND
    audio is usable. The comedy fields live on **quotes** (`analysis.key_quotes[].comedic`),
    not moments — this helper walks the quotes, then joins each comedic quote
    to its parent moment (via `linked_moment_idx`) for `subjects_on_camera`,
    falling back to a `frame_face` SQL lookup if the moment doesn't carry that.

    Args:
      p_id: optional — only return moments where this person is on camera
      comedy_type: optional — `outrageous_claim` / `setup_punchline` /
        `self_deprecation` / `irony` / `banter` / `reaction` / `one_liner` /
        `contrast` / `outrageous_claim_plus_reaction` (others may exist)
      min_confidence: drop quotes below this comedic.confidence
      require_audio_usable / require_not_windy: audio gates

    Returns: list ranked by comedy confidence desc.
    """
    con = _connect_ro()
    names = _people_names()
    asset_rows = con.execute(
        "SELECT asset_id, filename, shoot_label, asset_type FROM asset "
        "WHERE record_kind='video' AND has_machine_transcript=1"
    ).fetchall()
    asset_index = {r[0]: {"filename": r[1], "shoot_label": r[2], "asset_type": r[3]}
                   for r in asset_rows}

    results: list[dict] = []
    for aid, meta in asset_index.items():
        tx_path = TRANSCRIPTS_DIR / f"{aid}.transcript.json"
        if not tx_path.exists():
            continue
        try:
            tx = json.loads(tx_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        analysis = tx.get("analysis") or {}
        quotes = analysis.get("key_quotes") or []
        moments = analysis.get("key_moments") or []
        if not quotes:
            continue
        aq = _audio_quality_for(con, aid)
        if require_audio_usable and (not aq or not aq.get("is_usable")):
            continue
        if require_not_windy and aq and aq.get("is_windy"):
            continue

        for q in quotes:
            c = q.get("comedic") or {}
            if not _is_truthy(c.get("is_comedic")):
                continue
            conf = _parse_score(c.get("confidence"))
            if conf is None or conf < min_confidence:
                continue
            if comedy_type and c.get("type") != comedy_type:
                continue
            start_sec = q.get("start_sec")
            end_sec = q.get("end_sec")
            if start_sec is None or end_sec is None:
                continue

            # Subjects-on-camera: prefer parent moment's pre-computed list if
            # this quote is linked to one; otherwise SQL frame_face lookup.
            on_camera: list[str] = []
            linked_idx = q.get("linked_moment_idx")
            if isinstance(linked_idx, int) and 0 <= linked_idx < len(moments):
                m_subjects = moments[linked_idx].get("subjects_on_camera") or []
                if m_subjects:
                    on_camera = list(m_subjects)
            if not on_camera:
                on_camera = _faces_at(con, aid, start_sec, end_sec)
            if p_id and p_id not in on_camera:
                continue
            if not on_camera:
                continue

            results.append({
                "asset_id": aid,
                "filename": meta["filename"],
                "shoot_label": meta["shoot_label"],
                "asset_type": meta["asset_type"],
                "start_sec": start_sec,
                "end_sec": end_sec,
                "text": q.get("text") or "",
                "speaker_p_id": q.get("speaker"),
                "speaker_name": names.get(q.get("speaker"), q.get("speaker")),
                "comedy_type": c.get("type"),
                "comedy_confidence": conf,
                "comedy_notes": c.get("notes"),
                "story_function": q.get("story_function"),
                "linked_moment_idx": linked_idx,
                "linked_moment_label": (
                    moments[linked_idx].get("label")
                    if isinstance(linked_idx, int) and 0 <= linked_idx < len(moments)
                    else None
                ),
                "audio_quality": aq,
                "on_camera_p_ids": on_camera,
                "on_camera_names": [names.get(x, x) for x in on_camera],
            })

    con.close()
    results.sort(key=lambda r: -r["comedy_confidence"])
    return results[:limit]


# ---------------- 5. bib appearances ----------------

def find_bib_appearances(
    *,
    bib_number: str | None = None,
    p_id: str | None = None,
    require_in_focus: bool = True,
    exclude_setup_or_teardown: bool = True,
    asset_type: str | None = None,
    limit: int = 30,
) -> list[dict]:
    """Frames where a numeric bib is visible, optionally restricted by bib
    number or athlete p_id, with shot_quality gates.

    Returns: list ranked by (bib.confidence desc, sharpness desc).
    """
    if not bib_number and not p_id:
        raise ValueError("provide bib_number or p_id")
    con = _connect_ro()
    names = _people_names()

    sql = (
        "SELECT b.asset_id, b.shot_idx, b.frame_time_sec, b.bib_number, "
        "       b.confidence, b.ocr_engine, b.p_id, "
        "       a.filename, a.shoot_label, a.asset_type, "
        "       sq.sharpness_score, sq.is_in_focus, sq.is_setup_or_teardown "
        "FROM bib_hit b "
        "JOIN asset a ON a.asset_id = b.asset_id "
        "LEFT JOIN shot_quality sq "
        "  ON sq.asset_id = b.asset_id AND sq.shot_idx = b.shot_idx "
        "WHERE 1=1 "
    )
    params: list = []
    if bib_number:
        sql += " AND b.bib_number = ?"
        params.append(bib_number)
    if p_id:
        sql += " AND b.p_id = ?"
        params.append(p_id)
    if asset_type:
        sql += " AND a.asset_type = ?"
        params.append(asset_type)
    if require_in_focus:
        sql += " AND COALESCE(sq.is_in_focus, 0) = 1"
    if exclude_setup_or_teardown:
        sql += " AND COALESCE(sq.is_setup_or_teardown, 0) = 0"

    sql += (" ORDER BY b.confidence DESC, "
            "          COALESCE(sq.sharpness_score, 0) DESC "
            " LIMIT ?")
    params.append(limit)
    rows = con.execute(sql, params).fetchall()
    out = []
    for r in rows:
        aid, sidx, ft = r[0], r[1], r[2]
        on_camera = _faces_at(con, aid, ft, ft, tolerance_sec=1.0)
        bib_pid = r[6]
        out.append({
            "asset_id": aid, "shot_idx": sidx, "frame_time_sec": ft,
            "bib_number": r[3], "bib_confidence": r[4],
            "ocr_engine": r[5],
            "athlete_p_id": bib_pid,
            "athlete_name": names.get(bib_pid, bib_pid) if bib_pid else None,
            "filename": r[7], "shoot_label": r[8], "asset_type": r[9],
            "sharpness_score": r[10],
            "is_in_focus": r[11], "is_setup_or_teardown": r[12],
            "on_camera_p_ids": on_camera,
            "on_camera_names": [names.get(x, x) for x in on_camera],
        })
    con.close()
    return out


# ---------------- 6. quotes about a topic (FTS5 + soundbite-score join) ----------------

def find_quotes_about_topic(
    topic: str,
    *,
    speaker_p_id: str | None = None,
    min_soundbite_score: float | None = None,
    story_function: str | None = None,
    limit: int = 30,
) -> list[dict]:
    """FTS5 keyword search on transcript segments cross-referenced with
    key-quote scoring. Matches segments whose text matches `topic`; if a scored
    `key_quote` overlaps the segment, attaches its soundbite_quality / comedy /
    story_function fields.

    Args:
      topic: FTS5 MATCH string (single keyword, phrase in quotes, OR boolean)
      speaker_p_id: filter to segments spoken by this person
      min_soundbite_score: require a matching scored quote with score >= this
      story_function: require a matching scored quote with this story_function

    Returns: list ranked by soundbite_score (if scored) or segment match relevance.
    """
    con = _connect_ro()
    names = _people_names()

    # FTS5 keyword filter; speaker filter via JOIN to segment table for speaker_p_id
    sql = (
        "SELECT f.asset_id, f.seg_idx, s.start_sec, s.end_sec, s.text, "
        "       s.speaker_p_id, a.filename, a.shoot_label, a.asset_type "
        "FROM segment_fts f "
        "JOIN segment s ON s.asset_id = f.asset_id AND s.seg_idx = f.seg_idx "
        "JOIN asset a ON a.asset_id = f.asset_id "
        "WHERE f.text MATCH ? "
    )
    params: list = [topic]
    if speaker_p_id:
        sql += " AND s.speaker_p_id = ?"
        params.append(speaker_p_id)
    sql += " ORDER BY bm25(segment_fts) LIMIT ?"
    # Pull a wider net than `limit` so that the post-filter on soundbite_score
    # can still meet the cap.
    params.append(limit * 4 if (min_soundbite_score is not None or story_function) else limit)

    seg_rows = con.execute(sql, params).fetchall()
    # Group by asset_id for transcript loads
    by_asset: dict[str, list[tuple]] = defaultdict(list)
    for r in seg_rows:
        by_asset[r[0]].append(r)

    results: list[dict] = []
    for aid, rs in by_asset.items():
        tx_path = TRANSCRIPTS_DIR / f"{aid}.transcript.json"
        quote_lookup: dict[int, dict] = {}
        if tx_path.exists():
            try:
                tx = json.loads(tx_path.read_text(encoding="utf-8"))
                analysis = tx.get("analysis") or {}
                for q in analysis.get("key_quotes") or []:
                    qs, qe = q.get("start_sec"), q.get("end_sec")
                    if qs is None or qe is None:
                        continue
                    # Index by integer second of quote span for quick overlap lookup
                    for sec in range(int(qs), int(qe) + 1):
                        quote_lookup.setdefault(sec, q)
            except Exception:
                pass

        for r in rs:
            seg_idx, ss, se, text, speaker_pid, filename, shoot, atype = (
                r[1], r[2], r[3], r[4], r[5], r[6], r[7], r[8])
            # Find any overlapping key_quote
            matched_quote = None
            if ss is not None and se is not None:
                for sec in range(int(ss), int(se) + 1):
                    if sec in quote_lookup:
                        matched_quote = quote_lookup[sec]
                        break
            soundbite_score = None
            story_fn = None
            if matched_quote:
                sb = matched_quote.get("soundbite_quality") or {}
                soundbite_score = _parse_score(sb.get("score"))
                story_fn = matched_quote.get("story_function")
            # Apply post-filters
            if min_soundbite_score is not None and (
                soundbite_score is None or soundbite_score < min_soundbite_score
            ):
                continue
            if story_function and story_fn != story_function:
                continue
            results.append({
                "asset_id": aid, "seg_idx": seg_idx,
                "start_sec": ss, "end_sec": se,
                "text": text,
                "speaker_p_id": speaker_pid,
                "speaker_name": names.get(speaker_pid, speaker_pid) if speaker_pid else None,
                "filename": filename, "shoot_label": shoot, "asset_type": atype,
                "soundbite_score": soundbite_score,
                "story_function": story_fn,
                "has_g023_quote": matched_quote is not None,
            })
            if len(results) >= limit:
                break
        if len(results) >= limit:
            break

    con.close()
    # Sort: prefer scored quotes (soundbite_score desc), then unscored.
    results.sort(key=lambda r: (r.get("soundbite_score") is None,
                                -(r.get("soundbite_score") or 0)))
    return results[:limit]


# ---------------- module exports ----------------

__all__ = [
    "find_soundbites_with_face",
    "find_broll_with_quality",
    "find_broll_v2",
    "find_dense_caption_matches",
    "find_funny_moments_on_camera",
    "find_bib_appearances",
    "find_quotes_about_topic",
]
