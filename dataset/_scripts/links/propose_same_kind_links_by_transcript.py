"""
Propose same-kind co-recording links via machine transcript similarity.

Two flavors:
  - video ↔ video: B-cam ↔ A-cam pointing at the same scene, both rolling.
  - audio ↔ audio: dual Zoom recorders capturing the same interview.

Same scoring as `propose_audio_video_links_by_transcript.py` (Jaccard +
SequenceMatcher + substring boost), reused via runpy. Pairs are scored within
the same shoot day (and optionally same `shoot_label`) and applied
symmetrically: if A and B co-record, each gains a `same_kind_*` edge under `linked_assets`.

Output:
  - JSONL of pair candidates (one per pair, sorted best-first).
  - Optional apply log (one line per write).

Usage:
  python _scripts/links/propose_same_kind_links_by_transcript.py
  python _scripts/links/propose_same_kind_links_by_transcript.py --kind video --min-score 0.45
  python _scripts/links/propose_same_kind_links_by_transcript.py --kind both --apply --apply-min-score 0.55
  python _scripts/links/propose_same_kind_links_by_transcript.py --apply --no-require-shoot-label
"""
from __future__ import annotations

import argparse
import json
import os
import runpy
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from _lib.linked_assets import (  # noqa: E402
    ESTABLISHED_SAME_KIND,
    LK_SAME_KIND_AUDIO,
    LK_SAME_KIND_VIDEO,
    add_edge,
)

PROPOSE = ROOT / "_scripts" / "propose_audio_video_links_by_transcript.py"

VIDEO_DIR = ROOT / "assets/video"
AUDIO_DIR = ROOT / "assets/audio"

KIND_TO_DIR = {
    "video": VIDEO_DIR,
    "audio": AUDIO_DIR,
}
KIND_TO_SUFFIX = {
    "video": ".video.json",
    "audio": ".audio.json",
}

DEFAULT_OUT_DIR = ROOT / "_review_drafts"


def atomic_write_json(path: Path, data: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def append_jsonl(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def merge_same_kind_link(
    *,
    kind: str,
    self_id: str,
    partner_id: str,
) -> bool:
    """Add partner_id to self's linked_<kind>_asset_ids[] (idempotent).

    Returns True if the file was modified.
    """
    if self_id == partner_id:
        return False
    path = KIND_TO_DIR[kind] / f"{self_id}{KIND_TO_SUFFIX[kind]}"
    if not path.exists():
        return False
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return False
    if kind == "video":
        changed = add_edge(
            raw,
            "video",
            partner_id,
            LK_SAME_KIND_VIDEO,
            symmetric=True,
            established_by=ESTABLISHED_SAME_KIND,
        )
    else:
        changed = add_edge(
            raw,
            "audio",
            partner_id,
            LK_SAME_KIND_AUDIO,
            symmetric=True,
            established_by=ESTABLISHED_SAME_KIND,
        )
    if not changed:
        return False
    atomic_write_json(path, raw)
    return True


def score_pairs_for_kind(
    rows,
    transcript_score_row,
    *,
    require_shoot_label: bool,
    min_score: float,
    min_tokens: int,
):
    """Yield (score, row_a, row_b) pairs above min_score within same shoot day
    (and shoot_label if required).
    """
    by_key = defaultdict(list)
    for r in rows:
        if not r.norm_text:
            continue
        if r.tok_count < min_tokens:
            continue
        date = r.primary_date or ""
        if not date:
            # No date — can't fence on day; skip.
            continue
        label = (r.shoot_label or "") if require_shoot_label else "__any__"
        by_key[(date, label)].append(r)

    for (date, label), group in by_key.items():
        if len(group) < 2:
            continue
        n = len(group)
        for i in range(n):
            a = group[i]
            for j in range(i + 1, n):
                b = group[j]
                if a.asset_id == b.asset_id:
                    continue
                s = transcript_score_row(a, b, use_duration=False)
                if s >= min_score:
                    yield (s, a, b, date, label)


def run_for_kind(
    kind: str,
    rows,
    transcript_score_row,
    *,
    require_shoot_label: bool,
    min_score: float,
    min_tokens: int,
    apply: bool,
    apply_min_score: float,
    apply_log: Path,
    no_apply_log: bool,
    out_path: Path,
) -> tuple[int, int, int, int]:
    """Run same-kind matching for one kind. Returns counts.

    Returns: (pair_candidates_written, pairs_applied, asset_writes, apply_log_lines).
    """
    pairs = list(
        score_pairs_for_kind(
            rows,
            transcript_score_row,
            require_shoot_label=require_shoot_label,
            min_score=min_score,
            min_tokens=min_tokens,
        )
    )
    pairs.sort(key=lambda x: -x[0])

    candidate_lines: list[str] = []
    pairs_applied = 0
    asset_writes = 0
    log_lines = 0
    now = datetime.now(timezone.utc).isoformat()

    for s, a, b, date, label in pairs:
        rec: dict[str, Any] = {
            "kind": kind,
            "asset_a": a.asset_id,
            "asset_b": b.asset_id,
            "score": round(s, 4),
            "shoot_date": date,
            "shoot_label_a": a.shoot_label,
            "shoot_label_b": b.shoot_label,
            "duration_a_sec": a.duration_sec,
            "duration_b_sec": b.duration_sec,
            "transcript_preview_a": (a.transcript_text or "")[:200],
            "transcript_preview_b": (b.transcript_text or "")[:200],
        }

        if apply and s >= apply_min_score:
            wrote_a = merge_same_kind_link(kind=kind, self_id=a.asset_id, partner_id=b.asset_id)
            wrote_b = merge_same_kind_link(kind=kind, self_id=b.asset_id, partner_id=a.asset_id)
            if wrote_a or wrote_b:
                pairs_applied += 1
                asset_writes += int(wrote_a) + int(wrote_b)
                rec["applied"] = True
                rec["wrote_a"] = wrote_a
                rec["wrote_b"] = wrote_b
                if not no_apply_log:
                    append_jsonl(
                        apply_log,
                        {
                            "logged_at": now,
                            "event": "apply",
                            "kind": kind,
                            "asset_a": a.asset_id,
                            "asset_b": b.asset_id,
                            "score": round(s, 4),
                            "shoot_date": date,
                            "shoot_label_a": a.shoot_label,
                            "shoot_label_b": b.shoot_label,
                            "apply_min_score": apply_min_score,
                            "wrote_a": wrote_a,
                            "wrote_b": wrote_b,
                            "link_source": "propose_same_kind_links_by_transcript",
                        },
                    )
                    log_lines += 1
            else:
                rec["applied"] = False
                rec["note"] = "already_linked"

        candidate_lines.append(json.dumps(rec, ensure_ascii=False))

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(candidate_lines) + ("\n" if candidate_lines else ""), encoding="utf-8")

    return (len(candidate_lines), pairs_applied, asset_writes, log_lines)


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Propose same-kind transcript co-recording links for video↔video and audio↔audio.",
    )
    ap.add_argument(
        "--kind",
        choices=["video", "audio", "both"],
        default="both",
        help="Which media kind to score (default: both).",
    )
    ap.add_argument("--min-score", type=float, default=0.35, help="Minimum score to record as candidate.")
    ap.add_argument(
        "--min-tokens",
        type=int,
        default=8,
        help="Skip transcripts with fewer than this many non-stopword tokens.",
    )
    ap.add_argument(
        "--require-shoot-label",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Only pair assets sharing the same path_metadata.shoot_label (default: on).",
    )
    ap.add_argument("--apply", action="store_true", help="Write symmetric linked_assets same_kind edges for pairs above threshold.")
    ap.add_argument("--apply-min-score", type=float, default=0.55, help="Apply threshold (default: 0.55).")
    ap.add_argument(
        "--apply-log",
        type=Path,
        default=DEFAULT_OUT_DIR / "same_kind_link_apply_log.jsonl",
        help="Append-only JSONL log for successful --apply writes.",
    )
    ap.add_argument("--no-apply-log", action="store_true", help="With --apply, do not append to the apply log.")
    ap.add_argument(
        "--video-output",
        type=Path,
        default=DEFAULT_OUT_DIR / "same_kind_link_candidates_video.jsonl",
        help="JSONL output for video↔video pairs.",
    )
    ap.add_argument(
        "--audio-output",
        type=Path,
        default=DEFAULT_OUT_DIR / "same_kind_link_candidates_audio.jsonl",
        help="JSONL output for audio↔audio pairs.",
    )
    args = ap.parse_args()

    plink = runpy.run_path(str(PROPOSE), run_name="plink_probe")
    load_catalog_rows = plink["load_catalog_rows"]
    transcript_score_row = plink["transcript_score_row"]

    audio_rows, video_rows = load_catalog_rows()

    summary: list[str] = []
    if args.kind in ("video", "both"):
        cands, pairs_app, asset_w, log_w = run_for_kind(
            "video",
            video_rows,
            transcript_score_row,
            require_shoot_label=args.require_shoot_label,
            min_score=args.min_score,
            min_tokens=args.min_tokens,
            apply=args.apply,
            apply_min_score=args.apply_min_score,
            apply_log=args.apply_log,
            no_apply_log=args.no_apply_log,
            out_path=args.video_output,
        )
        summary.append(
            f"video pairs: candidates={cands} applied_pairs={pairs_app} "
            f"asset_writes={asset_w} log_lines={log_w}"
        )
        try:
            summary.append(f"  wrote {args.video_output.relative_to(ROOT)}")
        except Exception:
            summary.append(f"  wrote {args.video_output}")

    if args.kind in ("audio", "both"):
        cands, pairs_app, asset_w, log_w = run_for_kind(
            "audio",
            audio_rows,
            transcript_score_row,
            require_shoot_label=args.require_shoot_label,
            min_score=args.min_score,
            min_tokens=args.min_tokens,
            apply=args.apply,
            apply_min_score=args.apply_min_score,
            apply_log=args.apply_log,
            no_apply_log=args.no_apply_log,
            out_path=args.audio_output,
        )
        summary.append(
            f"audio pairs: candidates={cands} applied_pairs={pairs_app} "
            f"asset_writes={asset_w} log_lines={log_w}"
        )
        try:
            summary.append(f"  wrote {args.audio_output.relative_to(ROOT)}")
        except Exception:
            summary.append(f"  wrote {args.audio_output}")

    print("\n".join(summary))


if __name__ == "__main__":
    main()
