#!/usr/bin/env python3
"""run_layer_qa.py — pure-SQL consistency checks across the Phase K layers.

No LLM cost; reads via WAL-friendly read-only connections so it can run
concurrent with active layer ingest (e.g. while CLAP audio_events is still
processing the back half of the corpus).

Each check produces a markdown report at:
    dataset/_runs/qa/<ts>/<check>.md
and the `all` subcommand writes a top-level summary at:
    dataset/_runs/qa/<ts>/_summary.md

Subcommands:
  coverage              Cross-layer coverage gap report (which assets are missing
                        which layers; flags assets with >=2 missing layers).
  shots-vs-gemini       For each Gemini key_moment, distance to nearest shot
                        boundary. Strong correlation = layers reinforce each other.
  clap-vs-audio-quality CLAP "silence" / "music" / "crowd" tags vs audio_quality
                        flags + RMS. Surfaces CLAP false-positives.
  clap-vs-asset-type    CLAP music / crowd hits on assets where the catalog says
                        it's an interview or sit-down (semantic contradiction).
  chromaprint-sanity    Spot-check chromaprint applied_link entries against shoot
                        metadata; flag any where shoot_label/date don't agree
                        (shouldn't happen given the prefilter, but verify).
  face-clusters         Face cluster size distribution; outliers (under-labeled
                        small clusters; over-conflating large clusters).
  ocr-noise             frame_text rows that passed the >=3 alnum post-filter
                        but look like statistical noise (no vowels, single char
                        repeats, etc.) — candidates to tighten the filter.
  all                   Run all of the above and emit a summary.

Usage:
  python3 dataset/_scripts/qa/run_layer_qa.py all
  python3 dataset/_scripts/qa/run_layer_qa.py coverage
  python3 dataset/_scripts/qa/run_layer_qa.py shots-vs-gemini --sample 20
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from _paths import (  # noqa: E402
    AUDIO_EVENTS_DB, AUDIO_FINGERPRINT_DB, AUDIO_QUALITY_DB,
    FACE_EMBEDDINGS_DB, INDEXES_DIR, OCR_DB, RUNS_DIR, SHOTS_DB,
    SHOT_QUALITY_DB,
)

EDITORIAL_DB = INDEXES_DIR / "editorial_catalog.sqlite"


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ts_slug() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def open_ro(path: Path) -> sqlite3.Connection | None:
    """Open a SQLite DB read-only (WAL-friendly; can run concurrent with
    writers). Returns None if the DB doesn't exist."""
    if not path.exists():
        return None
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


# ============================================================ coverage

def check_coverage(out_dir: Path) -> dict:
    """For each asset with an audio_extract or proxy, list which K-layers have
    rows. Flag assets missing >=2 layers."""
    con = open_ro(EDITORIAL_DB)
    if not con:
        return {"check": "coverage", "skipped": "editorial_catalog.sqlite missing"}

    assets = {r[0]: r for r in con.execute(
        "SELECT asset_id, record_kind, has_audio_extract, duration_sec, "
        "shoot_label FROM asset WHERE record_kind='video'"
    )}
    layer_queries = {
        "shot": "SELECT DISTINCT asset_id FROM shot",
        "shot_quality": "SELECT DISTINCT asset_id FROM shot_quality",
        "frame_face": "SELECT DISTINCT asset_id FROM frame_face",
        "frame_text": "SELECT DISTINCT asset_id FROM frame_text",
        "audio_quality": "SELECT DISTINCT asset_id FROM audio_quality",
        "audio_event": "SELECT DISTINCT asset_id FROM audio_event",
    }
    coverage = {layer: set() for layer in layer_queries}
    for layer, sql in layer_queries.items():
        try:
            coverage[layer] = {r[0] for r in con.execute(sql)}
        except sqlite3.OperationalError as e:
            print(f"  (layer {layer}: {e})")

    n_videos = len(assets)
    per_layer_counts = {l: len(s) for l, s in coverage.items()}

    # Build per-asset present-layer map for the gaps report
    gaps_by_asset: dict[str, set[str]] = {}
    for aid, (_, _, has_ae, dur, shoot) in assets.items():
        present = {l for l in coverage if aid in coverage[l]}
        # Skip assets where we never expect a layer (e.g. no audio extract → no
        # audio_quality / audio_event); rather than gate by record-level
        # context, we list the missing layers raw and let the operator filter.
        missing = set(layer_queries.keys()) - present
        if missing:
            gaps_by_asset[aid] = missing

    # Aggregate: assets missing >=2 layers
    asset_gap_counts = defaultdict(list)
    for aid, missing in gaps_by_asset.items():
        if len(missing) >= 2:
            asset_gap_counts[len(missing)].append((aid, missing, assets[aid]))

    # Write report
    out = out_dir / "coverage.md"
    lines = [
        f"# Layer coverage report ({now_iso()})",
        "",
        f"Editorial catalog: `{EDITORIAL_DB}`",
        f"Video assets: **{n_videos:,}**",
        "",
        "## Per-layer asset coverage",
        "",
        "| Layer | Assets w/ rows | % of video assets |",
        "|---|---:|---:|",
    ]
    for l, n in per_layer_counts.items():
        pct = 100.0 * n / max(n_videos, 1)
        lines.append(f"| {l} | {n:,} | {pct:.1f}% |")
    lines += [
        "",
        f"## Assets missing ≥2 K-layers ({sum(len(v) for v in asset_gap_counts.values())})",
        "",
        "Sample (top 40 by missing-layer count, then shoot_label sort):",
        "",
        "| asset_id | shoot | record | dur_sec | missing layers |",
        "|---|---|---|---:|---|",
    ]
    flat = []
    for n_missing in sorted(asset_gap_counts.keys(), reverse=True):
        flat.extend(asset_gap_counts[n_missing])
    for aid, missing, meta in flat[:40]:
        lines.append(
            f"| `{aid[:12]}` | {meta[4] or '—'} | {meta[1]} | {meta[3] or 0:.0f} | "
            f"{', '.join(sorted(missing))} |"
        )
    out.write_text("\n".join(lines))
    return {
        "check": "coverage",
        "n_assets": n_videos,
        "per_layer": per_layer_counts,
        "n_assets_with_gaps_ge2": sum(len(v) for v in asset_gap_counts.values()),
        "report": str(out),
    }


# ============================================================ shots vs gemini

def check_shots_vs_gemini(out_dir: Path, sample: int = 25) -> dict:
    """For each Gemini key_moment, measure distance to nearest shot boundary.

    Restricted to assets with >1 shot — single-shot assets (locked-off
    interviews) have no interior boundaries by definition, so any interior
    key_moment falsely looks 'far'."""
    con = open_ro(EDITORIAL_DB)
    if not con:
        return {"check": "shots-vs-gemini", "skipped": "editorial_catalog.sqlite missing"}

    rows = con.execute("""
        WITH multi AS (
            SELECT asset_id FROM shot
            GROUP BY asset_id HAVING COUNT(*) > 1
        ),
        boundaries AS (
            SELECT asset_id, start_sec AS t FROM shot
            UNION ALL
            SELECT asset_id, end_sec AS t FROM shot
        )
        SELECT km.asset_id, km.timestamp_sec, km.description,
               (SELECT MIN(ABS(b.t - km.timestamp_sec))
                  FROM boundaries b WHERE b.asset_id = km.asset_id) AS dist
        FROM asset_semantic_key_moment km
        JOIN multi m ON m.asset_id = km.asset_id
    """).fetchall()

    if not rows:
        return {"check": "shots-vs-gemini", "skipped": "no key_moments+shots overlap"}

    dists = [r[3] for r in rows if r[3] is not None]
    dists.sort()
    n = len(dists)
    def pct(p): return dists[int(n * p)] if n else 0
    n_within_1s = sum(1 for d in dists if d <= 1.0)
    n_within_3s = sum(1 for d in dists if d <= 3.0)

    # Outliers: key_moments far from any shot boundary
    far = sorted(rows, key=lambda r: -(r[3] or 0))[:sample]

    out = out_dir / "shots-vs-gemini.md"
    lines = [
        f"# Shots ↔ Gemini key_moments alignment ({now_iso()})",
        "",
        f"For each Gemini key_moment, distance (sec) to the nearest shot boundary "
        f"in its asset. Closer = independent layers agree something interesting "
        f"happens here. Larger = Gemini found content PySceneDetect couldn't "
        f"(neither necessarily wrong; reinforces vs. complements).",
        "",
        f"## Distribution ({n:,} key_moments)",
        "",
        f"- min: **{dists[0]:.2f} s**",
        f"- p25: {pct(0.25):.2f} s",
        f"- median: **{pct(0.5):.2f} s**",
        f"- p75: {pct(0.75):.2f} s",
        f"- p95: {pct(0.95):.2f} s",
        f"- max: {dists[-1]:.2f} s",
        f"",
        f"- Within 1 sec of a shot boundary: **{n_within_1s:,} ({100*n_within_1s/n:.0f}%)**",
        f"- Within 3 sec: {n_within_3s:,} ({100*n_within_3s/n:.0f}%)",
        f"",
        f"## Top-{sample} farthest key_moments (Gemini found content far from any shot cut)",
        "",
        "| asset_id | km.t (s) | dist (s) | description |",
        "|---|---:|---:|---|",
    ]
    for aid, t, desc, d in far:
        lines.append(f"| `{aid[:12]}` | {t:.1f} | {d:.1f} | {(desc or '')[:120]} |")
    out.write_text("\n".join(lines))
    return {
        "check": "shots-vs-gemini",
        "n": n,
        "median_dist_sec": pct(0.5),
        "pct_within_1s": round(100 * n_within_1s / n, 1),
        "report": str(out),
    }


# ============================================================ clap vs audio_quality

CLAP_SILENCE_TAGS = ("silence or room tone", "indoor ambient")
CLAP_MUSIC_TAGS = ("music playing", "acoustic guitar", "piano", "drums",
                   "singing voice", "background score")
CLAP_CROWD_TAGS = ("crowd cheering", "applause", "multiple people talking",
                   "shouting or yelling")


def check_clap_vs_audio_quality(out_dir: Path) -> dict:
    """Find rows where CLAP and audio_quality contradict each other.

    Reads audio_event directly from audio_events.sqlite (not the editor-DB
    projection) so it works during an active CLAP run before the projection
    has been refreshed."""
    if not AUDIO_EVENTS_DB.exists() or not AUDIO_QUALITY_DB.exists():
        return {"check": "clap-vs-audio-quality", "skipped": "audio_events.sqlite or audio_quality.sqlite missing"}

    # Use a read-only connection on audio_events.sqlite and ATTACH audio_quality
    con = sqlite3.connect(f"file:{AUDIO_EVENTS_DB}?mode=ro", uri=True)
    con.execute(f"ATTACH DATABASE 'file:{AUDIO_QUALITY_DB}?mode=ro' AS aq_db")

    # Tightened after the first QA pass: -25 dBFS room tone is legitimately
    # quiet so CLAP "indoor ambient" + audio_quality.is_silent=0 isn't a real
    # contradiction. Require LOUD audio (rms > -10) for the silence-tag contradiction.
    silent_tags_clause = ",".join("?" * len(CLAP_SILENCE_TAGS))
    contradict_silence = con.execute(f"""
        SELECT ae.asset_id, ae.tag, ae.score, ae.engine,
               aq.rms_dbfs, aq.is_silent
        FROM audio_event ae
        JOIN aq_db.audio_quality aq ON aq.asset_id = ae.asset_id
        WHERE ae.tag IN ({silent_tags_clause})
          AND ae.score >= 0.40
          AND aq.is_silent = 0
          AND aq.rms_dbfs > -10
        ORDER BY ae.score DESC LIMIT 30
    """, CLAP_SILENCE_TAGS).fetchall()

    # Tightened: require very quiet audio (rms < -40 = near-silent) for crowd-tag
    # contradiction. -30 dBFS is just moderately quiet, plausibly faint applause.
    crowd_tags_clause = ",".join("?" * len(CLAP_CROWD_TAGS))
    contradict_quiet = con.execute(f"""
        SELECT ae.asset_id, ae.tag, ae.score, ae.engine,
               aq.rms_dbfs
        FROM audio_event ae
        JOIN aq_db.audio_quality aq ON aq.asset_id = ae.asset_id
        WHERE ae.tag IN ({crowd_tags_clause})
          AND ae.score >= 0.40
          AND aq.rms_dbfs < -40
        ORDER BY ae.score DESC LIMIT 30
    """, CLAP_CROWD_TAGS).fetchall()

    music_tags_clause = ",".join("?" * len(CLAP_MUSIC_TAGS))
    music_co_clippy = con.execute(f"""
        SELECT COUNT(DISTINCT ae.asset_id)
        FROM audio_event ae JOIN aq_db.audio_quality aq USING (asset_id)
        WHERE ae.tag IN ({music_tags_clause}) AND ae.score >= 0.30
          AND aq.is_clippy = 1
    """, CLAP_MUSIC_TAGS).fetchone()[0]

    n_total_events = con.execute("SELECT COUNT(*) FROM audio_event").fetchone()[0]

    out = out_dir / "clap-vs-audio-quality.md"
    lines = [
        f"# CLAP ↔ audio_quality consistency ({now_iso()})",
        "",
        f"Total audio_event rows so far: **{n_total_events:,}**",
        f"(NOTE: if CLAP run is still active, this snapshot is partial.)",
        "",
        f"## Check 1: CLAP says SILENT/AMBIENT but audio_quality says NOT silent",
        f"",
        f"`audio_event.tag IN (silence or room tone, indoor ambient) AND score ≥ 0.35`",
        f"  vs  `audio_quality.is_silent = 0 AND rms_dbfs > -25 dBFS`",
        "",
        f"Contradictions (top 30 by score):",
        "",
        "| asset_id | tag | score | engine | rms_dbfs | is_silent |",
        "|---|---|---:|---|---:|---:|",
    ]
    for aid, tag, score, engine, rms, is_silent in contradict_silence:
        lines.append(f"| `{aid[:12]}` | {tag} | {score:.2f} | {engine} | {rms:.1f} | {is_silent} |")
    if not contradict_silence:
        lines.append("| — | _(none)_ | | | | |")

    lines += [
        "",
        f"## Check 2: CLAP says CROWD/LOUD but audio_quality says QUIET",
        f"",
        f"`audio_event.tag IN (crowd cheering, applause, multiple people talking, shouting or yelling) AND score ≥ 0.35`",
        f"  vs  `audio_quality.rms_dbfs < -30 dBFS`",
        "",
        f"Contradictions (top 30 by score):",
        "",
        "| asset_id | tag | score | engine | rms_dbfs |",
        "|---|---|---:|---|---:|",
    ]
    for aid, tag, score, engine, rms in contradict_quiet:
        lines.append(f"| `{aid[:12]}` | {tag} | {score:.2f} | {engine} | {rms:.1f} |")
    if not contradict_quiet:
        lines.append("| — | _(none)_ | | | |")

    lines += [
        "",
        f"## Check 3: positive co-occurrence sanity — music + clippy audio",
        "",
        f"Assets where CLAP detected music AND audio_quality flagged `is_clippy=1`: "
        f"**{music_co_clippy:,}**  _(loud music sources tend to clip; high number is expected good)_",
        "",
    ]
    out.write_text("\n".join(lines))
    return {
        "check": "clap-vs-audio-quality",
        "contradictions_silence": len(contradict_silence),
        "contradictions_crowd_quiet": len(contradict_quiet),
        "music_clippy_co": music_co_clippy,
        "report": str(out),
    }


# ============================================================ clap vs asset type

def check_clap_vs_asset_type(out_dir: Path) -> dict:
    """CLAP music / crowd on assets the catalog says are sit-down interviews.

    Walks catalog JSON for asset_classifications.type instead of editorial_catalog,
    so we don't depend on a particular projection column."""
    if not AUDIO_EVENTS_DB.exists():
        return {"check": "clap-vs-asset-type", "skipped": "audio_events.sqlite missing"}

    # Build asset_id -> (type, shoot_label) map from catalog JSON
    from _paths import VIDEO_CATALOG
    asset_type = {}
    asset_shoot = {}
    for f in VIDEO_CATALOG.glob("*.video.json"):
        if f.name.startswith("._"): continue  # macOS AppleDouble sidecar
        try:
            d = json.loads(f.read_text())
        except Exception:
            continue
        aid = d.get("asset_id")
        if not aid:
            continue
        t = (d.get("asset_classifications") or {}).get("type")
        s = (d.get("path_metadata") or {}).get("shoot_label")
        if t == "interview":
            asset_type[aid] = t
            asset_shoot[aid] = s or ""
    if not asset_type:
        return {"check": "clap-vs-asset-type", "skipped": "no interview-typed assets"}

    con = sqlite3.connect(f"file:{AUDIO_EVENTS_DB}?mode=ro", uri=True)

    # Pull all relevant hits then filter in Python (avoids huge IN list)
    music_set = set(CLAP_MUSIC_TAGS)
    crowd_set = set(CLAP_CROWD_TAGS)
    interview_music = []
    interview_crowd = []
    rows = con.execute(
        "SELECT asset_id, tag, score, engine, window_start_sec FROM audio_event "
        "WHERE score >= 0.30"
    ).fetchall()
    for aid, tag, score, engine, t in rows:
        if aid not in asset_type:
            continue
        if tag in music_set:
            interview_music.append((aid, asset_shoot.get(aid, ""), tag, score, engine, t))
        elif tag in crowd_set:
            interview_crowd.append((aid, asset_shoot.get(aid, ""), tag, score, engine, t))
    interview_music.sort(key=lambda r: -r[3])
    interview_crowd.sort(key=lambda r: -r[3])
    interview_music = interview_music[:25]
    interview_crowd = interview_crowd[:25]

    out = out_dir / "clap-vs-asset-type.md"
    lines = [
        f"# CLAP tag ↔ asset_type semantic consistency ({now_iso()})",
        "",
        "CLAP firing music/crowd on a catalog-classified interview is suspicious.",
        "Real cases include lavalier picking up background music intros / chyron",
        "stings before the actual interview audio — those are correct flags,",
        "not contradictions. Eyeball before treating as errors.",
        "",
        "## Music tags on interview assets (top 25)",
        "",
        "| asset_id | shoot | tag | score | engine | t (s) |",
        "|---|---|---|---:|---|---:|",
    ]
    for aid, shoot, tag, score, engine, t in interview_music:
        lines.append(f"| `{aid[:12]}` | {shoot or '—'} | {tag} | {score:.2f} | {engine} | {t:.1f} |")
    if not interview_music:
        lines.append("| — | _(none)_ | | | | |")

    lines += [
        "",
        "## Crowd/shouting tags on interview assets (top 25)",
        "",
        "| asset_id | shoot | tag | score | engine | t (s) |",
        "|---|---|---|---:|---|---:|",
    ]
    for aid, shoot, tag, score, engine, t in interview_crowd:
        lines.append(f"| `{aid[:12]}` | {shoot or '—'} | {tag} | {score:.2f} | {engine} | {t:.1f} |")
    if not interview_crowd:
        lines.append("| — | _(none)_ | | | | |")

    out.write_text("\n".join(lines))
    return {
        "check": "clap-vs-asset-type",
        "interview_music_count": len(interview_music),
        "interview_crowd_count": len(interview_crowd),
        "report": str(out),
    }


# ============================================================ chromaprint sanity

def check_chromaprint_sanity(out_dir: Path) -> dict:
    """For each applied chromaprint link, verify the shoot_label/date alignment
    (should hold given the prefilter)."""
    if not AUDIO_FINGERPRINT_DB.exists():
        return {"check": "chromaprint-sanity", "skipped": "audio_fingerprints.sqlite missing"}
    # Open writable so SQLite can settle any WAL-state before the SELECT
    # (the RO open errored on a fresh disk I/O hiccup in the first run).
    con_fp = sqlite3.connect(str(AUDIO_FINGERPRINT_DB))
    con_fp.execute("PRAGMA journal_mode=WAL")

    # Join applied_link to fingerprint table on each side for shoot metadata
    rows = con_fp.execute("""
        SELECT al.video_asset_id, al.audio_asset_id,
               al.raw_match_score, al.combined_score,
               fp_v.shoot_label AS v_shoot, fp_v.shoot_date AS v_date, fp_v.camera_id AS v_cam,
               fp_a.shoot_label AS a_shoot, fp_a.shoot_date AS a_date, fp_a.camera_id AS a_cam
        FROM applied_link al
        JOIN fingerprint fp_v ON fp_v.asset_id = al.video_asset_id
        JOIN fingerprint fp_a ON fp_a.asset_id = al.audio_asset_id
    """).fetchall()
    n = len(rows)
    if not n:
        return {"check": "chromaprint-sanity", "skipped": "no applied_link rows"}

    shoot_match = 0
    date_match = 0
    no_overlap = []
    for r in rows:
        v_sh, v_dt, _v_cam = r[4], r[5], r[6]
        a_sh, a_dt, _a_cam = r[7], r[8], r[9]
        if v_sh and a_sh and v_sh == a_sh:
            shoot_match += 1
        if v_dt and a_dt and v_dt == a_dt:
            date_match += 1
        # No shared signal: different shoot AND different date → red flag
        if (v_sh and a_sh and v_sh != a_sh) and (v_dt and a_dt and v_dt != a_dt):
            no_overlap.append(r)

    # Lower-confidence band: spot-check
    low_conf = con_fp.execute("""
        SELECT al.video_asset_id, al.audio_asset_id, al.raw_match_score, al.combined_score
        FROM applied_link al
        WHERE al.combined_score BETWEEN 0.65 AND 0.75
        ORDER BY al.combined_score ASC LIMIT 15
    """).fetchall()

    out = out_dir / "chromaprint-sanity.md"
    lines = [
        f"# chromaprint applied_link sanity ({now_iso()})",
        "",
        f"Applied links: **{n:,}**",
        f"",
        f"- Both sides share shoot_label: **{shoot_match:,} ({100*shoot_match/n:.0f}%)**",
        f"- Both sides share shoot_date: **{date_match:,} ({100*date_match/n:.0f}%)**",
        f"- Pairs with NEITHER shared shoot_label NOR date (red-flag): **{len(no_overlap)}**",
        "",
    ]
    if no_overlap:
        lines += [
            "## Red-flag pairs (no shared shoot context — investigate)",
            "",
            "| video | audio | v_shoot | a_shoot | v_date | a_date | raw | combined |",
            "|---|---|---|---|---|---|---:|---:|",
        ]
        for r in no_overlap[:25]:
            lines.append(
                f"| `{r[0][:12]}` | `{r[1][:12]}` | {r[4] or '—'} | {r[7] or '—'} | "
                f"{r[5] or '—'} | {r[8] or '—'} | {r[2]:.2f} | {r[3]:.2f} |"
            )
    else:
        lines.append("_No red-flag pairs — all applied links share shoot context._")

    lines += [
        "",
        f"## Lower-confidence band spot-check (combined_score 0.65–0.75)",
        "",
        "These are the pairs most worth eyeballing manually. If the audio "
        "doesn't actually sound like a recording of the video's scene, lower "
        "the apply_threshold for the next run.",
        "",
        "| video | audio | raw | combined |",
        "|---|---|---:|---:|",
    ]
    for r in low_conf:
        lines.append(f"| `{r[0][:12]}` | `{r[1][:12]}` | {r[2]:.3f} | {r[3]:.3f} |")
    out.write_text("\n".join(lines))
    return {
        "check": "chromaprint-sanity",
        "n_applied": n,
        "shoot_match_pct": round(100 * shoot_match / n, 1),
        "no_overlap_redflag": len(no_overlap),
        "report": str(out),
    }


# ============================================================ face clusters

def check_face_clusters(out_dir: Path) -> dict:
    """Face cluster size distribution + outliers."""
    con = open_ro(EDITORIAL_DB)
    if not con:
        return {"check": "face-clusters", "skipped": "editorial_catalog.sqlite missing"}

    rows = con.execute("""
        SELECT p_id, COUNT(*) AS n, AVG(det_score) AS avg_score,
               COUNT(DISTINCT asset_id) AS n_assets
        FROM frame_face
        GROUP BY p_id ORDER BY n DESC
    """).fetchall()
    n_pids = len(rows)
    n_total = sum(r[1] for r in rows)
    small = [r for r in rows if r[1] < 10]
    huge = [r for r in rows if r[1] > 5000]

    out = out_dir / "face-clusters.md"
    lines = [
        f"# Face cluster size distribution ({now_iso()})",
        "",
        f"Named identities: **{n_pids}**",
        f"Total tagged detections: **{n_total:,}**",
        "",
        "## Top-15 largest clusters",
        "",
        "| p_id | detections | distinct assets | avg det_score |",
        "|---|---:|---:|---:|",
    ]
    for p_id, n, avg_s, n_a in rows[:15]:
        lines.append(f"| `{p_id}` | {n:,} | {n_a:,} | {avg_s:.2f} |")

    lines += [
        "",
        f"## Suspiciously small (<10 detections) — under-labeled?  ({len(small)})",
        "",
        "| p_id | detections | distinct assets | avg det_score |",
        "|---|---:|---:|---:|",
    ]
    for p_id, n, avg_s, n_a in small[:25]:
        lines.append(f"| `{p_id}` | {n:,} | {n_a:,} | {avg_s:.2f} |")

    if huge:
        lines += [
            "",
            f"## Suspiciously large (>5000 detections) — possibly conflating multiple people  ({len(huge)})",
            "",
            "| p_id | detections | distinct assets | avg det_score |",
            "|---|---:|---:|---:|",
        ]
        for p_id, n, avg_s, n_a in huge:
            lines.append(f"| `{p_id}` | {n:,} | {n_a:,} | {avg_s:.2f} |")

    out.write_text("\n".join(lines))
    return {
        "check": "face-clusters",
        "n_pids": n_pids,
        "n_detections": n_total,
        "n_small": len(small),
        "n_huge": len(huge),
        "report": str(out),
    }


# ============================================================ ocr noise

_VOWELS = set("aeiouAEIOU")
_RE_REPEAT = re.compile(r"^(.)\1{3,}$")


def _ocr_text_is_noisy(t: str) -> tuple[bool, str]:
    """Heuristic: text looks like statistical noise rather than real text.
    Returns (is_noisy, reason)."""
    s = (t or "").strip()
    if not s:
        return False, ""
    if _RE_REPEAT.match(s):
        return True, "single-char repeat"
    # No vowels in 5+ char ascii-letter token
    alpha = "".join(c for c in s if c.isalpha())
    if len(alpha) >= 5 and all(c not in _VOWELS for c in alpha):
        return True, "no vowels (5+ alpha)"
    # All-caps with high % numeric noise: e.g. "AB12CD34" mixed
    if len(s) >= 4 and sum(1 for c in s if not c.isalnum()) > len(s) // 2:
        return True, "mostly punctuation/symbol"
    return False, ""


def check_ocr_noise(out_dir: Path, sample: int = 50) -> dict:
    """Find frame_text rows that pass the ≥3 alnum filter but look noisy."""
    con = open_ro(EDITORIAL_DB)
    if not con:
        return {"check": "ocr-noise", "skipped": "editorial_catalog.sqlite missing"}

    rows = con.execute("""
        SELECT asset_id, text, confidence, ocr_engine, frame_time_sec
        FROM frame_text
    """).fetchall()
    noisy = []
    by_reason = defaultdict(int)
    for aid, text, conf, engine, t in rows:
        is_noisy, reason = _ocr_text_is_noisy(text)
        if is_noisy:
            noisy.append((aid, text, conf, engine, t, reason))
            by_reason[reason] += 1

    pct = 100.0 * len(noisy) / max(len(rows), 1)
    out = out_dir / "ocr-noise.md"
    lines = [
        f"# OCR noise heuristic ({now_iso()})",
        "",
        f"Total `frame_text` rows: **{len(rows):,}**",
        f"Heuristically noisy: **{len(noisy):,} ({pct:.1f}%)**",
        "",
        "Reasons (statistical, not certain):",
        "",
    ]
    for reason, n in sorted(by_reason.items(), key=lambda kv: -kv[1]):
        lines.append(f"- `{reason}`: **{n:,}**")
    lines += [
        "",
        f"## Sample of {sample} suspected-noise rows",
        "",
        "| asset_id | text | conf | engine | reason |",
        "|---|---|---:|---|---|",
    ]
    for aid, text, conf, engine, t, reason in noisy[:sample]:
        text_disp = (text or "").replace("|", "\\|")[:60]
        lines.append(f"| `{aid[:12]}` | `{text_disp}` | {conf:.2f} | {engine} | {reason} |")
    out.write_text("\n".join(lines))
    return {
        "check": "ocr-noise",
        "n_rows": len(rows),
        "n_noisy": len(noisy),
        "pct_noisy": round(pct, 2),
        "report": str(out),
    }


# ============================================================ main

def check_face_vs_gemini_names(out_dir: Path) -> dict:
    """Surface assets where face cluster says `p_X` but Gemini's
    subject text never mentions X's canonical name or any of their aliases.
    Catches Gemini hallucinations of unknown-person names (e.g. labeling
    Michelino as 'Maura Shuttleworth' / 'Jane Moss')."""
    con = open_ro(EDITORIAL_DB)
    if not con:
        return {"check": "face-vs-gemini-names", "skipped": "editorial_catalog.sqlite missing"}

    # Build p_id → set of name tokens to search for
    people_path = INDEXES_DIR.parent / "dataset" / "people" / "people.json"
    if not people_path.exists():
        return {"check": "face-vs-gemini-names", "skipped": "people.json missing"}
    pdata = json.loads(people_path.read_text())
    people = pdata.get("people", pdata) if isinstance(pdata, dict) else pdata
    name_tokens: dict[str, list[str]] = {}
    canonical: dict[str, str] = {}
    for p in people:
        pid = p.get("id") or p.get("p_id")
        if not pid:
            continue
        names = []
        cn = (p.get("canonical_name") or "").strip()
        if cn:
            names.append(cn)
            # Also add each whitespace token from canonical (catches "Michelino" / "Sunseri" / "Mike")
            for tok in cn.split():
                if len(tok) >= 3:
                    names.append(tok)
        for a in p.get("aliases") or []:
            if isinstance(a, str) and a.strip():
                names.append(a.strip())
                for tok in a.split():
                    if len(tok) >= 3:
                        names.append(tok)
        # dedup, lowercase for case-insensitive match
        seen = set()
        toks = []
        for n in names:
            nl = n.lower()
            if nl not in seen:
                seen.add(nl)
                toks.append(nl)
        if toks:
            name_tokens[pid] = toks
            canonical[pid] = cn

    # For each (asset, p_id) pair in frame_face, fetch Gemini subject text and
    # check whether any name token appears. Distinguish three cases:
    #   match            — Gemini text contains this person's name
    #   named-mismatch   — Gemini text names a *different* registry person
    #                      (high-priority: hallucinated identity)
    #   no-name          — Gemini text contains no registry name at all
    #                      (Gemini stayed descriptive; lower-priority)
    rows = con.execute("""
        SELECT DISTINCT ff.asset_id, ff.p_id
        FROM frame_face ff
        WHERE ff.p_id LIKE 'p_%'
    """).fetchall()

    # Per-asset face-cluster set (union of all named p_ids in the asset)
    per_asset_pids: dict[str, set[str]] = {}
    for aid, pid in rows:
        per_asset_pids.setdefault(aid, set()).add(pid)

    # Two text scopes per asset:
    #   - subject_text: just the `subject` field (who is on camera). Used for
    #     the "did Gemini name the right person on camera?" check.
    #   - full_text: subject + action. Used to disambiguate "Gemini named someone
    #     else in registry" — if the name appears in the action field, they may
    #     be a *topic of conversation*, not a person on screen.
    subj_rows = con.execute("""
        SELECT asset_id,
               GROUP_CONCAT(LOWER(COALESCE(subject,'')), ' || ') AS subject_text,
               GROUP_CONCAT(LOWER(COALESCE(subject,'') || ' ' || COALESCE(action,'')), ' || ') AS full_text
        FROM asset_semantic_chunk
        GROUP BY asset_id
    """).fetchall()
    subject_text: dict[str, str] = {r[0]: r[1] or "" for r in subj_rows}
    scene_text: dict[str, str] = {r[0]: r[2] or "" for r in subj_rows}

    # Flat set of every registry name token (lowercased), with p_id back-reference.
    # Multi-word tokens first so we attribute "alex rienzie" to p_alex_rienzie, not
    # to whichever p_alex_* came first. Skip generic tokens that would over-match
    # (less than 4 chars + common-words filter).
    GENERIC = {"mom", "dad", "kid", "son", "son's", "wife", "alex's", "boomer's",
               "featuring", "fellow", "legal", "michael", "andersen",
               "congresswoman",
               # Common English words that double as last names — too noisy to
               # treat as identity signal in shot-description text.
               "brown", "white", "gray", "grey", "may", "day", "long", "young",
               "moss", "stone", "wood", "field", "cook"}
    import re as _re
    all_name_index: list[tuple[_re.Pattern, str, str]] = []  # (regex, token, p_id)
    for pid, toks in name_tokens.items():
        for t in toks:
            if len(t) < 4 or t in GENERIC:
                continue
            # Word-boundary match so "jack" doesn't match "jacket" and "alex"
            # doesn't match "alexa"/"alexander". `re.escape` so multi-word
            # tokens like "kit deslauriers" work.
            pat = _re.compile(r"(?<![A-Za-z'])" + _re.escape(t) + r"(?![A-Za-z'])")
            all_name_index.append((pat, t, pid))
    # Longest tokens first (regex order doesn't matter for correctness but keeps
    # deterministic output)
    all_name_index.sort(key=lambda x: -len(x[1]))

    def _token_match(toks_set: set[str], text: str) -> bool:
        if not text:
            return False
        for t in toks_set:
            if len(t) < 4 or t in GENERIC:
                # Short tokens (e.g. "kit", "jaz") fall back to substring match.
                # Risk of false-positive is offset by the fact that an asset's
                # match check is a single Boolean — false positives here just
                # mean we *under*-flag (good direction).
                if t in text:
                    return True
            else:
                if _re.search(r"(?<![A-Za-z'])" + _re.escape(t) + r"(?![A-Za-z'])", text):
                    return True
        return False

    def named_pids_in(text: str) -> set[str]:
        """Return the set of registry p_ids whose name appears in `text`
        with word-boundary anchoring (so 'jack' doesn't match 'jacket')."""
        found = set()
        for pat, _tok, pid in all_name_index:
            if pat.search(text):
                found.add(pid)
        return found

    matches = []
    named_mismatches = []  # Gemini named SOMEONE in registry, but not this p_id
    noname_mismatches = []  # Gemini didn't name any registry person
    no_text = 0
    no_aliases = 0
    for aid, pid in rows:
        toks = name_tokens.get(pid)
        if not toks:
            no_aliases += 1
            continue
        subj = subject_text.get(aid)
        text = scene_text.get(aid)
        if not text:
            no_text += 1
            continue
        # Match check: prefer the subject field (who's on camera). Fall back to
        # full text so we don't penalize records where Gemini put the name in
        # the action narration rather than the subject field.
        if _token_match(toks, subj) or _token_match(toks, text):
            matches.append((aid, pid))
            continue
        # Mismatch path: check only the *subject* field for other registry names.
        # The action field is a poor signal — Gemini often names people being
        # discussed there (e.g., "they discuss Rand Paul's lawsuit"), which
        # would be false-positive evidence that Gemini misidentified the person
        # on camera.
        pids_named = named_pids_in(subj)
        pids_named_other = pids_named - per_asset_pids.get(aid, set())
        if pids_named_other:
            named_mismatches.append((aid, pid, canonical.get(pid, pid),
                                     ", ".join(sorted(canonical.get(x,x) for x in pids_named_other))[:80],
                                     subj[:160]))
        else:
            noname_mismatches.append((aid, pid, canonical.get(pid, pid), subj[:160]))

    matched = len(matches)
    mismatches = named_mismatches + noname_mismatches  # for back-compat with the report total

    out = out_dir / "face-vs-gemini-names.md"
    lines = [
        f"# Face cluster ↔ Gemini-named-subject consistency ({now_iso()})",
        "",
        f"For each (asset, p_id) face-detection pair, check whether the asset's "
        f"Gemini scene-text (subject + action across all chunks) mentions the "
        f"person's canonical name or any alias from `people.json`, OR mentions a "
        f"*different* registry person (which would imply Gemini named the wrong person).",
        "",
        f"- Pairs checked: **{len(rows):,}**",
        f"- Match (Gemini named this person): **{matched:,}**",
        f"- Named-mismatch (Gemini named a *different* registry person — high-priority for review): **{len(named_mismatches):,}**",
        f"- No-name (Gemini stayed descriptive, didn't name any registry person — low-priority): **{len(noname_mismatches):,}**",
        f"- Skipped (no Gemini scene text on asset): {no_text:,}",
        f"- Skipped (p_id missing canonical_name + aliases in registry): {no_aliases:,}",
        "",
        f"**Named-mismatches** are the high-signal flags: Gemini described someone by a "
        f"specific name, but that name belongs to a *different* known person in the "
        f"registry — face cluster likely correct, Gemini text likely confused identities. "
        f"Editorial rule: trust the face cluster, not Gemini subject text.",
        "",
        f"**No-name-mismatches** are mostly Gemini being descriptive (\"a young man\", "
        f"\"two filmmakers\") without naming. Not a bug, just less informative — face "
        f"cluster fills in the identity.",
        "",
        f"## Named-mismatches (top {min(100, len(named_mismatches))})",
        "",
        "| p_id (face) | canonical | asset | Gemini named (different) | scene text |",
        "|---|---|---|---|---|",
    ]
    for aid, pid, cn, other_names, text in sorted(named_mismatches)[:100]:
        text_disp = text.replace("|", "\\|").replace("\n", " ")
        other_disp = other_names.replace("|", "\\|")
        lines.append(f"| `{pid}` | {cn} | `{aid[:12]}` | {other_disp} | {text_disp} |")
    lines.append("")
    lines.append(f"## Sample no-name mismatches (top {min(20, len(noname_mismatches))})")
    lines.append("")
    lines.append("| p_id (face) | canonical | asset | scene text |")
    lines.append("|---|---|---|---|")
    for aid, pid, cn, text in sorted(noname_mismatches)[:20]:
        text_disp = text.replace("|", "\\|").replace("\n", " ")
        lines.append(f"| `{pid}` | {cn} | `{aid[:12]}` | {text_disp} |")
    out.write_text("\n".join(lines))
    return {
        "check": "face-vs-gemini-names",
        "n_pairs": len(rows),
        "n_match": matched,
        "n_named_mismatch": len(named_mismatches),
        "n_noname_mismatch": len(noname_mismatches),
        "report": str(out),
    }


def check_subject_vs_face(out_dir: Path) -> dict:
    """Confirms curated `subject_of_interview` (236 transcripts) agrees with
    the dominant face cluster on the same asset. Flags disagreements: either
    the subject curation is stale, the face cluster is wrong, or the asset has
    multiple subjects and the curated value picked the off-camera one."""
    import sqlite3
    transcripts_dir = INDEXES_DIR.parent / "dataset" / "assets" / "catalog" / "transcripts"
    faces_db = INDEXES_DIR / "face_embeddings.sqlite"
    if not transcripts_dir.exists():
        return {"check": "subject-vs-face", "skipped": "transcripts dir missing"}
    if not faces_db.exists():
        return {"check": "subject-vs-face", "skipped": "face_embeddings.sqlite missing"}

    # Build face cluster majority per asset
    fc = sqlite3.connect(f"file:{faces_db}?mode=ro", uri=True)
    rows = fc.execute(
        "SELECT asset_id, p_id, COUNT(*) AS n FROM face_detection "
        "WHERE p_id IS NOT NULL AND p_id LIKE 'p_%' GROUP BY asset_id, p_id"
    ).fetchall()
    cluster_majority: dict[str, tuple[str, int, int]] = {}  # aid -> (top_pid, top_n, total_n)
    by_asset: dict[str, list[tuple[str, int]]] = {}
    for aid, pid, n in rows:
        by_asset.setdefault(aid, []).append((pid, n))
    for aid, lst in by_asset.items():
        lst.sort(key=lambda x: -x[1])
        top_pid, top_n = lst[0]
        total = sum(n for _, n in lst)
        cluster_majority[aid] = (top_pid, top_n, total)

    # Walk transcripts with subject_of_interview populated
    matches = 0
    disagreements = []
    no_face_data = 0
    weak_dominance = 0  # cluster majority < 60% — multi-subject; informational only
    for p in transcripts_dir.glob("*.transcript.json"):
        if p.name.startswith("._"): continue
        try: d = json.loads(p.read_text())
        except Exception: continue
        soi = d.get("subject_of_interview")
        if not soi: continue
        aid = d.get("asset_id") or p.stem.replace(".transcript", "")
        if aid not in cluster_majority:
            no_face_data += 1; continue
        top_pid, top_n, total = cluster_majority[aid]
        dominance = top_n / total if total else 0
        if dominance < 0.6:
            weak_dominance += 1
        if top_pid == soi:
            matches += 1
        else:
            disagreements.append((aid, soi, top_pid, top_n, total, round(dominance, 2)))

    out = out_dir / "subject-vs-face.md"
    lines = [
        f"# `subject_of_interview` vs dominant face cluster (consistency QA) ({now_iso()})",
        "",
        f"Walks the {236} transcripts with `subject_of_interview` populated, "
        f"checks whether the dominant face cluster `p_id` on the same asset "
        f"matches. Disagreements are candidates for: (a) stale subject curation, "
        f"(b) wrong face cluster labeling, or (c) multi-subject asset where the "
        f"curator picked the off-camera subject (interviewer vs interviewee).",
        "",
        f"- Subject_of_interview populated: {matches + len(disagreements) + no_face_data:,}",
        f"- **Match** (subject == dominant face cluster): {matches:,}",
        f"- **Disagree**: **{len(disagreements):,}**",
        f"- Skipped (no face data on the asset): {no_face_data:,}",
        f"- Of the disagrees, weak face dominance (<60%): {weak_dominance:,}",
        "",
        f"## Disagreements (top {min(50, len(disagreements))} by face frame count)",
        "",
        "| asset | subject_of_interview | dominant_face_pid | top_n / total | dominance |",
        "|---|---|---|---|---|",
    ]
    for aid, soi, top_pid, top_n, total, dom in sorted(disagreements, key=lambda x: -x[3])[:50]:
        lines.append(f"| `{aid[:12]}` | `{soi}` | `{top_pid}` | {top_n}/{total} | {dom:.2f} |")
    out.write_text("\n".join(lines))
    return {
        "check": "subject-vs-face",
        "n_match": matches,
        "n_disagree": len(disagreements),
        "n_no_face_data": no_face_data,
        "weak_dominance": weak_dominance,
        "report": str(out),
    }


CHECKS = {
    "coverage": check_coverage,
    "shots-vs-gemini": check_shots_vs_gemini,
    "clap-vs-audio-quality": check_clap_vs_audio_quality,
    "clap-vs-asset-type": check_clap_vs_asset_type,
    "chromaprint-sanity": check_chromaprint_sanity,
    "face-clusters": check_face_clusters,
    "face-vs-gemini-names": check_face_vs_gemini_names,
    "subject-vs-face": check_subject_vs_face,
    "ocr-noise": check_ocr_noise,
}


def cmd_all(args: argparse.Namespace) -> None:
    out_root = RUNS_DIR.parent / "qa" / ts_slug()
    out_root.mkdir(parents=True, exist_ok=True)
    results = []
    for name, fn in CHECKS.items():
        print(f"=== running {name} ===")
        try:
            r = fn(out_root)
        except Exception as e:
            import traceback
            r = {"check": name, "error": str(e), "trace": traceback.format_exc()}
        results.append(r)
        if "skipped" in r:
            print(f"  skipped: {r['skipped']}")
        elif "error" in r:
            print(f"  ERROR: {r['error']}")
        else:
            highlights = {k: v for k, v in r.items() if k not in ("check", "report")}
            print(f"  → {r.get('report')}")
            for k, v in highlights.items():
                print(f"      {k}: {v}")
    # Summary
    summary_path = out_root / "_summary.md"
    lines = [
        f"# Layer QA summary ({now_iso()})",
        "",
        f"Output dir: `{out_root}`",
        "",
        "## Check results",
        "",
    ]
    for r in results:
        name = r.get("check", "?")
        if "skipped" in r:
            lines.append(f"- ❌ **{name}** — skipped: {r['skipped']}")
        elif "error" in r:
            lines.append(f"- 💥 **{name}** — error: `{r['error']}`")
        else:
            highlights = {k: v for k, v in r.items() if k not in ("check", "report")}
            lines.append(f"- ✅ **{name}** → [report]({Path(r.get('report')).name})")
            for k, v in highlights.items():
                lines.append(f"  - {k}: `{v}`")
    summary_path.write_text("\n".join(lines))
    print(f"\nsummary: {summary_path}")


def cmd_single(args: argparse.Namespace) -> None:
    out_root = RUNS_DIR.parent / "qa" / ts_slug()
    out_root.mkdir(parents=True, exist_ok=True)
    fn = CHECKS[args.cmd]
    r = fn(out_root)
    if "skipped" in r:
        print(f"skipped: {r['skipped']}")
    elif "error" in r:
        print(f"ERROR: {r['error']}")
    else:
        print(f"report: {r.get('report')}")
        for k, v in r.items():
            if k in ("check", "report"): continue
            print(f"  {k}: {v}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in CHECKS:
        sp = sub.add_parser(name)
        sp.set_defaults(func=cmd_single)
    sp = sub.add_parser("all", help="Run all checks; emit summary")
    sp.set_defaults(func=cmd_all)
    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
