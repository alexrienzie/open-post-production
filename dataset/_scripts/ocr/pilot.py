#!/usr/bin/env python3
"""pilot.py — OCR engine pilot. Stratified ~30 frames sampled across
diverse sample corpus content (press / interview / race / verite-with-text / stills).
Runs both RapidOCR and Apple Vision on each, writes a markdown report with
crops so the user can verify what's on screen vs what each engine extracted.

One-off; safe to delete after the engine + threshold decision lands.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
import warnings
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from _ocr import (  # noqa: E402
    run_rapidocr, run_apple_vision, extract_frame_at, is_bib_text,
    normalized_bbox_to_pixels,
)
from _paths import (  # noqa: E402
    INDEXES_DIR, RUNS_DIR, DERIVATIVE_MEDIA,
    resolve_proxy_via_asset_map, derivative_relative,
)

warnings.filterwarnings("ignore", message="urllib3 v2 only supports OpenSSL")


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# Stratified sample: each band yields (label, SQL filter on asset, sample size, sample-from-shots?)
# Frames are picked at shot midpoints when sampling from videos.
BANDS = [
    ("press / podcast / news",
     "a.asset_type = 'third_party' OR a.shoot_label LIKE '%Podcast%' OR a.shoot_label LIKE '%News%'",
     8, True),
    ("interview lower-thirds",
     "a.asset_type = 'interview'",
     6, True),
    ("race shoots (bibs + signage)",
     "(a.shoot_label LIKE '%<shoot-a>%' OR a.shoot_label LIKE '%<shoot-b>%' "
     "OR a.shoot_label LIKE '%<shoot-c>%' OR a.shoot_label LIKE '%<shoot-d>%' "
     "OR a.shoot_label LIKE '%<shoot-e>%' OR a.shoot_label LIKE '%<shoot-f>%')",
     6, True),
    ("verite with visible text in semantic_subject",
     "(a.asset_type IN ('verite')) AND "
     "(a.semantic_subject LIKE '%sign%' OR a.semantic_subject LIKE '%screen%' "
     "OR a.semantic_subject LIKE '%phone%' OR a.semantic_subject LIKE '%laptop%' "
     "OR a.semantic_subject LIKE '%bib%' OR a.semantic_subject LIKE '%label%' "
     "OR a.semantic_subject LIKE '%t-shirt%' OR a.semantic_subject LIKE '%logo%')",
     6, True),
]
STILL_SAMPLE_N = 4   # cataloged stills with text-likely subjects


def _pick_video_samples(con: sqlite3.Connection) -> list[dict]:
    """For each video band, pick N (asset, shot, time) tuples at random."""
    samples = []
    for label, where, n, _is_video in BANDS:
        rows = list(con.execute(f"""
            WITH stable AS (
                SELECT s.asset_id, s.shot_idx, s.start_sec, s.end_sec, s.duration_sec,
                       COALESCE(a.shoot_label, a.category_name, '?') shoot,
                       a.asset_type, a.semantic_subject
                FROM shot s JOIN asset a ON s.asset_id = a.asset_id
                WHERE a.record_kind = 'video'
                  AND s.duration_sec > 2.0
                  AND ({where})
            )
            SELECT * FROM stable ORDER BY RANDOM() LIMIT ?
        """, (n,)))
        for r in rows:
            aid, shot_idx, start, end, dur, shoot, atype, subj = r
            mid = (start + end) / 2.0
            samples.append({
                "band": label,
                "asset_id": aid, "record_kind": "video",
                "shot_idx": shot_idx, "frame_time_sec": mid,
                "duration_sec": dur, "shoot": shoot, "asset_type": atype,
                "semantic_subject": subj,
            })
    return samples


def _pick_still_samples(con: sqlite3.Connection, n: int) -> list[dict]:
    """Pick N stills with text-likely semantic subjects."""
    rows = list(con.execute("""
        SELECT a.asset_id, a.source_path, a.shoot_label, a.category_name,
               a.asset_type, a.semantic_subject
        FROM asset a
        WHERE a.record_kind = 'still'
          AND (a.semantic_subject LIKE '%sign%' OR a.semantic_subject LIKE '%screen%'
            OR a.semantic_subject LIKE '%phone%' OR a.semantic_subject LIKE '%laptop%'
            OR a.semantic_subject LIKE '%bib%' OR a.semantic_subject LIKE '%logo%'
            OR a.semantic_subject LIKE '%t-shirt%' OR a.semantic_subject LIKE '%text%'
            OR a.semantic_subject LIKE '%poster%' OR a.semantic_subject LIKE '%book%')
        ORDER BY RANDOM() LIMIT ?
    """, (n,)))
    out = []
    for aid, sp, shoot, cat, atype, subj in rows:
        out.append({
            "band": "cataloged stills",
            "asset_id": aid, "record_kind": "still", "source_path": sp,
            "shoot": shoot or cat or "?", "asset_type": atype,
            "semantic_subject": subj,
        })
    return out


def _resolve_frame(sample: dict) -> tuple[Path | None, "np.ndarray | None"]:
    """Return (saved_jpeg_path, img_bgr) for this sample. For videos: extract via
    ffmpeg and save crop alongside the report. For stills: load file directly."""
    import cv2
    if sample["record_kind"] == "video":
        proxy = resolve_proxy_via_asset_map(sample["asset_id"])
        if proxy is None or not proxy.exists():
            return None, None
        img = extract_frame_at(proxy, sample["frame_time_sec"])
        return proxy, img
    # still
    sp = sample.get("source_path") or ""
    try:
        rel = derivative_relative(sp)
    except ValueError:
        return None, None
    disk = DERIVATIVE_MEDIA / rel
    if not disk.exists():
        return None, None
    # Handle HEIC via ffmpeg fallback
    ext = disk.suffix.lower()
    if ext in (".heic", ".heif"):
        # Use the same helper as faces — round-trip via ffmpeg → mjpeg
        img = extract_frame_at(disk, 0.0)  # ffmpeg can decode HEIC at t=0
        return disk, img
    if ext in (".arw", ".dng", ".cr2", ".cr3", ".nef", ".raf", ".orf"):
        return disk, None
    img = cv2.imread(str(disk))
    return disk, img


def main() -> None:
    import cv2
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--report-dir", type=Path, default=None,
                    help="report + crops dir (default: dataset/_runs/ingest_pipeline/ocr/pilot_<ts>/)")
    args = ap.parse_args()

    ts_short = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_dir = args.report_dir or (RUNS_DIR / "ocr" / f"pilot_{ts_short}")
    report_dir.mkdir(parents=True, exist_ok=True)
    crops_dir = report_dir / "crops"
    crops_dir.mkdir(parents=True, exist_ok=True)

    con = sqlite3.connect(f"file:{INDEXES_DIR / 'editorial_catalog.sqlite'}?mode=ro", uri=True)

    samples = _pick_video_samples(con) + _pick_still_samples(con, STILL_SAMPLE_N)
    print(f"sampled {len(samples)} frames across {len(BANDS) + 1} bands")
    print(f"report → {report_dir}/pilot.md")

    sections: list[str] = [
        f"# OCR engine pilot ({now_iso()})",
        "",
        f"Stratified {len(samples)}-frame sample comparing **RapidOCR** vs **Apple Vision** "
        f"on your corpus content. Crops saved next to this report — open `crops/` and cross-reference "
        f"each frame against the OCR output to score recall and false-positives.",
        "",
        f"**Engines:**",
        f"- RapidOCR (`rapidocr-onnxruntime` 1.4.4) — pure ONNX, CPU",
        f"- Apple Vision (`pyobjc-framework-Vision` 11.1) — native macOS, Apple Neural Engine",
        "",
    ]

    bands_seen: dict[str, int] = {}
    counters_rapid = {"total_hits": 0, "bib_hits": 0, "total_latency": 0.0, "frames": 0}
    counters_vision = {"total_hits": 0, "bib_hits": 0, "total_latency": 0.0, "frames": 0}
    t_start = time.time()

    for sample in samples:
        band = sample["band"]
        if band not in bands_seen:
            sections.append(f"## {band}")
            sections.append("")
            bands_seen[band] = 0
        bands_seen[band] += 1

        print(f"  [{sample['band']}] {sample['asset_id'][:12]} (kind={sample['record_kind']})", flush=True)
        src_path, img = _resolve_frame(sample)
        if img is None:
            sections.append(f"### `{sample['asset_id'][:12]}`  ·  {sample.get('shoot') or '?'}")
            sections.append(f"_no frame available (path={src_path}, kind={sample['record_kind']})_")
            sections.append("")
            continue

        crop_path = crops_dir / f"{sample['asset_id'][:12]}_{sample['record_kind']}_{int(sample.get('frame_time_sec', 0))}.jpg"
        cv2.imwrite(str(crop_path), img)

        # RapidOCR
        try:
            t0 = time.time()
            rapid = run_rapidocr(img)
            el_rapid = time.time() - t0
        except Exception as e:
            rapid, el_rapid = [], 0.0
            print(f"    RapidOCR ERR: {e}")

        # Apple Vision — prefer path when available (stills + raw on disk)
        try:
            t0 = time.time()
            if src_path is not None and src_path.exists() and sample["record_kind"] == "still" and src_path.suffix.lower() not in (".heic", ".heif"):
                vision = run_apple_vision(path=src_path)
            else:
                vision = run_apple_vision(img_bgr=img)
            el_vision = time.time() - t0
        except Exception as e:
            vision, el_vision = [], 0.0
            print(f"    Apple Vision ERR: {e}")

        counters_rapid["frames"] += 1
        counters_rapid["total_hits"] += len(rapid)
        counters_rapid["bib_hits"] += sum(1 for h in rapid if is_bib_text(h["text"]))
        counters_rapid["total_latency"] += el_rapid
        counters_vision["frames"] += 1
        counters_vision["total_hits"] += len(vision)
        counters_vision["bib_hits"] += sum(1 for h in vision if is_bib_text(h["text"]))
        counters_vision["total_latency"] += el_vision

        # Markdown section per frame
        ts_str = f"t={sample.get('frame_time_sec', 0):.1f}s" if sample["record_kind"] == "video" else "(still)"
        sections.append(f"### `{sample['asset_id'][:12]}`  ·  {sample.get('shoot') or '?'}  ·  {sample.get('asset_type') or '?'}  ·  {ts_str}")
        if sample.get("semantic_subject"):
            sections.append(f"_Gemini subject: {sample['semantic_subject'][:140]}_")
        sections.append("")
        sections.append(f"![]({crop_path.relative_to(report_dir).as_posix()})")
        sections.append("")
        sections.append(f"| engine | hits | bib | latency | extracted text |")
        sections.append(f"|---|---|---|---|---|")
        def _row(name, hits, lat):
            if not hits:
                return f"| {name} | 0 | 0 | {lat:.2f}s | _(none)_ |"
            n_bib = sum(1 for h in hits if is_bib_text(h["text"]))
            txts = " · ".join(f"`{h['text']}` ({h['confidence']:.2f})" for h in hits[:8])
            if len(hits) > 8:
                txts += f" … +{len(hits) - 8}"
            return f"| {name} | {len(hits)} | {n_bib} | {lat:.2f}s | {txts} |"
        sections.append(_row("RapidOCR", rapid, el_rapid))
        sections.append(_row("Apple Vision", vision, el_vision))
        sections.append("")

    # Summary
    el_total = time.time() - t_start
    sections.insert(7, "")
    sections.insert(7, f"- Elapsed: {el_total:.1f}s total ({len(samples)} frames × 2 engines)")
    sections.insert(7, f"- Apple Vision: {counters_vision['total_hits']} text hits ({counters_vision['bib_hits']} bib-pattern), avg {counters_vision['total_latency']/max(1,counters_vision['frames']):.2f}s per frame")
    sections.insert(7, f"- RapidOCR: {counters_rapid['total_hits']} text hits ({counters_rapid['bib_hits']} bib-pattern), avg {counters_rapid['total_latency']/max(1,counters_rapid['frames']):.2f}s per frame")
    sections.insert(7, "**Aggregate:**")
    sections.insert(7, "")

    report_path = report_dir / "pilot.md"
    report_path.write_text("\n".join(sections))
    print(f"\n=== done in {el_total:.1f}s ===")
    print(f"report: {report_path}")
    print(f"crops:  {crops_dir}")
    print(f"\nopen \"{report_dir}\"")


if __name__ == "__main__":
    main()
