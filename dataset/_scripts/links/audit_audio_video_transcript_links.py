"""Second-pass audit for missed audio ↔ video transcript links.

Uses the same scoring as propose_audio_video_links_by_transcript.py but compares:
1) candidates restricted to **same calendar day + shoot_label**
2) candidates on **same day only** (any shoot_label)

Flags cases where transcript similarity is strong globally on that day but the
strict label fence hides the obvious video — a common omission when folders
were labeled slightly differently across devices.

Writes: _review_drafts/audio_video_link_audit_relaxed.jsonl (rows where same-day relaxed score ≥ 0.35)
Prints summary counts.

Usage:
  python _scripts/links/audit_audio_video_transcript_links.py
"""
from __future__ import annotations

import json
import runpy
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "_review_drafts/audio_video_link_audit_relaxed.jsonl"
PROPOSE = ROOT / "_scripts" / "propose_audio_video_links_by_transcript.py"


def main() -> None:
    plink = runpy.run_path(str(PROPOSE), run_name="plink_probe")
    load_catalog_rows = plink["load_catalog_rows"]
    index_videos_by_date = plink["index_videos_by_date"]
    scored_videos_for_audio = plink["scored_videos_for_audio"]

    audio_rows, video_rows = load_catalog_rows()
    by_date = index_videos_by_date(video_rows)

    strong_relax = 0
    label_blocks = 0
    high_strict_unlinked = 0
    lines: list[str] = []

    for a in audio_rows:
        if a.linked_video_asset_id:
            continue
        if not a.norm_text:
            continue

        strict = scored_videos_for_audio(
            a, by_date, require_shoot_label=True, min_score=0.0, use_duration=False,
        )
        relaxed = scored_videos_for_audio(
            a, by_date, require_shoot_label=False, min_score=0.0, use_duration=False,
        )

        b_s = strict[0] if strict else None
        b_r = relaxed[0] if relaxed else None

        if b_r and b_r[0] >= 0.42:
            strong_relax += 1

        if b_s and b_s[0] >= 0.42:
            high_strict_unlinked += 1

        same_top = (
            b_s is not None
            and b_r is not None
            and b_s[1].asset_id == b_r[1].asset_id
        )
        if b_r and b_r[0] >= 0.45 and b_s and (not same_top) and b_s[0] < b_r[0] - 0.02:
            label_blocks += 1

        # Always record rows where relaxed pool has a plausible match
        if not b_r or b_r[0] < 0.35:
            continue

        rec = {
            "audio_asset_id": a.asset_id,
            "shoot_date": a.primary_date,
            "shoot_label": a.shoot_label,
            "strict_top": (
                {"video_asset_id": b_s[1].asset_id, "score": round(b_s[0], 4), "shoot_label": b_s[1].shoot_label}
                if b_s
                else None
            ),
            "relaxed_top": {
                "video_asset_id": b_r[1].asset_id,
                "score": round(b_r[0], 4),
                "shoot_label": b_r[1].shoot_label,
            },
            "strict_relaxed_same_video": same_top if b_s and b_r else None,
            "second_relaxed": (
                {"video_asset_id": relaxed[1][1].asset_id, "score": round(relaxed[1][0], 4)}
                if len(relaxed) > 1
                else None
            ),
        }
        lines.append(json.dumps(rec, ensure_ascii=False))

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")

    print(
        "audio: unlinked + usable machine transcript\n"
        f"  count: {sum(1 for a in audio_rows if not a.linked_video_asset_id and a.norm_text)}\n"
        f"  relaxed_top score >= 0.42: {strong_relax}\n"
        f"  strict_top score >= 0.42 (still unlinked): {high_strict_unlinked}\n"
        f"  label_fence_suspect (relaxed >=0.45 beats strict, diff top): {label_blocks}\n"
        f"Wrote {OUT.relative_to(ROOT)} ({len(lines)} rows with relaxed_top >= 0.35)"
    )


if __name__ == "__main__":
    main()
