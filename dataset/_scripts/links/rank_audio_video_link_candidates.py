"""
Rank audio→video transcript-link candidates produced by propose_audio_video_links_by_transcript.py.

Input:  JSONL, one row per audio, with .candidates[] (sorted best-first by that script)
Output: JSONL, one row per audio with best/second scores, margin, and previews.

This is deliberately lightweight: no deps, no catalog writes.

Usage:
  python _scripts/links/rank_audio_video_link_candidates.py ^
    --input _review_drafts/audio_video_transcript_link_candidates_wide.jsonl ^
    --output _review_drafts/audio_video_transcript_link_candidates_ranked.jsonl ^
    --min-best 0.55 --min-margin 0.18
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]


def _f(x: Any) -> float:
    try:
        return float(x)
    except Exception:
        return 0.0


def main() -> None:
    ap = argparse.ArgumentParser(description="Rank candidate JSONL by best score and margin.")
    ap.add_argument("--input", type=Path, required=True)
    ap.add_argument("--output", type=Path, required=True)
    ap.add_argument("--min-best", type=float, default=0.0)
    ap.add_argument("--min-margin", type=float, default=0.0)
    ap.add_argument("--limit", type=int, default=0, help="If set, only write top N after sorting.")
    args = ap.parse_args()

    rows: list[dict[str, Any]] = []
    for line in args.input.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except Exception:
            continue

        cands = list(r.get("candidates") or [])
        best = cands[0] if cands else None
        second = cands[1] if len(cands) > 1 else None

        best_s = _f(best.get("score")) if isinstance(best, dict) else 0.0
        second_s = _f(second.get("score")) if isinstance(second, dict) else 0.0
        margin = best_s - second_s

        out = {
            "audio_asset_id": r.get("audio_asset_id"),
            "primary_timeline_date": r.get("primary_timeline_date"),
            "shoot_label": r.get("shoot_label"),
            "best": {
                "video_asset_id": best.get("video_asset_id") if isinstance(best, dict) else None,
                "score": round(best_s, 4),
                "video_duration_sec": best.get("video_duration_sec") if isinstance(best, dict) else None,
                "video_transcript_preview": best.get("video_transcript_preview") if isinstance(best, dict) else None,
            }
            if isinstance(best, dict)
            else None,
            "second": {
                "video_asset_id": second.get("video_asset_id") if isinstance(second, dict) else None,
                "score": round(second_s, 4),
            }
            if isinstance(second, dict)
            else None,
            "candidate_count": len(cands),
            "best_score": round(best_s, 4),
            "second_score": round(second_s, 4),
            "margin": round(margin, 4),
            "audio_transcript_preview": r.get("audio_transcript_preview"),
        }

        if best_s < args.min_best or margin < args.min_margin:
            continue
        rows.append(out)

    rows.sort(key=lambda x: (x.get("best_score", 0.0), x.get("margin", 0.0)), reverse=True)
    if args.limit and args.limit > 0:
        rows = rows[: args.limit]

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + ("\n" if rows else ""), encoding="utf-8")

    try:
        in_disp = str(args.input.relative_to(ROOT))
    except Exception:
        in_disp = str(args.input)
    try:
        out_disp = str(args.output.relative_to(ROOT))
    except Exception:
        out_disp = str(args.output)

    print(f"Read {in_disp}")
    print(f"Wrote {out_disp}")
    print(f"Rows passing gate (min_best={args.min_best}, min_margin={args.min_margin}): {len(rows)}")


if __name__ == "__main__":
    main()

