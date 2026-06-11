#!/usr/bin/env python3
"""
transcode_r3d.py — Two-stage R3D -> H.264 720p proxy pipeline.

Stage 1 (REDline): R3D -> ProRes 422 Proxy 720p (.mov), BT.709 gamma + colorspace,
                   audio embedded from sidecar .wav if present in the .RDC bundle.
Stage 2 (ffmpeg):  ProRes -> H.264 .mp4 matching make_proxies.py's locked CMD_HASH
                   spec exactly (h264_videotoolbox 3000k VBR, AAC 128k stereo, +faststart).

Stage 2 ffmpeg invocation is byte-identical to make_proxies.py so the same CMD_HASH
(prefix db669c79afc9b0d3...) stamps into the proxy block. Stage 1 details captured in
extra discriminator fields (transcode_method, redline_intermediate_codec, etc).

Output: per-shoot under `derivative media/<shoot>/.../<stem>.mp4` (mirroring the
catalog source_path tree, with `.R3D` rewritten to `.mp4`). Proxy block is written
into the catalog at `dataset/assets/video/<asset_id>.video.json`.

Usage:
    python3 transcode_r3d.py --asset-ids /tmp/r3d_asset_ids.txt
                             [--limit N] [--force]
                             [--report dataset/_scripts/verify_ssd_match_report.json]
"""
import os, sys, json, hashlib, subprocess, time, argparse, glob
from pathlib import Path
from datetime import datetime, timezone

import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))  # shared modules live at _scripts root
from _paths import (
    VIDEO_CATALOG, RUNS_DIR,
    proxy_output_path, workspace_tilde,
)

SCRIPT_DIR = Path(__file__).resolve().parent
CLIPS_DIR = VIDEO_CATALOG
RUNS_LOG = RUNS_DIR / "transcode_r3d_runs.jsonl"
ERRORS_LOG = RUNS_DIR / "transcode_r3d_errors.jsonl"
REPORT_IN = SCRIPT_DIR / "verify_ssd_match_report.json"

REDLINE = "/Applications/REDCINE-X Professional/REDCINE-X PRO.app/Contents/MacOS/REDline"
FFMPEG = "/opt/homebrew/bin/ffmpeg"
FFPROBE = "/opt/homebrew/bin/ffprobe"

TEMP_DIR = Path("/tmp/redline")
DEFAULT_BATCH_TAG = "r3d_redline_2026-05-08"
MIN_R3D_SIZE_BYTES = 5_000_000  # files smaller than 5 MB are aborted/empty captures
DEST_SSD_LABEL = "the workspace SSD"

# --- ffmpeg stage 2: byte-identical template from make_proxies.py ---
VF_FILTER = (
    "scale='if(gt(iw,ih),min(1280,iw),-2)':'if(gt(iw,ih),-2,min(1280,ih))',"
    "scale=trunc(iw/2)*2:trunc(ih/2)*2"
)
KEYFRAME_EXPR = "expr:gte(t,n_forced*1)"
FFMPEG_CMD_TEMPLATE = (
    "ffmpeg -hide_banner -loglevel error -nostdin "
    "-i {src} "
    f"-vf {VF_FILTER} "
    "-c:v h264_videotoolbox -b:v 3000k -maxrate 4000k -bufsize 8000k "
    f"-force_key_frames {KEYFRAME_EXPR} -g 240 "
    "-c:a aac -b:a 128k -ac 2 "
    "-movflags +faststart "
    "-y {dst}"
)
CMD_HASH = hashlib.sha256(FFMPEG_CMD_TEMPLATE.encode()).hexdigest()
EXPECTED_CMD_HASH_PREFIX = "db669c79afc9b0d3"


def now_utc_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def log_jsonl(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(obj, default=str) + "\n")


def atomic_write_json(path, obj):
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=2)
    os.replace(tmp, path)


def parse_fps(rate_str):
    if not rate_str or rate_str in ("0/0", "N/A"):
        return None
    if "/" in rate_str:
        num, den = rate_str.split("/", 1)
        try:
            n, d = float(num), float(den)
            if d == 0:
                return None
            return round(n / d, 3)
        except ValueError:
            return None
    try:
        return round(float(rate_str), 3)
    except ValueError:
        return None


def probe_mp4(path):
    """Return {width, height, duration_sec, fps, has_audio} for a mp4."""
    try:
        out = subprocess.run(
            [FFPROBE, "-v", "error",
             "-show_entries", "stream=codec_type,codec_name,width,height,r_frame_rate:format=duration",
             "-of", "json", str(path)],
            capture_output=True, text=True, timeout=60,
        )
        if out.returncode != 0:
            return None
        data = json.loads(out.stdout)
        streams = data.get("streams") or []
        v = next((s for s in streams if s.get("codec_type") == "video"), None)
        a = next((s for s in streams if s.get("codec_type") == "audio"), None)
        if not v:
            return None
        dur = float((data.get("format") or {}).get("duration") or 0.0)
        return {
            "width": int(v.get("width") or 0),
            "height": int(v.get("height") or 0),
            "duration_sec": dur,
            "fps": parse_fps(v.get("r_frame_rate")),
            "has_audio": a is not None,
            "video_codec": v.get("codec_name"),
            "audio_codec": (a or {}).get("codec_name"),
        }
    except Exception:
        return None


def find_audio_sidecar(rdc_bundle_path, segment_idx):
    """Find matching .wav sidecar inside the .RDC bundle.
    RED naming: B001_C020_0901YT_A01_001.wav  (track A01, segment 001)
    For multi-segment clips, pair the .wav segment number with the .R3D segment."""
    bundle = Path(rdc_bundle_path)
    if not bundle.is_dir():
        return None
    # Prefer matching segment number; fall back to first .wav if only one exists.
    candidates = sorted(bundle.glob("*_A0*_*.wav"))
    if not candidates:
        return None
    seg = f"_{segment_idx:03d}.wav"
    for c in candidates:
        if c.name.endswith(seg):
            return str(c)
    if len(candidates) == 1:
        return str(candidates[0])
    return str(candidates[0])  # best effort


def parse_segment_index(r3d_path):
    """B001_C001_0901HS_001.R3D -> 1, _002.R3D -> 2, etc."""
    name = Path(r3d_path).stem  # strips .R3D
    parts = name.split("_")
    try:
        return int(parts[-1])
    except (ValueError, IndexError):
        return 1


_BUNDLE_TOTAL_FRAMES_CACHE = {}


def get_bundle_total_frames(rdc_bundle_path):
    """Run REDline --printMeta on segment 1 of the bundle, parse Total Frames.
    Returns int frame count or None if probe fails.
    Cached per bundle (one REDline call per multi-segment bundle, not per segment).
    """
    key = str(rdc_bundle_path)
    if key in _BUNDLE_TOTAL_FRAMES_CACHE:
        return _BUNDLE_TOTAL_FRAMES_CACHE[key]
    bundle = Path(rdc_bundle_path)
    seg1s = sorted(bundle.glob("*_001.R3D"))
    if not seg1s:
        return None
    proc = subprocess.run(
        [REDLINE, "--i", str(seg1s[0]), "--printMeta", "3", "--useMeta"],
        capture_output=True, text=True, timeout=60,
    )
    # NOTE: REDline returns exit code 1 on --printMeta-only invocations (no encode output
    # produced), even when the metadata prints correctly. Don't gate on returncode here —
    # rely on stdout content instead.
    lines = [ln for ln in proc.stdout.splitlines() if ln.strip() and "Warning:" not in ln]
    if len(lines) < 2:
        return None
    header = lines[0].split(",")
    data = lines[1].split(",")
    try:
        idx = header.index("Total Frames")
        total = int(data[idx])
        _BUNDLE_TOTAL_FRAMES_CACHE[key] = total
        return total
    except (ValueError, IndexError):
        return None


def compute_segment_range(r3d_path, rdc_bundle_path):
    """For a multi-segment bundle, compute (start_frame, frame_count) for this segment
    using file-size proportions of the .R3D files. RED splits at the format's ~4 GB
    boundary, so file sizes are very close to constant-bitrate proportional.

    Returns (start_frame, frame_count) or None if bundle is single-segment / probe fails.
    """
    bundle = Path(rdc_bundle_path)
    segs = sorted(bundle.glob("*.R3D"))
    if len(segs) <= 1:
        return None

    sizes = [s.stat().st_size for s in segs]
    total_bytes = sum(sizes)
    if total_bytes == 0:
        return None

    total_frames = get_bundle_total_frames(rdc_bundle_path)
    if total_frames is None or total_frames <= 0:
        return None

    # Allocate per-segment frame counts; absorb rounding error in the last segment so sum == total.
    frames_per = [int(round((sz / total_bytes) * total_frames)) for sz in sizes]
    frames_per[-1] = total_frames - sum(frames_per[:-1])
    if frames_per[-1] <= 0:
        # Defensive: if rounding pushed last segment to <=0, redistribute.
        frames_per = [int((sz / total_bytes) * total_frames) for sz in sizes]
        frames_per[-1] = total_frames - sum(frames_per[:-1])

    target = Path(r3d_path).resolve()
    try:
        seg_idx = [s.resolve() for s in segs].index(target)
    except ValueError:
        return None

    start = sum(frames_per[:seg_idx])
    count = frames_per[seg_idx]
    if count <= 0:
        return None
    return (start, count, total_frames, len(segs))


def transcode_one(asset_id, ssd_path, catalog_source_path, ssd_label, batch_tag, force):
    """Run REDline + ffmpeg + write catalog proxy block. Return result dict."""
    src = Path(ssd_path)
    json_path = CLIPS_DIR / f"{asset_id}.video.json"

    if not src.exists():
        return {"status": "error", "asset_id": asset_id, "reason": "source_missing", "ssd_path": str(src)}

    src_size = src.stat().st_size
    if src_size < MIN_R3D_SIZE_BYTES:
        return {"status": "skipped", "asset_id": asset_id, "reason": "r3d_too_small",
                "size_bytes": src_size, "ssd_path": str(src)}

    if not json_path.exists():
        return {"status": "error", "asset_id": asset_id, "reason": "catalog_missing", "json_path": str(json_path)}

    with open(json_path) as f:
        clip = json.load(f)

    try:
        mp4_out = proxy_output_path(clip)
    except ValueError as e:
        return {"status": "error", "asset_id": asset_id, "reason": "source_path_missing", "detail": str(e)}
    mp4_out.parent.mkdir(parents=True, exist_ok=True)
    if not force and mp4_out.exists() and "proxy" in clip and clip["proxy"].get("ffmpeg_command_hash") == CMD_HASH:
        return {"status": "skipped", "asset_id": asset_id, "reason": "already_encoded"}

    # --- Audio detection + segment range (for multi-segment .RDC bundles) ---
    rdc_bundle = src.parent
    seg_idx = parse_segment_index(src)
    audio_sidecar = find_audio_sidecar(rdc_bundle, seg_idx)
    seg_range = compute_segment_range(str(src), str(rdc_bundle))
    # seg_range is None for single-segment bundles, (start_frame, frame_count, total_bundle_frames, n_segments) otherwise.

    # --- Stage 1: REDline ---
    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    intermediate = TEMP_DIR / f"{asset_id}.mov"
    if intermediate.exists():
        intermediate.unlink()

    redline_cmd = [
        REDLINE,
        "--i", str(src),
        "--o", asset_id,
        "--outDir", str(TEMP_DIR),
        "--format", "201",     # Apple ProRes
        "--PRcodec", "3",      # ProRes 422 Proxy
        "--resizeX", "1280", "--resizeY", "720", "--fit", "1",
        "--gammaCurve", "1",   # BT.709
        "--colorSpace", "1",   # BT.709
        "--useMeta",
    ]
    if seg_range is not None:
        start_frame, frame_count, _, _ = seg_range
        redline_cmd += ["--start", str(start_frame), "--frameCount", str(frame_count)]
    if audio_sidecar:
        redline_cmd += ["--audio", audio_sidecar]

    t0 = time.time()
    proc = subprocess.run(redline_cmd, capture_output=True, text=True, timeout=1800)
    redline_secs = time.time() - t0

    if proc.returncode != 0 or not intermediate.exists() or intermediate.stat().st_size == 0:
        return {"status": "error", "asset_id": asset_id, "reason": "redline_failed",
                "returncode": proc.returncode,
                "stderr": (proc.stderr or "").strip()[-2000:],
                "stdout_tail": (proc.stdout or "").strip()[-500:],
                "redline_secs": round(redline_secs, 2)}

    # --- Stage 2: ffmpeg (matches make_proxies.py CMD_HASH) ---
    ffmpeg_cmd = [
        FFMPEG, "-hide_banner", "-loglevel", "error", "-nostdin",
        "-i", str(intermediate),
        "-vf", VF_FILTER,
        "-c:v", "h264_videotoolbox", "-b:v", "3000k",
        "-maxrate", "4000k", "-bufsize", "8000k",
        "-force_key_frames", KEYFRAME_EXPR, "-g", "240",
        "-c:a", "aac", "-b:a", "128k", "-ac", "2",
        "-movflags", "+faststart",
        "-y", str(mp4_out),
    ]
    t0 = time.time()
    proc = subprocess.run(ffmpeg_cmd, capture_output=True, text=True, timeout=3600)
    ffmpeg_secs = time.time() - t0

    # Cleanup intermediate immediately to save disk
    try:
        intermediate.unlink()
    except Exception:
        pass

    if proc.returncode != 0 or not mp4_out.exists() or mp4_out.stat().st_size == 0:
        return {"status": "error", "asset_id": asset_id, "reason": "ffmpeg_failed",
                "returncode": proc.returncode,
                "stderr": (proc.stderr or "").strip()[-2000:]}

    info = probe_mp4(mp4_out)
    if info is None:
        return {"status": "error", "asset_id": asset_id, "reason": "mp4_probe_failed"}

    proxy = {
        "path": workspace_tilde(mp4_out),
        "format": "mp4",
        "codec": "h264",
        "width": info["width"],
        "height": info["height"],
        "video_bitrate_kbps": 3000,
        "keyframe_interval_sec": 1,
        "source_fps": info["fps"],
        "audio_codec": "aac" if info["has_audio"] else None,
        "audio_bitrate_kbps": 128 if info["has_audio"] else None,
        "filesize_bytes": mp4_out.stat().st_size,
        "duration_sec": round(info["duration_sec"], 4),
        "encoded_at": now_utc_iso(),
        "ffmpeg_command_hash": CMD_HASH,
        "source_clip_path_at_encoding": catalog_source_path,
        "source_ssd_path_at_encoding": str(src),
        "source_ssd_label": ssd_label,
        "destination_ssd_label": DEST_SSD_LABEL,
        "backfill_batch": batch_tag,
        "source_added_in_v6": True,
        # R3D-specific discriminators
        "transcode_method": "redline_to_h264_two_stage",
        "redline_intermediate_codec": "prores_422_proxy",
        "redline_color_space": "BT.709",
        "redline_gamma": "BT.709",
        "audio_sidecar_used": audio_sidecar is not None,
        "audio_sidecar_path": audio_sidecar,
        "rdc_bundle_segments": seg_range[3] if seg_range else 1,
        "rdc_segment_start_frame": seg_range[0] if seg_range else 0,
        "rdc_segment_frame_count": seg_range[1] if seg_range else None,
    }

    clip["proxy"] = proxy
    try:
        atomic_write_json(json_path, clip)
    except Exception as e:
        return {"status": "error", "asset_id": asset_id, "reason": "json_write_failed", "detail": str(e)}

    return {"status": "encoded", "asset_id": asset_id,
            "duration_sec": info["duration_sec"], "mp4_size": mp4_out.stat().st_size,
            "redline_secs": round(redline_secs, 2),
            "ffmpeg_secs": round(ffmpeg_secs, 2),
            "audio_sidecar_used": audio_sidecar is not None,
            "has_audio_in_output": info["has_audio"]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--asset-ids", required=True, help="File with one asset_id per line")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--force", action="store_true", help="Re-encode even if proxy already exists")
    ap.add_argument("--report", default=str(REPORT_IN))
    ap.add_argument("--batch-tag", default=DEFAULT_BATCH_TAG)
    args = ap.parse_args()

    if not CMD_HASH.startswith(EXPECTED_CMD_HASH_PREFIX):
        print(f"FATAL: CMD_HASH drift. expected prefix {EXPECTED_CMD_HASH_PREFIX} got {CMD_HASH[:16]}")
        sys.exit(2)

    RUNS_DIR.mkdir(parents=True, exist_ok=True)

    with open(args.asset_ids) as f:
        wanted = [line.strip() for line in f if line.strip()]

    report = json.load(open(args.report))
    by_aid = {}
    for bucket in ["hash_matched", "fallback_matched"]:
        for e in report[bucket]:
            by_aid[e["asset_id"]] = e

    queue = []
    for aid in wanted:
        e = by_aid.get(aid)
        if not e:
            queue.append({"asset_id": aid, "_status": "no_match"})
            continue
        queue.append({
            "asset_id": aid,
            "ssd_path": e["_ssd_path"],
            "ssd_label": e["_ssd"],
            "catalog_source_path": e["source_path"],
        })

    if args.limit:
        queue = queue[:args.limit]

    print(f"Queue: {len(queue)} clips")
    print(f"CMD_HASH: {CMD_HASH[:16]}... (matches make_proxies.py)")
    print(f"Batch tag: {args.batch_tag}")
    print()

    summary = {"encoded": 0, "skipped": 0, "error": 0, "no_match": 0}
    t_start = time.time()

    for i, item in enumerate(queue, 1):
        if item.get("_status") == "no_match":
            summary["no_match"] += 1
            log_jsonl(ERRORS_LOG, {"asset_id": item["asset_id"], "reason": "no_match_in_report",
                                   "ts": now_utc_iso()})
            print(f"[{i:3d}/{len(queue)}] {item['asset_id'][:16]} NO_MATCH")
            continue

        result = transcode_one(
            item["asset_id"], item["ssd_path"], item["catalog_source_path"],
            item["ssd_label"], args.batch_tag, args.force,
        )
        result["ts"] = now_utc_iso()
        log_jsonl(RUNS_LOG, result)
        if result["status"] == "error":
            log_jsonl(ERRORS_LOG, result)

        summary[result["status"]] = summary.get(result["status"], 0) + 1
        tag = result["status"].upper()
        extra = ""
        if result["status"] == "encoded":
            extra = f" {result['duration_sec']:.1f}s, {result['mp4_size']/1e6:.1f}MB, " \
                    f"redline={result['redline_secs']:.1f}s ffmpeg={result['ffmpeg_secs']:.1f}s " \
                    f"audio={'Y' if result['has_audio_in_output'] else 'N'}"
        elif result["status"] == "skipped":
            extra = f" ({result['reason']})"
        elif result["status"] == "error":
            extra = f" ({result['reason']})"
        print(f"[{i:3d}/{len(queue)}] {item['asset_id'][:16]} {tag}{extra}")

    elapsed = time.time() - t_start
    print()
    print("=" * 60)
    print(f"Done in {elapsed/60:.1f} min")
    for k, v in summary.items():
        print(f"  {k:<10}: {v}")
    print(f"Run log:    {RUNS_LOG}")
    print(f"Error log:  {ERRORS_LOG}")


if __name__ == "__main__":
    main()
