"""
Layer 2: Top-N selects per beat (small LLM use).

For beats with many tagged records, you don't want to wade through everything.
This script takes the top-N candidates per beat (by a simple signal heuristic),
then calls `claude --print` once per beat to rank the best 3 editorial selects.

Outputs:
  _review_drafts/beat_selects_<ts>.md        (editor-readable)
  _review_drafts/beat_selects_<ts>.jsonl     (machine-readable per-beat results)
  _review_drafts/beat_selects_<ts>_errors.jsonl
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import subprocess
import textwrap
import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional


# editor/queries/<this> -> editor/queries -> editor -> open-post-stack
WORKSPACE = Path(__file__).resolve().parent.parent.parent
DATASET_ROOT = WORKSPACE / "dataset"
MOMENTS_PATH = DATASET_ROOT / "story" / "moments.json"
TRANSCRIPTS_DIR = DATASET_ROOT / "assets" / "catalog" / "transcripts"
PROMPT_PATH = DATASET_ROOT / "_prompts" / "beat_select_rank_prompt.md"
REVIEW_DRAFTS_DIR = DATASET_ROOT / "_review_drafts"
ROOT = DATASET_ROOT  # back-compat

DEFAULT_CLAUDE_MODEL = "claude-sonnet-4-6"
# Short JSON ranking: Flash-tier model (2.0-flash retired for new users per API 404).
DEFAULT_GEMINI_MODEL = "gemini-2.5-flash"
DEFAULT_BACKEND = "auto"  # auto | claude | gemini


def _utc_ts_slug(now: Optional[dt.datetime] = None) -> str:
    now = now or dt.datetime.now(dt.timezone.utc)
    return now.strftime("%Y%m%dT%H%M%SZ")


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _load_moments() -> list[dict[str, Any]]:
    raw = _read_json(MOMENTS_PATH)
    return raw.get("moments_outline") or []


def _moment_def(moment: dict[str, Any]) -> str:
    act = moment.get("act")
    act_s = f"{act}" if isinstance(act, int) else "?"
    return f"{moment.get('moment_id')} — {moment.get('title')} (Act {act_s}): {moment.get('summary_one_line')}"


@dataclass(frozen=True)
class Candidate:
    moment_id: str
    asset_id: str
    record_path: str
    duration_sec: float
    shot_kind: str
    subject_of_interview: Optional[str]
    key_quotes: list[dict[str, Any]]
    summary_one_line: Optional[str]

    @property
    def duration_min(self) -> float:
        return float(self.duration_sec or 0.0) / 60.0

    @property
    def key_quotes_count(self) -> int:
        return len(self.key_quotes or [])

    @property
    def is_signal_rich(self) -> bool:
        return bool(self.subject_of_interview and self.key_quotes_count >= 2)

    @property
    def signal_score(self) -> float:
        # Heuristic: quote count dominates; then duration as tie-breaker.
        return (self.key_quotes_count * 100.0) + self.duration_min


def _extract_candidate(record: dict[str, Any], path: Path, moment_id: str) -> Candidate:
    asset_id = str(record.get("asset_id") or path.name.split(".")[0])
    duration_sec = float(record.get("playback_duration_sec") or 0.0)

    craft = record.get("craft") if isinstance(record.get("craft"), dict) else {}
    shot_kind = str((craft or {}).get("shot_kind") or "unknown")

    analysis = record.get("analysis") if isinstance(record.get("analysis"), dict) else {}
    subject = record.get("subject_of_interview")
    if subject is None:
        subject = analysis.get("subject_of_interview")
    if subject is not None:
        subject = str(subject).strip() or None

    key_quotes = analysis.get("key_quotes") if isinstance(analysis.get("key_quotes"), list) else []
    # Keep at most 5 quotes to control prompt size.
    key_quotes = key_quotes[:5]

    summary = analysis.get("summary_one_line")
    if summary is not None:
        summary = str(summary).strip() or None

    return Candidate(
        moment_id=str(moment_id),
        asset_id=asset_id,
        record_path=str(path.as_posix()),
        duration_sec=duration_sec,
        shot_kind=shot_kind,
        subject_of_interview=subject,
        key_quotes=key_quotes,
        summary_one_line=summary,
    )


def _format_candidate_for_prompt(c: Candidate, idx: int) -> str:
    quotes_lines: list[str] = []
    for q in c.key_quotes:
        text = str(q.get("text") or "").strip()
        if not text:
            continue
        why = str(q.get("why") or "").strip()
        if why:
            quotes_lines.append(f'- "{text}" — {why}')
        else:
            quotes_lines.append(f'- "{text}"')
    quotes_block = "\n".join(quotes_lines) if quotes_lines else "(none)"

    return textwrap.dedent(
        f"""
        [#{idx}] asset_id={c.asset_id}
        - duration_min: {c.duration_min:.1f}
        - craft.shot_kind: {c.shot_kind}
        - subject_of_interview: {c.subject_of_interview or "null"}
        - key_quotes_count: {c.key_quotes_count}
        - summary_one_line: {c.summary_one_line or "(none)"}
        - key_quotes (max 5):
        {textwrap.indent(quotes_block, "  ")}
        - record_path: {c.record_path}
        """
    ).strip()


def _call_claude(model: str, prompt: str, timeout_sec: int) -> str:
    proc = subprocess.run(
        ["claude", "--print", "--model", model],
        input=prompt,
        text=True,
        capture_output=True,
        timeout=timeout_sec,
    )
    if proc.returncode != 0:
        stderr = (proc.stderr or "").strip()
        stdout = (proc.stdout or "").strip()
        msg = stderr or stdout or f"claude exited {proc.returncode}"
        raise RuntimeError(msg)
    return (proc.stdout or "").strip()

def _read_hkcu_gemini_key() -> str:
    """Windows: read GEMINI_API_KEY from HKCU\\Environment (persistent user env)."""
    if os.name != "nt":
        return ""
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Environment") as h:
            v, _ = winreg.QueryValueEx(h, "GEMINI_API_KEY")
            return str(v).strip()
    except OSError:
        return ""


def _gemini_key_candidates() -> list[str]:
    """Order: process env first, then HKCU (Windows) if different — fixes stale inherited env in long-lived shells."""
    out: list[str] = []
    proc = (os.getenv("GEMINI_API_KEY") or "").strip()
    if proc:
        out.append(proc)
    if os.name == "nt":
        hkcu = _read_hkcu_gemini_key()
        if hkcu and hkcu not in out:
            out.append(hkcu)
    return out


def _looks_like_gemini_bad_api_key(exc: BaseException) -> bool:
    s = str(exc).lower()
    return "api_key_invalid" in s or "api key expired" in s or "invalid api key" in s


def _looks_like_gemini_quota(exc: BaseException) -> bool:
    s = str(exc).lower()
    return "429" in s or "resource has been exhausted" in s or "quota" in s


def _call_gemini(
    model: str,
    prompt: str,
    timeout_sec: int,
    *,
    max_retries: int = 8,
    initial_backoff_sec: int = 30,
    max_backoff_sec: int = 600,
) -> str:
    import google.generativeai as genai

    keys = _gemini_key_candidates()
    if not keys:
        raise RuntimeError(
            "GEMINI_API_KEY not set. Add it under User environment variables as GEMINI_API_KEY, "
            "or export $env:GEMINI_API_KEY in PowerShell, then re-run with --backend gemini (or --backend auto)."
        )

    last_exc: Optional[BaseException] = None
    for i, api_key in enumerate(keys):
        genai.configure(api_key=api_key)
        m = genai.GenerativeModel(model)
        backoff = initial_backoff_sec
        for attempt in range(max(1, max_retries)):
            try:
                resp = m.generate_content(
                    prompt,
                    generation_config={
                        "temperature": 0.2,
                        "response_mime_type": "application/json",
                        "max_output_tokens": 4096,
                    },
                    request_options={"timeout": timeout_sec},
                )
                text = getattr(resp, "text", None)
                if not text or not str(text).strip():
                    raise RuntimeError(f"empty response from gemini model={model}")
                return str(text).strip()
            except BaseException as e:
                last_exc = e
                if _looks_like_gemini_bad_api_key(e) and i + 1 < len(keys):
                    break
                if _looks_like_gemini_quota(e) and attempt + 1 < max_retries:
                    sleep_for = min(backoff, max_backoff_sec)
                    print(
                        f"[gemini-quota] retry {attempt + 1}/{max_retries}; sleeping {sleep_for}s",
                        flush=True,
                    )
                    time.sleep(sleep_for)
                    backoff = min(int(backoff * 1.5), max_backoff_sec)
                    continue
                raise
    assert last_exc is not None
    raise last_exc


def _choose_backend(backend: str) -> str:
    b = (backend or "").strip().lower()
    if b in ("claude", "gemini"):
        return b
    if b == "auto":
        # Prefer gemini if configured since it's API-key based; claude CLI may be rate-limited.
        if _gemini_key_candidates():
            return "gemini"
        return "claude"
    raise ValueError(f"Unknown backend: {backend}")


def _extract_json_object(text: str) -> dict[str, Any]:
    """Best-effort extraction for cases where the model prints leading/trailing text."""
    s = (text or "").strip()
    if not s:
        raise ValueError("Empty model output")
    if s.startswith("{") and s.endswith("}"):
        return json.loads(s)
    start = s.find("{")
    end = s.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("No JSON object found in model output")
    blob = s[start : end + 1]
    return json.loads(blob)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", default=DEFAULT_BACKEND, help="auto | claude | gemini")
    ap.add_argument(
        "--model",
        default=None,
        help=f"Model id for backend (default: {DEFAULT_CLAUDE_MODEL} / {DEFAULT_GEMINI_MODEL})",
    )
    ap.add_argument("--min-tagged", type=int, default=20, help="Only run LLM on beats with >= this many tagged records")
    ap.add_argument(
        "--min-signal-rich",
        type=int,
        default=1,
        help="Only run LLM on beats with >= this many signal-rich records "
        "(subject_of_interview + ≥2 key_quotes)",
    )
    ap.add_argument("--top-n", type=int, default=20, help="Consider only top-N candidates per beat by signal")
    ap.add_argument("--timeout-sec", type=int, default=600)
    ap.add_argument("--gemini-max-retries", type=int, default=8, help="Retries on Gemini 429/quota errors")
    ap.add_argument(
        "--gemini-initial-backoff-sec",
        type=int,
        default=45,
        help="First backoff after a 429 (seconds); increase if Tier-1 quota is tight",
    )
    ap.add_argument("--gemini-max-backoff-sec", type=int, default=600)
    ap.add_argument(
        "--sleep-between-beats-sec",
        type=float,
        default=-1.0,
        help="After each Gemini beat call, wait this many seconds before the next LLM call. "
        "Default: 120 for gemini *pro* models, 45 for flash, 0 otherwise. Use 0 to disable.",
    )
    ap.add_argument("--out-dir", default=str(REVIEW_DRAFTS_DIR))
    ap.add_argument("--limit-beats", type=int, default=0, help="If >0, only process first N beats (debug)")
    ap.add_argument("--no-llm", action="store_true", help="Heuristic-only ranking (no claude calls)")
    args = ap.parse_args()

    backend = _choose_backend(args.backend)
    if args.model is None:
        args.model = DEFAULT_GEMINI_MODEL if backend == "gemini" else DEFAULT_CLAUDE_MODEL
    if float(args.sleep_between_beats_sec) < 0:
        if backend == "gemini" and not args.no_llm:
            # Pro burns quota faster; default to ~0.5 RPM spacing unless user overrides.
            is_pro = "pro" in str(args.model).lower()
            args.sleep_between_beats_sec = 120.0 if is_pro else 45.0
        else:
            args.sleep_between_beats_sec = 0.0

    moments = _load_moments()
    if args.limit_beats and args.limit_beats > 0:
        moments = moments[: args.limit_beats]

    moments_by_id = {b.get("moment_id"): b for b in moments if isinstance(b, dict) and b.get("moment_id")}
    moment_ids = [b.get("moment_id") for b in moments if isinstance(b, dict) and b.get("moment_id")]

    # Gather candidates per beat
    per_moment: dict[str, list[Candidate]] = defaultdict(list)
    for p in TRANSCRIPTS_DIR.glob("*.transcript.json"):
        try:
            record = _read_json(p)
        except Exception:
            continue
        r_moment_ids = record.get("moment_ids") if isinstance(record.get("moment_ids"), list) else []
        if not r_moment_ids:
            continue
        for mid in r_moment_ids:
            mid = str(mid)
            if mid not in moments_by_id:
                continue
            per_moment[mid].append(_extract_candidate(record, p, mid))

    prompt_header = PROMPT_PATH.read_text(encoding="utf-8").strip()
    ts = _utc_ts_slug()
    out_dir = Path(args.out_dir)
    out_md = out_dir / f"beat_selects_{ts}.md"
    out_jsonl = out_dir / f"beat_selects_{ts}.jsonl"
    out_err = out_dir / f"beat_selects_{ts}_errors.jsonl"
    out_dir.mkdir(parents=True, exist_ok=True)

    md_lines: list[str] = []
    md_lines.append("# Beat selects (Layer 2)\n")
    md_lines.append(f"- Generated at (UTC): **{dt.datetime.now(dt.timezone.utc).isoformat()}**\n")
    md_lines.append(f"- Backend: **{backend}**\n")
    md_lines.append(f"- Model: **{args.model}**\n")
    md_lines.append(
        f"- Moments considered: **{len(moment_ids)}** "
        f"(thresholds min_tagged={args.min_tagged}, min_signal_rich={args.min_signal_rich}, top_n={args.top_n})\n"
    )
    if backend == "gemini" and not args.no_llm:
        md_lines.append(
            f"- Gemini pacing: sleep_between_beats={float(args.sleep_between_beats_sec):g}s, "
            f"429_backoff_initial={int(args.gemini_initial_backoff_sec)}s, "
            f"429_retries={int(args.gemini_max_retries)}\n"
        )
    md_lines.append("\n---\n")

    with out_jsonl.open("w", encoding="utf-8") as fj, out_err.open("w", encoding="utf-8") as fe:
        gemini_beat_calls_done = 0
        for moment_id in moment_ids:
            moment = moments_by_id.get(moment_id) or {}
            candidates = per_moment.get(moment_id, [])
            tagged_total = len(candidates)
            signal_rich_total = sum(1 for c in candidates if c.is_signal_rich)

            if tagged_total < int(args.min_tagged):
                continue
            if signal_rich_total < int(args.min_signal_rich):
                continue

            candidates.sort(key=lambda c: c.signal_score, reverse=True)
            top = candidates[: int(args.top_n)]

            moment_def = _moment_def(moment)
            md_lines.append(f"\n## {moment_id} — {moment.get('title')}\n")
            md_lines.append(f"- Moment: {moment_def}\n")
            md_lines.append(f"- Tagged records: **{tagged_total}**; candidates scored: **{len(top)}**\n\n")

            prompt_parts: list[str] = [prompt_header, "\n\n", "Moment:\n", moment_def, "\n\n", "Candidates:\n"]
            for i, c in enumerate(top, start=1):
                prompt_parts.append(_format_candidate_for_prompt(c, i))
                prompt_parts.append("\n\n")
            prompt = "".join(prompt_parts).strip() + "\n"

            try:
                if (
                    not args.no_llm
                    and backend == "gemini"
                    and gemini_beat_calls_done > 0
                    and float(args.sleep_between_beats_sec) > 0
                ):
                    print(
                        f"[gemini-pacing] sleeping {float(args.sleep_between_beats_sec):g}s before {moment_id} …",
                        flush=True,
                    )
                    time.sleep(float(args.sleep_between_beats_sec))
                if args.no_llm:
                    picks = [c for c in top if c.is_signal_rich]
                    picks.sort(key=lambda c: c.signal_score, reverse=True)
                    if not picks:
                        picks = list(top)
                        picks.sort(key=lambda c: c.signal_score, reverse=True)
                    picks = picks[:3]
                    data = {
                        "moment_id": moment_id,
                        "top_picks": [
                            {
                                "rank": i,
                                "asset_id": c.asset_id,
                                "why": "Heuristic pick (no LLM available): high key_quotes density and duration.",
                                "what_it_covers": [
                                    f"shot_kind={c.shot_kind}",
                                    f"subject_of_interview={c.subject_of_interview or 'null'}",
                                    f"key_quotes_count={c.key_quotes_count}",
                                    f"duration_min={c.duration_min:.1f}",
                                ],
                                "risks_or_notes": [
                                    "Review for redundancy/mistagging; heuristic is a rough filter.",
                                    f"record_path={c.record_path}",
                                ],
                            }
                            for i, c in enumerate(picks, start=1)
                        ],
                        "overall_notes": [
                            "Generated without LLM ranking; run again later without --no-llm for editorial rationale.",
                        ],
                    }
                    fj.write(json.dumps(data, ensure_ascii=False) + "\n")
                else:
                    if backend == "gemini":
                        gemini_beat_calls_done += 1
                        raw = _call_gemini(
                            args.model,
                            prompt,
                            timeout_sec=int(args.timeout_sec),
                            max_retries=int(args.gemini_max_retries),
                            initial_backoff_sec=int(args.gemini_initial_backoff_sec),
                            max_backoff_sec=int(args.gemini_max_backoff_sec),
                        )
                    else:
                        raw = _call_claude(args.model, prompt, timeout_sec=int(args.timeout_sec))
                    data = _extract_json_object(raw)
                    data["moment_id"] = moment_id
                    fj.write(json.dumps(data, ensure_ascii=False) + "\n")

                md_lines.append("### Top picks\n")
                for pick in data.get("top_picks") or []:
                    md_lines.append(
                        f"- **#{pick.get('rank')} {pick.get('asset_id')}** — {pick.get('why')}\n"
                    )
                    covers = pick.get("what_it_covers") or []
                    if covers:
                        md_lines.append("  - covers:\n")
                        for x in covers:
                            md_lines.append(f"    - {x}\n")
                    notes = pick.get("risks_or_notes") or []
                    if notes:
                        md_lines.append("  - notes:\n")
                        for x in notes:
                            md_lines.append(f"    - {x}\n")
                overall = data.get("overall_notes") or []
                if overall:
                    md_lines.append("\n### Overall notes\n")
                    for n in overall:
                        md_lines.append(f"- {n}\n")
            except Exception as e:
                fe.write(json.dumps({"moment_id": moment_id, "error": str(e)}, ensure_ascii=False) + "\n")
                md_lines.append(f"### Error\n- {e}\n")

    out_md.write_text("".join(md_lines), encoding="utf-8")
    print(str(out_md))
    print(str(out_jsonl))
    print(str(out_err))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

