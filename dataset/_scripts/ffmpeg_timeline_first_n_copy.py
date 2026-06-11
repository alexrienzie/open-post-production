#!/usr/bin/env python3
"""
First N seconds of sequence time from timeline JSON -> proxy slices -> ffmpeg concat.

Modes:
  --mode copy   (default) Per-segment -ss before -i + stream copy; fastest, often glitchy
                at cuts (open-GOP / timestamp jumps).
  --mode stable Per-segment -ss after -i + light re-encode to uniform H.264/AAC, then
                concat -c copy; much smoother playback, slower.

Uses trimmed/overlap-naive ordering: video clips sorted by (sequence_start_seconds,
timeline_row_index), each trimmed to [0, window_sec], then concatenated.

Requires ffmpeg on PATH. Proxies: <proxies_root>/<asset_id>.mp4
"""

from __future__ import annotations

import argparse
import json
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Tuple

# dataset/_scripts/<this>.py -> parents[2]=open-post-stack project root
_WORKSPACE_ROOT = Path(__file__).resolve().parents[2]


def _vf_uniform_16_9(height: int) -> str:
    w = (height * 16) // 9
    w -= w % 2
    return (
        f"scale={w}:{height}:force_original_aspect_ratio=decrease:flags=lanczos,"
        f"pad={w}:{height}:(ow-iw)/2:(oh-ih)/2:black,setsar=1,format=yuv420p"
    )


def _trim_clip_to_window(
    c: Dict[str, Any], win_lo: float, win_hi: float
) -> Tuple[float, float, float, float] | None:
    """Return (seq_s, seq_e, src_s, src_e) intersected with [win_lo, win_hi], or None."""
    if c.get("track_item_type") != "video" or not c.get("asset_id"):
        return None
    if c.get("media_kind") == "nested_sequence":
        return None
    seq_s = float(c["sequence_start_seconds"])
    seq_e = float(c["sequence_end_seconds"])
    if seq_e <= seq_s:
        return None
    lo = max(seq_s, win_lo)
    hi = min(seq_e, win_hi)
    if hi <= lo:
        return None
    src_s = float(c.get("source_in_seconds") or 0.0)
    src_e = float(c.get("source_out_seconds") or 0.0)
    if src_e <= src_s:
        return None
    dur = seq_e - seq_s
    ratio_lo = (lo - seq_s) / dur
    ratio_hi = (hi - seq_s) / dur
    out_src_s = src_s + ratio_lo * (src_e - src_s)
    out_src_e = src_s + ratio_hi * (src_e - src_s)
    return lo, hi, out_src_s, out_src_e


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--timeline", required=True, help="Timeline JSON with asset_id on video clips")
    ap.add_argument(
        "--proxies",
        default=str((_WORKSPACE_ROOT / "derivative media").as_posix()),
        help="Legacy flat proxy root; prefer per-clip paths from asset_map when available",
    )
    ap.add_argument("--window-sec", type=float, default=300.0, help="Sequence time [0, this]")
    ap.add_argument(
        "-o",
        "--output",
        default=str(
            (_WORKSPACE_ROOT / "editor" / "outputs" / "preview" / "Act_I_first5min_copy.mp4").as_posix()
        ),
    )
    ap.add_argument(
        "--mode",
        choices=("copy", "stable"),
        default="copy",
        help="copy=stream copy (fast, glitchy cuts). stable=re-encode segments (smooth).",
    )
    ap.add_argument("--height", type=int, default=720, help="Stable mode: output height (16:9).")
    ap.add_argument("--crf", type=int, default=23, help="Stable mode: H.264 CRF.")
    ap.add_argument("--preset", type=str, default="veryfast", help="Stable mode: x264 preset.")
    args = ap.parse_args()

    data = json.loads(Path(args.timeline).read_text(encoding="utf-8"))
    win_hi = float(args.window_sec)
    fps = float(data.get("video_fps_approx") or 23.976)

    slots: List[Tuple[float, int, str, float, float, float, float]] = []
    for row in data.get("timeline_rows", []):
        idx = int(row.get("timeline_row_index", 0))
        for c in row.get("clips", []):
            t = _trim_clip_to_window(c, 0.0, win_hi)
            if t is None:
                continue
            seq_s, seq_e, src_s, src_e = t
            aid = str(c["asset_id"])
            slots.append((seq_s, seq_e, idx, aid, src_s, src_e, seq_e - seq_s))
    slots.sort(key=lambda x: (x[0], x[2]))

    proxies = Path(args.proxies)
    out_final = Path(args.output)
    out_final.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="ff_") as td:
        tdir = Path(td)
        part_paths: List[Path] = []
        for i, (_qs, _qe, _idx, aid, ss, se, _dseq) in enumerate(slots):
            proxy = proxies / f"{aid}.mp4"
            if not proxy.exists():
                print(f"Missing proxy, skip: {proxy}")
                continue
            dur = max(0.001, se - ss)
            part = tdir / f"part_{i:04d}.mp4"
            if args.mode == "copy":
                cmd = [
                    "ffmpeg",
                    "-y",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-ss",
                    f"{ss:.6f}",
                    "-i",
                    str(proxy),
                    "-t",
                    f"{dur:.6f}",
                    "-c",
                    "copy",
                    str(part),
                ]
            else:
                vf = _vf_uniform_16_9(int(args.height))
                cmd = [
                    "ffmpeg",
                    "-y",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-i",
                    str(proxy),
                    "-ss",
                    f"{ss:.6f}",
                    "-t",
                    f"{dur:.6f}",
                    "-vf",
                    vf,
                    "-r",
                    str(fps),
                    "-c:v",
                    "libx264",
                    "-preset",
                    str(args.preset),
                    "-crf",
                    str(int(args.crf)),
                    "-pix_fmt",
                    "yuv420p",
                    "-profile:v",
                    "high",
                    "-level",
                    "4.0",
                    "-c:a",
                    "aac",
                    "-b:a",
                    "128k",
                    "-ar",
                    "48000",
                    "-ac",
                    "2",
                    "-movflags",
                    "+faststart",
                    str(part),
                ]
            r = subprocess.run(cmd, capture_output=True, text=True)
            if r.returncode != 0:
                print(r.stderr[-800:] if r.stderr else r)
                raise SystemExit(f"ffmpeg extract failed for {aid}")
            part_paths.append(part)

        if not part_paths:
            raise SystemExit("No segments produced (missing proxies or no clips in window?)")

        lst = tdir / "concat.txt"
        lines = []
        for p in part_paths:
            s = str(p.resolve()).replace("'", "'\\''")
            lines.append(f"file '{s}'")
        lst.write_text("\n".join(lines) + "\n", encoding="utf-8")

        cmd2 = [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(lst),
            "-c",
            "copy",
            str(out_final),
        ]
        r2 = subprocess.run(cmd2, capture_output=True, text=True)
        if r2.returncode != 0:
            print(r2.stderr[-1200:] if r2.stderr else r2)
            raise SystemExit("ffmpeg concat failed (try without -c copy if codec mismatch)")

    print(
        f"Wrote {out_final} ({len(part_paths)} segments, mode={args.mode})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
