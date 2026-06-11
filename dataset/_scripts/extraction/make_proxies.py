#!/usr/bin/env python3
"""
make_proxies.py — Encode 720p H.264 proxies for cataloged video clips.

Outputs land per-shoot under `derivative media/<shoot>/<original-filename>` (with
`.R3D` → `.mp4` for raw camera output), mirroring the catalog source_path tree.

    catalog: dataset/assets/video/<asset_id>.video.json
    logs:    dataset/_runs/ingest_pipeline/proxy_runs.jsonl, proxy_errors.jsonl
    report:  dataset/_scripts/verify_ssd_match_report.json (default)

Encode parameters and FFMPEG_CMD_TEMPLATE produce CMD_HASH prefix db669c79afc9b0d3…

Stamps two discriminator fields onto the proxy block:
    backfill_batch:        "v6_added_2026-05-07"  (override with --batch-tag)
    source_added_in_v6:    true

Safety: refuses to run a full-report encode unless --allow-full-encode is
passed explicitly. Default usage requires --asset-ids.

Usage:
    python3 dataset/_scripts/extraction/make_proxies.py --asset-ids /tmp/added_609.txt
                                                 [--limit N] [--dry-run] [--force]
                                                 [--workers-per-ssd 2] [--report path]
                                                 [--batch-tag v6_added_2026-05-07]
"""
import os, sys, json, hashlib, subprocess, time, shutil, argparse, threading
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import defaultdict, Counter
from datetime import datetime, timezone

import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))  # shared modules live at _scripts root
from _paths import (
    VIDEO_CATALOG, DERIVATIVE_MEDIA, RUNS_DIR,
    proxy_output_path, workspace_tilde,
)

SCRIPT_DIR = Path(__file__).resolve().parent
CLIPS_DIR = VIDEO_CATALOG  # back-compat alias used throughout encode_one()
ERRORS_LOG = RUNS_DIR / "proxy_errors.jsonl"
RUNS_LOG = RUNS_DIR / "proxy_runs.jsonl"
REPORT_IN = SCRIPT_DIR / "verify_ssd_match_report.json"

DEFAULT_BATCH_TAG = "v6_added_2026-05-07"
DEST_SSD_LABEL = "the workspace SSD"

FFMPEG = "/opt/homebrew/bin/ffmpeg"
FFPROBE = "/opt/homebrew/bin/ffprobe"

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

R3D_EXT = ".r3d"
DISK_SPACE_REQUIRED_GB = 50  # 609 clips ≈ 24 hr → ~32 GB output worst-case
ENCODE_TIMEOUT_SEC = 7200

_log_lock = threading.Lock()


def now_utc_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def log_jsonl(path, obj):
    line = json.dumps(obj, default=str)
    with _log_lock:
        with open(path, "a") as f:
            f.write(line + "\n")


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


def probe_source(src_path):
    try:
        out = subprocess.run(
            [FFPROBE, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=codec_name,r_frame_rate",
             "-of", "json", str(src_path)],
            capture_output=True, text=True, timeout=60,
        )
        if out.returncode != 0 or not out.stdout.strip():
            return None
        data = json.loads(out.stdout)
        streams = data.get("streams") or []
        if not streams:
            return None
        s = streams[0]
        codec = s.get("codec_name")
        if not codec:
            return None
        return {"codec_name": codec, "fps": parse_fps(s.get("r_frame_rate"))}
    except Exception:
        return None


def probe_output(mp4_path):
    try:
        out = subprocess.run(
            [FFPROBE, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height:format=duration",
             "-of", "json", str(mp4_path)],
            capture_output=True, text=True, timeout=60,
        )
        if out.returncode != 0:
            return None
        data = json.loads(out.stdout)
        streams = data.get("streams") or []
        fmt = data.get("format") or {}
        if not streams or "duration" not in fmt:
            return None
        s = streams[0]
        return {
            "width": s.get("width"),
            "height": s.get("height"),
            "duration_sec": float(fmt["duration"]),
        }
    except Exception:
        return None


def encode_one(entry, batch_tag, force=False, dry_run=False):
    asset_id = entry["asset_id"]
    kind = entry["kind"]
    ssd_label = entry.get("_ssd")
    ssd_path = entry.get("_ssd_path")
    catalog_source_path = entry.get("source_path")
    filename = entry.get("filename", "")

    json_path = CLIPS_DIR / f"{asset_id}.video.json"

    if filename.lower().endswith(R3D_EXT):
        return {"status": "r3d_skipped", "asset_id": asset_id, "kind": kind,
                "ssd": ssd_label, "filename": filename}

    try:
        clip = json.loads(json_path.read_text())
    except Exception as e:
        return {"status": "error", "asset_id": asset_id, "kind": kind, "ssd": ssd_label,
                "reason": "json_read_failed", "detail": str(e),
                "json_path": str(json_path)}

    try:
        mp4_path = proxy_output_path(clip)
    except ValueError as e:
        return {"status": "error", "asset_id": asset_id, "kind": kind, "ssd": ssd_label,
                "reason": "source_path_missing", "detail": str(e)}
    mp4_path.parent.mkdir(parents=True, exist_ok=True)

    if not force:
        px = clip.get("proxy")
        if (px and px.get("ffmpeg_command_hash") == CMD_HASH
                and mp4_path.exists() and mp4_path.stat().st_size > 0):
            return {"status": "skipped", "asset_id": asset_id, "kind": kind,
                    "ssd": ssd_label, "reason": "already_current"}

    if not ssd_path or not os.path.exists(ssd_path):
        return {"status": "error", "asset_id": asset_id, "kind": kind, "ssd": ssd_label,
                "reason": "ssd_source_missing", "ssd_path": ssd_path}

    src_info = probe_source(ssd_path)
    if src_info is None:
        return {"status": "error", "asset_id": asset_id, "kind": kind, "ssd": ssd_label,
                "reason": "source_probe_failed_or_codec_missing",
                "ssd_path": ssd_path, "filename": filename}

    if dry_run:
        return {"status": "dry_run", "asset_id": asset_id, "kind": kind, "ssd": ssd_label,
                "ssd_path": ssd_path, "mp4_path": str(mp4_path),
                "source_codec": src_info["codec_name"], "source_fps": src_info["fps"]}

    t0 = time.time()
    try:
        proc = subprocess.run(
            [FFMPEG, "-hide_banner", "-loglevel", "error", "-nostdin",
             "-i", ssd_path,
             "-vf", VF_FILTER,
             "-c:v", "h264_videotoolbox", "-b:v", "3000k",
             "-maxrate", "4000k", "-bufsize", "8000k",
             "-force_key_frames", KEYFRAME_EXPR, "-g", "240",
             "-c:a", "aac", "-b:a", "128k", "-ac", "2",
             "-movflags", "+faststart",
             "-y", str(mp4_path)],
            capture_output=True, text=True, timeout=ENCODE_TIMEOUT_SEC,
        )
    except subprocess.TimeoutExpired:
        return {"status": "error", "asset_id": asset_id, "kind": kind, "ssd": ssd_label,
                "reason": "ffmpeg_timeout", "ssd_path": ssd_path}
    except Exception as e:
        return {"status": "error", "asset_id": asset_id, "kind": kind, "ssd": ssd_label,
                "reason": "ffmpeg_exception", "detail": str(e)}
    encode_secs = time.time() - t0

    if proc.returncode != 0:
        return {"status": "error", "asset_id": asset_id, "kind": kind, "ssd": ssd_label,
                "reason": "ffmpeg_failed", "returncode": proc.returncode,
                "stderr": (proc.stderr or "").strip()[:2000],
                "ssd_path": ssd_path}

    if not mp4_path.exists() or mp4_path.stat().st_size == 0:
        return {"status": "error", "asset_id": asset_id, "kind": kind, "ssd": ssd_label,
                "reason": "mp4_empty_or_missing", "ssd_path": ssd_path}

    out_info = probe_output(mp4_path)
    if out_info is None:
        return {"status": "error", "asset_id": asset_id, "kind": kind, "ssd": ssd_label,
                "reason": "mp4_probe_failed", "ssd_path": ssd_path}

    mp4_size = mp4_path.stat().st_size

    proxy = {
        "path": workspace_tilde(mp4_path),
        "format": "mp4",
        "codec": "h264",
        "width": out_info["width"],
        "height": out_info["height"],
        "video_bitrate_kbps": 3000,
        "keyframe_interval_sec": 1,
        "source_fps": src_info["fps"],
        "audio_codec": "aac",
        "audio_bitrate_kbps": 128,
        "filesize_bytes": mp4_size,
        "duration_sec": round(out_info["duration_sec"], 4),
        "encoded_at": now_utc_iso(),
        "ffmpeg_command_hash": CMD_HASH,
        "source_clip_path_at_encoding": catalog_source_path,
        "source_ssd_path_at_encoding": ssd_path,
        "source_ssd_label": ssd_label,
        "destination_ssd_label": DEST_SSD_LABEL,
        "backfill_batch": batch_tag,
        "source_added_in_v6": True,
    }

    clip["proxy"] = proxy

    try:
        atomic_write_json(json_path, clip)
    except Exception as e:
        return {"status": "error", "asset_id": asset_id, "kind": kind, "ssd": ssd_label,
                "reason": "json_write_failed", "detail": str(e)}

    return {"status": "encoded", "asset_id": asset_id, "kind": kind, "ssd": ssd_label,
            "duration_sec": out_info["duration_sec"], "mp4_size": mp4_size,
            "encode_secs": round(encode_secs, 2),
            "source_codec": src_info["codec_name"], "source_fps": src_info["fps"]}


def sample_across_ssds(work_list, n):
    by_ssd = defaultdict(list)
    for e in work_list:
        by_ssd[e["_ssd"]].append(e)
    ssds = sorted(by_ssd.keys())
    out, i = [], 0
    while len(out) < n:
        any_left = False
        for s in ssds:
            if i < len(by_ssd[s]):
                out.append(by_ssd[s][i])
                any_left = True
                if len(out) >= n:
                    break
        if not any_left:
            break
        i += 1
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None,
                    help="Process only N entries, sampled across SSDs (smoke test)")
    ap.add_argument("--asset-ids", type=str, default=None,
                    help="Comma-separated asset_ids OR path to a file with one per line.")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print what would be encoded without writing")
    ap.add_argument("--force", action="store_true",
                    help="Ignore idempotency, re-encode everything in scope")
    ap.add_argument("--workers-per-ssd", type=int, default=2)
    ap.add_argument("--report", type=str, default=None,
                    help=f"Path to verify report (defaults to {REPORT_IN.name})")
    ap.add_argument("--batch-tag", type=str, default=DEFAULT_BATCH_TAG,
                    help=f"Discriminator stamped on proxy block (default: {DEFAULT_BATCH_TAG})")
    ap.add_argument("--allow-full-encode", action="store_true",
                    help="Required to run without --asset-ids. Without this flag the "
                         "script aborts on full-report runs to avoid re-encoding "
                         "the entire matched set.")
    args = ap.parse_args()

    report_path = Path(args.report).resolve() if args.report else REPORT_IN

    print(f"=== make_proxies.py | {now_utc_iso()} ===")
    print(f"Catalog:    {CLIPS_DIR}")
    print(f"Logs:       {RUNS_DIR}")
    print(f"Proxy root: {DERIVATIVE_MEDIA} (per-shoot subfolders)")
    print(f"Batch tag:  {args.batch_tag}")
    print(f"ffmpeg_command_hash: {CMD_HASH[:16]}...")

    RUNS_DIR.mkdir(parents=True, exist_ok=True)

    if not CLIPS_DIR.exists():
        print(f"ABORT: catalog dir not found: {CLIPS_DIR}")
        sys.exit(1)

    if not args.dry_run:
        DERIVATIVE_MEDIA.mkdir(parents=True, exist_ok=True)
        if not os.access(DERIVATIVE_MEDIA, os.W_OK):
            print(f"ABORT: derivative media root {DERIVATIVE_MEDIA} is not writable.")
            sys.exit(1)

        free_gb = shutil.disk_usage(DERIVATIVE_MEDIA).free / 1e9
        print(f"Free space on the workspace SSD: {free_gb:.1f} GB")
        if free_gb < DISK_SPACE_REQUIRED_GB and not args.limit:
            print(f"ABORT: less than {DISK_SPACE_REQUIRED_GB} GB free on the workspace SSD.")
            sys.exit(1)

    if not report_path.exists():
        print(f"ABORT: {report_path} not found. Run verify_ssd_match.py first.")
        sys.exit(1)
    print(f"Report: {report_path}")
    report = json.loads(report_path.read_text())
    work_list = report["hash_matched"] + report["fallback_matched"]
    pre_video = len(work_list)
    work_list = [e for e in work_list if e.get("kind") == "video"]
    print(f"Loaded {pre_video} entries; {len(work_list)} video clips after filter "
          f"({len(report['hash_matched'])} hash + {len(report['fallback_matched'])} fallback)")

    if args.asset_ids:
        raw = args.asset_ids
        ids_path = Path(raw)
        if ids_path.exists() and ids_path.is_file():
            ids = [ln.strip() for ln in ids_path.read_text().splitlines() if ln.strip()]
        else:
            ids = [s.strip() for s in raw.split(",") if s.strip()]
        id_set = set(ids)
        before = len(work_list)
        work_list = [e for e in work_list if e["asset_id"] in id_set]
        found = {e["asset_id"] for e in work_list}
        missing = id_set - found
        print(f"Curated set: {len(work_list)}/{len(id_set)} asset_ids found (filtered from {before})")
        if missing:
            print(f"  WARNING: {len(missing)} asset_ids not in matched buckets:")
            for m in sorted(missing)[:20]:
                print(f"    {m}")
            if len(missing) > 20:
                print(f"    ... and {len(missing)-20} more")
    elif not args.allow_full_encode:
        print("ABORT: no --asset-ids supplied. Refusing full-report encode without "
              "--allow-full-encode (safety: would attempt to encode the entire "
              "matched set, ~4,300 clips).")
        sys.exit(1)

    if args.limit:
        work_list = sample_across_ssds(work_list, args.limit)
        print(f"Sampling {len(work_list)} entries across SSDs for smoke test")

    if not work_list:
        print("Nothing to do.")
        sys.exit(0)

    by_ssd = defaultdict(list)
    for e in work_list:
        by_ssd[e["_ssd"]].append(e)
    print(f"\nWork by SSD:")
    for ssd in sorted(by_ssd):
        print(f"  {ssd:<14s}: {len(by_ssd[ssd])}")

    log_jsonl(RUNS_LOG, {
        "event": "run_start",
        "timestamp": now_utc_iso(),
        "ffmpeg_command_hash": CMD_HASH,
        "proxy_root": str(DERIVATIVE_MEDIA),
        "destination_ssd_label": DEST_SSD_LABEL,
        "batch_tag": args.batch_tag,
        "total_entries": len(work_list),
        "by_ssd": {ssd: len(L) for ssd, L in by_ssd.items()},
        "limit": args.limit,
        "dry_run": args.dry_run,
        "force": args.force,
        "workers_per_ssd": args.workers_per_ssd,
    })

    counters = Counter()
    per_ssd_done = Counter()
    t_start = time.time()
    last_print_n = [0]

    def handle_result(res):
        counters[res["status"]] += 1
        ssd = res.get("ssd") or "?"
        per_ssd_done[ssd] += 1
        done = sum(counters.values())
        if res["status"] == "error":
            log_jsonl(ERRORS_LOG, {**res, "timestamp": now_utc_iso()})
        elif res["status"] == "r3d_skipped":
            log_jsonl(ERRORS_LOG, {**res, "reason": "red_raw_unsupported",
                                   "timestamp": now_utc_iso()})
        if done - last_print_n[0] >= 25 or done == len(work_list):
            elapsed = time.time() - t_start
            rate = done / elapsed if elapsed > 0 else 0
            eta = (len(work_list) - done) / rate if rate > 0 else 0
            ssd_str = "  ".join(f"{s}: {per_ssd_done[s]}/{len(by_ssd[s])}"
                                for s in sorted(by_ssd))
            print(f"[{done:>5}/{len(work_list)}] {100*done/len(work_list):5.1f}%  "
                  f"elapsed {elapsed/60:5.1f}m  ETA {eta/60:5.1f}m  "
                  f"{ssd_str}  errors: {counters['error']}")
            last_print_n[0] = done

    executors = {}
    futures = []
    for ssd, entries in by_ssd.items():
        ex = ThreadPoolExecutor(max_workers=args.workers_per_ssd,
                                thread_name_prefix=f"ssd-{ssd}")
        executors[ssd] = ex
        for entry in entries:
            futures.append(ex.submit(encode_one, entry,
                                     args.batch_tag, args.force, args.dry_run))

    try:
        for fut in as_completed(futures):
            try:
                res = fut.result()
            except Exception as e:
                res = {"status": "error", "reason": "future_exception", "detail": str(e)}
            handle_result(res)
    finally:
        for ex in executors.values():
            ex.shutdown(wait=True)

    elapsed = time.time() - t_start
    log_jsonl(RUNS_LOG, {
        "event": "run_end",
        "timestamp": now_utc_iso(),
        "elapsed_sec": round(elapsed, 1),
        "total": len(work_list),
        "by_status": dict(counters),
        "by_ssd_completed": dict(per_ssd_done),
    })

    print("\n=== Summary ===")
    print(f"Elapsed: {elapsed/60:.1f} min")
    for status, n in counters.most_common():
        print(f"  {status:<18}: {n}")
    print(f"Errors logged to: {ERRORS_LOG}")
    print(f"Run summary in: {RUNS_LOG}")


if __name__ == "__main__":
    main()
