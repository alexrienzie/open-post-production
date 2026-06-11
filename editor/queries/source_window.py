"""Suggest a clean source-in/out window inside an asset.

Reads `asset_semantic_chunk` (camera_movement, action) and
`asset_semantic_key_moment` (timestamp_sec + description) for an asset, then
returns a `(source_in_sec, source_out_sec)` window that:

1. Starts on or just after a key_moment whose description reads "clean/static/
   wide/stable" (positive flag), OR avoids the next "pan/drift/whip/sigh/
   handheld/shake" key_moment (negative flag).
2. Is `target_duration_sec` long if the asset is long enough; otherwise uses
   the longest clean window available.

This is a heuristic — not a replacement for human review. It pushes the
common case (default to frame 0 picks up wobble) toward a better starting
point so the editor isn't constantly adjusting.

Usage:

    from editor.queries.source_window import suggest_source_window
    src_in, src_out = suggest_source_window(asset_id, target_duration_sec=6.0)
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Optional

from ._paths import editorial_catalog_sqlite_path


# Phrase classes for scoring key_moment descriptions.
_POSITIVE = (
    "clean",
    "static",
    "wide",
    "stable",
    "settled",
    "establishing",
    "reveal",
    "centered",
    "in focus",
    "racks focus",
)
_NEGATIVE = (
    "pan",
    "drift",
    "whip",
    "sigh",
    "handheld",
    "shake",
    "tilt",
    "wobble",
    "out of focus",
    "blurry",
    "obscur",
)


def _classify(description: str) -> str:
    d = (description or "").lower()
    pos_hit = any(p in d for p in _POSITIVE)
    neg_hit = any(n in d for n in _NEGATIVE)
    if pos_hit and not neg_hit:
        return "positive"
    if neg_hit:
        return "negative"
    return "neutral"


def suggest_source_window(
    asset_id: str,
    target_duration_sec: float,
    *,
    catalog_db: Optional[Path] = None,
    min_head_buffer_sec: float = 1.0,
    fallback_offset_sec: float = 2.0,
) -> tuple[float, float]:
    """Return (source_in_sec, source_out_sec) for a clean window.

    Args:
        asset_id: catalog asset id
        target_duration_sec: desired window length
        min_head_buffer_sec: minimum offset from a positive key_moment
            (avoids landing right on the timestamp; lets it settle)
        fallback_offset_sec: if no key_moments, skip this many seconds of
            potential operator settling at the head of the clip

    Returns:
        (source_in_sec, source_out_sec). Both clamped to the asset's duration.
    """
    db = catalog_db or editorial_catalog_sqlite_path()
    con = sqlite3.connect(str(db))
    con.row_factory = sqlite3.Row
    asset = con.execute(
        "SELECT duration_sec FROM asset WHERE asset_id = ?", (asset_id,)
    ).fetchone()
    if not asset:
        con.close()
        raise ValueError(f"asset_id not found: {asset_id}")
    duration = float(asset["duration_sec"] or 0.0)
    if duration <= 0:
        con.close()
        return (0.0, target_duration_sec)

    kms = con.execute(
        "SELECT timestamp_sec, description "
        "FROM asset_semantic_key_moment "
        "WHERE asset_id = ? ORDER BY timestamp_sec",
        (asset_id,),
    ).fetchall()
    movement = con.execute(
        "SELECT camera_movement FROM asset_semantic_chunk "
        "WHERE asset_id = ? LIMIT 1",
        (asset_id,),
    ).fetchone()
    con.close()

    overall_movement = (movement["camera_movement"] if movement else "") or ""
    overall_movement = overall_movement.lower()

    if not kms:
        # No key_moments. Skip a small head buffer for operator settling.
        # `static` shots can start at 0 safely; `handheld`/`mixed` benefit
        # from a couple seconds in.
        head = 0.0 if overall_movement == "static" else fallback_offset_sec
        head = min(head, max(0.0, duration - target_duration_sec))
        return (round(head, 3), round(min(head + target_duration_sec, duration), 3))

    # Classify each key_moment.
    classed = [
        {
            "t": float(km["timestamp_sec"]),
            "class": _classify(km["description"]),
            "desc": km["description"] or "",
        }
        for km in kms
    ]

    # Find candidate windows: start at each positive km + buffer, run forward
    # until next negative km or end of clip. Pick the first that fits
    # target_duration_sec.
    positives = [c for c in classed if c["class"] == "positive"]
    negatives = [c for c in classed if c["class"] == "negative"]

    def _next_negative_after(t: float) -> Optional[float]:
        for n in negatives:
            if n["t"] > t + 0.01:
                return n["t"]
        return None

    for pos in positives:
        start = pos["t"] + min_head_buffer_sec
        next_neg = _next_negative_after(start)
        room = (next_neg if next_neg is not None else duration) - start
        if room >= target_duration_sec:
            end = min(start + target_duration_sec, duration)
            return (round(start, 3), round(end, 3))

    # No clean positive window fits. Try: window between two negatives, picking
    # the largest clean gap.
    edges = [0.0] + sorted(n["t"] for n in negatives) + [duration]
    best = (0.0, min(target_duration_sec, duration))
    best_room = best[1] - best[0]
    for i in range(len(edges) - 1):
        a, b = edges[i], edges[i + 1]
        if i > 0:
            a = a + min_head_buffer_sec  # skip just past the negative
        room = b - a
        if room > best_room:
            best = (a, min(a + target_duration_sec, b, duration))
            best_room = room
    return (round(best[0], 3), round(best[1], 3))


__all__ = ["suggest_source_window"]
