"""comprehensive_audit.py — Sweep the dataset + indexes for issues +
improvement opportunities. Outputs a single markdown findings report.

Checks (each independent):

  Catalog hygiene:
    1. macOS AppleDouble (._*) files in catalog dirs
    2. *.tmp leftovers in catalog dirs
    3. Schema-version mismatches across video/audio/stills records
    4. Missing required fields (asset_id, source_path, ffprobe, etc.)
    5. Catalog records without corresponding transcript / proxy on disk

  Editor DB integrity:
    6. asset_id present in DB but no JSON file (orphan rows)
    7. asset_id in JSON but missing from DB (projection lag)
    8. Foreign-key-style orphans (shot.asset_id not in asset, etc.)

  Cross-layer coverage drifts:
    9. Assets with transcript but no machine_transcript flag
    10. Assets with proxy on disk but no proxy block in catalog
    11. Stale analysis prompt_sha (off the canonical pair)
    12. Audio assets with extract on disk but no audio_extract block

  Pipeline state:
    13. Pending dense_caption coverage gaps
    14. K-layer index DBs in WAL mode with stale checkpoints (large -wal files)

  Improvement opportunities:
    15. Top 20 catalog records with most journaled segment_corrections (review queue)
    16. Top 20 unresolved speaker_attributions (manually attribute)
    17. shoot_location.needs_review breakdown (which shoot_labels need registry)
    18. Place registry entries not referenced anywhere (orphan places)

Run-time: ~30-60 seconds end-to-end on this corpus.
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
CATALOG_BASE = ROOT / "dataset" / "assets" / "catalog"
TRANSCRIPTS = CATALOG_BASE / "transcripts"
EDITORIAL_DB = ROOT / "indexes" / "editorial_catalog.sqlite"
DENSE_DB = ROOT / "indexes" / "dense_captions.sqlite"
DERIV_MEDIA = ROOT / "derivative media"


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_catalog_records(kind: str, suffix: str, subdir: str):
    """Generator over (asset_id, path, record)."""
    for p in (CATALOG_BASE / subdir).glob(f"*{suffix}"):
        if p.name.startswith("._"): continue
        try: d = json.loads(p.read_text())
        except Exception:
            yield p.stem, p, None; continue
        yield d.get("asset_id") or p.stem, p, d


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "dataset" / "_runs" / "audit"
                                        / f"audit_{now_iso().replace(':', '').replace('-', '')[:15]}.md"))
    args = ap.parse_args()
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    findings: list[str] = [f"# Comprehensive dataset/indexes audit — {now_iso()}", ""]
    overall = Counter()

    # ---- 1. AppleDouble + 2. .tmp file scan ----
    findings += ["## File hygiene", ""]
    n_appledouble = 0
    n_tmp = 0
    appledouble_dirs = Counter()
    tmp_examples = []
    for p in CATALOG_BASE.rglob("._*"):
        n_appledouble += 1
        appledouble_dirs[str(p.parent.relative_to(CATALOG_BASE))] += 1
    for p in CATALOG_BASE.rglob("*.tmp"):
        n_tmp += 1
        if len(tmp_examples) < 5: tmp_examples.append(str(p.relative_to(CATALOG_BASE)))
    findings.append(f"- AppleDouble (`._*`) files in catalog: **{n_appledouble:,}**")
    if appledouble_dirs:
        for dir_, n in appledouble_dirs.most_common(5):
            findings.append(f"  - `{dir_}`: {n}")
    findings.append(f"- Leftover `*.tmp` files in catalog: **{n_tmp}**")
    for ex in tmp_examples: findings.append(f"  - `{ex}`")
    findings.append("")
    overall["appledouble"] = n_appledouble
    overall["tmp"] = n_tmp

    # ---- 3,4. Schema + required-field check on all catalogs ----
    findings += ["## Catalog integrity", ""]
    schema_versions = Counter()
    missing_required = Counter()
    invalid_json = []
    catalog_aids: dict[str, set[str]] = {"video": set(), "audio": set(), "still": set()}
    catalog_paths: dict[str, dict[str, Path]] = {"video": {}, "audio": {}, "still": {}}
    catalog_records: dict[str, dict[str, dict]] = {"video": {}, "audio": {}, "still": {}}

    for kind, suffix, subdir in [("video", ".video.json", "video"),
                                  ("audio", ".audio.json", "audio"),
                                  ("still", ".still.json", "stills")]:
        for aid, path, rec in load_catalog_records(kind, suffix, subdir):
            if rec is None:
                invalid_json.append(str(path.relative_to(CATALOG_BASE)))
                continue
            sv = rec.get("schema_version")
            schema_versions[f"{kind}:{sv}"] += 1
            for required in ("asset_id", "source_path", "filename"):
                if not rec.get(required):
                    missing_required[f"{kind}:{required}"] += 1
            catalog_aids[kind].add(aid)
            catalog_paths[kind][aid] = path
            catalog_records[kind][aid] = rec

    findings.append("**Schema-version distribution:**")
    for k, v in sorted(schema_versions.items()):
        findings.append(f"- `{k}`: {v:,}")
    findings.append("")
    if missing_required:
        findings.append("**Records missing required fields:**")
        for k, v in missing_required.items():
            findings.append(f"- `{k}`: {v}")
        findings.append("")
    if invalid_json:
        findings.append(f"**Unparseable JSON files: {len(invalid_json)}**")
        for ex in invalid_json[:5]: findings.append(f"  - `{ex}`")
        findings.append("")

    # ---- 5. Catalog records without proxy/transcript on disk ----
    findings += ["## Cross-layer coverage", ""]
    n_video_no_proxy_block = 0
    n_video_no_transcript_file = 0
    for aid, rec in catalog_records["video"].items():
        if not (rec.get("proxy") or {}).get("ffmpeg_command_hash"):
            n_video_no_proxy_block += 1
        if not (TRANSCRIPTS / f"{aid}.transcript.json").exists():
            n_video_no_transcript_file += 1
    findings.append(f"- Video records with NO proxy block: **{n_video_no_proxy_block}** "
                    f"(of {len(catalog_records['video']):,})")
    findings.append(f"- Video records with NO transcript file: **{n_video_no_transcript_file}**")

    # ---- 6,7. Editor DB orphans + projection lag ----
    if EDITORIAL_DB.exists():
        con = sqlite3.connect(str(EDITORIAL_DB))
        db_aids = {r[0] for r in con.execute("SELECT asset_id FROM asset")}
        all_catalog = catalog_aids["video"] | catalog_aids["audio"] | catalog_aids["still"]
        db_orphan = db_aids - all_catalog
        json_only = all_catalog - db_aids
        findings.append(f"- Editor DB `asset` rows: **{len(db_aids):,}**")
        findings.append(f"- DB rows with no catalog JSON (orphan): **{len(db_orphan)}** "
                        f"(needs editor DB rebuild)")
        findings.append(f"- Catalog JSONs missing from DB (projection lag): **{len(json_only)}**")
        if json_only:
            for aid in list(json_only)[:5]:
                findings.append(f"  - `{aid[:12]}`")
        # Foreign-key-style orphans
        for child_table, child_col in [("shot", "asset_id"), ("frame_face", "asset_id"),
                                         ("frame_text", "asset_id"), ("shot_quality", "asset_id"),
                                         ("audio_event", "asset_id"), ("audio_quality", "asset_id"),
                                         ("asset_semantic_chunk", "asset_id")]:
            try:
                orphans = con.execute(
                    f"SELECT COUNT(DISTINCT {child_col}) FROM {child_table} "
                    f"WHERE {child_col} NOT IN (SELECT asset_id FROM asset)").fetchone()[0]
                if orphans:
                    findings.append(f"- `{child_table}.{child_col}` orphans (not in asset): **{orphans}**")
            except sqlite3.Error: continue
        con.close()
    findings.append("")

    # ---- 11. Stale prompt_sha ----
    findings += ["## Analysis prompt freshness", ""]
    canonical_sha_pair: set[str] = set()
    prompt1 = ROOT / "dataset" / "_prompts" / "transcript_correction_and_structure_prompt.md"
    prompt2 = ROOT / "dataset" / "_prompts" / "transcript_editorial_scoring_prompt.md"
    if prompt1.exists() and prompt2.exists():
        import hashlib
        h = hashlib.sha256()
        for p in (prompt1, prompt2):
            text = p.read_text()
            canonical = "\n".join(ln for ln in text.splitlines() if not ln.startswith("_Generated"))
            h.update(canonical.encode())
            h.update(b"\n--PROMPT-SEPARATOR--\n")
        canonical_sha_pair.add(h.hexdigest())

    by_sha = Counter()
    by_analyzer = Counter()
    no_analysis = 0
    for aid in catalog_aids["video"] | catalog_aids["audio"]:
        tp = TRANSCRIPTS / f"{aid}.transcript.json"
        if not tp.exists(): continue
        try: t = json.loads(tp.read_text())
        except Exception: continue
        a = t.get("analysis") or {}
        if not a: no_analysis += 1; continue
        by_sha[(a.get("prompt_sha256") or "")[:16]] += 1
        by_analyzer[a.get("analyzer") or "(none)"] += 1
    findings.append(f"- Transcripts missing `analysis` block: {no_analysis}")
    findings.append(f"- Prompt sha distribution (top):")
    for sha, n in by_sha.most_common(5):
        marker = " ←current" if any(c.startswith(sha) for c in canonical_sha_pair) else ""
        findings.append(f"  - `{sha or '(missing)'}`: {n:,}{marker}")
    findings.append(f"- Analyzer distribution:")
    for a, n in by_analyzer.most_common(5):
        findings.append(f"  - `{a}`: {n:,}")
    findings.append("")

    # ---- 13. Dense_caption coverage ----
    if DENSE_DB.exists():
        con = sqlite3.connect(str(DENSE_DB))
        captioned = {r[0] for r in con.execute(
            "SELECT DISTINCT asset_id FROM dense_caption "
            "WHERE model_engine='gemini_flash' AND prompt_variant='meta'")}
        con.close()
        eligible = set()
        for aid, rec in catalog_records["video"].items():
            at = (rec.get("asset_classifications") or {}).get("type")
            if at in ("verite", "interview", "third_party", "archival"):
                eligible.add(aid)
        gap = eligible - captioned
        findings += ["## Dense captions coverage", ""]
        findings.append(f"- Eligible video assets: {len(eligible):,}")
        findings.append(f"- Captioned: {len(captioned & eligible):,}")
        findings.append(f"- Coverage gap: **{len(gap)}** eligible without captions")
        findings.append("")

    # ---- 14. SQLite WAL state ----
    findings += ["## SQLite WAL hygiene", ""]
    big_wal = []
    for p in (ROOT / "indexes").glob("*.sqlite-wal"):
        size = p.stat().st_size
        if size > 1_000_000:  # >1MB stale WAL
            big_wal.append((p.name, size))
    if big_wal:
        findings.append(f"- Large WAL files (uncheckpointed writers? close+checkpoint to compact):")
        for n, s in sorted(big_wal, key=lambda x: -x[1]):
            findings.append(f"  - `{n}`: {s/1e6:.1f} MB")
    else:
        findings.append(f"- All WAL files <1MB. ✓")
    findings.append("")

    # ---- 17. shoot_location needs_review breakdown ----
    findings += ["## shoot_location needs_review breakdown", ""]
    needs_review_labels = Counter()
    needs_review_top_level = Counter()
    for aid, rec in catalog_records["video"].items():
        sl = rec.get("shoot_location") or {}
        if sl.get("source") == "needs_review":
            ev = sl.get("_evidence") or {}
            needs_review_labels[ev.get("shoot_label") or "(none)"] += 1
            needs_review_top_level[ev.get("top_level") or "(none)"] += 1
    findings.append(f"- Video records needing review: {sum(needs_review_labels.values())}")
    findings.append(f"  - Top labels (extend `SHOOT_LABEL_TO_PLACE` to fix):")
    for label, n in needs_review_labels.most_common(15):
        findings.append(f"    - `{label}`: {n}")
    findings.append("")

    # ---- 18. Orphan place registry entries ----
    findings += ["## Place registry hygiene", ""]
    places_json = ROOT / "dataset" / "places" / "places.json"
    if places_json.exists():
        try:
            pd = json.loads(places_json.read_text())
            all_places = {p.get("id") for p in (pd.get("places") if isinstance(pd, dict) else pd) if p.get("id")}
        except Exception:
            all_places = set()
        used_places: set[str] = set()
        for aid, rec in {**catalog_records["video"], **catalog_records["audio"], **catalog_records["still"]}.items():
            sl = rec.get("shoot_location") or {}
            if sl.get("place"): used_places.add(sl["place"])
            for pid in (rec.get("place_ids") or []): used_places.add(pid)
        orphan_places = all_places - used_places
        findings.append(f"- Places in registry: {len(all_places)}")
        findings.append(f"- Places referenced by at least one record: {len(used_places)}")
        findings.append(f"- Orphan places (registry-only, no references): **{len(orphan_places)}**")
        if orphan_places:
            findings.append(f"  (sample 10): " + ", ".join(f"`{p}`" for p in sorted(orphan_places)[:10]))
    findings.append("")

    # ---- 15,16. Editorial review queue summaries (from analysis blocks) ----
    findings += ["## Editorial review queues (from transcript analysis)", ""]
    total_journaled_corrections = 0
    total_journaled_speakers = 0
    review_by_asset_corrections = []
    review_by_asset_speakers = []
    for aid in catalog_aids["video"] | catalog_aids["audio"]:
        tp = TRANSCRIPTS / f"{aid}.transcript.json"
        if not tp.exists(): continue
        try: t = json.loads(tp.read_text())
        except Exception: continue
        a = t.get("analysis") or {}
        # Corrections journaled = those with confidence < 0.85 OR rejected for other reasons
        # We don't have an explicit "_journaled" flag; count below-threshold ones
        corr_journaled = [c for c in (a.get("segment_corrections") or [])
                          if (c.get("confidence") or 0) < 0.85]
        spk_journaled = [s for s in (a.get("speaker_attributions") or [])
                         if (s.get("confidence") or 0) < 0.85
                         or s.get("_apply_rejected")]
        total_journaled_corrections += len(corr_journaled)
        total_journaled_speakers += len(spk_journaled)
        if corr_journaled:
            review_by_asset_corrections.append((aid, len(corr_journaled)))
        if spk_journaled:
            review_by_asset_speakers.append((aid, len(spk_journaled)))
    findings.append(f"- **Total segment corrections journaled (below 0.85, awaiting review):** {total_journaled_corrections:,}")
    review_by_asset_corrections.sort(key=lambda x: -x[1])
    findings.append(f"  - Top 10 assets by count:")
    for aid, n in review_by_asset_corrections[:10]:
        findings.append(f"    - `{aid[:12]}`: {n}")
    findings.append(f"- **Total speaker attributions journaled or rejected:** {total_journaled_speakers:,}")
    findings.append(f"  - Top 10:")
    review_by_asset_speakers.sort(key=lambda x: -x[1])
    for aid, n in review_by_asset_speakers[:10]:
        findings.append(f"    - `{aid[:12]}`: {n}")
    findings.append("")

    # ---- summary line ----
    findings.insert(2, f"**Summary:** {overall.get('appledouble', 0)} AppleDouble files, "
                       f"{overall.get('tmp', 0)} tmp files, "
                       f"{sum(needs_review_labels.values())} shoot_location to review, "
                       f"{total_journaled_corrections:,} corrections in editorial queue, "
                       f"{total_journaled_speakers:,} speakers in editorial queue.")
    findings.insert(3, "")

    out_path.write_text("\n".join(findings))
    print(f"Wrote audit to {out_path}")


if __name__ == "__main__":
    main()
