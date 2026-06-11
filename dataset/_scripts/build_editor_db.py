"""Build editorial_catalog.sqlite — denormalized join surface for the LLM-driven editor.

Sandbox has no PyPI access (no duckdb), so this uses sqlite3 from stdlib.
Same view shapes; performance is fine for ~15k records. To get DuckDB speed
later: ATTACH 'editorial_catalog.sqlite' AS sq (TYPE SQLITE) from a duckdb shell.

Tables:
  asset, segment, event, press_mention, speaker, person_appearance,
  asset_people, asset_orgs, press_people, press_orgs, event_people, event_orgs,
  asset_semantic_chunk, asset_semantic_key_moment

Usage: python3 _scripts/build_editor_db.py
Output: ../indexes/editorial_catalog.sqlite (sibling of dataset/)
"""
from __future__ import annotations
import json
import sqlite3
from pathlib import Path

from semantic_catalog import iter_semantic_from_record, semantic_projection_from_record
from workspace_paths import editorial_catalog_sqlite_path, indexes_dir

ROOT = Path(__file__).resolve().parent.parent
OUT = editorial_catalog_sqlite_path()
# Remaining standalone SQLite stores read by this builder (the rest were pushed
# down to per-asset catalog JSON; see
# `indexes/indexes_README.md` and `dataset/_scripts/migrate_indexes_to_catalog.py`).
FACE_DB = indexes_dir() / "face_embeddings.sqlite"
AUDIO_EVENTS_DB = indexes_dir() / "audio_events.sqlite"

# Per-asset catalog dirs (signal sources for shots, shot_quality, ocr,
# bib_hits, dense_captions, audio_quality, still_aesthetic).
VIDEO_DIR = ROOT / "assets" / "video"
AUDIO_DIR = ROOT / "assets" / "audio"
STILL_DIR = ROOT / "assets" / "stills"

SCHEMA = """
DROP TABLE IF EXISTS asset;
CREATE TABLE asset (
    asset_id TEXT PRIMARY KEY, record_kind TEXT,
    source_path TEXT, filename TEXT, filesize_bytes INTEGER,
    duration_sec REAL, width INTEGER, height INTEGER,
    codec TEXT, color_profile TEXT,
    shoot_date TEXT, shoot_label TEXT, category_name TEXT, camera_id TEXT,
    audio_recorder TEXT, primary_timeline_date TEXT,
    has_machine_transcript INTEGER, has_audio_extract INTEGER, human_transcript INTEGER,
    people_ids_json TEXT, org_ids_json TEXT, moment_ids_json TEXT,
    place_ids_json TEXT,
    bucket TEXT, asset_type TEXT,
    semantic_location TEXT, semantic_subject TEXT, semantic_editorial_notes TEXT
);
CREATE INDEX idx_asset_semantic_loc ON asset(semantic_location) WHERE semantic_location IS NOT NULL;
CREATE INDEX idx_asset_date ON asset(primary_timeline_date);
CREATE INDEX idx_asset_kind ON asset(record_kind);
CREATE INDEX idx_asset_camera ON asset(camera_id);

DROP TABLE IF EXISTS segment;
CREATE TABLE segment (
    asset_id TEXT, seg_idx INTEGER, start_sec REAL, end_sec REAL,
    speaker_id TEXT, speaker_p_id TEXT, text TEXT,
    PRIMARY KEY (asset_id, seg_idx)
);
CREATE INDEX idx_segment_asset ON segment(asset_id);
CREATE INDEX idx_segment_time ON segment(asset_id, start_sec, end_sec);
CREATE INDEX idx_segment_speaker ON segment(speaker_p_id) WHERE speaker_p_id IS NOT NULL;

DROP TABLE IF EXISTS segment_fts;
CREATE VIRTUAL TABLE segment_fts USING fts5(
    text,
    asset_id UNINDEXED,
    seg_idx UNINDEXED,
    tokenize='porter unicode61'
);

DROP TABLE IF EXISTS event;
CREATE TABLE event (
    event_id TEXT PRIMARY KEY, source TEXT, primary_timeline_date TEXT,
    title TEXT, summary TEXT, category TEXT,
    people_ids_json TEXT, org_ids_json TEXT, moment_ids_json TEXT
);
CREATE INDEX idx_event_date ON event(primary_timeline_date);
CREATE INDEX idx_event_source ON event(source);

DROP TABLE IF EXISTS press_mention;
CREATE TABLE press_mention (
    id TEXT PRIMARY KEY, kind TEXT, parent_kind TEXT, parent_id TEXT,
    primary_timeline_date TEXT, title TEXT, publication TEXT,
    summary_one_line TEXT, storylines_json TEXT, tone_sentiment TEXT,
    people_ids_json TEXT, org_ids_json TEXT, moment_ids_json TEXT
);
CREATE INDEX idx_press_date ON press_mention(primary_timeline_date);
CREATE INDEX idx_press_kind ON press_mention(kind);
CREATE INDEX idx_press_parent ON press_mention(parent_kind, parent_id);

DROP TABLE IF EXISTS asset_people;
CREATE TABLE asset_people (asset_id TEXT, p_id TEXT, PRIMARY KEY (asset_id, p_id));
CREATE INDEX idx_ap_p ON asset_people(p_id);

DROP TABLE IF EXISTS asset_orgs;
CREATE TABLE asset_orgs (asset_id TEXT, o_id TEXT, PRIMARY KEY (asset_id, o_id));
CREATE INDEX idx_ao_o ON asset_orgs(o_id);

DROP TABLE IF EXISTS press_people;
CREATE TABLE press_people (id TEXT, p_id TEXT, PRIMARY KEY (id, p_id));
CREATE INDEX idx_pp_p ON press_people(p_id);

DROP TABLE IF EXISTS press_orgs;
CREATE TABLE press_orgs (id TEXT, o_id TEXT, PRIMARY KEY (id, o_id));
CREATE INDEX idx_po_o ON press_orgs(o_id);

DROP TABLE IF EXISTS event_people;
CREATE TABLE event_people (event_id TEXT, p_id TEXT, PRIMARY KEY (event_id, p_id));
CREATE INDEX idx_ep_p ON event_people(p_id);

DROP TABLE IF EXISTS event_orgs;
CREATE TABLE event_orgs (event_id TEXT, o_id TEXT, PRIMARY KEY (event_id, o_id));
CREATE INDEX idx_eo_o ON event_orgs(o_id);

DROP TABLE IF EXISTS person_appearance;
CREATE TABLE person_appearance (
    p_id TEXT, asset_id TEXT, seg_idx INTEGER,
    start_sec REAL, end_sec REAL, text TEXT
);
CREATE INDEX idx_pa_p ON person_appearance(p_id);
CREATE INDEX idx_pa_asset ON person_appearance(asset_id);

DROP TABLE IF EXISTS speaker;
CREATE TABLE speaker (
    asset_id TEXT, speaker_id TEXT, p_id TEXT, label_raw TEXT,
    is_stub INTEGER, segment_count INTEGER, total_duration_sec REAL, first_seen_sec REAL,
    PRIMARY KEY (asset_id, speaker_id)
);
CREATE INDEX idx_sp_p ON speaker(p_id) WHERE p_id IS NOT NULL;

DROP TABLE IF EXISTS asset_semantic_key_moment;
DROP TABLE IF EXISTS asset_semantic_chunk;
CREATE TABLE asset_semantic_chunk (
    asset_id TEXT NOT NULL,
    chunk_id TEXT NOT NULL,
    chunk_idx INTEGER NOT NULL,
    start_sec REAL,
    end_sec REAL,
    model TEXT,
    subject TEXT,
    action TEXT,
    setting_location TEXT,
    setting_time_of_day TEXT,
    setting_weather TEXT,
    camera_shot_size TEXT,
    camera_movement TEXT,
    camera_perspective TEXT,
    audio_character TEXT,
    emotional_tone TEXT,
    editorial_notes TEXT,
    PRIMARY KEY (chunk_id)
);
CREATE INDEX idx_asc_asset ON asset_semantic_chunk(asset_id);
CREATE INDEX idx_asc_loc ON asset_semantic_chunk(setting_location)
    WHERE setting_location IS NOT NULL;
CREATE INDEX idx_asc_notes ON asset_semantic_chunk(editorial_notes)
    WHERE editorial_notes IS NOT NULL;

CREATE TABLE asset_semantic_key_moment (
    asset_id TEXT NOT NULL,
    chunk_id TEXT NOT NULL,
    moment_idx INTEGER NOT NULL,
    timestamp_sec REAL NOT NULL,
    description TEXT,
    PRIMARY KEY (chunk_id, moment_idx)
);
CREATE INDEX idx_askm_asset ON asset_semantic_key_moment(asset_id);
CREATE INDEX idx_askm_time ON asset_semantic_key_moment(asset_id, timestamp_sec);

DROP TABLE IF EXISTS asset_place;
CREATE TABLE asset_place (
    asset_id TEXT NOT NULL,
    pl_id TEXT NOT NULL,
    source TEXT,
    confidence TEXT,
    matched_phrase TEXT,
    PRIMARY KEY (asset_id, pl_id)
);
CREATE INDEX idx_asset_place_pl ON asset_place(pl_id);
CREATE INDEX idx_asset_place_asset ON asset_place(asset_id);

DROP TABLE IF EXISTS frame_face;
CREATE TABLE frame_face (
    face_pk        INTEGER PRIMARY KEY,
    asset_id       TEXT NOT NULL,
    record_kind    TEXT NOT NULL,
    chunk_id       TEXT,
    frame_idx      INTEGER NOT NULL,
    frame_time_sec REAL,
    p_id           TEXT NOT NULL,
    cluster_id     INTEGER,
    det_score      REAL,
    bbox_json      TEXT,
    identified_via TEXT
);
CREATE INDEX idx_ff_pid    ON frame_face(p_id);
CREATE INDEX idx_ff_asset  ON frame_face(asset_id);
CREATE INDEX idx_ff_passet ON frame_face(p_id, asset_id);
CREATE INDEX idx_ff_cluster ON frame_face(cluster_id) WHERE cluster_id IS NOT NULL;

DROP TABLE IF EXISTS shot;
CREATE TABLE shot (
    asset_id     TEXT NOT NULL,
    shot_idx     INTEGER NOT NULL,
    start_sec    REAL NOT NULL,
    end_sec      REAL NOT NULL,
    duration_sec REAL NOT NULL,
    PRIMARY KEY (asset_id, shot_idx)
);
CREATE INDEX idx_shot_asset ON shot(asset_id);
CREATE INDEX idx_shot_dur ON shot(duration_sec);

DROP TABLE IF EXISTS frame_text;
CREATE TABLE frame_text (
    frame_text_pk  INTEGER PRIMARY KEY,
    asset_id       TEXT NOT NULL,
    record_kind    TEXT NOT NULL,
    shot_idx       INTEGER,
    frame_time_sec REAL NOT NULL,
    bbox_json      TEXT,
    text           TEXT NOT NULL,
    confidence     REAL NOT NULL,
    ocr_engine     TEXT NOT NULL
);
CREATE INDEX idx_ft_asset ON frame_text(asset_id);
CREATE INDEX idx_ft_asset_shot ON frame_text(asset_id, shot_idx);
CREATE INDEX idx_ft_text ON frame_text(text);

DROP TABLE IF EXISTS bib_hit;
CREATE TABLE bib_hit (
    bib_pk         INTEGER PRIMARY KEY,
    asset_id       TEXT NOT NULL,
    shot_idx       INTEGER,
    frame_time_sec REAL NOT NULL,
    bib_number     TEXT NOT NULL,
    confidence     REAL NOT NULL,
    ocr_engine     TEXT NOT NULL,
    p_id           TEXT
);
CREATE INDEX idx_bib_number ON bib_hit(bib_number);
CREATE INDEX idx_bib_asset ON bib_hit(asset_id);
CREATE INDEX idx_bib_pid ON bib_hit(p_id) WHERE p_id IS NOT NULL;

DROP VIEW IF EXISTS shot_text;
CREATE VIEW shot_text AS
SELECT ft.asset_id, ft.shot_idx,
       COUNT(*) n_hits,
       COUNT(DISTINCT ft.text) n_distinct_text,
       GROUP_CONCAT(DISTINCT ft.text) text_set
  FROM frame_text ft
 WHERE ft.shot_idx IS NOT NULL
 GROUP BY ft.asset_id, ft.shot_idx;

DROP TABLE IF EXISTS shot_quality;
CREATE TABLE shot_quality (
    asset_id              TEXT NOT NULL,
    shot_idx              INTEGER NOT NULL,
    n_frames_sampled      INTEGER,
    sharpness_score       REAL,
    motion_score          REAL,
    exposure_mean         REAL,
    clipping_ratio        REAL,
    is_blurry             INTEGER,
    is_dark               INTEGER,
    is_blown              INTEGER,
    is_setup_or_teardown  INTEGER,
    is_in_focus           INTEGER,
    aesthetic_score       REAL,
    is_aesthetic          INTEGER,
    PRIMARY KEY (asset_id, shot_idx)
);
CREATE INDEX idx_sq_asset ON shot_quality(asset_id);
CREATE INDEX idx_sq_focus ON shot_quality(is_in_focus);
CREATE INDEX idx_sq_setup ON shot_quality(is_setup_or_teardown);
CREATE INDEX idx_sq_aesthetic ON shot_quality(is_aesthetic);

DROP TABLE IF EXISTS audio_quality;
CREATE TABLE audio_quality (
    asset_id          TEXT PRIMARY KEY,
    record_kind       TEXT NOT NULL,
    duration_sec      REAL,
    rms_dbfs          REAL,
    peak_dbfs         REAL,
    clipping_ratio    REAL,
    silence_ratio     REAL,
    dc_offset         REAL,
    low_freq_ratio    REAL,
    is_silent         INTEGER,
    is_quiet          INTEGER,
    is_clippy         INTEGER,
    is_windy          INTEGER,
    is_usable         INTEGER
);
CREATE INDEX idx_aq_usable ON audio_quality(is_usable);
CREATE INDEX idx_aq_clippy ON audio_quality(is_clippy);

DROP TABLE IF EXISTS audio_event;
CREATE TABLE audio_event (
    event_pk          INTEGER PRIMARY KEY,
    asset_id          TEXT NOT NULL,
    record_kind       TEXT NOT NULL,
    window_start_sec  REAL NOT NULL,
    window_end_sec    REAL NOT NULL,
    tag               TEXT NOT NULL,
    theme             TEXT NOT NULL,
    score             REAL NOT NULL,
    rank_in_win       INTEGER NOT NULL,
    engine            TEXT NOT NULL
);
CREATE INDEX idx_ae_asset ON audio_event(asset_id);
CREATE INDEX idx_ae_tag ON audio_event(tag);
CREATE INDEX idx_ae_theme ON audio_event(theme);
CREATE INDEX idx_ae_asset_time ON audio_event(asset_id, window_start_sec);
CREATE INDEX idx_ae_engine ON audio_event(engine);

DROP TABLE IF EXISTS dense_caption;
CREATE TABLE dense_caption (
    asset_id        TEXT NOT NULL,
    chunk_id        TEXT,
    shot_idx        INTEGER,
    frame_time_sec  REAL NOT NULL,
    sample_pos      TEXT,
    caption_text    TEXT NOT NULL,
    caption_json    TEXT,
    model_engine    TEXT NOT NULL,
    prompt_variant  TEXT NOT NULL
);
CREATE INDEX idx_dc_asset ON dense_caption(asset_id);
CREATE INDEX idx_dc_shot ON dense_caption(asset_id, shot_idx);
CREATE INDEX idx_dc_engine ON dense_caption(model_engine);

DROP TABLE IF EXISTS still_aesthetic;
CREATE TABLE still_aesthetic (
    asset_id         TEXT PRIMARY KEY,
    aesthetic_score  REAL,
    is_aesthetic     INTEGER,
    image_ext        TEXT
);
CREATE INDEX idx_sa_score ON still_aesthetic(aesthetic_score);
CREATE INDEX idx_sa_is_aesthetic ON still_aesthetic(is_aesthetic);

DROP VIEW IF EXISTS v_asset_with_transcript;
CREATE VIEW v_asset_with_transcript AS
SELECT a.asset_id, a.record_kind, a.source_path, a.duration_sec,
       a.shoot_date, a.primary_timeline_date, a.camera_id, a.codec, a.color_profile,
       a.has_audio_extract, a.has_machine_transcript,
       (SELECT COUNT(*) FROM segment s WHERE s.asset_id = a.asset_id) AS segment_count,
       (SELECT COUNT(DISTINCT speaker_id) FROM speaker sp WHERE sp.asset_id = a.asset_id) AS speaker_count
  FROM asset a;

DROP VIEW IF EXISTS v_asset_enriched;
CREATE VIEW v_asset_enriched AS
SELECT a.*,
       (SELECT COUNT(*) FROM asset_semantic_chunk c WHERE c.asset_id = a.asset_id) AS semantic_chunk_count,
       (SELECT COUNT(*) FROM asset_semantic_key_moment km WHERE km.asset_id = a.asset_id) AS key_moment_count,
       (SELECT COUNT(*) FROM asset_place ap WHERE ap.asset_id = a.asset_id) AS place_link_count,
       (SELECT GROUP_CONCAT(ap.pl_id, ',') FROM asset_place ap WHERE ap.asset_id = a.asset_id) AS place_ids
  FROM asset a;
"""


def load_assets(cur):
    asset_specs = [
        ("assets/video", "video"),
        ("assets/audio", "audio"),
        ("assets/stills", "still"),
    ]
    chunk_n = 0
    km_n = 0
    n = 0
    for d, kind in asset_specs:
        for p in (ROOT / d).glob("*.json"):
            try:
                r = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                continue
            asset_id = r.get("asset_id")
            if not asset_id:
                continue
            ff = r.get("ffprobe") or {}
            pm = r.get("path_metadata") or {}
            people_ids = r.get("people_ids") or []
            org_ids = r.get("org_ids") or []
            moment_ids = r.get("moment_ids") or []
            place_ids = r.get("place_ids") or []
            has_t = (ROOT / "assets/transcripts" / f"{asset_id}.transcript.json").exists()
            sem_loc, sem_subj, sem_notes = semantic_projection_from_record(r)
            cur.execute(
                "INSERT OR REPLACE INTO asset VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    asset_id, r.get("record_kind") or kind, r.get("source_path"),
                    r.get("filename"), r.get("filesize_bytes"),
                    ff.get("duration_sec"), ff.get("width"), ff.get("height"),
                    ff.get("codec"), ff.get("color_profile"),
                    pm.get("shoot_date"), pm.get("shoot_label"), pm.get("category_name"),
                    pm.get("camera_id"), r.get("audio_recorder"),
                    r.get("primary_timeline_date"),
                    int(has_t), int(bool(r.get("audio_extract"))),
                    int(bool(r.get("human_transcript") or r.get("human_transcript_pdf") or r.get("has_transcript"))),
                    json.dumps(people_ids), json.dumps(org_ids), json.dumps(moment_ids),
                    json.dumps(place_ids),
                    (r.get("asset_classifications") or {}).get("bucket"),
                    (r.get("asset_classifications") or {}).get("type"),
                    sem_loc, sem_subj, sem_notes,
                ),
            )
            n += 1
            for pid in people_ids:
                cur.execute("INSERT OR IGNORE INTO asset_people VALUES (?,?)", (asset_id, pid))
            for oid in org_ids:
                cur.execute("INSERT OR IGNORE INTO asset_orgs VALUES (?,?)", (asset_id, oid))
            pm_audit = r.get("place_match") or {}
            src_map = pm_audit.get("sources") if isinstance(pm_audit, dict) else {}
            match_list = pm_audit.get("matches") if isinstance(pm_audit, dict) else []
            if not isinstance(match_list, list):
                match_list = []
            match_by_id = {
                m["pl_id"]: m for m in match_list if isinstance(m, dict) and m.get("pl_id")
            }
            for pl_id in place_ids:
                if not isinstance(pl_id, str):
                    continue
                m = match_by_id.get(pl_id) or {}
                srcs = src_map.get(pl_id) if isinstance(src_map, dict) else []
                source = ",".join(srcs) if srcs else "catalog"
                cur.execute(
                    "INSERT OR REPLACE INTO asset_place VALUES (?,?,?,?,?)",
                    (
                        asset_id,
                        pl_id,
                        source,
                        m.get("confidence"),
                        m.get("matched_phrase"),
                    ),
                )
            ch_rows, km_rows = iter_semantic_from_record(asset_id, r)
            for row in ch_rows:
                cur.execute(
                    """INSERT OR REPLACE INTO asset_semantic_chunk VALUES (
                        ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    row,
                )
                chunk_n += 1
            for row in km_rows:
                cur.execute(
                    "INSERT OR REPLACE INTO asset_semantic_key_moment VALUES (?,?,?,?,?)",
                    row,
                )
                km_n += 1
    return n, chunk_n, km_n


def load_transcripts(cur):
    seg_n = 0
    sp_n = 0
    pa_n = 0
    for p in (ROOT / "assets/transcripts").glob("*.json"):
        try:
            r = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        asset_id = r.get("asset_id")
        if not asset_id:
            continue
        for i, seg in enumerate(r.get("segments") or []):
            cur.execute(
                "INSERT OR REPLACE INTO segment VALUES (?,?,?,?,?,?,?)",
                (asset_id, i, seg.get("start_sec"), seg.get("end_sec"),
                 seg.get("speaker_raw"), seg.get("speaker"), seg.get("text")),
            )
            seg_n += 1
            if seg.get("speaker"):
                cur.execute(
                    "INSERT INTO person_appearance VALUES (?,?,?,?,?,?)",
                    (seg["speaker"], asset_id, i, seg.get("start_sec"), seg.get("end_sec"), seg.get("text")),
                )
                pa_n += 1
        for sp in r.get("speakers") or []:
            cur.execute(
                "INSERT OR REPLACE INTO speaker VALUES (?,?,?,?,?,?,?,?)",
                (asset_id, sp.get("speaker_id"), sp.get("p_id"), sp.get("label_raw"),
                 int(bool(sp.get("is_stub"))), sp.get("segment_count"),
                 sp.get("total_duration_sec"), sp.get("first_seen_sec")),
            )
            sp_n += 1
        for pid in r.get("people_ids") or []:
            cur.execute("INSERT OR IGNORE INTO asset_people VALUES (?,?)", (asset_id, pid))
        for oid in r.get("org_ids") or []:
            cur.execute("INSERT OR IGNORE INTO asset_orgs VALUES (?,?)", (asset_id, oid))
    return seg_n, sp_n, pa_n


def _iter_jsonl(path):
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            yield json.loads(line)
        except Exception:
            continue


def load_events(cur):
    n = 0
    us = ROOT / "timeline/us_events.jsonl"
    for r in _iter_jsonl(us):
        eid = r.get("event_id")
        if not eid:
            continue
        ppl = r.get("people_ids") or []
        org = r.get("org_ids") or []
        beat = r.get("moment_ids") or []
        cur.execute(
            "INSERT OR REPLACE INTO event VALUES (?,?,?,?,?,?,?,?,?)",
            (eid, "us_news", r.get("primary_timeline_date"),
             r.get("title"), r.get("summary"), r.get("category"),
             json.dumps(ppl), json.dumps(org), json.dumps(beat)),
        )
        n += 1
        for pid in ppl:
            cur.execute("INSERT OR IGNORE INTO event_people VALUES (?,?)", (eid, pid))
        for oid in org:
            cur.execute("INSERT OR IGNORE INTO event_orgs VALUES (?,?)", (eid, oid))

    return n


def load_press(cur):
    n = 0
    for p in (ROOT / "documents/press/articles").glob("*.json"):
        try:
            r = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        rid = r.get("article_id")
        if not rid:
            continue
        an = r.get("analysis") or {}
        ppl = r.get("people_ids") or []
        org = r.get("org_ids") or []
        beat = r.get("moment_ids") or []
        cur.execute(
            "INSERT OR REPLACE INTO press_mention VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (rid, "article", None, None,
             r.get("primary_timeline_date") or (r.get("metadata") or {}).get("publish_date"),
             (r.get("metadata") or {}).get("title"),
             (r.get("metadata") or {}).get("publication_name"),
             an.get("summary_one_line"),
             json.dumps(an.get("storylines") or []),
             (an.get("tone") or {}).get("sentiment"),
             json.dumps(ppl), json.dumps(org), json.dumps(beat)),
        )
        n += 1
        for pid in ppl:
            cur.execute("INSERT OR IGNORE INTO press_people VALUES (?,?)", (rid, pid))
        for oid in org:
            cur.execute("INSERT OR IGNORE INTO press_orgs VALUES (?,?)", (rid, oid))

    for p in (ROOT / "documents/press/comments").glob("*.json"):
        try:
            r = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        rid = r.get("comment_id")
        if not rid:
            continue
        an = r.get("analysis") or {}
        parent = r.get("parent") or {}
        ppl = r.get("people_ids") or []
        org = r.get("org_ids") or []
        beat = r.get("moment_ids") or []
        cur.execute(
            "INSERT OR REPLACE INTO press_mention VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (rid, "comment", parent.get("kind"), parent.get("id"),
             r.get("primary_timeline_date"),
             None, None, an.get("summary_one_line"),
             json.dumps(an.get("storylines") or []), None,
             json.dumps(ppl), json.dumps(org), json.dumps(beat)),
        )
        n += 1
        for pid in ppl:
            cur.execute("INSERT OR IGNORE INTO press_people VALUES (?,?)", (rid, pid))
        for oid in org:
            cur.execute("INSERT OR IGNORE INTO press_orgs VALUES (?,?)", (rid, oid))

    sp_dir = ROOT / "documents/press/social_posts"
    if sp_dir.exists():
        for p in sp_dir.glob("*.json"):
            try:
                r = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                continue
            rid = r.get("post_id")
            if not rid:
                continue
            an = r.get("analysis") or {}
            ppl = r.get("people_ids") or []
            org = r.get("org_ids") or []
            beat = r.get("moment_ids") or []
            cur.execute(
                "INSERT OR REPLACE INTO press_mention VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (rid, "social_post", None, None,
                 r.get("primary_timeline_date"),
                 None, None,
                 an.get("summary_one_line"),
                 json.dumps(an.get("storylines") or []),
                 (an.get("tone") or {}).get("sentiment"),
                 json.dumps(ppl), json.dumps(org), json.dumps(beat)),
            )
            n += 1
            for pid in ppl:
                cur.execute("INSERT OR IGNORE INTO press_people VALUES (?,?)", (rid, pid))
            for oid in org:
                cur.execute("INSERT OR IGNORE INTO press_orgs VALUES (?,?)", (rid, oid))
    return n


def rebuild_segment_fts(cur) -> int:
    cur.execute("DELETE FROM segment_fts")
    cur.execute(
        """
        INSERT INTO segment_fts(text, asset_id, seg_idx)
        SELECT text, asset_id, seg_idx FROM segment
         WHERE text IS NOT NULL AND TRIM(text) != ''
        """
    )
    return cur.execute("SELECT COUNT(*) FROM segment_fts").fetchone()[0]


def load_shots(cur: sqlite3.Cursor) -> int:
    """Project per-asset shot boundaries from video catalog JSON's `shots.items[]`."""
    n = 0
    for p in VIDEO_DIR.glob("*.video.json"):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        asset_id = d.get("asset_id")
        block = d.get("shots") or {}
        items = block.get("items") or []
        if not asset_id or not items:
            continue
        rows = [(asset_id, it["shot_idx"], it["start_sec"], it["end_sec"], it["duration_sec"])
                for it in items]
        cur.executemany(
            "INSERT INTO shot (asset_id, shot_idx, start_sec, end_sec, duration_sec) VALUES (?, ?, ?, ?, ?)",
            rows,
        )
        n += len(rows)
    return n


# Lazy-loaded English word list for OCR pseudo-word detection
_ENGLISH_WORDS: set[str] | None = None


def _english_words() -> set[str]:
    """Macros /usr/share/dict/words; returns set of lowercase words len >= 3.
    Returns empty set if dictionary not available (won't filter on this rule)."""
    global _ENGLISH_WORDS
    if _ENGLISH_WORDS is None:
        try:
            with open("/usr/share/dict/words") as f:
                _ENGLISH_WORDS = {w.strip().lower() for w in f if 3 <= len(w.strip()) <= 30}
        except FileNotFoundError:
            _ENGLISH_WORDS = set()
    return _ENGLISH_WORDS


def _looks_like_pseudoword(text: str) -> bool:
    """True if text has all-uppercase alpha tokens (6+ chars) that contain NO
    5-char substring matching an English dictionary word. Catches Apple Vision
    hallucinations like 'SAIARE', 'SIKAND', 'ALECAPEO' that survive the simpler
    >=3-alnum + Cyrillic filters.

    Conservative: only flags ALL-UPPERCASE 6+ char tokens (so mixed-case brand
    names like 'UltrA', 'SAFIRE' are kept). Won't catch every hallucination
    (e.g. 'WINCHE POGS' has 'winch' as a substring, slipping through) but
    eliminates the worst cases.
    """
    import re
    dict_words = _english_words()
    if not dict_words:
        return False
    # Tokenize: all-uppercase alpha sequences of length >= 6
    tokens = re.findall(r'\b[A-Z]{6,}\b', text)
    for t in tokens:
        tl = t.lower()
        # Full token in dict?
        if tl in dict_words:
            continue
        # Any 5-char substring in dict?
        if any(tl[i:i + 5] in dict_words for i in range(len(tl) - 4)):
            continue
        # No dictionary anchor anywhere in this token → looks like noise
        return True
    return False


def _ocr_text_passes_filter(text: str) -> bool:
    """Drop hits with <3 alphanumeric chars, pure punctuation, or Cyrillic
    script. Apple Vision occasionally emits high-confidence hallucinations
    like '== =' or pattern-matches Cyrillic glyphs against noisy frames in
    English-language footage — both kinds are dropped before reaching the
    editorial surface. Raw rows remain in `ocr.sqlite` for debugging.

    Filter calibration history (visible in `dataset/_runs/qa/.../ocr-*.md`):
      - >=3 alnum + non-punctuation only
      - + drop Cyrillic glyphs (254 hits / 0.8% of Apple Vision,
        100% hallucinations on this English-language corpus)
      - tried a dictionary-substring pseudo-word filter; reverted
        — over-filtered real signal (modern brand names like TYLENOL, show
        titles like THE TRACER PODCAST, concatenated proper nouns like
        JOEYWILSON) that aren't in /usr/share/dict/words. `_looks_like_pseudoword`
        stays in the file as a reference but isn't called.

    Known follow-up (tracked in `indexes/indexes_GAPS.md`): an LLM-verdict
    pass at projection time would catch hallucinations like SAIARE, WINCHE POGS,
    upon teadionln (~13% noise rate per QA sample). ~$1-2 with Gemini Flash
    across the 61K frame_text rows. Skipped V1 since the noise is editorially
    flag-able by eye when results return.
    """
    if not text:
        return False
    alnum = sum(1 for c in text if c.isalnum())
    if alnum < 3:
        return False
    # Cyrillic range U+0400-U+04FF + Cyrillic Supplement U+0500-U+052F
    if any("Ѐ" <= c <= "ԯ" for c in text):
        return False
    return True


def load_shot_quality(cur: sqlite3.Cursor) -> int:
    """Project per-shot quality metrics from video catalog JSON's `shot_quality.items[]`.

    Pulls NIMA `aesthetic_score` / `is_aesthetic` when present on each item;
    falls back to NULL otherwise.
    """
    n = 0
    for p in VIDEO_DIR.glob("*.video.json"):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        asset_id = d.get("asset_id")
        block = d.get("shot_quality") or {}
        items = block.get("items") or []
        if not asset_id or not items:
            continue
        rows = [(
            asset_id, it["shot_idx"], it.get("n_frames_sampled"),
            it.get("sharpness_score"), it.get("motion_score"),
            it.get("exposure_mean"), it.get("clipping_ratio"),
            it.get("is_blurry"), it.get("is_dark"), it.get("is_blown"),
            it.get("is_setup_or_teardown"), it.get("is_in_focus"),
            it.get("aesthetic_score"), it.get("is_aesthetic"),
        ) for it in items]
        cur.executemany(
            "INSERT INTO shot_quality (asset_id, shot_idx, n_frames_sampled, "
            "sharpness_score, motion_score, exposure_mean, clipping_ratio, "
            "is_blurry, is_dark, is_blown, is_setup_or_teardown, is_in_focus, "
            "aesthetic_score, is_aesthetic) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        n += len(rows)
    return n


def load_audio_quality(cur: sqlite3.Cursor) -> int:
    """Project per-asset audio quality metrics from catalog JSON.

    Videos:  read `video.json["audio_extract"]["audio_quality"]`  (record_kind='video')
    Audios:  read `audio.json["audio_quality"]`                    (record_kind='audio')
    """
    n = 0
    for kind, directory, path_fn in (
        ("video", VIDEO_DIR, lambda d: ((d.get("audio_extract") or {}).get("audio_quality") or {})),
        ("audio", AUDIO_DIR, lambda d: (d.get("audio_quality") or {})),
    ):
        for p in directory.glob(f"*.{kind}.json"):
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                continue
            asset_id = d.get("asset_id")
            aq = path_fn(d)
            metrics = aq.get("metrics") or {}
            if not asset_id or not metrics:
                continue
            cur.execute(
                "INSERT OR REPLACE INTO audio_quality (asset_id, record_kind, duration_sec, "
                "rms_dbfs, peak_dbfs, clipping_ratio, silence_ratio, dc_offset, "
                "low_freq_ratio, is_silent, is_quiet, is_clippy, is_windy, is_usable) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    asset_id, kind, aq.get("duration_sec"),
                    metrics.get("rms_dbfs"), metrics.get("peak_dbfs"),
                    metrics.get("clipping_ratio"), metrics.get("silence_ratio"),
                    metrics.get("dc_offset"), metrics.get("low_freq_ratio"),
                    metrics.get("is_silent"), metrics.get("is_quiet"),
                    metrics.get("is_clippy"), metrics.get("is_windy"), metrics.get("is_usable"),
                ),
            )
            n += 1
    return n


def load_audio_events(cur: sqlite3.Cursor) -> int:
    """Project timed CLAP audio-event tags from audio_events.sqlite (WAL)."""
    if not AUDIO_EVENTS_DB.exists():
        print("  (no audio_events.sqlite — skipping audio_event projection)")
        return 0
    cur.connection.commit()  # Source DB is WAL; commit before ATTACH
    cur.execute("ATTACH DATABASE ? AS ae_db", (str(AUDIO_EVENTS_DB),))
    rows = cur.execute("""
        SELECT event_pk, asset_id, record_kind, window_start_sec, window_end_sec,
               tag, theme, score, rank_in_win, engine
        FROM ae_db.audio_event
    """).fetchall()
    cur.executemany(
        "INSERT INTO audio_event (event_pk, asset_id, record_kind, "
        "window_start_sec, window_end_sec, tag, theme, score, rank_in_win, engine) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    cur.connection.commit()  # Commit before DETACH (WAL source-DB pattern)
    cur.execute("DETACH DATABASE ae_db")
    return len(rows)


def load_ocr_detections(cur: sqlite3.Cursor) -> tuple[int, int]:
    """Project OCR rows from catalog JSON (`ocr_detections.items[]` on video.json
    and still.json) into `frame_text`; project `bib_hits.items[]` into `bib_hit`.

    Filtering applied at projection time (raw rows remain in catalog JSON):
      - drop where qa_verdict='suspicious' (Gemini Flash QA pass tagged as hallucination)
      - drop where _ocr_text_passes_filter rejects (<3 alnum, pure punctuation, Cyrillic)

    Source-PK columns (`frame_text_pk`, `bib_pk`) are auto-assigned by SQLite
    since the catalog-JSON migration dropped the original ocr_pk relation.
    Returns (frame_text_rows_inserted, bib_hit_rows_inserted).
    """
    n_text = 0
    n_bib = 0
    for kind, directory, glob_pat in (
        ("video", VIDEO_DIR, "*.video.json"),
        ("still", STILL_DIR, "*.still.json"),
    ):
        for p in directory.glob(glob_pat):
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                continue
            asset_id = d.get("asset_id")
            if not asset_id:
                continue
            ocr_block = d.get("ocr_detections") or {}
            ocr_items = ocr_block.get("items") or []
            kept = [
                (asset_id, kind, it.get("shot_idx"), it["frame_time_sec"],
                 it.get("bbox_json"), it["text"], it["confidence"], it["ocr_engine"])
                for it in ocr_items
                if (it.get("qa_verdict") != "suspicious")
                and _ocr_text_passes_filter(it.get("text") or "")
            ]
            if kept:
                cur.executemany(
                    "INSERT INTO frame_text (asset_id, record_kind, shot_idx, "
                    "frame_time_sec, bbox_json, text, confidence, ocr_engine) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    kept,
                )
                n_text += len(kept)

            bib_block = d.get("bib_hits") or {}
            bib_items = bib_block.get("items") or []
            bib_rows = [
                (asset_id, it.get("shot_idx"), it["frame_time_sec"],
                 it["bib_number"], it["confidence"],
                 it.get("source_ocr_engine") or it.get("ocr_engine"),
                 it.get("p_id"))
                for it in bib_items
            ]
            if bib_rows:
                cur.executemany(
                    "INSERT INTO bib_hit (asset_id, shot_idx, frame_time_sec, "
                    "bib_number, confidence, ocr_engine, p_id) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    bib_rows,
                )
                n_bib += len(bib_rows)
    return n_text, n_bib


def load_dense_captions(cur: sqlite3.Cursor) -> int:
    """Project per-frame dense captions from video catalog JSON's `dense_captions.items[]`."""
    n = 0
    for p in VIDEO_DIR.glob("*.video.json"):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        asset_id = d.get("asset_id")
        block = d.get("dense_captions") or {}
        items = block.get("items") or []
        if not asset_id or not items:
            continue
        rows = [(
            asset_id, it.get("chunk_id"), it.get("shot_idx"),
            it["frame_time_sec"], it.get("sample_pos"),
            it["caption_text"], it.get("caption_json"),
            it["model_engine"], it["prompt_variant"],
        ) for it in items]
        cur.executemany(
            "INSERT INTO dense_caption (asset_id, chunk_id, shot_idx, frame_time_sec, "
            "sample_pos, caption_text, caption_json, model_engine, prompt_variant) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        n += len(rows)
    return n


def load_still_aesthetic(cur: sqlite3.Cursor) -> int:
    """Project per-still NIMA aesthetic score from still catalog JSON's `still_aesthetic.metrics`."""
    n = 0
    for p in STILL_DIR.glob("*.still.json"):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        asset_id = d.get("asset_id")
        block = d.get("still_aesthetic") or {}
        metrics = block.get("metrics") or {}
        if not asset_id or not metrics:
            continue
        cur.execute(
            "INSERT OR REPLACE INTO still_aesthetic (asset_id, aesthetic_score, "
            "is_aesthetic, image_ext) VALUES (?, ?, ?, ?)",
            (asset_id, metrics.get("aesthetic_score"),
             metrics.get("is_aesthetic"), metrics.get("image_ext")),
        )
        n += 1
    return n


def load_face_detections(cur: sqlite3.Cursor) -> int:
    """Project NAMED face detections from face_embeddings.sqlite into frame_face.

    Unidentified faces (p_id IS NULL) are deliberately excluded to honor the
    consent posture chosen during face-index setup (named identities only on
    the editorial query surface). See the faces layer scripts under dataset/_scripts/faces/."""
    if not FACE_DB.exists():
        print("  (no face_embeddings.sqlite — skipping frame_face projection)")
        return 0
    cur.execute("ATTACH DATABASE ? AS face_db", (str(FACE_DB),))
    rows = cur.execute("""
        SELECT face_pk, asset_id, record_kind, chunk_id, frame_idx,
               frame_time_sec, p_id, cluster_id, det_score, bbox_json, identified_via
        FROM face_db.face_detection WHERE p_id IS NOT NULL
    """).fetchall()
    cur.executemany("""
        INSERT INTO frame_face (face_pk, asset_id, record_kind, chunk_id, frame_idx,
                                frame_time_sec, p_id, cluster_id, det_score,
                                bbox_json, identified_via)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, rows)
    cur.execute("DETACH DATABASE face_db")
    return len(rows)


def main() -> None:
    OUT.parent.mkdir(exist_ok=True)
    if OUT.exists():
        OUT.unlink()
    conn = sqlite3.connect(str(OUT))
    conn.executescript(SCHEMA)
    cur = conn.cursor()

    a, asc, askm = load_assets(cur);     conn.commit()
    seg, sp, pa = load_transcripts(cur); conn.commit()
    fts_n = rebuild_segment_fts(cur);    conn.commit()
    e = load_events(cur);     conn.commit()
    pr = load_press(cur);     conn.commit()
    ff = load_face_detections(cur);      conn.commit()
    sh = load_shots(cur);                conn.commit()
    ft, bh = load_ocr_detections(cur);   conn.commit()
    sq = load_shot_quality(cur);         conn.commit()
    aq = load_audio_quality(cur);        conn.commit()
    ae = load_audio_events(cur);         conn.commit()
    dc = load_dense_captions(cur);       conn.commit()
    sa = load_still_aesthetic(cur);      conn.commit()

    print("=== editorial_catalog.sqlite built ===")
    for tbl in ("asset", "segment", "event", "press_mention",
                "asset_people", "asset_orgs", "press_people", "press_orgs",
                "event_people", "event_orgs", "person_appearance", "speaker",
                "asset_semantic_chunk", "asset_semantic_key_moment", "asset_place",
                "frame_face", "shot", "frame_text", "bib_hit", "shot_quality",
                "audio_quality", "audio_event", "dense_caption", "still_aesthetic"):
        c = cur.execute("SELECT COUNT(*) FROM " + tbl).fetchone()[0]
        print("  %-22s %8d" % (tbl, c))
    print("  %-22s %8d" % ("segment_fts", fts_n))
    sz_mb = OUT.stat().st_size / 1e6
    try:
        out_display = OUT.relative_to(ROOT)
    except ValueError:
        # OUT lives outside ROOT (indexes/ is a sibling of dataset/); fall back to absolute.
        out_display = OUT
    print("File: %s  (%.1f MB)" % (out_display, sz_mb))
    # Clean WAL/shm before close so bindfs doesn't shadow the -shm into .fuse_hidden*.
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except Exception:
        pass
    conn.close()


if __name__ == "__main__":
    main()
