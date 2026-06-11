#!/usr/bin/env python3
"""llm_qa.py — Gemini-Flash cross-checks of the K-layer outputs.

Three checks, each cheap (~$0.01-0.05 total at sample sizes below):

  ocr-vs-gemini      For sampled (asset, OCR text) rows, ask Gemini whether
                     seeing that on-screen text is consistent with the scene
                     description Gemini Pro wrote during ingest. Catches
                     Apple Vision / RapidOCR hallucinations the >=3-alnum +
                     Cyrillic post-filters didn't catch.
  face-vs-gemini     For each labeled face cluster, sample a few detections
                     and ask Gemini whether the asset-level Gemini description
                     mentions / is consistent with that person being on-screen.
                     Catches cluster mis-labels (e.g. brother-conflation).
  chromaprint-vs-gemini  For lower-confidence applied chromaprint links
                         (combined_score 0.65-0.80), ask Gemini whether the
                         two assets' descriptions plausibly describe the same
                         recording session. Catches false-positive lavalier-
                         camera links inside the same shoot folder.

Auth:
  Reads GEMINI_API_KEY from env. If not set, falls back to parsing
  ~/.zshrc (best-effort; warns).

Usage:
  python3 dataset/_scripts/qa/llm_qa.py ocr-vs-gemini --sample 50
  python3 dataset/_scripts/qa/llm_qa.py face-vs-gemini --per-cluster 5
  python3 dataset/_scripts/qa/llm_qa.py chromaprint-vs-gemini
  python3 dataset/_scripts/qa/llm_qa.py all
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import sqlite3
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from _paths import (  # noqa: E402
    AUDIO_FINGERPRINT_DB, EMBEDDINGS_DB, INDEXES_DIR, RUNS_DIR, VIDEO_CATALOG,
)

EDITORIAL_DB = INDEXES_DIR / "editorial_catalog.sqlite"
GEMINI_MODEL = "gemini-2.5-pro"
QA_DIR = RUNS_DIR.parent / "qa"


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ts_slug() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _api_key() -> str:
    k = os.environ.get("GEMINI_API_KEY")
    if k:
        return k
    # Fallback: parse .zshrc
    rc = Path.home() / ".zshrc"
    if rc.exists():
        for line in rc.read_text().splitlines():
            m = re.match(r'^\s*export\s+GEMINI_API_KEY\s*=\s*"?([^"\s]+)"?', line)
            if m:
                print("  (using GEMINI_API_KEY from .zshrc)", file=sys.stderr)
                return m.group(1)
    raise RuntimeError("GEMINI_API_KEY not in env and not findable in ~/.zshrc")


def _client():
    from google import genai
    return genai.Client(api_key=_api_key())


def _generate_json(client, prompt: str, max_retries: int = 3) -> dict | None:
    """Call Gemini with response_mime_type=application/json; parse + return dict.
    Returns None on persistent failure."""
    from google.genai import types
    cfg = types.GenerateContentConfig(
        response_mime_type="application/json",
        temperature=0.1,
    )
    for attempt in range(max_retries):
        try:
            resp = client.models.generate_content(
                model=GEMINI_MODEL, contents=prompt, config=cfg,
            )
            txt = resp.text
            return json.loads(txt)
        except Exception as e:
            if attempt == max_retries - 1:
                print(f"    Gemini failure after {max_retries} attempts: {e}", file=sys.stderr)
                return None
            time.sleep(2 ** attempt)


# ============================================================ asset → gemini-summary lookup

def _build_asset_summary_index() -> dict[str, dict]:
    """For each asset that has Gemini semantics, build a compact summary dict
    (one entry per asset, joining all chunks)."""
    import json as _j
    out = {}
    n = 0
    for f in VIDEO_CATALOG.glob("*.video.json"):
        if f.name.startswith("._"): continue  # macOS AppleDouble sidecar
        try:
            d = _j.loads(f.read_text())
        except Exception:
            continue
        aid = d.get("asset_id")
        s = d.get("asset_semantic_summary") or {}
        chunks = s.get("chunks") or []
        if not chunks:
            continue
        # Compact: subject, setting, action joined across chunks
        subjects = [c.get("subject", "") for c in chunks if c.get("subject")]
        actions = [c.get("action", "") for c in chunks if c.get("action")]
        settings = [
            (c.get("setting") or {}).get("location", "") if isinstance(c.get("setting"), dict) else ""
            for c in chunks
        ]
        settings = [s for s in settings if s]
        kms = []
        for c in chunks:
            for km in (c.get("key_moments") or []):
                desc = km.get("description") if isinstance(km, dict) else None
                if desc:
                    kms.append(desc[:160])
        out[aid] = {
            "subject": " | ".join(subjects[:3])[:300],
            "action": " | ".join(actions[:3])[:300],
            "location": " | ".join(settings[:3])[:200],
            "key_moments": kms[:10],
            "shoot_label": (d.get("path_metadata") or {}).get("shoot_label", ""),
        }
        n += 1
    print(f"  built summary index for {n:,} assets", file=sys.stderr)
    return out


# ============================================================ ocr-vs-gemini

def cmd_ocr_vs_gemini(args: argparse.Namespace) -> dict:
    out_dir = QA_DIR / ts_slug()
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / "ocr-vs-gemini.md"

    con = sqlite3.connect(f"file:{EDITORIAL_DB}?mode=ro", uri=True)
    summaries = _build_asset_summary_index()

    # Sample distinct (asset_id, text) pairs from frame_text.
    rows = con.execute("""
        SELECT asset_id, text, confidence, ocr_engine
        FROM frame_text
        WHERE LENGTH(TRIM(text)) >= 4
    """).fetchall()
    rng = random.Random(args.seed)
    rng.shuffle(rows)
    rows = rows[: args.sample]
    print(f"  sampling {len(rows)} frame_text rows")

    client = _client()
    results = []
    n_consistent = 0
    n_suspicious = 0
    n_unknown = 0
    for i, (aid, text, conf, engine) in enumerate(rows):
        summary = summaries.get(aid)
        if not summary:
            continue
        prompt = (
            "You are a documentary editor doing QA on automated OCR results.\n"
            f"Asset shoot: {summary['shoot_label']}\n"
            f"Scene subject: {summary['subject']}\n"
            f"Scene action: {summary['action']}\n"
            f"Scene location: {summary['location']}\n"
            f"\n"
            f"OCR engine `{engine}` extracted this text from a frame in this asset:\n"
            f"    {text!r}\n"
            f"\n"
            "Does seeing that on-screen text plausibly fit this scene? Reply ONLY with a JSON object:\n"
            '  {"verdict": "consistent" | "suspicious" | "unknown", "reason": "<one short sentence>"}\n'
            "\n"
            "Use 'consistent' if the text plausibly appears in the scene (signs, lower-thirds, "
            "graphics, subtitles, screens, T-shirts, bibs). Use 'suspicious' if it looks like OCR "
            "noise that doesn't fit the scene. Use 'unknown' if the scene description is too vague to judge."
        )
        r = _generate_json(client, prompt)
        if r is None:
            continue
        verdict = (r.get("verdict") or "").strip().lower()
        reason = (r.get("reason") or "")[:200]
        if verdict == "consistent": n_consistent += 1
        elif verdict == "suspicious": n_suspicious += 1
        else: n_unknown += 1
        results.append({
            "asset_id": aid, "text": text, "engine": engine, "conf": conf,
            "verdict": verdict, "reason": reason,
            "scene": f"{summary['subject'][:80]} / {summary['location'][:60]}",
        })
        if (i + 1) % 10 == 0:
            print(f"  [{i+1}/{len(rows)}] consistent={n_consistent} suspicious={n_suspicious} unknown={n_unknown}")

    # Write report
    lines = [
        f"# OCR ↔ Gemini consistency QA ({now_iso()})",
        "",
        f"Sampled {len(results)} `frame_text` rows; asked Gemini-Flash whether each "
        "OCR hit plausibly fits the asset's Gemini scene description.",
        "",
        f"- consistent: **{n_consistent}** ({100*n_consistent/max(len(results),1):.0f}%)",
        f"- suspicious: **{n_suspicious}** ({100*n_suspicious/max(len(results),1):.0f}%) ← OCR likely wrong",
        f"- unknown:    {n_unknown} ({100*n_unknown/max(len(results),1):.0f}%)",
        "",
        "## All suspicious hits",
        "",
        "| asset | engine | text | reason | scene |",
        "|---|---|---|---|---|",
    ]
    for r in results:
        if r["verdict"] != "suspicious":
            continue
        text_disp = (r["text"] or "").replace("|", "\\|")[:60]
        reason_disp = r["reason"].replace("|", "\\|")[:140]
        scene_disp = r["scene"].replace("|", "\\|")
        lines.append(
            f"| `{r['asset_id'][:12]}` | {r['engine']} | `{text_disp}` | "
            f"{reason_disp} | {scene_disp} |"
        )

    lines += [
        "",
        "## All consistent hits (spot-check)",
        "",
        "| asset | engine | text | scene |",
        "|---|---|---|---|",
    ]
    for r in results[:20]:
        if r["verdict"] != "consistent":
            continue
        text_disp = (r["text"] or "").replace("|", "\\|")[:60]
        scene_disp = r["scene"].replace("|", "\\|")
        lines.append(
            f"| `{r['asset_id'][:12]}` | {r['engine']} | `{text_disp}` | {scene_disp} |"
        )

    out.write_text("\n".join(lines))
    print(f"  report: {out}")
    return {"n": len(results), "n_suspicious": n_suspicious, "report": str(out)}


# ============================================================ face-vs-gemini

def cmd_face_vs_gemini(args: argparse.Namespace) -> dict:
    out_dir = QA_DIR / ts_slug()
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / "face-vs-gemini.md"

    con = sqlite3.connect(f"file:{EDITORIAL_DB}?mode=ro", uri=True)
    summaries = _build_asset_summary_index()

    # Load people registry for known names
    people_path = INDEXES_DIR.parent / "dataset" / "people" / "people.json"
    people = {}
    if people_path.exists():
        pd = json.loads(people_path.read_text())
        items = pd.get("people", []) if isinstance(pd, dict) else pd
        for p in items:
            pid = p.get("p_id") or p.get("id")
            name = p.get("name") or p.get("display_name") or pid
            if pid:
                people[pid] = name

    # For each labeled p_id, sample N assets where they appear, and ask Gemini
    # if the scene description mentions or is consistent with that person.
    pids = [r[0] for r in con.execute(
        "SELECT DISTINCT p_id FROM frame_face WHERE p_id LIKE 'p_%'"
    ).fetchall()]
    print(f"  {len(pids)} labeled p_ids")

    rng = random.Random(args.seed)
    client = _client()
    results = []
    for p_id in pids:
        name = people.get(p_id, p_id)
        assets = [r[0] for r in con.execute(
            "SELECT DISTINCT asset_id FROM frame_face WHERE p_id=?", (p_id,)
        ).fetchall()]
        rng.shuffle(assets)
        for aid in assets[: args.per_cluster]:
            summary = summaries.get(aid)
            if not summary:
                continue
            prompt = (
                "You are a documentary editor doing QA on automated face recognition.\n"
                f"A face-detection cluster labeled as `{name}` (people_id `{p_id}`) "
                f"placed detections in this asset.\n"
                f"\n"
                f"Asset shoot: {summary['shoot_label']}\n"
                f"Scene subject (Gemini Pro): {summary['subject']}\n"
                f"Scene action: {summary['action']}\n"
                f"Scene location: {summary['location']}\n"
                f"Key moments: {' / '.join(summary['key_moments'][:5])}\n"
                "\n"
                f"Does the scene plausibly include `{name}` on-screen? Reply ONLY with JSON:\n"
                '  {"verdict": "likely" | "unlikely" | "unknown", "reason": "<one short sentence>"}\n'
                "\n"
                "Use 'likely' if the description names them or describes context where they'd be present "
                "(e.g. interview subject in their own interview, identified-by-name in any chunk). "
                "Use 'unlikely' if the scene describes content where this person clearly wouldn't be "
                "(e.g. wildlife B-roll, drone footage with no people, an interview with someone else). "
                "Use 'unknown' if the description is too generic to judge."
            )
            r = _generate_json(client, prompt)
            if r is None:
                continue
            verdict = (r.get("verdict") or "").strip().lower()
            results.append({
                "p_id": p_id, "name": name, "asset_id": aid,
                "verdict": verdict, "reason": (r.get("reason") or "")[:200],
                "shoot": summary["shoot_label"],
                "scene": f"{summary['subject'][:80]} / {summary['action'][:60]}",
            })
        print(f"  {p_id} ({name}): {min(len(assets), args.per_cluster)} samples")

    # Summarize per p_id
    per_pid = defaultdict(lambda: {"likely": 0, "unlikely": 0, "unknown": 0})
    for r in results:
        per_pid[r["p_id"]][r["verdict"] if r["verdict"] in ("likely","unlikely","unknown") else "unknown"] += 1

    lines = [
        f"# Face cluster ↔ Gemini scene consistency QA ({now_iso()})",
        "",
        f"Sampled up to {args.per_cluster} assets per labeled p_id; asked Gemini-Flash "
        "whether the asset's scene description is consistent with that person being on-screen.",
        "",
        "**A high 'unlikely' rate for a p_id suggests cluster contamination** "
        "(face index conflating multiple people under one label).",
        "",
        "## Per-cluster verdict distribution",
        "",
        "| p_id | name | likely | unlikely | unknown | flag? |",
        "|---|---|---:|---:|---:|---|",
    ]
    for p_id, counts in sorted(per_pid.items(), key=lambda kv: -kv[1]["unlikely"]):
        n = sum(counts.values())
        flag = "⚠️ many unlikely" if (counts["unlikely"] >= 2 and counts["unlikely"] / max(n, 1) > 0.4) else ""
        lines.append(
            f"| `{p_id}` | {people.get(p_id, p_id)} | "
            f"{counts['likely']} | {counts['unlikely']} | {counts['unknown']} | {flag} |"
        )
    lines += [
        "",
        "## All 'unlikely' detections",
        "",
        "| p_id | asset | shoot | scene | reason |",
        "|---|---|---|---|---|",
    ]
    for r in results:
        if r["verdict"] != "unlikely":
            continue
        lines.append(
            f"| `{r['p_id']}` | `{r['asset_id'][:12]}` | {r['shoot']} | "
            f"{r['scene']} | {r['reason']} |"
        )
    out.write_text("\n".join(lines))
    print(f"  report: {out}")
    n_unlikely = sum(1 for r in results if r["verdict"] == "unlikely")
    return {"n": len(results), "n_unlikely": n_unlikely, "report": str(out)}


# ============================================================ chromaprint-vs-gemini

def cmd_chromaprint_vs_gemini(args: argparse.Namespace) -> dict:
    out_dir = QA_DIR / ts_slug()
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / "chromaprint-vs-gemini.md"

    if not AUDIO_FINGERPRINT_DB.exists():
        return {"check": "chromaprint-vs-gemini", "skipped": "audio_fingerprints.sqlite missing"}

    summaries = _build_asset_summary_index()

    con_fp = sqlite3.connect(str(AUDIO_FINGERPRINT_DB))
    rows = con_fp.execute("""
        SELECT video_asset_id, audio_asset_id, raw_match_score, combined_score
        FROM applied_link
        WHERE combined_score BETWEEN ? AND ?
        ORDER BY combined_score ASC
    """, (args.min_score, args.max_score)).fetchall()
    print(f"  {len(rows)} applied links in band [{args.min_score:.2f}-{args.max_score:.2f}]")

    client = _client()
    results = []
    for i, (vid, aid, raw, comb) in enumerate(rows):
        vs = summaries.get(vid)
        if not vs:
            # Audio source has no Gemini chunk (audio assets don't go through Gemini Pro)
            # but the video should. If video missing, skip.
            continue
        # Audio asset usually has no semantic_summary (Gemini was video-only).
        # Pull its source_path for context instead.
        a_src = ""
        _audio_catalog = Path(__file__).resolve().parents[2] / "assets" / "audio"
        af = next(_audio_catalog.glob(f"{aid}*.audio.json"), None)
        if af:
            try:
                a_src = (json.loads(af.read_text()).get("source_path") or "")[-100:]
            except Exception:
                pass
        prompt = (
            "You are a documentary editor doing QA on an automated audio↔video linker for a feature "
            "documentary.\n"
            "\n"
            "PROJECT HARDWARE CONTEXT (important — generic intuition fails here):\n"
            "  - Audio filenames starting `DJI_NN_YYYYMMDD_HHMMSS.WAV` are from the DJI Mic 2 wireless\n"
            "    lavalier system (a professional radio mic kit, the project's primary lavalier rig).\n"
            "    They are NOT drone audio. The 'DJI' prefix only indicates the manufacturer.\n"
            "  - `.RDC` bundle audio (files like `A037_C007_…RDC/…wav`) is from RED cameras with XLR\n"
            "    inputs. Although technically 'onboard' to the camera, this IS the production-grade\n"
            "    audio source for that shoot (often a shotgun or lavalier connected through the\n"
            "    XLR pre-amps). Linking other-angle videos to this audio is exactly what we want\n"
            "    for multi-camera setups where one camera carries the production audio.\n"
            "  - Actual drone footage exists but is uncommon. If you see your dedicated DJI shoot folder in the path,\n"
            "    that's a drone shoot folder.\n"
            "  - Tentacle Track recordings (`Tentacle Track E/AUDIO/…wav`) are from a Tentacle Sync\n"
            "    timecode bag-recorder rig — professional production audio.\n"
            "\n"
            "The chromaprint linker proposes that a video and audio asset capture the same recording session.\n"
            "\n"
            f"VIDEO asset (`{vid[:12]}`):\n"
            f"  shoot: {vs['shoot_label']}\n"
            f"  subject: {vs['subject']}\n"
            f"  action: {vs['action']}\n"
            f"  location: {vs['location']}\n"
            "\n"
            f"AUDIO asset (`{aid[:12]}`):\n"
            f"  source path: ...{a_src}\n"
            f"  (Gemini didn't summarize audio assets; only the source path is available.)\n"
            "\n"
            f"Bit-Hamming chromaprint similarity: {raw:.3f}  combined: {comb:.3f}\n"
            "  (>0.7 = aligned same source; 0.65-0.80 is the band we're QA-ing.)\n"
            "\n"
            "Is it plausible that this audio asset is the production-recorder backup or production\n"
            "audio source for this video? Reply ONLY with JSON:\n"
            '  {"verdict": "plausible" | "implausible" | "unknown", "reason": "<one short sentence>"}\n'
            "\n"
            "Use 'plausible' if: same shoot folder + the audio file pattern looks like lavalier WAV,\n"
            "bag-recorder WAV, or RED .RDC audio (these are all legitimate production audio sources).\n"
            "Use 'implausible' ONLY if there's a clear semantic mismatch — e.g. video is a child\n"
            "playing on a beach but the audio path is `news_voiceover.wav` from a different shoot.\n"
            "Do NOT flag DJI_NN_…WAV as drone audio; do NOT flag RED .RDC audio as 'just camera audio'."
        )
        r = _generate_json(client, prompt)
        if r is None:
            continue
        results.append({
            "vid": vid, "aid": aid, "raw": raw, "comb": comb,
            "verdict": (r.get("verdict") or "").strip().lower(),
            "reason": (r.get("reason") or "")[:200],
            "v_shoot": vs["shoot_label"], "v_subject": vs["subject"][:120],
            "a_src": a_src,
        })

    n_plausible = sum(1 for r in results if r["verdict"] == "plausible")
    n_implausible = sum(1 for r in results if r["verdict"] == "implausible")
    n_unknown = sum(1 for r in results if r["verdict"] == "unknown")

    lines = [
        f"# chromaprint applied_link ↔ Gemini scene plausibility QA ({now_iso()})",
        "",
        f"Asked Gemini-Flash whether each lower-band chromaprint applied link "
        f"(combined_score {args.min_score:.2f}–{args.max_score:.2f}) plausibly describes "
        f"the same recording session.",
        "",
        f"- plausible:   **{n_plausible}** ({100*n_plausible/max(len(results),1):.0f}%)",
        f"- implausible: **{n_implausible}** ({100*n_implausible/max(len(results),1):.0f}%) ← review / roll back",
        f"- unknown:     {n_unknown}",
        "",
        "## Implausible links (candidate rollbacks)",
        "",
        "| video | audio | raw | comb | v_shoot | v_subject | a_src | reason |",
        "|---|---|---:|---:|---|---|---|---|",
    ]
    for r in results:
        if r["verdict"] != "implausible":
            continue
        bar = "\\|"
        v_subj = r['v_subject'].replace('|', bar)
        a_src = r['a_src'].replace('|', bar)
        reason = r['reason'].replace('|', bar)
        lines.append(
            f"| `{r['vid'][:12]}` | `{r['aid'][:12]}` | {r['raw']:.2f} | {r['comb']:.2f} | "
            f"{r['v_shoot']} | {v_subj} | `{a_src}` | {reason} |"
        )
    out.write_text("\n".join(lines))
    print(f"  report: {out}")
    return {"n": len(results), "n_implausible": n_implausible, "report": str(out)}


# ============================================================ main

def cmd_all(args: argparse.Namespace) -> None:
    print("=== ocr-vs-gemini ===")
    r1 = cmd_ocr_vs_gemini(args)
    print(f"  → {r1}")
    print("=== face-vs-gemini ===")
    r2 = cmd_face_vs_gemini(args)
    print(f"  → {r2}")
    print("=== chromaprint-vs-gemini ===")
    r3 = cmd_chromaprint_vs_gemini(args)
    print(f"  → {r3}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("ocr-vs-gemini")
    sp.add_argument("--sample", type=int, default=80)
    sp.add_argument("--seed", type=int, default=20260525)
    sp.set_defaults(func=cmd_ocr_vs_gemini)

    sp = sub.add_parser("face-vs-gemini")
    sp.add_argument("--per-cluster", type=int, default=4)
    sp.add_argument("--seed", type=int, default=20260525)
    sp.set_defaults(func=cmd_face_vs_gemini)

    sp = sub.add_parser("chromaprint-vs-gemini")
    sp.add_argument("--min-score", type=float, default=0.65)
    sp.add_argument("--max-score", type=float, default=0.80)
    sp.set_defaults(func=cmd_chromaprint_vs_gemini)

    sp = sub.add_parser("all")
    sp.add_argument("--sample", type=int, default=80)
    sp.add_argument("--per-cluster", type=int, default=4)
    sp.add_argument("--min-score", type=float, default=0.65)
    sp.add_argument("--max-score", type=float, default=0.80)
    sp.add_argument("--seed", type=int, default=20260525)
    sp.set_defaults(func=cmd_all)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
