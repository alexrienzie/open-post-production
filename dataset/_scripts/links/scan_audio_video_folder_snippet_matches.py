"""
For each catalog **video**, find **audio** rows whose `source_path` lies under the
same shoot directory as the video (the video file's parent folder, and any
subfolders like `Audio\\`), and whose **filename** shares a **snippet** with the
video filename (e.g. `C8859` ↔ `C8859 Audio.mp3`, `...8859...`).

Read-only report — does not mutate catalog.

Output:
  _audit/audio_video_folder_snippet_matches.csv

Usage:
  python _scripts/links/scan_audio_video_folder_snippet_matches.py
  python _scripts/links/scan_audio_video_folder_snippet_matches.py --interview-video-only
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
VIDEO_DIR = ROOT / "assets/video"
AUDIO_DIR = ROOT / "assets/audio"
OUT_CSV = ROOT / "_audit/audio_video_folder_snippet_matches.csv"
OUT_INTERVIEW = ROOT / "_audit/audio_video_folder_snippet_matches_interview_videos.csv"


def win_path(p: str) -> str:
    return str(Path(p)).replace("/", "\\")


def path_cf(p: str) -> str:
    return win_path(p).casefold()


def video_linked_audio_ids(rec: dict) -> set[str]:
    out: set[str] = set()
    la = rec.get("linked_assets")
    if not isinstance(la, dict):
        return out
    for e in la.get("audio") or []:
        if isinstance(e, dict):
            tid = e.get("target_asset_id")
            if isinstance(tid, str) and len(tid) == 64:
                out.add(tid)
    return out


def snippet_tokens(filename: str) -> list[str]:
    """
    Tokens must appear as substrings of the audio filename (case-insensitive).
    Conservative: avoid very short numeric-only tokens.
    """
    stem = Path(filename).stem.casefold()
    toks: set[str] = set()

    if len(stem) >= 4:
        toks.add(stem)

    # Sony-style C1234 / c9876
    m = re.search(r"(c)(\d{3,6})\b", stem, re.I)
    if m:
        toks.add(m.group(0).casefold())
        toks.add(m.group(2))  # digits only

    # Long digit runs (Zoom, dates embedded, etc.)
    for run in re.findall(r"\d{4,}", stem):
        toks.add(run)

    # ARRI-style AxxxCyyy — use C-part if present
    m2 = re.search(r"c(\d{3,6})\b", stem, re.I)
    if m2:
        toks.add("c" + m2.group(1))
        toks.add(m2.group(1))

    # IMG_1234
    m3 = re.search(r"img_(\d{4,})", stem, re.I)
    if m3:
        toks.add("img_" + m3.group(1))
        toks.add(m3.group(1))

    # Drop overly short / generic
    out: list[str] = []
    for t in sorted(toks, key=lambda x: -len(x)):
        if not t:
            continue
        if len(t) < 4 and t.isdigit():
            continue
        if t in ("1080", "1920", "2024", "2025", "2023"):
            continue
        out.append(t)
    return out


def audio_under_video_dir(audio_cf: str, video_dir_cf: str) -> bool:
    if not video_dir_cf:
        return False
    if audio_cf == video_dir_cf:
        return True
    return audio_cf.startswith(video_dir_cf + "\\")


def load_audios() -> list[tuple[str, str, str, str]]:
    """asset_id, source_path, filename, basename_cf"""
    rows: list[tuple[str, str, str, str]] = []
    for p in AUDIO_DIR.glob("*.audio.json"):
        try:
            r = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        aid = r.get("asset_id")
        sp = r.get("source_path")
        fn = r.get("filename")
        if not isinstance(aid, str) or len(aid) != 64:
            continue
        if not isinstance(sp, str) or not isinstance(fn, str):
            continue
        rows.append((aid, sp, fn, fn.casefold()))
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--interview-video-only",
        action="store_true",
        help="Only videos tagged asset_classifications.type == interview",
    )
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args()
    out_path = args.out
    if out_path is None:
        out_path = OUT_INTERVIEW if args.interview_video_only else OUT_CSV
    args.out = out_path

    audios = load_audios()
    print(f"catalog audio rows: {len(audios)}", file=sys.stderr)

    fieldnames = [
        "video_asset_id",
        "video_filename",
        "video_source_path",
        "video_type",
        "video_shoot_parent",
        "snippet_tokens",
        "matched_token",
        "audio_asset_id",
        "audio_filename",
        "audio_source_path",
        "already_linked_on_video",
    ]

    rows_out: list[dict] = []
    videos_scanned = 0
    videos_with_any_candidate = 0

    for vp in sorted(VIDEO_DIR.glob("*.video.json")):
        try:
            vrec = json.loads(vp.read_text(encoding="utf-8"))
        except Exception:
            continue
        ac = vrec.get("asset_classifications")
        vtype = ac.get("type") if isinstance(ac, dict) else ""
        if args.interview_video_only and vtype != "interview":
            continue
        vaid = vrec.get("asset_id")
        vsp = vrec.get("source_path")
        vfn = vrec.get("filename")
        if not isinstance(vaid, str) or len(vaid) != 64:
            continue
        if not isinstance(vsp, str) or not isinstance(vfn, str):
            continue

        videos_scanned += 1
        vpath_cf = path_cf(vsp)
        vdir_cf = path_cf(str(Path(win_path(vsp)).parent))
        toks = snippet_tokens(vfn)
        if not toks:
            continue

        linked = video_linked_audio_ids(vrec)
        found_local = 0

        for aaid, asp, afn, abase_cf in audios:
            apath_cf = path_cf(asp)
            if not audio_under_video_dir(apath_cf, vdir_cf):
                continue
            hit = None
            for t in toks:
                if t.casefold() in abase_cf:
                    hit = t
                    break
            if hit is None:
                continue
            found_local += 1
            rows_out.append(
                {
                    "video_asset_id": vaid,
                    "video_filename": vfn,
                    "video_source_path": vsp,
                    "video_type": str(vtype or ""),
                    "video_shoot_parent": win_path(str(Path(win_path(vsp)).parent)),
                    "snippet_tokens": "|".join(toks),
                    "matched_token": hit,
                    "audio_asset_id": aaid,
                    "audio_filename": afn,
                    "audio_source_path": asp,
                    "already_linked_on_video": "yes" if aaid in linked else "no",
                }
            )

        if found_local:
            videos_with_any_candidate += 1

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        for row in rows_out:
            w.writerow(row)

    novel = sum(1 for r in rows_out if r["already_linked_on_video"] == "no")
    print(f"videos scanned: {videos_scanned}")
    print(f"videos with >=1 folder+snippet audio candidate: {videos_with_any_candidate}")
    print(f"total video-audio candidate pairs: {len(rows_out)}")
    print(f"pairs not yet on video linked_assets.audio: {novel}")
    print(f"wrote {args.out.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
