"""
Extract relevance-bounded time windows from long (deferred) podcast transcripts.

Reads a CSV like `_review_drafts/podcast_deferred_notes.csv` with columns:
  Length (min), kind, file, asset_id, note

Modes:
  - heuristic: keyword/phrase scoring on each segment (no extra dependencies).
  - embed: local dense retrieval with fastembed + BGE-small (ONNX). First run may
    download the model from Hugging Face; no API key. Install:
    pip install -r _scripts/requirements_podcast_clip.txt

Outputs JSONL (one object per input row).

Usage:
  python _scripts/transcripts/extract_podcast_relevance_windows.py
  python _scripts/transcripts/extract_podcast_relevance_windows.py --method heuristic
  python _scripts/transcripts/extract_podcast_relevance_windows.py --only-many-topics
  python _scripts/transcripts/extract_podcast_relevance_windows.py --keywords-file _review_drafts/podcast_keywords.txt
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]

DEFAULT_INPUT = ROOT / "_review_drafts/podcast_deferred_notes.csv"
DEFAULT_OUTPUT = ROOT / "_review_drafts/podcast_clip_windows.jsonl"

TRANSCRIPTS_DIR = ROOT / "assets/transcripts"
VIDEO_DIR = ROOT / "assets/video"
AUDIO_DIR = ROOT / "assets/audio"


DEFAULT_KEYWORDS: list[tuple[str, float]] = [
    # Core story anchors
    ("grand teton", 3.0),
    # Keep "teton" relatively weak; it appears in lots of unrelated Wyoming chatter.
    ("teton", 0.75),
    ("fkt", 2.0),
    ("fastest known", 2.0),
    ("fastest known time", 2.5),
    ("michelino", 2.0),
    ("sunseri", 2.0),
    ("lupine meadows", 2.0),
    ("old climbers", 3.0),
    ("willie unsold", 3.0),
    ("unsold", 1.0),
    # Legal / park conflict thread
    ("national park service", 2.0),
    ("park service", 1.5),
    ("nps", 1.5),
    ("ranger", 1.0),
    # Prefer phrase-level legal anchors (generic "court"/"criminal" hits are extremely noisy on news pods)
    ("trial", 2.0),
    ("federal court", 2.0),
    ("district court", 2.0),
    ("tenth circuit", 2.5),
    ("10th circuit", 2.5),
    ("supreme court", 2.5),
    ("scotus", 2.0),
    ("verdict", 1.5),
    ("prosecut", 1.5),
    ("brady", 1.5),
    ("foia", 1.5),
    ("department of justice", 1.5),
    ("doj", 1.0),
    # Permit / filming thread (often adjacent to NPS conflict)
    ("commercial filming", 1.5),
    ("film permit", 1.5),
    ("special use permit", 1.5),
    # Trail-specific controversy language (use cautiously; still useful when paired with stronger hits)
    ("shortcut", 1.0),
    ("switchback", 1.25),
    ("erosion", 0.75),
    ("closed for restoration", 1.5),
    ("closed for regrowth", 1.5),
    # FKT ecosystem / controversy
    ("fastestknowntime", 1.5),
    ("outside magazine", 1.0),
    ("strava", 1.0),
]

# Default anchors for semantic retrieval (embed mode); CSV `note` + filename are included per row.
DEFAULT_QUERY_ANCHORS = (
    "Grand Teton fastest known time FKT National Park Service NPS ranger "
    "litigation trial Michelino Sunseri Wyoming federal court filming permit"
)

_EMBEDDER_CACHE: dict[str, Any] = {}


FILM_TITLE = "<your film title, lowercase>"  # strong story cue — replace, along with the cue lists above, with your film's terms

def atomic_replace_text(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    os.replace(tmp, path)


def load_keywords(path: Path | None) -> list[tuple[str, float]]:
    if path is None:
        return list(DEFAULT_KEYWORDS)
    out: list[tuple[str, float]] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "\t" in line:
            a, b = line.split("\t", 1)
            phrase = a.strip()
            weight = float(b.strip())
        else:
            phrase = line
            weight = 1.0
        if phrase:
            out.append((phrase.lower(), weight))
    return out or list(DEFAULT_KEYWORDS)


def build_retrieval_query(row: dict[str, str], *, extra: str) -> str:
    note = (row.get("note") or "").strip()
    file = (row.get("file") or "").strip()
    stem = Path(file.replace('"', "")).stem.replace("_", " ").strip()
    parts = [p for p in (stem, note, DEFAULT_QUERY_ANCHORS, extra.strip()) if p]
    return ". ".join(parts)


def format_query_for_embedding(model_name: str, query: str) -> str:
    if "bge" in model_name.lower():
        return f"Represent this sentence for searching relevant passages: {query}"
    return query


def get_text_embedding_model(model_name: str) -> Any:
    if model_name not in _EMBEDDER_CACHE:
        try:
            from fastembed import TextEmbedding
        except ImportError as e:
            raise SystemExit(
                "embed mode requires fastembed. Install:\n"
                "  pip install -r _scripts/requirements_podcast_clip.txt"
            ) from e
        _EMBEDDER_CACHE[model_name] = TextEmbedding(model_name=model_name)
    return _EMBEDDER_CACHE[model_name]


def _stack_model_embeddings(model: Any, texts: list[str], *, batch_size: int):
    import numpy as np

    rows: list[Any] = []
    for emb in model.embed(texts, batch_size=batch_size):
        rows.append(np.asarray(emb, dtype=np.float32))
    if not rows:
        return np.zeros((0, 0), dtype=np.float32)
    return np.stack(rows, axis=0)


def _cosine_scores(query_vec, matrix) -> Any:
    import numpy as np

    q = np.asarray(query_vec, dtype=np.float32).reshape(-1)
    m = np.asarray(matrix, dtype=np.float32)
    if m.size == 0:
        return np.zeros((0,), dtype=np.float32)
    qn = np.linalg.norm(q)
    q = q / max(qn, 1e-12)
    mn = np.linalg.norm(m, axis=1, keepdims=True)
    mn = np.clip(mn, 1e-12, None)
    m = m / mn
    return m @ q


@dataclass
class TextChunk:
    start: float
    end: float
    text: str


def chunk_transcript(
    tr: dict,
    *,
    max_chunk_chars: int,
    max_chunk_sec: float,
) -> list[TextChunk]:
    chunks: list[TextChunk] = []
    cur_parts: list[str] = []
    cur_start: float | None = None
    cur_end: float | None = None

    for seg in tr.get("segments") or []:
        try:
            ss = float(seg.get("start_sec"))
            ee = float(seg.get("end_sec"))
        except Exception:
            continue
        txt = (seg.get("text") or "").strip()
        if not txt:
            continue
        if cur_start is None:
            cur_start = ss
            cur_end = ee
            cur_parts = [txt]
            continue
        merged_len = sum(len(p) for p in cur_parts) + len(txt) + 1
        merged_dur = ee - cur_start
        if merged_len > max_chunk_chars or merged_dur > max_chunk_sec:
            chunks.append(TextChunk(cur_start, cur_end, " ".join(cur_parts)))
            cur_start = ss
            cur_end = ee
            cur_parts = [txt]
        else:
            cur_parts.append(txt)
            cur_end = ee

    if cur_start is not None and cur_parts:
        chunks.append(TextChunk(cur_start, cur_end, " ".join(cur_parts)))
    return chunks


def _cluster_chunks_by_time(
    indexed: list[tuple[int, float]], chunks: list[TextChunk], merge_gap: float
) -> list[list[int]]:
    """indexed: (idx, sim) pairs for selected chunks; returns lists of chunk indices."""
    if not indexed:
        return []
    order = sorted(indexed, key=lambda t: chunks[t[0]].start)
    runs: list[list[int]] = [[order[0][0]]]
    for idx, _sim in order[1:]:
        prev_i = runs[-1][-1]
        if chunks[idx].start <= chunks[prev_i].end + merge_gap:
            runs[-1].append(idx)
        else:
            runs.append([idx])
    return runs


def _run_score(run: list[int], sims: Any) -> float:
    s = 0.0
    for i in run:
        s += float(sims[i])
    return s


def _run_peak(run: list[int], sims: Any) -> float:
    return max(float(sims[i]) for i in run) if run else 0.0


def _run_span_sec(run: list[int], chunks: list[TextChunk]) -> float:
    if not run:
        return 0.0
    return max(chunks[i].end for i in run) - min(chunks[i].start for i in run)


def _run_midpoint(run: list[int], chunks: list[TextChunk]) -> float:
    if not run:
        return 0.0
    return 0.5 * (min(chunks[i].start for i in run) + max(chunks[i].end for i in run))


def _run_time_bounds(run: list[int], chunks: list[TextChunk]) -> tuple[float, float]:
    return min(chunks[i].start for i in run), max(chunks[i].end for i in run)


def _runs_separated(a: list[int], b: list[int], chunks: list[TextChunk], gap: float) -> bool:
    a0, a1 = _run_time_bounds(a, chunks)
    b0, b1 = _run_time_bounds(b, chunks)
    return a1 + gap < b0 or b1 + gap < a0


# Down-weight runs whose text never names the story (reduces “podcast intro” false positives).
_LEXICAL_STRONG = (
    "michelino",
    "sinceri",
    "sunseri",
    "sonseri",
    "grand teton",
    "teton national",
    "fastest known",
    " fkt",
    "fkt,",
    "fkt.",
    "fkt ",
    "fkt?",
    "national park service",
    "tenth circuit",
    FILM_TITLE,
    "docuseries",
)


def _run_lexical_boost(run: list[int], chunks: list[TextChunk]) -> float:
    blob = " ".join(chunks[i].text.lower() for i in run)
    if any(s in blob for s in _LEXICAL_STRONG):
        return 1.0
    # ASR often says "teton uh fkt" or "FKTs" — not always matched by _LEXICAL_STRONG tokens.
    if "teton" in blob and "fkt" in blob:
        return 1.0
    if "teton" in blob and any(
        x in blob for x in ("record", "summit", "park", "permit", "nps", "ranger", "route", "climb")
    ):
        return 0.88
    if "teton" in blob:
        return 0.72
    return 0.58


def _run_sponsor_penalty(run: list[int], chunks: list[TextChunk]) -> float:
    """Down-rank ad reads (high generic similarity to 'podcast' / brand queries)."""
    blob = " ".join(chunks[i].text.lower() for i in run)
    hits = 0
    if "brought to you by" in blob:
        hits += 1
    if "official" in blob and "partner" in blob:
        hits += 1
    if "use code" in blob or "promo code" in blob or "discount" in blob:
        hits += 1
    if ".com" in blob and ("%" in blob or "off your" in blob or "hop on" in blob):
        hits += 1
    if "hydration solutions" in blob or "ultravest" in blob:
        hits += 1
    if hits >= 2:
        return 0.38
    if hits == 1:
        return 0.62
    return 1.0


def _run_tail_penalty(run: list[int], chunks: list[TextChunk], playback_duration: float | None) -> float:
    if playback_duration is None or playback_duration <= 0:
        return 1.0
    mid = _run_midpoint(run, chunks)
    frac = mid / playback_duration
    if frac >= 0.92:
        return 0.42
    if frac >= 0.86:
        return 0.68
    if frac >= 0.82:
        return 0.82
    return 1.0


def _chunk_strong_story_cue(text: str) -> bool:
    tl = text.lower()
    if "grand teton" in tl:
        return True
    if "teton" in tl and "fkt" in tl:
        return True
    return False


def _chunk_recall_bridge(text: str) -> bool:
    """Extra lexical recall so sparse embed hits still form one narrative run (e.g. long news pods)."""
    tl = text.lower()
    if _chunk_strong_story_cue(text):
        return True
    if "docuseries" in tl:
        return True
    if FILM_TITLE in tl:
        return True
    return False


def _run_story_tier(run: list[int], chunks: list[TextChunk]) -> int:
    """Prefer runs that explicitly name the Grand Teton / FKT thread over generic Michelino chatter."""
    blob = " ".join(chunks[i].text.lower() for i in run)
    if ("docuseries" in blob or "lawsuit" in blob or "filmmaker" in blob) and (
        "grand teton" in blob or "teton" in blob or "michelino" in blob
    ):
        return 4
    if ("teton" in blob and "fkt" in blob) or ("grand teton" in blob and "fkt" in blob):
        return 4
    if "grand teton" in blob or " fkt" in blob or "fkt " in blob or "fkt," in blob or "fkt." in blob:
        return 3
    if "michelino" in blob or "sinceri" in blob or "sunseri" in blob or "sonseri" in blob:
        return 2
    if "teton" in blob:
        return 1
    return 0


def _run_teaser_intro_penalty(run: list[int], chunks: list[TextChunk]) -> float:
    """Down-rank generic cold opens; extra penalty when the guest is named but story hooks aren't spoken yet."""
    blob = " ".join(chunks[i].text.lower() for i in run)
    start = min(chunks[i].start for i in run) if run else 0.0
    if start > 240:
        return 1.0
    if ("grand teton" in blob) or (" fkt" in blob) or ("fkt " in blob) or ("fkt," in blob):
        return 1.0
    cold_open = (
        "welcome" in blob
        or "joins the pod" in blob
        or "joins the podcast" in blob
        or "special episode" in blob
        or "good morning" in blob
    )
    if not cold_open:
        return 1.0
    if "michelino" in blob or "sunseri" in blob or "sinceri" in blob:
        return 0.52
    return 0.6


def _embed_similarity_threshold(sims: Any, *, sim_relative: float, sim_floor: float) -> tuple[float, float]:
    """
    Threshold from mean(top-m chunk scores) so one loud intro/ad can't wash out mid-episode hits.
    Returns (threshold, reported_max) where reported_max is global max for diagnostics.
    """
    if len(sims) == 0:
        return sim_floor, 0.0
    vals = sorted(float(sims[i]) for i in range(len(sims)))
    reported_max = vals[-1]
    m = max(5, min(14, len(vals) // 25 + 6))
    ref = sum(vals[-m:]) / m
    thresh = max(sim_floor, ref * sim_relative)
    return thresh, reported_max


def _unify_rank_score(
    run: list[int], sims: Any, chunks: list[TextChunk], playback_duration: float | None
) -> float:
    """Higher = better primary segment: embedding × lexical × sponsor/tail penalties × √(duration)."""
    peak = _run_peak(run, sims)
    span = max(30.0, _run_span_sec(run, chunks))
    boost = _run_lexical_boost(run, chunks)
    start = min(chunks[i].start for i in run)
    early_pen = 1.0
    if start < 180 and span < 130:
        early_pen = 0.72
    elif start < 360 and span < 200:
        early_pen = 0.86
    return (
        peak
        * boost
        * early_pen
        * _run_teaser_intro_penalty(run, chunks)
        * _run_sponsor_penalty(run, chunks)
        * _run_tail_penalty(run, chunks, playback_duration)
        * math.sqrt(span)
    )


def _hit_from_embed_run(
    run: list[int],
    sims: Any,
    chunks: list[TextChunk],
    *,
    pad: float,
    max_total_sec: float | None,
    play_dur_f: float | None,
    window_role: str | None = None,
) -> Hit:
    rstart = min(chunks[i].start for i in run)
    rend = max(chunks[i].end for i in run)
    best_i = max(run, key=lambda i: float(sims[i]))
    center = (chunks[best_i].start + chunks[best_i].end) / 2.0
    s0 = max(0.0, rstart - pad)
    e0 = rend + pad
    if max_total_sec is not None and (e0 - s0) > max_total_sec:
        s0, e0 = clamp_window_duration(
            s0,
            e0,
            playback_duration=play_dur_f,
            max_sec=max_total_sec,
            center=center,
        )
    peak_sim = max(float(sims[i]) for i in run)
    return Hit(
        start=s0,
        end=e0,
        score=peak_sim,
        keywords=set(),
        embed_peak=peak_sim,
        window_role=window_role,
    )


def _pick_companion_runs(
    runs: list[list[int]],
    primary: list[int],
    sims: Any,
    chunks: list[TextChunk],
    *,
    playback_duration: float | None,
    max_companions: int,
    min_gap: float,
    min_rank_frac: float,
) -> list[list[int]]:
    if max_companions <= 0 or not runs or not primary:
        return []
    pk = frozenset(primary)
    ps = _unify_rank_score(primary, sims, chunks, playback_duration)
    p_span = _run_span_sec(primary, chunks)
    pmid = _run_midpoint(primary, chunks)
    others = [r for r in runs if frozenset(r) != pk]
    others.sort(
        key=lambda r: _unify_rank_score(r, sims, chunks, playback_duration),
        reverse=True,
    )
    picked: list[list[int]] = []
    for r in others:
        if len(picked) >= max_companions:
            break
        if not _runs_separated(primary, r, chunks, min_gap):
            continue
        rs = _unify_rank_score(r, sims, chunks, playback_duration)
        if rs < ps * min_rank_frac:
            continue
        if _run_lexical_boost(r, chunks) < 0.72:
            continue
        rmid = _run_midpoint(r, chunks)
        r_span = _run_span_sec(r, chunks)
        later = rmid > pmid + 45.0
        longer = r_span > p_span * 1.45
        nearly_as_strong = rs >= ps * 0.9
        if not (later or longer or nearly_as_strong):
            continue
        ok = True
        for p2 in picked:
            if not _runs_separated(r, p2, chunks, min_gap):
                ok = False
                break
        if ok:
            picked.append(r)
    return picked


def clamp_window_duration(
    start: float,
    end: float,
    *,
    playback_duration: float | None,
    max_sec: float,
    center: float,
) -> tuple[float, float]:
    dur = float(playback_duration) if playback_duration is not None else end
    span = max(0.0, end - start)
    if max_sec <= 0 or span <= max_sec:
        lo = max(0.0, start)
        hi = min(dur, end) if playback_duration is not None else end
        return lo, hi
    half = max_sec / 2.0
    lo = max(0.0, center - half)
    hi = min(dur, lo + max_sec) if playback_duration is not None else center + half
    if playback_duration is not None and hi - lo < max_sec:
        lo = max(0.0, hi - max_sec)
    elif playback_duration is None and hi - lo < max_sec:
        hi = lo + max_sec
    return lo, hi


def load_catalog_source_path(asset_id: str) -> str:
    vp = VIDEO_DIR / f"{asset_id}.video.json"
    if vp.exists():
        return str(json.loads(vp.read_text(encoding="utf-8")).get("source_path") or "")
    ap = AUDIO_DIR / f"{asset_id}.audio.json"
    if ap.exists():
        return str(json.loads(ap.read_text(encoding="utf-8")).get("source_path") or "")
    return ""


@dataclass
class Hit:
    start: float
    end: float
    score: float
    keywords: set[str]
    embed_peak: float | None = None
    window_role: str | None = None


def score_segment(text: str, keywords: list[tuple[str, float]]) -> tuple[float, set[str]]:
    t = text.lower()
    score = 0.0
    matched: set[str] = set()
    for phrase, w in keywords:
        if phrase in t:
            score += w
            matched.add(phrase)
    return score, matched


def merge_hits(hits: list[Hit], *, pad: float, merge_gap: float) -> list[Hit]:
    if not hits:
        return []
    hits_sorted = sorted(hits, key=lambda h: (h.start, h.end))
    cur = Hit(
        start=max(0.0, hits_sorted[0].start - pad),
        end=hits_sorted[0].end + pad,
        score=hits_sorted[0].score,
        keywords=set(hits_sorted[0].keywords),
        embed_peak=hits_sorted[0].embed_peak,
        window_role=hits_sorted[0].window_role,
    )
    merged: list[Hit] = []
    for h in hits_sorted[1:]:
        s = max(0.0, h.start - pad)
        e = h.end + pad
        if s <= cur.end + merge_gap:
            cur.end = max(cur.end, e)
            cur.score += h.score
            cur.keywords |= h.keywords
            if h.embed_peak is not None:
                cur.embed_peak = h.embed_peak if cur.embed_peak is None else max(cur.embed_peak, h.embed_peak)
        else:
            merged.append(cur)
            cur = Hit(
                start=s,
                end=e,
                score=h.score,
                keywords=set(h.keywords),
                embed_peak=h.embed_peak,
                window_role=h.window_role,
            )
    merged.append(cur)
    return merged


def apply_budget(windows: list[Hit], *, max_total_sec: float | None, max_windows: int | None) -> list[Hit]:
    if max_windows is not None:
        windows = sorted(windows, key=lambda w: (w.score / max(1e-6, (w.end - w.start))), reverse=True)
        windows = windows[: max(1, max_windows)]

    if max_total_sec is None:
        return sorted(windows, key=lambda w: w.start)

    picked: list[Hit] = []
    total = 0.0
    for w in sorted(windows, key=lambda w: (w.score / max(1e-6, (w.end - w.start))), reverse=True):
        dur = max(0.0, w.end - w.start)
        if total + dur <= max_total_sec:
            picked.append(w)
            total += dur
    return sorted(picked, key=lambda w: w.start)


def excerpt_for_window(tr: dict, start: float, end: float, *, max_chars: int) -> str:
    parts: list[str] = []
    n = 0
    for seg in tr.get("segments") or []:
        try:
            ss = float(seg.get("start_sec"))
            ee = float(seg.get("end_sec"))
        except Exception:
            continue
        if ee < start or ss > end:
            continue
        txt = (seg.get("text") or "").strip()
        if not txt:
            continue
        parts.append(txt)
        n += len(txt) + 1
        if n >= max_chars:
            break
    out = " ".join(parts).strip()
    if len(out) > max_chars:
        out = out[: max_chars - 1] + "…"
    return out


def _row_base_meta(asset_id: str, row: dict[str, str], tr_path: Path, tr: dict) -> dict:
    return {
        "asset_id": asset_id,
        "file": row.get("file") or "",
        "kind": row.get("kind") or "",
        "note": row.get("note") or "",
        "source_path": load_catalog_source_path(asset_id),
        "transcript_path": str(tr_path.relative_to(ROOT)).replace("\\", "/"),
        "playback_duration_sec": tr.get("playback_duration_sec"),
    }


def process_row_heuristic(
    *,
    row: dict[str, str],
    keywords: list[tuple[str, float]],
    pad: float,
    merge_gap: float,
    min_score: float,
    max_total_sec: float | None,
    max_windows: int | None,
    excerpt_chars: int,
) -> dict:
    asset_id = (row.get("asset_id") or "").strip()
    tr_path = TRANSCRIPTS_DIR / f"{asset_id}.transcript.json"
    if not tr_path.exists():
        return {
            "asset_id": asset_id,
            "file": row.get("file") or "",
            "kind": row.get("kind") or "",
            "note": row.get("note") or "",
            "error": f"missing transcript: {tr_path.relative_to(ROOT)}",
        }

    tr = json.loads(tr_path.read_text(encoding="utf-8"))
    hits: list[Hit] = []
    for seg in tr.get("segments") or []:
        text = seg.get("text") or ""
        sc, kw = score_segment(text, keywords)
        if sc < min_score or not kw:
            continue
        try:
            ss = float(seg.get("start_sec"))
            ee = float(seg.get("end_sec"))
        except Exception:
            continue
        hits.append(Hit(start=ss, end=ee, score=sc, keywords=kw))

    merged = merge_hits(hits, pad=pad, merge_gap=merge_gap)
    merged = apply_budget(merged, max_total_sec=max_total_sec, max_windows=max_windows)

    windows_out: list[dict] = []
    for w in merged:
        windows_out.append(
            {
                "start_sec": round(w.start, 3),
                "end_sec": round(w.end, 3),
                "duration_sec": round(max(0.0, w.end - w.start), 3),
                "score": round(w.score, 3),
                "keywords": sorted(w.keywords),
                "text_excerpt": excerpt_for_window(tr, w.start, w.end, max_chars=excerpt_chars),
            }
        )

    out = _row_base_meta(asset_id, row, tr_path, tr)
    out["method"] = "heuristic"
    out["window_count"] = len(windows_out)
    out["windows"] = windows_out
    return out


def process_row_embed(
    *,
    row: dict[str, str],
    model_name: str,
    embed_batch_size: int,
    pad: float,
    merge_gap: float,
    max_total_sec: float | None,
    max_windows: int | None,
    excerpt_chars: int,
    chunk_max_chars: int,
    chunk_max_sec: float,
    sim_relative: float,
    sim_floor: float,
    unify: bool,
    query_extra: str,
    ignore_until_sec: float,
    ignore_tail_sec: float,
    companion_windows: int,
    companion_min_gap_sec: float,
    companion_min_rank_frac: float,
) -> dict:
    asset_id = (row.get("asset_id") or "").strip()
    tr_path = TRANSCRIPTS_DIR / f"{asset_id}.transcript.json"
    if not tr_path.exists():
        return {
            "asset_id": asset_id,
            "file": row.get("file") or "",
            "kind": row.get("kind") or "",
            "note": row.get("note") or "",
            "error": f"missing transcript: {tr_path.relative_to(ROOT)}",
        }

    tr = json.loads(tr_path.read_text(encoding="utf-8"))
    chunks = chunk_transcript(tr, max_chunk_chars=chunk_max_chars, max_chunk_sec=chunk_max_sec)
    if not chunks:
        return {
            "asset_id": asset_id,
            "file": row.get("file") or "",
            "kind": row.get("kind") or "",
            "note": row.get("note") or "",
            "error": f"no spoken-text chunks in transcript: {tr_path.relative_to(ROOT)}",
        }

    raw_query = build_retrieval_query(row, extra=query_extra)
    qtext = format_query_for_embedding(model_name, raw_query)
    play_dur = tr.get("playback_duration_sec")
    play_dur_f = float(play_dur) if play_dur is not None else None

    model = get_text_embedding_model(model_name)
    mat = _stack_model_embeddings(model, [qtext] + [c.text for c in chunks], batch_size=embed_batch_size)
    sims = _cosine_scores(mat[0], mat[1:])
    if ignore_until_sec > 0 or ignore_tail_sec > 0:
        import numpy as np

        sims = np.asarray(sims, dtype=np.float32).copy()
        tail_cut: float | None = None
        if ignore_tail_sec > 0 and play_dur_f is not None and play_dur_f > 0:
            tail_cut = max(0.0, play_dur_f - ignore_tail_sec)
        for i, c in enumerate(chunks):
            if ignore_until_sec > 0 and c.end <= ignore_until_sec:
                sims[i] *= 0.05
            if tail_cut is not None and c.start >= tail_cut:
                sims[i] *= 0.05
    thresh, max_sim = _embed_similarity_threshold(
        sims, sim_relative=sim_relative, sim_floor=sim_floor
    )
    selected = [i for i in range(len(chunks)) if float(sims[i]) >= thresh]
    if not selected:
        order = list(range(len(chunks)))
        order.sort(key=lambda i: float(sims[i]), reverse=True)
        selected = order[: min(3, len(order))]

    # Recall: explicit Grand Teton / FKT phrases can embed weakly vs generic anchors; keep those chunks.
    recall_floor = max(sim_floor * 0.82, sim_floor - 0.06)
    recall_idxs = [
        i
        for i in range(len(chunks))
        if _chunk_recall_bridge(chunks[i].text) and float(sims[i]) >= recall_floor
    ]
    # "teton uh fkt" etc. often embeds poorly vs an NPS/litigation query; still a hard story anchor.
    story_anchor_floor = max(0.055, min(recall_floor * 0.32, sim_floor * 0.28))
    story_anchor_idxs = [
        i
        for i in range(len(chunks))
        if _chunk_strong_story_cue(chunks[i].text) and float(sims[i]) >= story_anchor_floor
    ]
    if recall_idxs or story_anchor_idxs:
        selected = sorted(set(selected) | set(recall_idxs) | set(story_anchor_idxs))

    indexed = [(i, float(sims[i])) for i in selected]
    runs = _cluster_chunks_by_time(indexed, chunks, merge_gap)

    merged_hits: list[Hit] = []

    if unify:
        if runs:
            anchor_runs = [
                r for r in runs if any(_chunk_recall_bridge(chunks[i].text) for i in r)
            ]
            lex_ok = [r for r in runs if _run_lexical_boost(r, chunks) >= 0.88]
            story_ok = [
                r
                for r in lex_ok
                if _run_story_tier(r, chunks) >= 2
                or any(_chunk_strong_story_cue(chunks[i].text) for i in r)
            ]
            if story_ok:
                candidate_pool = story_ok
            elif anchor_runs:
                candidate_pool = anchor_runs
            elif lex_ok:
                candidate_pool = lex_ok
            else:
                candidate_pool = runs
            primary_run = max(
                candidate_pool,
                key=lambda r: (
                    _run_story_tier(r, chunks),
                    _unify_rank_score(r, sims, chunks, play_dur_f),
                ),
            )
            ordered: list[tuple[list[int], str]] = [
                (primary_run, "primary"),
            ]
            for comp in _pick_companion_runs(
                runs,
                primary_run,
                sims,
                chunks,
                playback_duration=play_dur_f,
                max_companions=max(0, companion_windows),
                min_gap=companion_min_gap_sec,
                min_rank_frac=companion_min_rank_frac,
            ):
                ordered.append((comp, "companion"))
            merged_hits = [
                _hit_from_embed_run(
                    run,
                    sims,
                    chunks,
                    pad=pad,
                    max_total_sec=max_total_sec,
                    play_dur_f=play_dur_f,
                    window_role=role,
                )
                for run, role in ordered
            ]
            merged_hits = apply_budget(merged_hits, max_total_sec=max_total_sec, max_windows=max_windows)
            merged_hits.sort(
                key=lambda h: ((0 if (h.window_role or "") == "primary" else 1), h.start),
            )
    else:
        for run in runs:
            rstart = min(chunks[i].start for i in run)
            rend = max(chunks[i].end for i in run)
            peak_sim = max(float(sims[i]) for i in run)
            merged_hits.append(
                Hit(
                    start=max(0.0, rstart - pad),
                    end=rend + pad,
                    score=_run_score(run, sims),
                    keywords=set(),
                    embed_peak=peak_sim,
                )
            )
        merged_hits = apply_budget(merged_hits, max_total_sec=max_total_sec, max_windows=max_windows)

    windows_out: list[dict] = []
    for w in merged_hits:
        wo: dict[str, Any] = {
            "start_sec": round(w.start, 3),
            "end_sec": round(w.end, 3),
            "duration_sec": round(max(0.0, w.end - w.start), 3),
            "score": round(w.score, 3),
            "keywords": sorted(w.keywords),
            "text_excerpt": excerpt_for_window(tr, w.start, w.end, max_chars=excerpt_chars),
        }
        if w.embed_peak is not None:
            wo["embedding_peak_sim"] = round(float(w.embed_peak), 4)
        if w.window_role:
            wo["window_role"] = w.window_role
        windows_out.append(wo)

    out = _row_base_meta(asset_id, row, tr_path, tr)
    out["method"] = "embed"
    out["embedding_model"] = model_name
    out["retrieval_query"] = raw_query if len(raw_query) < 600 else raw_query[:599] + "…"
    out["embed_max_chunk_sim"] = round(max_sim, 4)
    out["embed_threshold"] = round(thresh, 4)
    out["ignore_until_sec"] = float(ignore_until_sec)
    out["ignore_tail_sec"] = float(ignore_tail_sec)
    out["companion_windows"] = int(companion_windows) if unify else 0
    out["unify"] = unify
    out["window_count"] = len(windows_out)
    out["windows"] = windows_out
    return out


def read_csv_rows(path: Path) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    with path.open(encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for r in reader:
            rows.append({k: (v or "") for k, v in r.items()})
    return rows


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    ap.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    ap.add_argument(
        "--method",
        choices=("embed", "heuristic"),
        default="embed",
        help="embed: local BGE via fastembed (no API key). heuristic: keyword scoring.",
    )
    ap.add_argument("--only-many-topics", action="store_true")
    ap.add_argument("--keywords-file", type=Path, default=None)
    ap.add_argument("--pad-sec", type=float, default=45.0)
    ap.add_argument("--merge-gap-sec", type=float, default=20.0)
    ap.add_argument("--min-score", type=float, default=1.0)
    ap.add_argument("--max-total-sec", type=float, default=20 * 60.0)
    ap.add_argument("--max-windows", type=int, default=25)
    ap.add_argument("--excerpt-chars", type=int, default=1200)
    ap.add_argument("--embedding-model", type=str, default="BAAI/bge-small-en-v1.5")
    ap.add_argument("--embed-batch-size", type=int, default=64)
    ap.add_argument("--chunk-max-chars", type=int, default=520)
    ap.add_argument("--chunk-max-sec", type=float, default=75.0)
    ap.add_argument(
        "--sim-relative",
        type=float,
        default=0.82,
        help="Keep chunks with sim >= max_sim * this (after --sim-floor).",
    )
    ap.add_argument(
        "--sim-floor",
        type=float,
        default=0.2,
        help="Minimum cosine similarity to keep a chunk (unless fallback top-3).",
    )
    ap.add_argument(
        "--query-extra",
        type=str,
        default="",
        help="Extra text appended to the retrieval query (embed mode).",
    )
    ap.add_argument(
        "--ignore-until-sec",
        type=float,
        default=0.0,
        help="embed: down-weight chunks that end before this timestamp (×0.05; intros/sponsors).",
    )
    ap.add_argument(
        "--ignore-tail-sec",
        type=float,
        default=0.0,
        help="embed: down-weight chunks that start after (duration - this) (×0.05; outros/plugs).",
    )
    ap.add_argument(
        "--unify",
        dest="unify",
        action="store_true",
        help="embed: merge selected chunks into one time window (default).",
    )
    ap.add_argument(
        "--no-unify",
        dest="unify",
        action="store_false",
        help="embed: emit separate windows per contiguous high-sim run.",
    )
    ap.set_defaults(unify=True)
    ap.add_argument(
        "--companion-windows",
        type=int,
        default=1,
        help="embed+unify: add up to N extra disjoint windows (e.g. later extended segment after an early tease).",
    )
    ap.add_argument(
        "--companion-min-gap-sec",
        type=float,
        default=120.0,
        help="embed: minimum time gap between primary and companion runs.",
    )
    ap.add_argument(
        "--companion-min-rank-frac",
        type=float,
        default=0.72,
        help="embed: companion run must have rank-score ≥ this fraction of primary (see _unify_rank_score).",
    )
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    inp = args.input.resolve()
    out = args.output.resolve()
    if not inp.is_file():
        raise SystemExit(f"Missing input CSV: {inp}")

    keywords = load_keywords(args.keywords_file.resolve() if args.keywords_file else None)

    rows = read_csv_rows(inp)
    if args.only_many_topics:
        rows = [r for r in rows if (r.get("note") or "").strip().lower() == "many topics"]

    buf_lines: list[str] = []
    stats = {"rows_in": 0, "rows_out": 0, "errors": 0, "windows": 0, "method": args.method}

    for r in rows:
        stats["rows_in"] += 1
        if args.method == "heuristic":
            obj = process_row_heuristic(
                row=r,
                keywords=keywords,
                pad=float(args.pad_sec),
                merge_gap=float(args.merge_gap_sec),
                min_score=float(args.min_score),
                max_total_sec=float(args.max_total_sec) if args.max_total_sec is not None else None,
                max_windows=int(args.max_windows) if args.max_windows is not None else None,
                excerpt_chars=int(args.excerpt_chars),
            )
        else:
            obj = process_row_embed(
                row=r,
                model_name=str(args.embedding_model),
                embed_batch_size=int(args.embed_batch_size),
                pad=float(args.pad_sec),
                merge_gap=float(args.merge_gap_sec),
                max_total_sec=float(args.max_total_sec) if args.max_total_sec is not None else None,
                max_windows=int(args.max_windows) if args.max_windows is not None else None,
                excerpt_chars=int(args.excerpt_chars),
                chunk_max_chars=int(args.chunk_max_chars),
                chunk_max_sec=float(args.chunk_max_sec),
                sim_relative=float(args.sim_relative),
                sim_floor=float(args.sim_floor),
                unify=bool(args.unify),
                query_extra=str(args.query_extra or ""),
                ignore_until_sec=float(args.ignore_until_sec),
                ignore_tail_sec=float(args.ignore_tail_sec),
                companion_windows=int(args.companion_windows),
                companion_min_gap_sec=float(args.companion_min_gap_sec),
                companion_min_rank_frac=float(args.companion_min_rank_frac),
            )
        if obj.get("error"):
            stats["errors"] += 1
        else:
            stats["windows"] += int(obj.get("window_count") or 0)
        buf_lines.append(json.dumps(obj, ensure_ascii=False))
        stats["rows_out"] += 1

    text = "\n".join(buf_lines) + ("\n" if buf_lines else "")
    print(f"input:  {inp}")
    print(f"output: {out}")
    print(
        "stats:",
        {k: stats[k] for k in ("method", "rows_in", "rows_out", "errors", "windows")},
    )
    if args.dry_run:
        return
    out.parent.mkdir(parents=True, exist_ok=True)
    atomic_replace_text(out, text)


if __name__ == "__main__":
    main()
