#!/usr/bin/env python3
"""
verify_ssd_match.py — Walk the mirror SSDs, hash files, match against the video and
audio catalogs at dataset/assets/. Output a JSON match report consumed by
extract_audio.py and make_proxies.py.

Relies on the mirror-SSD volumes (MOUNTS below) being mounted.
Skips designated B-roll folders + Windows system dirs by default.

Outputs:
    dataset/_scripts/verify_ssd_match_report.json — match buckets keyed by asset_id
"""
import os, sys, json, hashlib, time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from collections import Counter, defaultdict

import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))  # shared modules live at _scripts root
from _paths import VIDEO_CATALOG, AUDIO_CATALOG

SCRIPT_DIR = Path(__file__).resolve().parent
REPORT_OUT = SCRIPT_DIR / "verify_ssd_match_report.json"

SSDS = [
    ("Backup-1", "/Volumes/Backup-1"),  # your mirror-SSD volume names
    ("Backup-2", "/Volumes/Backup-2"),
    ("Backup-3", "/Volumes/Backup-3"),
]

VIDEO_EXTS = {".mp4", ".mov", ".mxf", ".mts", ".mkv", ".avi", ".m4v", ".braw", ".r3d"}
AUDIO_EXTS = {".wav", ".mp3", ".m4a", ".flac", ".aif", ".aiff"}
WANTED = VIDEO_EXTS | AUDIO_EXTS

SKIP_DIRS = {
    "$RECYCLE.BIN", "System Volume Information",
    ".Spotlight-V100", ".fseventsd", ".TemporaryItems", ".Trashes",
    ".DocumentRevisions-V100",
    "AI_Proxies",  # editor proxies — large but not source content
}

# Catalog top_level folders we deliberately don't extract audio from
EXCLUDE_BROLL = {
    "<b-roll-folder-1>", "<b-roll-folder-2>", "<b-roll-folder-3>",
    "<b-roll-folder-4>", "<b-roll-folder-5>", "<b-roll-folder-6>",  # set to your corpus
}
DEFERRED_FOLDER = "<deferred-folder>"  # a shoot folder whose media lives on a separate drive

HEAD_TAIL = 1_000_000


def partial_hash(path, size):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        if size <= 2 * HEAD_TAIL:
            while b := f.read(1 << 20):
                h.update(b)
        else:
            h.update(f.read(HEAD_TAIL))
            f.seek(-HEAD_TAIL, os.SEEK_END)
            h.update(f.read(HEAD_TAIL))
    h.update(size.to_bytes(8, "big"))
    return h.hexdigest()


def walk_and_hash(label, root):
    if not os.path.isdir(root):
        return label, [], f"NOT MOUNTED: {root}"
    found = []
    for dp, dn, fn in os.walk(root, followlinks=False):
        dn[:] = [d for d in dn if d not in SKIP_DIRS]
        for f in fn:
            ext = os.path.splitext(f)[1].lower()
            if ext not in WANTED:
                continue
            fp = os.path.join(dp, f)
            try:
                st = os.stat(fp)
                if st.st_size < 50_000:
                    continue  # truly empty/placeholder; phone-dump clips can be < 1 MB
                found.append((partial_hash(fp, st.st_size), fp, st.st_size, ext))
            except OSError:
                pass
    return label, found, None


def relative_top_level(source_path):
    """Extract top-level shoot folder from a Windows source_path."""
    return source_path.replace("D:\\Project\\", "").split("\\")[0]


def main():
    print("=" * 72)
    print("Workspace SSD Match Verification (v6 schema)")
    print("=" * 72)

    print("\nPhase 1: Walk + hash SSDs in parallel...")
    t0 = time.time()
    ssd_index = {}
    ssd_per = {}
    with ThreadPoolExecutor(max_workers=3) as ex:
        for label, found, err in ex.map(lambda x: walk_and_hash(*x), SSDS):
            if err:
                print(f"  [{label}] ERROR: {err}")
                ssd_per[label] = 0
                continue
            ssd_per[label] = len(found)
            for h, fp, sz, ext in found:
                ssd_index[h] = {"ssd": label, "path": fp, "size": sz, "ext": ext}
    print(f"  Done in {time.time()-t0:.1f}s. {len(ssd_index)} unique-by-hash files.")
    for label, n in ssd_per.items():
        print(f"    {label:<14}: {n}")

    print(f"\nPhase 2: Load catalog from {VIDEO_CATALOG} + {AUDIO_CATALOG}...")
    videos = []
    for p in VIDEO_CATALOG.glob("*.video.json"):
        if p.name.startswith("._"): continue  # macOS AppleDouble sidecar
        videos.append(json.loads(p.read_text()))
    audios = []
    for p in AUDIO_CATALOG.glob("*.audio.json"):
        if p.name.startswith("._"): continue  # macOS AppleDouble sidecar
        audios.append(json.loads(p.read_text()))
    print(f"  {len(videos)} video records, {len(audios)} audio records")

    bn_map = defaultdict(list)
    for h, info in ssd_index.items():
        bn_map[os.path.basename(info["path"]).lower()].append(
            (info["ssd"], info["path"], info["size"], h))

    print("\nPhase 3: Match catalog → SSD index...")

    def fallback_for(asset):
        bn = (asset.get("filename") or "").lower()
        sz = asset.get("filesize_bytes") or 0
        if not sz:
            return None
        cands = bn_map.get(bn, [])
        good = [c for c in cands
                if abs(c[2] - sz) / max(sz, 1) < 0.01 or abs(c[2] - sz) < 5_000_000]
        if not good:
            return None
        if len(good) == 1:
            return {"ssd": good[0][0], "path": good[0][1], "method": "single"}
        rel = (asset.get("path_metadata") or {}).get("shoot_label") or ""
        for ssd, p, csz, h in good:
            if rel and rel in p:
                return {"ssd": ssd, "path": p, "method": "multi_folder_overlap"}
        good.sort(key=lambda c: abs(c[2] - sz))
        return {"ssd": good[0][0], "path": good[0][1], "method": "multi_closest"}

    cat_results = {
        "hash_matched": [],
        "fallback_matched": [],
        "deferred_dji": [],
        "excluded_broll": [],
        "still_unmatched": [],
    }

    for asset, kind in [(v, "video") for v in videos] + [(a, "audio") for a in audios]:
        sp = asset.get("source_path") or ""
        top = relative_top_level(sp)
        aid = asset["asset_id"]
        entry = {
            "asset_id": aid, "kind": kind,
            "source_path": sp,
            "filename": asset.get("filename"),
            "filesize_bytes": asset.get("filesize_bytes"),
            "duration_sec": (asset.get("ffprobe") or {}).get("duration_sec"),
            "top_level": top,
            "has_audio_extract": bool(asset.get("audio_extract")
                                      and asset["audio_extract"].get("ffmpeg_command_hash")),
            "has_machine_transcript": bool(asset.get("has_machine_transcript")),
        }
        if top in EXCLUDE_BROLL:
            cat_results["excluded_broll"].append(entry)
        elif top == DEFERRED_FOLDER and aid not in ssd_index:
            cat_results["deferred_dji"].append(entry)
        elif aid in ssd_index:
            entry["_ssd"] = ssd_index[aid]["ssd"]
            entry["_ssd_path"] = ssd_index[aid]["path"]
            cat_results["hash_matched"].append(entry)
        else:
            fb = fallback_for(asset)
            if fb:
                entry["_ssd"] = fb["ssd"]
                entry["_ssd_path"] = fb["path"]
                entry["_fallback_method"] = fb["method"]
                cat_results["fallback_matched"].append(entry)
            else:
                cat_results["still_unmatched"].append(entry)

    def hr(L):
        return sum((e.get("duration_sec") or 0) for e in L) / 3600

    def gb(L):
        return sum((e.get("filesize_bytes") or 0) for e in L) / 1e9

    print("\n" + "=" * 72)
    print("RESULTS")
    print("=" * 72)
    print(f"\n{'Bucket':<22} {'Count':>7} {'Hours':>10} {'Source GB':>12}")
    print("-" * 72)
    for b in ["hash_matched", "fallback_matched", "deferred_dji",
              "excluded_broll", "still_unmatched"]:
        L = cat_results[b]
        print(f"  {b:<20} {len(L):>7} {hr(L):>10.1f} {gb(L):>12.1f}")

    matched = cat_results["hash_matched"] + cat_results["fallback_matched"]
    print(f"\nTotal matched: {len(matched)} ({hr(matched):.1f} hr)")
    by_ssd = Counter(e["_ssd"] for e in matched)
    for ssd, n in sorted(by_ssd.items()):
        print(f"  {ssd:<14}: {n}")

    # What still needs work
    needs_extract = [e for e in matched if not e["has_audio_extract"]]
    needs_transcribe = [e for e in matched if e["has_audio_extract"]
                        and not e["has_machine_transcript"]]
    print(f"\nWork remaining (matched only, B-roll excluded):")
    print(f"  needs WAV extraction:     {len(needs_extract)}")
    print(f"  needs transcription only: {len(needs_transcribe)}")

    if cat_results["still_unmatched"]:
        print(f"\nSTILL_UNMATCHED ({len(cat_results['still_unmatched'])}) — first 10:")
        for u in cat_results["still_unmatched"][:10]:
            sz = (u.get("filesize_bytes") or 0) / 1e6
            print(f"  ({sz:7.1f} MB) {u['source_path']}")

    REPORT_OUT.write_text(json.dumps({
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "ssd_index_size": len(ssd_index),
        "ssd_per": ssd_per,
        "summary": {b: len(cat_results[b]) for b in cat_results},
        "by_ssd": dict(by_ssd),
        **cat_results,
    }, indent=2, default=str))
    print(f"\nReport: {REPORT_OUT}")


if __name__ == "__main__":
    main()
