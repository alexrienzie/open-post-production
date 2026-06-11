#!/usr/bin/env python3
"""Refresh the full Act-scoped sidecar pipeline end-to-end.

The canonical iteration loop:

  1. Premiere edit → export xmeml → drop in editor/xml exports/
  2. Run this script — it does:
       a. Rebuild resolver index (XML → content_key map)
       b. Re-extract Act sidecar (preserves editorial fields via content-key match)
       c. Populate chunk_* fields from catalog `asset_semantic_summary`
          (covers new clipitems whose semantic extract exists)
       d. Denormalize (transcript segments inlined per clip)
       e. Cut-boundary self-eval (mid-word warnings on each annotation)
       f. Optional: apply `chunk_suggested_span` to source in/out (`--apply-suggested-spans`)
       g. Render the HTML review view
  3. Review actII.html, iterate beat/scene structure by editing the sidecar
     directly, re-run this script to re-assign annotation beat/scene fields

Catalog-side data (asset metadata, classifications, transcripts, speakers)
flows through at HTML render + context build time — no separate populate
needed. To pick up new dataset edits (speaker p_id changes, bucket/type
changes), rebuild editorial_catalog.sqlite first via
dataset/_scripts/build_editor_db.py on the machine that hosts the dataset.

Step toggles let you skip any phase. Defaults run everything.

Usage:
  py refresh_act_sidecar.py
  py refresh_act_sidecar.py --xml "...new_export.xml"
  py refresh_act_sidecar.py --skip-extract --skip-gemini      # just re-render

Paths are derived from a project layout rooted at editor/. Override with
--editor-root if running from elsewhere.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Optional


def _safe_write_json(target: Path, data, retries: int = 3) -> None:
    """Write JSON to a mount-prone path with verification + retry.

    The E:\\open-post-stack bindfs/virtiofs mount silently truncates or null-pads large
    writes. Workaround: serialize to a same-disk temp file, fsync, atomic rename,
    then re-read + hash to confirm. Retry on mismatch."""
    payload = json.dumps(data, indent=2, ensure_ascii=False).encode("utf-8")
    expected_sha = hashlib.sha256(payload).hexdigest()
    target.parent.mkdir(parents=True, exist_ok=True)
    last_err = None
    for attempt in range(1, retries + 1):
        fd, tmp_str = tempfile.mkstemp(prefix=target.stem + ".", suffix=".tmp", dir=str(target.parent))
        tmp = Path(tmp_str)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, target)
        except Exception as e:
            last_err = e
            if tmp.exists():
                tmp.unlink()
            continue
        # Verify
        try:
            actual = target.read_bytes()
        except Exception as e:
            last_err = e
            continue
        actual_sha = hashlib.sha256(actual).hexdigest()
        if actual_sha == expected_sha:
            return
        last_err = RuntimeError(
            f"write verify mismatch on {target} (attempt {attempt}): "
            f"expected sha={expected_sha[:16]}.. ({len(payload)} bytes), "
            f"got sha={actual_sha[:16]}.. ({len(actual)} bytes)"
        )
        time.sleep(0.5 * attempt)  # backoff before retry
    raise RuntimeError(f"_safe_write_json failed after {retries} attempts: {last_err}")


# ----------------------------- Path layout -----------------------------


def derive_paths(editor_root: Path, act_id: str = "actII", xml_override: Optional[str] = None):
    """Return the canonical paths for an Act-scoped refresh. The defaults assume
    standard editor/ layout; overrides may be passed via CLI."""
    p = {
        "sidecar":       editor_root / "story" / "sidecars" / f"{act_id}.sidecar.json",
        "context":       editor_root / "story" / "sidecars" / f"{act_id}.context.json",
        "manifest":      editor_root / "story" / "sidecars" / f"{act_id}_beats_manifest.json",
        "resolver":      editor_root / "story" / "sidecars" / "_resolver" / f"{act_id}_clip_index.json",
        "html":          editor_root / "story" / "html views" / f"{act_id}.html",
        "scripts_dir":   editor_root / "story" / "_sidecar scripts",
        "asset_map":     editor_root.parent / "derivative media" / "_index" / "asset_map.json",
        "catalog_db":    editor_root.parent / "indexes" / "editorial_catalog.sqlite",
        "embeddings_db": editor_root.parent / "indexes" / "clip_and_still_embeddings.sqlite",
        "dataset_root":  editor_root.parent / "dataset",
        "transcripts":   editor_root.parent / "dataset" / "assets" / "transcripts",
        "dataset_catalog": editor_root.parent / "dataset" / "assets",
    }
    # XML: explicit override, or read xml_source from current sidecar
    if xml_override:
        p["xml"] = Path(xml_override)
    else:
        try:
            sc = json.loads(p["sidecar"].read_text(encoding="utf-8"))
            xml_src = sc.get("xml_source")
            if xml_src:
                # Normalize possibly-relative path
                p["xml"] = Path(xml_src) if Path(xml_src).is_absolute() else (p["sidecar"].parent / xml_src).resolve()
            else:
                p["xml"] = None
        except Exception:
            p["xml"] = None
    return p


# ----------------------------- Phases -----------------------------


def phase_archive_previous(P: dict, *, force: bool = False) -> int:
    """Snapshot current sidecar + resolver into _archive/ BEFORE refresh overwrites them.

    Triggers when:
      - `force=True` (set by --force-archive), OR
      - the current sidecar's xml_source basename differs from the new P["xml"], OR
      - the current sidecar's xml_sha256 differs from the sha256 of the new P["xml"]

    Skipped silently when xml_source matches (no meaningful refresh).

    Snapshot layout:
      editor/story/sidecars/_archive/{YYYYMMDD}T{HHMM}_{prev_xml_stem}/
          actII.sidecar.json           previous sidecar (annotations, beats, scenes)
          actII_clip_index.json        previous resolver (content_key -> clipitem id)
          _manifest.json               who/when/why + xml sha256 for both old and new
    """
    import datetime as _dt
    import hashlib as _hashlib
    import shutil as _shutil

    sidecar_path = P["sidecar"]
    if not Path(sidecar_path).exists():
        print("  [archive] no current sidecar to archive; skipping")
        return 0

    try:
        cur_sc = json.loads(Path(sidecar_path).read_text(encoding="utf-8"))
    except Exception as e:
        print(f"  [archive] WARN: failed to read current sidecar: {e}; skipping archive")
        return 0

    prev_xml_source = (cur_sc.get("xml_source") or "").strip()
    prev_xml_sha = cur_sc.get("xml_sha256")

    new_xml_path = Path(P["xml"]) if P.get("xml") else None
    new_xml_sha = None
    if new_xml_path and new_xml_path.exists():
        new_xml_sha = _hashlib.sha256(new_xml_path.read_bytes()).hexdigest()

    same_name = bool(prev_xml_source) and new_xml_path and (Path(prev_xml_source).name == new_xml_path.name)
    same_sha = bool(prev_xml_sha) and bool(new_xml_sha) and (prev_xml_sha == new_xml_sha)

    needs_archive = force or (not same_name) or (not same_sha and not force)
    # If neither name nor sha info available, skip rather than spam archives
    if not prev_xml_source and not force:
        print("  [archive] current sidecar has no xml_source; nothing to snapshot")
        return 0

    if not force and same_name and same_sha:
        print("  [archive] sidecar xml_source + sha match new --xml; skip (no significant change)")
        return 0

    prev_stem = Path(prev_xml_source).stem if prev_xml_source else "no_xml_source"
    # Sanitize for filesystem
    safe_stem = "".join(c if c.isalnum() or c in "._-+" else "_" for c in prev_stem)[:80]
    ts = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M")
    archive_dir = Path(sidecar_path).parent / "_archive" / f"{ts}_{safe_stem}"
    archive_dir.mkdir(parents=True, exist_ok=True)

    # Sidecar copy (preserve original filename so multi-act archives don't collide)
    _shutil.copy2(sidecar_path, archive_dir / Path(sidecar_path).name)
    sc_bytes = Path(sidecar_path).stat().st_size

    # Resolver copy (may not exist on first run)
    resolver_path = P["resolver"]
    resolver_bytes = None
    if Path(resolver_path).exists():
        _shutil.copy2(resolver_path, archive_dir / Path(resolver_path).name)
        resolver_bytes = Path(resolver_path).stat().st_size

    manifest = {
        "archived_utc": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds") + "Z",
        "previous_xml_source": prev_xml_source,
        "previous_xml_sha256": prev_xml_sha,
        "new_xml_source": str(new_xml_path) if new_xml_path else None,
        "new_xml_sha256": new_xml_sha,
        "reason": ("forced" if force else
                   "xml_source name changed" if not same_name else
                   "xml_source same but sha differs"),
        "sidecar_bytes": sc_bytes,
        "resolver_bytes": resolver_bytes,
        "n_annotations": len(cur_sc.get("annotations") or []),
        "n_beats": len(cur_sc.get("beats") or []),
    }
    (archive_dir / "_manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    print(f"  [archive] -> {archive_dir.relative_to(Path(sidecar_path).parent.parent.parent)}/")
    print(f"             reason: {manifest['reason']}")
    print(f"             prev xml: {Path(prev_xml_source).name if prev_xml_source else '(none)'}")
    print(f"             new  xml: {new_xml_path.name if new_xml_path else '(none)'}")
    print(f"             snapshot: sidecar {sc_bytes:,}B"
          + (f", resolver {resolver_bytes:,}B" if resolver_bytes else ", resolver missing"))
    return 0


def phase_rebuild_resolver(P: dict) -> int:
    print("[1/7] Rebuild resolver index")
    if not P["xml"] or not Path(P["xml"]).exists():
        print(f"  ERROR: XML not found: {P['xml']}", file=sys.stderr)
        return 2
    cmd = [
        sys.executable, str(P["scripts_dir"] / "build_resolver.py"),
        "--xml", str(P["xml"]),
        "--asset-map", str(P["asset_map"]),
        "--out", str(P["resolver"]),
    ]
    return _run(cmd)


def phase_extract_sidecar(P: dict) -> int:
    print("[2/7] Re-extract Act sidecar (preserving editorial fields)")
    if not P["xml"] or not Path(P["xml"]).exists():
        print(f"  ERROR: XML not found: {P['xml']}", file=sys.stderr)
        return 2
    cmd = [
        sys.executable, str(P["scripts_dir"] / "make_act_sidecar.py"),
        "--xml", str(P["xml"]),
        "--beats-manifest", str(P["manifest"]),
        "--asset-map", str(P["asset_map"]),
        "--prior-sidecar", str(P["sidecar"]),
        "--out", str(P["sidecar"]),
    ]
    return _run(cmd)


def phase_populate_gemini(P: dict) -> int:
    """Populate chunk_* on annotations from catalog `asset_semantic_summary`."""
    print("[3/7] Populate Gemini fields from catalog")
    if not P["sidecar"].exists():
        print(f"  ERROR: sidecar not found: {P['sidecar']}", file=sys.stderr)
        return 2

    scripts_dir = P["dataset_root"] / "_scripts"
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))
    from semantic_catalog import (  # noqa: E402
        apply_gemini_annotation_fields,
        load_overlap_chunks_all_video_still,
        read_catalog_record,
    )

    sc = json.loads(P["sidecar"].read_text(encoding="utf-8"))
    fps = sc.get("frame_rate", 24000 / 1001)

    chunks_by_asset = load_overlap_chunks_all_video_still(P["dataset_root"])

    def best_chunk(aid: str, src_in_s: float, src_out_s: float) -> dict | None:
        best = None
        best_overlap = 0.0
        for c in chunks_by_asset.get(aid) or []:
            start = c["chunk_start_sec"]
            end = c["chunk_end_sec"] if c["chunk_end_sec"] else 1e9
            overlap = max(0.0, min(src_out_s, end) - max(src_in_s, start))
            if overlap > best_overlap:
                best_overlap = overlap
                best = c
        if best_overlap <= 0.0 and chunks_by_asset.get(aid):
            mid = (src_in_s + src_out_s) / 2.0
            best = min(
                chunks_by_asset[aid],
                key=lambda ch: min(
                    abs(ch["chunk_start_sec"] - mid),
                    abs((ch["chunk_end_sec"] or mid) - mid),
                ),
            )
        return best

    n_pop = 0
    n_no_data = 0
    for ann in sc.get("annotations", []):
        k = ann.get("key", {})
        aid = k.get("asset_id")
        if not aid:
            continue
        chs = chunks_by_asset.get(aid)
        if not chs:
            n_no_data += 1
            continue
        src_in_s = (k.get("source_in_frames") or 0) / fps
        src_out_s = (k.get("source_out_frames") or 0) / fps
        if len(chs) == 1 and ((chs[0].get("chunk_end_sec") or 0) >= 1e8 or chs[0]["chunk_start_sec"] == 0.0):
            c = chs[0]
        else:
            c = best_chunk(aid, src_in_s, src_out_s)
        if not c:
            n_no_data += 1
            continue
        apply_gemini_annotation_fields(
            ann, c, window_in=src_in_s, window_out=src_out_s
        )
        rec = read_catalog_record(P["dataset_root"], aid)
        if rec and rec.get("place_ids"):
            ann["place_ids"] = rec["place_ids"]
        n_pop += 1

    P["sidecar"].write_text(json.dumps(sc, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"  populated: {n_pop}   no gemini data: {n_no_data}   total annotations: {len(sc.get('annotations', []))}")
    return 0


def _editor_import():
    editor_root = Path(__file__).resolve().parent.parent.parent
    if str(editor_root) not in sys.path:
        sys.path.insert(0, str(editor_root))
    from sidecar_cut_eval import (
        apply_suggested_spans, eval_cut_boundaries,
        eval_visual_cut_deltas, visual_cut_distribution,
    )
    return (apply_suggested_spans, eval_cut_boundaries,
            eval_visual_cut_deltas, visual_cut_distribution)


def phase_cut_eval(P: dict) -> int:
    print("[5/8] Cut-boundary self-eval (mid-word)")
    if not P["sidecar"].exists():
        print(f"  ERROR: sidecar not found: {P['sidecar']}", file=sys.stderr)
        return 2
    _, eval_cut_boundaries, _, _ = _editor_import()
    sc = json.loads(P["sidecar"].read_text(encoding="utf-8"))
    issues = eval_cut_boundaries(sc)
    by_clip = {i["clip_id"]: i for i in issues if i.get("clip_id")}
    n_tagged = 0
    for ann in sc.get("annotations") or []:
        cid = ann.get("clip_id")
        if cid and cid in by_clip:
            ann["cut_eval"] = by_clip[cid]
            n_tagged += 1
        elif ann.get("cut_eval"):
            del ann["cut_eval"]
    _safe_write_json(P["sidecar"], sc)
    print(f"  mid-word cut issues: {len(issues)} annotations tagged: {n_tagged}")
    return 0


def phase_visual_cut_eval(P: dict) -> int:
    """SigLIP visual-cut delta per consecutive timeline annotation pair."""
    print("[6/8] Visual-cut delta (SigLIP cosine across each cut)")
    if not P["sidecar"].exists():
        print(f"  ERROR: sidecar not found: {P['sidecar']}", file=sys.stderr)
        return 2
    _, _, eval_visual_cut_deltas, visual_cut_distribution = _editor_import()
    sc = json.loads(P["sidecar"].read_text(encoding="utf-8"))
    n_pairs = eval_visual_cut_deltas(sc)
    _safe_write_json(P["sidecar"], sc)
    print(f"  pairs scored: {n_pairs}")
    if n_pairs > 0:
        s = visual_cut_distribution(sc)
        print(f"  by interpretation:  {s.get('by_interpretation')}")
        if "cosine_percentiles" in s:
            print(f"  cosine percentiles: {s['cosine_percentiles']}")
            print(f"  suggested flag:     cosine_similarity ≤ {s['suggested_flag_threshold_cosine']}  "
                  f"(bottom 10% — surface for review)")
    return 0


def phase_apply_suggested_spans(P: dict, *, enabled: bool) -> int:
    if not enabled:
        print("[7/8] Apply suggested spans (skipped)")
        return 0
    print("[7/8] Apply chunk_suggested_span to source in/out")
    apply_suggested_spans_fn, _, _, _ = _editor_import()
    sc = json.loads(P["sidecar"].read_text(encoding="utf-8"))
    n, log = apply_suggested_spans_fn(sc)
    _safe_write_json(P["sidecar"], sc)
    print(f"  updated: {n} annotations")
    for entry in log[:8]:
        print(f"    {entry}")
    if len(log) > 8:
        print(f"    … +{len(log) - 8} more log lines")
    return 0


def phase_render_html(P: dict) -> int:
    print("[8/8] Render HTML review")
    cmd = [
        sys.executable, str(P["scripts_dir"] / "render_sidecar_html.py"),
        str(P["sidecar"]),
        "--resolver", str(P["resolver"]),
        "--catalog", str(P["catalog_db"]),
        "--transcripts", str(P["transcripts"]),
        "--dataset-catalog", str(P["dataset_catalog"]),
        "--out", str(P["html"]),
    ]
    return _run(cmd)


def phase_denormalize(P: dict) -> int:
    """Inline-denormalize the sidecar: enrich each annotation with catalog
    metadata, transcript text + segments, derived subject + speakers, and
    scene_label. Applies project glossary substitutions to transcript text +
    segments at the end so the sidecar reflects curated corrections without
    touching source transcripts."""
    print("[4/7] Denormalize sidecar (catalog + transcript + subject + glossary)")
    if not P["sidecar"].exists():
        print(f"  ERROR: sidecar not found: {P['sidecar']}", file=sys.stderr)
        return 2

    import re
    from collections import defaultdict
    sc = json.loads(P["sidecar"].read_text(encoding="utf-8"))
    fps = sc.get("frame_rate", 24000 / 1001)
    con = sqlite3.connect(str(P["catalog_db"]))
    con.row_factory = sqlite3.Row
    transcripts_dir = P["transcripts"]
    catalog_dir = P["dataset_catalog"]

    # Load project glossary for transcript correction. Each (variant, canonical)
    # pair is applied as a case-insensitive word-boundary regex over every
    # annotation's transcript_text + transcript_segments[].text.
    glossary_path = P["scripts_dir"] / "_project_glossary.json"
    glossary_pairs: list = []
    if glossary_path.exists():
        try:
            g = json.loads(glossary_path.read_text(encoding="utf-8"))
            for cat in ("people", "orgs", "places", "terms"):
                for entry in (g.get(cat) or []):
                    canonical = entry.get("canonical")
                    if not canonical:
                        continue
                    for variant in (entry.get("variants") or []):
                        pat = re.compile(r"\b" + re.escape(variant) + r"\b", re.IGNORECASE)
                        glossary_pairs.append((pat, canonical, variant))
        except Exception as e:
            print(f"  WARN: glossary load failed: {e}", file=sys.stderr)
    print(f"  glossary entries: {len(glossary_pairs)} variant->canonical pairs")

    INTERVIEWERS = {"p_alex_rienzie", "p_connor_burkesmith"}

    _asset = {}; _cls = {}; _tr = {}; _subj = {}

    def asset(aid):
        if aid in _asset: return _asset[aid]
        if not aid:
            _asset[aid] = {}; return {}
        row = con.execute("SELECT * FROM asset WHERE asset_id=?", (aid,)).fetchone()
        d = dict(row) if row else {}
        _asset[aid] = d
        return d

    def classifications(aid):
        if aid in _cls: return _cls[aid]
        if not aid:
            _cls[aid] = {}; return {}
        for sub, ext in (("video", ".video.json"), ("audio", ".audio.json"), ("stills", ".still.json")):
            p = catalog_dir / sub / f"{aid}{ext}"
            if p.exists():
                try:
                    r = json.loads(p.read_text(encoding="utf-8"))
                    _cls[aid] = r.get("asset_classifications") or {}
                    return _cls[aid]
                except Exception:
                    pass
        _cls[aid] = {}
        return {}

    def transcript(aid):
        if aid in _tr: return _tr[aid]
        if not aid:
            _tr[aid] = None; return None
        p = transcripts_dir / f"{aid}.transcript.json"
        if not p.exists():
            _tr[aid] = None; return None
        try:
            _tr[aid] = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            _tr[aid] = None
        return _tr[aid]

    def segments_overlap(aid, src_in, src_out):
        t = transcript(aid)
        if not t: return []
        segs = t.get("segments") or t.get("transcript") or []
        out = []
        for s in segs:
            if not isinstance(s, dict): continue
            ss = s.get("start_sec") if s.get("start_sec") is not None else s.get("start")
            se = s.get("end_sec") if s.get("end_sec") is not None else s.get("end")
            text = s.get("text")
            if ss is None or se is None or text is None: continue
            if se < src_in or ss > src_out: continue
            spk = s.get("speaker") or s.get("speaker_p_id") or s.get("speaker_raw") or ""
            out.append({"start_sec": ss, "end_sec": se, "speaker": spk, "text": text.strip()})
        return out

    def derive_subject(aid):
        if aid in _subj: return _subj[aid]
        t = transcript(aid)
        if not t:
            _subj[aid] = None; return None
        speakers = t.get("speakers") or []
        candidates = [s for s in speakers if s.get("p_id") and s["p_id"] not in INTERVIEWERS]
        if not candidates:
            _subj[aid] = None; return None
        best = max(candidates, key=lambda s: s.get("total_duration_sec", 0))
        pid = best.get("p_id")
        name = best.get("label_raw") if best.get("label_raw") and not best["label_raw"].startswith("Speaker ") else pid.replace("p_", "").replace("_", " ").title()
        _subj[aid] = {"p_id": pid, "name": name}
        return _subj[aid]

    def speakers_in_clip(aid, src_in, src_out):
        secs_by_pid = defaultdict(float)
        for seg in segments_overlap(aid, src_in, src_out):
            ss, se, pid = seg["start_sec"], seg["end_sec"], seg["speaker"]
            bucket = pid if pid and pid.startswith("p_") else None
            secs_by_pid[bucket] += max(0, min(se, src_out) - max(ss, src_in))
        out = []
        for pid, secs in sorted(secs_by_pid.items(), key=lambda x: -x[1]):
            if pid:
                out.append({"p_id": pid, "name": pid.replace("p_", "").replace("_", " ").title(), "seconds": round(secs, 2)})
            else:
                out.append({"p_id": None, "name": "Unknown", "seconds": round(secs, 2)})
        return out

    # Scene id → label lookup
    scenes_by_id = {}
    for b in sc.get("beats", []):
        for s in b.get("scenes", []):
            scenes_by_id[s["id"]] = s.get("label")

    n_enriched = 0
    for ann in sc.get("annotations", []):
        k = ann.get("key", {})
        aid = k.get("asset_id")
        a = asset(aid) if aid else {}
        cls = classifications(aid) if aid else {}

        src_in_s = (k.get("source_in_frames") or 0) / fps
        src_out_s = (k.get("source_out_frames") or 0) / fps
        src_dur = src_out_s - src_in_s if (src_in_s is not None and src_out_s is not None) else None
        tl_start_f = k.get("timeline_start_frames")
        tl_start_s = (tl_start_f / fps) if tl_start_f is not None and tl_start_f >= 0 else None
        tl_end_f = (tl_start_f + (k.get("source_out_frames") or 0) - (k.get("source_in_frames") or 0)) if tl_start_f is not None and tl_start_f >= 0 else None
        tl_end_s = (tl_end_f / fps) if tl_end_f is not None else None

        # Denormalized fields
        ann["scene_label"] = scenes_by_id.get(ann.get("scene"))
        ann["asset"] = {
            "filename": a.get("filename"),
            "source_path": a.get("source_path"),
            "duration_sec": a.get("duration_sec"),
            "shoot_date": a.get("shoot_date"),
            "shoot_label": a.get("shoot_label"),
            "primary_timeline_date": a.get("primary_timeline_date"),
            "camera_id": a.get("camera_id"),
            "audio_recorder": a.get("audio_recorder"),
            "codec": a.get("codec"),
            "classifications": cls,
        }
        ann["subject"] = derive_subject(aid) if aid else None
        ann["speakers"] = speakers_in_clip(aid, src_in_s, src_out_s) if aid else []
        ann["timing"] = {
            "source_in_sec": round(src_in_s, 3),
            "source_out_sec": round(src_out_s, 3),
            "source_duration_sec": round(src_dur, 3) if src_dur is not None else None,
            "timeline_start_sec": round(tl_start_s, 3) if tl_start_s is not None else None,
            "timeline_end_sec": round(tl_end_s, 3) if tl_end_s is not None else None,
        }
        # Transcript inlined (full segments overlapping the clip's source window)
        segs = segments_overlap(aid, src_in_s, src_out_s) if aid else []
        # Apply project glossary to each segment's text + the joined transcript
        n_glossary_hits = 0
        for s in segs:
            txt = s.get("text") or ""
            for pat, canonical, _variant in glossary_pairs:
                new_txt = pat.sub(canonical, txt)
                if new_txt != txt:
                    n_glossary_hits += 1
                txt = new_txt
            s["text"] = txt
        ann["transcript_segments"] = segs
        ann["transcript_text"] = " ".join(s["text"] for s in segs)
        ann["_glossary_hits"] = n_glossary_hits if n_glossary_hits else None
        n_enriched += 1

    _safe_write_json(P["sidecar"], sc)
    new_size = P["sidecar"].stat().st_size
    total_hits = sum(a.get("_glossary_hits") or 0 for a in sc.get("annotations", []))
    print(f"  denormalized: {n_enriched} annotations; glossary substitutions: {total_hits}; sidecar size {new_size/1024:.0f} KB")
    return 0


def _run(cmd: list[str]) -> int:
    res = subprocess.run(cmd, stdout=sys.stdout, stderr=sys.stderr)
    return res.returncode


# ----------------------------- Orchestrator -----------------------------


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--editor-root", default=None,
                    help="path to editor/ (default: parent of this script's parent)")
    ap.add_argument("--act-id", default="actII")
    ap.add_argument("--xml", default=None,
                    help="override XML path (default: read from current sidecar's xml_source)")
    ap.add_argument("--no-archive",       action="store_true",
                    help="skip snapshotting the prior sidecar to _archive/ before refresh")
    ap.add_argument("--force-archive",    action="store_true",
                    help="snapshot the prior sidecar to _archive/ even when xml hasn't changed")
    ap.add_argument("--skip-resolver",    action="store_true")
    ap.add_argument("--skip-extract",     action="store_true")
    ap.add_argument("--skip-gemini",      action="store_true")
    ap.add_argument("--skip-denormalize", action="store_true")
    ap.add_argument("--skip-cut-eval",    action="store_true")
    ap.add_argument("--skip-visual-cut",  action="store_true",
                    help="skip SigLIP visual-cut delta phase (faster refresh; loses cut-quality scores)")
    ap.add_argument("--apply-suggested-spans", action="store_true",
                    help="tighten source in/out to chunk_suggested_span when inside current trim")
    ap.add_argument("--skip-render",      action="store_true")
    args = ap.parse_args()

    editor_root = (Path(args.editor_root).resolve() if args.editor_root
                   else Path(__file__).resolve().parent.parent.parent)
    P = derive_paths(editor_root, args.act_id, args.xml)

    print(f"Refresh: act={args.act_id}  editor_root={editor_root}")
    print(f"  xml:       {P['xml']}")
    print(f"  sidecar:   {P['sidecar']}")
    print(f"  manifest:  {P['manifest']}")
    print(f"  resolver:  {P['resolver']}")
    print(f"  html:      {P['html']}")
    print()

    started = time.time()

    # Archive previous sidecar BEFORE any phase overwrites it.
    if not args.no_archive:
        rc = phase_archive_previous(P, force=args.force_archive)
        if rc != 0:
            print(f"\nABORT: phase_archive_previous returned {rc}", file=sys.stderr)
            return rc
        print()
    else:
        print("  (skipped phase_archive_previous)")
        print()

    phases = [
        (args.skip_resolver,    phase_rebuild_resolver, {1}),
        (args.skip_extract,     phase_extract_sidecar,  {1}),
        (args.skip_gemini,      phase_populate_gemini,  set()),
        (args.skip_denormalize, phase_denormalize,      set()),
        (args.skip_cut_eval,    phase_cut_eval,         set()),
        (args.skip_visual_cut,  phase_visual_cut_eval,  set()),
    ]
    for skip, phase, soft in phases:
        if skip:
            print(f"  (skipped {phase.__name__})")
            continue
        rc = phase(P)
        if rc != 0:
            if rc in soft:
                print(f"  (WARN: {phase.__name__} returned rc={rc} -- continuing)\n")
            else:
                print(f"\nABORT: {phase.__name__} returned {rc}", file=sys.stderr)
                return rc
        else:
            print()

    rc = phase_apply_suggested_spans(P, enabled=args.apply_suggested_spans)
    if rc != 0:
        print(f"\nABORT: phase_apply_suggested_spans returned {rc}", file=sys.stderr)
        return rc
    print()

    skip_render = args.skip_render
    if not skip_render:
        rc = phase_render_html(P)
        if rc != 0:
            print(f"\nABORT: phase_render_html returned {rc}", file=sys.stderr)
            return rc
        print()
    else:
        print("  (skipped phase_render_html)")

    elapsed = time.time() - started
    print(f"Done in {elapsed:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
