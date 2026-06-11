"""
Transcript analysis pass — Gemini 3.1 Pro Preview (API id `gemini-3.1-pro-preview`),
single-call per record. Override with `--model` (e.g. `gemini-2.5-pro`) if needed.

Replaces the older Claude / `claude --print` runner (kept on disk as
`run_transcript_analysis_skeleton.py` for emergency fallback) and the
deferred-only `run_deferred_via_gemini.py` (replaced by this script).

Why Gemini API for everything:
- 1M context window handles every transcript in a single call. No
  chunking, no merge logic, no long-transcript deferral bucket.
- API path is more reliable than the Claude CLI for a long batch.

Selection (worklist):
- Any record where `analysis.analyzed_at` is missing, OR
- `analysis.summary_one_line` is empty, OR
- `analysis.prompt_sha256` doesn't match the current canonical prompt SHA.
- Or, if `--only-asset-id` / `--only-asset-ids-file` is set: **only** those ids
  (forces a re-run even when the prompt SHA already matches — use with
  `--no-heuristic-skip` to replace heuristic stubs).
- Records on EXCLUDED_IDS or supplied via --exclude-asset-id are skipped.

Heuristic skip is preserved (no LLM call for trivial slates / empty-Whisper
clips). It's a cost-savings path, not a size workaround.

Inputs / outputs:
- Reads `_prompts/transcript_analysis_prompt.md` (regenerate via
  `_scripts/transcripts/build_transcript_prompt_context.py` after registry changes).
- Writes per-record JSON commits atomically; appends `_runs/<pass_name>/`
  manifest + log.jsonl + errors.jsonl per `_runs/README.md`.

Prerequisites:
- pip install -r _scripts/requirements_gemini.txt
- **Developer API (default):** $env:GEMINI_API_KEY = "<your-key>"
- **Vertex / Gemini Enterprise Agent Platform** (optional): set
  $env:GOOGLE_GENAI_USE_VERTEXAI = "True" and either:
  - $env:GOOGLE_CLOUD_PROJECT, $env:GOOGLE_CLOUD_LOCATION (e.g. global) and ADC
    (`gcloud auth application-default login`), or
  - Express mode: $env:GOOGLE_API_KEY with Vertex API key and pass `--vertex`.
  Then run with `--vertex` or rely on GOOGLE_GENAI_USE_VERTEXAI alone; calls use
  the unified `google-genai` client instead of `google-generativeai`.

Run from PowerShell:
    cd <workspace root>
    python _scripts\\run_transcript_analysis_via_gemini.py --max-records 5  # smoke test
    python _scripts\\run_transcript_analysis_via_gemini.py                  # full pass
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sqlite3
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from _lib.linked_assets import neighbor_target_ids  # noqa: E402
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))  # shared modules live at _scripts root
from workspace_paths import clip_and_still_embeddings_sqlite_path  # noqa: E402  # optional legacy ingest

TRANSCRIPTS = ROOT / "assets/transcripts"
PROMPT_PATH = ROOT / "_prompts/transcript_analysis_prompt.md"
RUNS_DIR = ROOT / "_runs"
LOCK_PATH = ROOT / "_runs" / ".transcript_analysis_via_gemini.lock"
DB_PATH = clip_and_still_embeddings_sqlite_path()

# Lazy singleton for Vertex / google-genai (thread-safe).
_VERTEX_CLIENT = None
_VERTEX_CLIENT_LOCK = threading.Lock()
VIDEO_DIR = ROOT / "assets/video"
AUDIO_DIR = ROOT / "assets/audio"

# Gemini 3.1 Pro Preview — https://ai.google.dev/gemini-api/docs/models/gemini-3.1-pro-preview
DEFAULT_MODEL = "gemini-3.1-pro-preview"
# Prefer failing fast on transient overload; we back off + retry.
DEFAULT_TIMEOUT_SEC = 120
DEFAULT_CONCURRENCY = 2
DEFAULT_INITIAL_BACKOFF_SEC = 30
DEFAULT_MAX_BACKOFF_SEC = 600
DEFAULT_MAX_RETRIES = 8
DEFAULT_MAX_OUTPUT_TOKENS = 16384

# Cowboy State Daily podcasts — content excluded by editor decision; not a
# technical exclusion. Add via --exclude-asset-id at the CLI for one-off
# excludes.
EXCLUDED_IDS = {
    "06e39153a6a30bbc105f58bc150b9779439668eecda9af48c581e83783a90cba",
    "44530975e706a95f7d2a5336b9a02c97f97bbd1812ee3d616ba34ce0fc5894c3",
}

# Reuse existing helpers from the Claude skeleton — atomic writes, lock,
# heuristic-skip detection, JSON parser, segment compaction, prompt-message
# builder. The Claude-specific pieces (CLI invocation, rate-limit detection)
# stay unused here.
sys.path.insert(0, str(ROOT / "_scripts"))
from run_transcript_analysis_skeleton import (  # noqa: E402
    SingleRunner,
    atomic_write_json,
    append_log,
    parse_response_json,
    build_user_message,
    cleanup_stale_tmps,
    needs_processing,
    is_trivial_record,
    trivial_analysis_output,
)
from validate_transcript_analysis import Validator  # noqa: E402
from build_transcript_prompt_context import canonical_prompt_sha  # noqa: E402


# ----------------------------------------------------------------------
# Gemini client
# ----------------------------------------------------------------------
def _env_truthy(name: str) -> bool:
    v = os.getenv(name, "").strip().lower()
    return v in ("1", "true", "yes", "on")


def _vertex_transport_enabled() -> bool:
    return _env_truthy("GOOGLE_GENAI_USE_VERTEXAI")


def _vertex_force_api_key_only() -> bool:
    """Prefer express-style Vertex auth (API key) even if GOOGLE_CLOUD_PROJECT is set."""
    return _env_truthy("VERTEX_API_KEY_ONLY")


def _ensure_legacy_generativeai_configured() -> None:
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError(
            "GEMINI_API_KEY env var not set. Set it via "
            "$env:GEMINI_API_KEY = '<your-key>' in PowerShell, then re-run."
        )
    import google.generativeai as genai
    genai.configure(api_key=api_key)


def _ensure_vertex_genai_configured() -> None:
    try:
        from google import genai as google_genai  # noqa: F401
    except ImportError as e:
        raise RuntimeError(
            "Vertex mode requires the google-genai package. Install with: "
            "pip install -r _scripts/requirements_gemini.txt"
        ) from e
    project = (os.getenv("GOOGLE_CLOUD_PROJECT") or "").strip()
    location = (os.getenv("GOOGLE_CLOUD_LOCATION") or "global").strip()
    api_key = (os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY") or "").strip()
    if _vertex_force_api_key_only() and not api_key:
        raise RuntimeError(
            "Vertex --vertex-api-key mode requires GOOGLE_API_KEY or GEMINI_API_KEY."
        )
    if not project and not api_key:
        raise RuntimeError(
            "Vertex mode: set GOOGLE_CLOUD_PROJECT (+ GOOGLE_CLOUD_LOCATION, ADC via "
            "gcloud auth application-default login) or set GOOGLE_API_KEY for express mode."
        )
    _ = google_genai  # use import side effects only; client built lazily


def _get_vertex_genai_client():
    """Singleton google.genai Client for Vertex / Agent Platform."""
    global _VERTEX_CLIENT
    with _VERTEX_CLIENT_LOCK:
        if _VERTEX_CLIENT is not None:
            return _VERTEX_CLIENT
        from google import genai
        from google.genai import types

        project = (os.getenv("GOOGLE_CLOUD_PROJECT") or "").strip()
        location = (os.getenv("GOOGLE_CLOUD_LOCATION") or "global").strip()
        api_key = (os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY") or "").strip()
        force_key = _vertex_force_api_key_only()
        # google.genai HttpOptions.timeout is in milliseconds (not seconds).
        _http_timeout_ms = int(DEFAULT_TIMEOUT_SEC * 1000)

        # Express / global API key: use key path even when GOOGLE_CLOUD_PROJECT is set
        # (common when gcloud left project in the environment).
        if force_key and api_key:
            http_opts = types.HttpOptions(timeout=_http_timeout_ms)
            _VERTEX_CLIENT = genai.Client(
                vertexai=True,
                api_key=api_key,
                http_options=http_opts,
            )
        elif project:
            http_opts = types.HttpOptions(
                api_version="v1",
                timeout=_http_timeout_ms,
            )
            _VERTEX_CLIENT = genai.Client(
                vertexai=True,
                project=project,
                location=location,
                http_options=http_opts,
            )
        elif api_key:
            http_opts = types.HttpOptions(timeout=_http_timeout_ms)
            _VERTEX_CLIENT = genai.Client(
                vertexai=True,
                api_key=api_key,
                http_options=http_opts,
            )
        else:
            raise RuntimeError(
                "Vertex client: set GOOGLE_CLOUD_PROJECT (+ ADC) or "
                "GOOGLE_API_KEY / GEMINI_API_KEY (use --vertex-api-key to force key auth "
                "when a project id is also present in the environment)."
            )
        return _VERTEX_CLIENT


def _ensure_gemini_configured() -> None:
    if _vertex_transport_enabled():
        _ensure_vertex_genai_configured()
    else:
        _ensure_legacy_generativeai_configured()


_QUOTA_KEYWORDS = (
    "quota",
    "rate",
    "429",
    "resource_exhausted",
    # Some SDKs surface timeouts as "... exceeded"
    "exceeded",
    # Treat overloads as retryable
    "503",
    "unavailable",
    "high demand",
)


def _is_quota_error(e: Exception) -> bool:
    s = str(e).lower()
    return any(k in s for k in _QUOTA_KEYWORDS)


def _finish_reason_hits_max_tokens(fr) -> bool:
    fr_str = str(fr).upper()
    return "MAX_TOKENS" in fr_str or fr_str.endswith(".2") or fr_str == "2"


def _call_gemini_vertex_once(
    prompt_input: str,
    *,
    model_name: str,
    max_output_tokens: int,
) -> str:
    from google.genai import types

    client = _get_vertex_genai_client()
    config = types.GenerateContentConfig(
        temperature=0.2,
        response_mime_type="application/json",
        max_output_tokens=max_output_tokens,
    )
    response = client.models.generate_content(
        model=model_name,
        contents=prompt_input,
        config=config,
    )
    try:
        cands = getattr(response, "candidates", None) or []
        if cands:
            fr = getattr(cands[0], "finish_reason", None)
            if fr is not None and _finish_reason_hits_max_tokens(fr):
                raise RuntimeError(
                    f"gemini truncated at max_output_tokens={max_output_tokens}; "
                    "raise this limit and retry"
                )
    except (AttributeError, IndexError):
        pass
    text = getattr(response, "text", None)
    if not text or not str(text).strip():
        raise RuntimeError(f"empty response from {model_name}")
    return str(text)


def _call_gemini_legacy_once(
    prompt_input: str,
    *,
    model_name: str,
    timeout_sec: int,
    max_output_tokens: int,
) -> str:
    import google.generativeai as genai

    model = genai.GenerativeModel(model_name)
    response = model.generate_content(
        prompt_input,
        generation_config={
            "temperature": 0.2,
            "response_mime_type": "application/json",
            "max_output_tokens": max_output_tokens,
        },
        request_options={"timeout": timeout_sec},
    )
    try:
        fr = response.candidates[0].finish_reason
        if _finish_reason_hits_max_tokens(fr):
            raise RuntimeError(
                f"gemini truncated at max_output_tokens={max_output_tokens}; "
                "raise this limit and retry"
            )
    except (AttributeError, IndexError):
        pass
    text = response.text
    if not text or not text.strip():
        raise RuntimeError(f"empty response from {model_name}")
    return text


def call_gemini_with_resume(
    prompt_input: str,
    *,
    model_name: str = DEFAULT_MODEL,
    timeout_sec: int = DEFAULT_TIMEOUT_SEC,
    max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
    initial_backoff_sec: int = DEFAULT_INITIAL_BACKOFF_SEC,
    max_backoff_sec: int = DEFAULT_MAX_BACKOFF_SEC,
    max_retries: int = DEFAULT_MAX_RETRIES,
) -> str:
    """Call Gemini with exponential backoff on quota / transient errors."""
    backoff = initial_backoff_sec
    retries = 0
    while True:
        try:
            if _vertex_transport_enabled():
                return _call_gemini_vertex_once(
                    prompt_input,
                    model_name=model_name,
                    max_output_tokens=max_output_tokens,
                )
            return _call_gemini_legacy_once(
                prompt_input,
                model_name=model_name,
                timeout_sec=timeout_sec,
                max_output_tokens=max_output_tokens,
            )
        except Exception as e:
            if _is_quota_error(e) and retries < max_retries:
                sleep_for = min(backoff, max_backoff_sec)
                print(
                    f"[gemini-quota] thread={threading.get_ident()} retry={retries+1}/{max_retries}; "
                    f"sleeping {sleep_for}s. err: {str(e)[:200]}",
                    flush=True,
                )
                time.sleep(sleep_for)
                backoff = min(int(backoff * 1.5), max_backoff_sec)
                retries += 1
                continue
            raise


def _load_json_if_exists(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _linked_asset_ids_for_transcript(asset_id: str) -> list[str]:
    """
    Gather a small set of linked asset_ids (same-kind co-recordings and
    audio<->video links) to provide extra semantic context.
    """
    linked: set[str] = set()

    v = _load_json_if_exists(VIDEO_DIR / f"{asset_id}.video.json")
    a = _load_json_if_exists(AUDIO_DIR / f"{asset_id}.audio.json")

    if v:
        for x in neighbor_target_ids(v):
            if x != asset_id:
                linked.add(x)

    if a:
        for x in neighbor_target_ids(a):
            if x != asset_id:
                linked.add(x)

    # Reverse direction: if this transcript is for video, we already got linked audios;
    # if it's for audio, we got linked video. That's enough.
    return sorted(linked)


def _fetch_gemini_semantic_rows(parent_asset_id: str, *, limit: int = 3) -> list[dict]:
    """
    Pull chunk semantics from catalog `asset_semantic_summary` for a given asset id.
    Returns compact dicts; safe to JSON-serialize and hand to Gemini as context.
    """
    from semantic_catalog import read_catalog_record

    rec = read_catalog_record(ROOT, parent_asset_id)
    if not rec:
        return []
    sm = rec.get("asset_semantic_summary")
    if not isinstance(sm, dict):
        return []
    chunks = sm.get("chunks")
    if not isinstance(chunks, list):
        return []
    out: list[dict] = []
    for ch in chunks[: int(limit)]:
        if not isinstance(ch, dict):
            continue
        semantic = {
            k: ch[k]
            for k in (
                "subject", "action", "setting", "camera", "audio_character",
                "emotional_tone", "editorial_notes", "key_moments",
            )
            if k in ch
        }
        if not semantic:
            continue
        out.append({
            "chunk_id": ch.get("chunk_id"),
            "start_sec": ch.get("start_sec"),
            "end_sec": ch.get("end_sec"),
            "semantic": semantic,
        })
    return out


def _compact_semantic_summary(semantic_rows: list[dict], *, max_chars: int = 4000) -> dict | None:
    """
    Deterministically compress Gemini semantic JSON into a smaller prompt payload.
    """
    if not semantic_rows:
        return None

    def pick(d: dict | None, key: str):
        if not isinstance(d, dict):
            return None
        v = d.get(key)
        return v if v not in ("", None, [], {}) else None

    chunks = []
    for r in semantic_rows:
        sem = r.get("semantic") if isinstance(r, dict) else None
        if not isinstance(sem, dict):
            continue
        chunks.append({
            "t": [r.get("start_sec"), r.get("end_sec")],
            "subject": pick(sem, "subject"),
            "action": pick(sem, "action"),
            "setting": pick(sem, "setting"),
            "audio_character": pick(sem, "audio_character"),
            "emotional_tone": pick(sem, "emotional_tone"),
            "editorial_notes": pick(sem, "editorial_notes"),
            "key_moments": pick(sem, "key_moments"),
        })

    summary = {"source": "catalog:asset_semantic_summary", "chunks": chunks}
    # Hard cap size (Gemini 2.5 Pro can take more, but we keep this compact).
    s = json.dumps(summary, ensure_ascii=False)
    if len(s) <= max_chars:
        return summary

    # If too large, progressively drop heavy fields.
    for c in chunks:
        c.pop("editorial_notes", None)
        c.pop("key_moments", None)
    summary2 = {"source": summary["source"], "chunks": chunks}
    s2 = json.dumps(summary2, ensure_ascii=False)
    if len(s2) <= max_chars:
        return summary2

    # Last resort: only keep timing + setting/action/subject.
    chunks3 = []
    for c in chunks:
        chunks3.append({
            "t": c.get("t"),
            "subject": c.get("subject"),
            "action": c.get("action"),
            "setting": c.get("setting"),
        })
    return {"source": summary["source"], "chunks": chunks3}


def analyze_one_via_gemini(
    prompt_text: str,
    transcript_record: dict,
    *,
    model_name: str = DEFAULT_MODEL,
    include_semantic_context: bool = True,
    max_linked_assets: int = 4,
) -> tuple[dict, dict]:
    """Single-call Gemini analysis. Returns (parsed_dict, metrics)."""
    extra: dict = {}
    if include_semantic_context:
        primary_rows = _fetch_gemini_semantic_rows(transcript_record.get("asset_id") or "")
        primary_summary = _compact_semantic_summary(primary_rows)
        if primary_summary:
            extra["asset_semantic_summary"] = primary_summary

        linked_ids = _linked_asset_ids_for_transcript(transcript_record.get("asset_id") or "")
        linked_summaries = []
        for lid in linked_ids[: max(0, int(max_linked_assets))]:
            rows = _fetch_gemini_semantic_rows(lid)
            s = _compact_semantic_summary(rows)
            if s:
                linked_summaries.append({"asset_id": lid, "summary": s})
        if linked_summaries:
            extra["linked_assets_semantic_summaries"] = linked_summaries

    user_msg = build_user_message(transcript_record, extra_context=extra or None)
    full_input = prompt_text + "\n\n---\n\n" + user_msg
    response_text = call_gemini_with_resume(full_input, model_name=model_name)
    metrics = {
        "input_chars": len(full_input),
        "input_tokens_est": len(full_input) // 4,
        "output_chars": len(response_text),
        "output_tokens_est": len(response_text) // 4,
    }
    return parse_response_json(response_text), metrics


# ----------------------------------------------------------------------
# Per-record processor
# ----------------------------------------------------------------------
@dataclass
class ProcessResult:
    asset_id: str
    ok: bool
    error: str = ""
    duration_ms: int = 0
    retried: bool = False
    skipped_via_heuristic: bool = False
    skipped_excluded: bool = False
    input_tokens_est: int = 0
    output_tokens_est: int = 0


def _stamp_analysis(out: dict, prompt_sha: str, analyzer_str: str) -> None:
    out.setdefault("analysis", {})
    out["analysis"]["analyzed_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
    out["analysis"]["prompt_sha256"] = prompt_sha
    out["analysis"]["analyzer"] = analyzer_str
    # Clear any legacy deferred markers if the model echoed them.
    out["analysis"].pop("deferred_reason", None)


def process_one(p: Path, prompt_text: str, prompt_sha: str, validator: Validator,
                *, model_name: str, pass_name: str,
                heuristic_skip: bool, excluded_ids: set[str],
                log_path: Path, errors_path: Path,
                include_semantic_context: bool,
                max_linked_assets: int) -> ProcessResult:
    aid_fallback = p.stem.replace(".transcript", "")
    try:
        rec = json.loads(p.read_text(encoding="utf-8"))
        aid = rec.get("asset_id") or aid_fallback

        # ---- Excluded by editor decision ----
        if aid in excluded_ids:
            append_log(log_path, {
                "asset_id": aid, "ok": True,
                "skipped_excluded": True, "reason": "excluded_id",
            })
            return ProcessResult(asset_id=aid, ok=True, skipped_excluded=True)

        # ---- Heuristic skip path (no LLM) ----
        if heuristic_skip:
            trivial, reason = is_trivial_record(rec)
            if trivial:
                analysis_out = trivial_analysis_output()
                _stamp_analysis(
                    analysis_out, prompt_sha,
                    f"heuristic-skip (transcript-batch {pass_name})",
                )
                v = validator.validate(analysis_out, rec)
                if not v.ok:
                    raise RuntimeError(f"trivial output failed validator: {v.errors}")
                merged = validator.merge(rec, analysis_out)
                # Stamp authoritative fields after merge in case merge replaced analysis.
                merged.setdefault("analysis", {})
                merged["analysis"]["analyzed_at"] = analysis_out["analysis"]["analyzed_at"]
                merged["analysis"]["prompt_sha256"] = prompt_sha
                merged["analysis"]["analyzer"] = analysis_out["analysis"]["analyzer"]
                atomic_write_json(p, merged)
                append_log(log_path, {
                    "asset_id": aid, "ok": True, "duration_ms": 0,
                    "skipped_via_heuristic": True, "reason": reason,
                })
                return ProcessResult(asset_id=aid, ok=True, skipped_via_heuristic=True)

        # ---- LLM path ----
        t0 = time.time()
        analysis_out, m1 = analyze_one_via_gemini(
            prompt_text,
            rec,
            model_name=model_name,
            include_semantic_context=include_semantic_context,
            max_linked_assets=max_linked_assets,
        )
        duration_ms = int((time.time() - t0) * 1000)
        total_input_tokens = m1["input_tokens_est"]
        total_output_tokens = m1["output_tokens_est"]

        retried = False
        v = validator.validate(analysis_out, rec)
        if not v.ok:
            retried = True
            retry_prompt = validator.build_retry_prompt(analysis_out, v.errors)
            analysis_out, m2 = analyze_one_via_gemini(
                prompt_text + "\n\n" + retry_prompt,
                rec,
                model_name=model_name,
                include_semantic_context=include_semantic_context,
                max_linked_assets=max_linked_assets,
            )
            total_input_tokens += m2["input_tokens_est"]
            total_output_tokens += m2["output_tokens_est"]
            v = validator.validate(analysis_out, rec)
            if not v.ok:
                append_log(errors_path, {
                    "asset_id": aid, "ok": False,
                    "error": "validation_failed_after_retry",
                    "details": v.errors,
                    "input_tokens_est": total_input_tokens,
                    "output_tokens_est": total_output_tokens,
                })
                append_log(log_path, {
                    "asset_id": aid, "ok": False,
                    "error": "validation_failed_after_retry",
                    "input_tokens_est": total_input_tokens,
                    "output_tokens_est": total_output_tokens,
                })
                return ProcessResult(asset_id=aid, ok=False, retried=True,
                                     error="validation_failed_after_retry",
                                     input_tokens_est=total_input_tokens,
                                     output_tokens_est=total_output_tokens)

        _stamp_analysis(
            analysis_out, prompt_sha,
            f"{model_name} (transcript-batch {pass_name})",
        )
        merged = validator.merge(rec, analysis_out)
        # The merge stomps the analysis block, then we re-stamp the authoritative
        # fields so they survive.
        merged.setdefault("analysis", {})
        merged["analysis"]["analyzed_at"] = analysis_out["analysis"]["analyzed_at"]
        merged["analysis"]["prompt_sha256"] = prompt_sha
        merged["analysis"]["analyzer"] = analysis_out["analysis"]["analyzer"]
        merged.get("analysis", {}).pop("deferred_reason", None)

        atomic_write_json(p, merged)
        append_log(log_path, {
            "asset_id": aid, "ok": True, "duration_ms": duration_ms,
            "retried": retried, "warnings": v.warnings,
            "input_tokens_est": total_input_tokens,
            "output_tokens_est": total_output_tokens,
        })
        return ProcessResult(asset_id=aid, ok=True, duration_ms=duration_ms,
                             retried=retried,
                             input_tokens_est=total_input_tokens,
                             output_tokens_est=total_output_tokens)

    except Exception as e:
        aid = aid_fallback
        try:
            aid = rec.get("asset_id") or aid_fallback  # type: ignore[name-defined]
        except Exception:
            pass
        msg = str(e)[:500]
        append_log(errors_path, {"asset_id": aid, "ok": False, "error": msg})
        append_log(log_path, {"asset_id": aid, "ok": False, "error": msg})
        return ProcessResult(asset_id=aid, ok=False, error=msg)


# ----------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pass-name", default=None,
                    help="Run subdir name in _runs/. Defaults to transcript_analysis_via_gemini_<ts>.")
    ap.add_argument("--max-records", type=int, default=0,
                    help="Cap N records (0 = no cap; useful for test runs).")
    ap.add_argument("--max-window-seconds", type=int, default=14400,
                    help="Soft total runtime cap (default 4h).")
    ap.add_argument("--model", default=DEFAULT_MODEL,
                    help=f"Gemini model id. Default: {DEFAULT_MODEL}")
    ap.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY,
                    help=f"Worker threads. Default: {DEFAULT_CONCURRENCY}.")
    ap.add_argument(
        "--no-semantic-context",
        action="store_true",
        help="Disable adding semantic context from catalog asset_semantic_summary to each call.",
    )
    ap.add_argument(
        "--max-linked-assets",
        type=int,
        default=4,
        help="Max linked asset ids to include semantic summaries for (default 4).",
    )
    ap.add_argument("--no-heuristic-skip", action="store_true",
                    help="Disable the no-LLM fast path for trivially short utility records.")
    ap.add_argument("--only-new", action="store_true",
                    help="Restrict the worklist to records that have never been analyzed "
                         "(analysis.analyzed_at is None). Skips records that were stamped under "
                         "an older prompt SHA — useful for catching up on newly-ingested transcripts "
                         "without re-running the full corpus.")
    ap.add_argument("--exclude-asset-id", action="append", default=[],
                    help="Asset id to exclude this run (repeatable). Adds to the built-in EXCLUDED_IDS set.")
    ap.add_argument(
        "--only-asset-id",
        action="append",
        default=[],
        help="Process only this asset_id (repeatable). Ignores only-new / needs_processing gates.",
    )
    ap.add_argument(
        "--only-asset-ids-file",
        default=None,
        help="Text file: one asset_id per line (# comments ok). Same as repeating --only-asset-id.",
    )
    ap.add_argument(
        "--refresh-indexes",
        action="store_true",
        help="After successful mutations, rebuild MANIFEST.json and ../indexes/editorial_catalog.sqlite.",
    )
    ap.add_argument(
        "--vertex",
        action="store_true",
        help="Call Gemini via google-genai on Vertex / Gemini Enterprise Agent Platform "
             "(sets GOOGLE_GENAI_USE_VERTEXAI). Needs GOOGLE_CLOUD_PROJECT + "
             "GOOGLE_CLOUD_LOCATION + ADC, or GOOGLE_API_KEY for express mode.",
    )
    ap.add_argument(
        "--vertex-api-key",
        action="store_true",
        help="With --vertex: use GOOGLE_API_KEY or GEMINI_API_KEY only (Vertex express), "
             "even if GOOGLE_CLOUD_PROJECT is set in the environment.",
    )
    args = ap.parse_args()

    if args.vertex:
        os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "True"
    if args.vertex_api_key:
        os.environ["VERTEX_API_KEY_ONLY"] = "True"
        os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "True"
    if not args.vertex and not args.vertex_api_key:
        # Avoid a globally set GOOGLE_GENAI_USE_VERTEXAI forcing Vertex when this
        # invocation did not ask for it.
        os.environ.pop("GOOGLE_GENAI_USE_VERTEXAI", None)
        os.environ.pop("VERTEX_API_KEY_ONLY", None)

    only_ids: set[str] = {x.strip() for x in args.only_asset_id if x.strip()}
    if args.only_asset_ids_file:
        ids_path = Path(args.only_asset_ids_file)
        if not ids_path.is_file():
            print(f"ERROR: --only-asset-ids-file not found: {ids_path}", file=sys.stderr)
            return 1
        for line in ids_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                only_ids.add(line)

    _ensure_gemini_configured()

    excluded_ids = set(EXCLUDED_IDS) | {x.strip() for x in args.exclude_asset_id if x.strip()}

    with SingleRunner(LOCK_PATH):
        if not PROMPT_PATH.exists():
            print(f"ERROR: prompt not found at {PROMPT_PATH}", file=sys.stderr)
            print("Run _scripts/transcripts/build_transcript_prompt_context.py first.", file=sys.stderr)
            return 1

        n_stale = cleanup_stale_tmps()
        if n_stale:
            print(f"[cleanup] removed {n_stale} stale .tmp files")

        prompt_text = PROMPT_PATH.read_text(encoding="utf-8")
        prompt_sha = canonical_prompt_sha(prompt_text)

        ts = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d_%H%M")
        pass_name = args.pass_name or f"transcript_analysis_via_gemini_{ts}"
        run_dir = RUNS_DIR / pass_name
        run_dir.mkdir(parents=True, exist_ok=True)

        manifest_path = run_dir / "manifest.json"
        log_path = run_dir / "log.jsonl"
        errors_path = run_dir / "errors.jsonl"

        if not manifest_path.exists():
            atomic_write_json(manifest_path, {
                "run_id": pass_name,
                "pass_name": "transcript_analysis_via_gemini",
                "started_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                "prompt_path": str(PROMPT_PATH.relative_to(ROOT)),
                "prompt_sha256": prompt_sha,
                "script_path": str(Path(__file__).relative_to(ROOT)),
                "model": args.model,
                "model_invocation": (
                    "google.genai (Vertex / Gemini Enterprise Agent Platform)"
                    if _vertex_transport_enabled()
                    else "google.generativeai (Gemini Developer API)"
                ),
                "concurrency": args.concurrency,
                "heuristic_skip_enabled": not args.no_heuristic_skip,
                "scope": (
                    "only-asset-id"
                    if only_ids
                    else ("only-new" if args.only_new else "needs_processing")
                ),
                "only_asset_ids": sorted(only_ids) if only_ids else [],
                "excluded_ids": sorted(excluded_ids),
                "domain": "transcripts",
                "input_count": None,
                "validated_pass": 0,
                "skipped_via_heuristic_count": 0,
                "skipped_excluded_count": 0,
                "retried_count": 0,
                "final_failure_count": 0,
                "completed_at": None,
            })

        validator = Validator.from_workspace()

        all_records = sorted(TRANSCRIPTS.glob("*.json"))
        todo: list[Path] = []
        if only_ids:
            by_id: dict[str, Path] = {}
            for p in all_records:
                try:
                    rec = json.loads(p.read_text(encoding="utf-8"))
                except Exception:
                    continue
                aid = rec.get("asset_id") or p.stem.replace(".transcript", "")
                if aid in only_ids:
                    by_id[aid] = p
            missing = sorted(only_ids - set(by_id.keys()))
            if missing:
                print(
                    f"[warn] {len(missing)} only-asset-id not found in catalog: "
                    f"{missing[:5]}{'…' if len(missing) > 5 else ''}",
                    file=sys.stderr,
                )
            todo = [by_id[i] for i in sorted(only_ids) if i in by_id]
            scope_note = "only-asset-id"
        else:
            for p in all_records:
                try:
                    rec = json.loads(p.read_text(encoding="utf-8"))
                except Exception:
                    continue
                if args.only_new:
                    # Restrict to records that have never been analyzed; ignore stale-SHA churn.
                    a = rec.get("analysis") or {}
                    if a.get("analyzed_at") is None:
                        todo.append(p)
                else:
                    if needs_processing(rec, prompt_sha):
                        todo.append(p)
            scope_note = "only-new" if args.only_new else "all stale + missing"
        print(f"[plan] {len(todo)} of {len(all_records)} records in worklist (scope: {scope_note})")
        _backend = "vertex/google-genai" if _vertex_transport_enabled() else "developer/google-generativeai"
        print(f"[plan] model={args.model}, concurrency={args.concurrency}, backend={_backend}, "
              f"heuristic_skip={'on' if not args.no_heuristic_skip else 'off'}, "
              f"excluded={len(excluded_ids)}")

        if args.max_records:
            todo = todo[: args.max_records]
            print(f"[plan] capping at --max-records={args.max_records}")

        run_started = time.time()
        ok_count = 0
        fail_count = 0
        retried_count = 0
        heuristic_skip_count = 0
        skipped_excluded_count = 0
        total_input_tokens = 0
        total_output_tokens = 0
        time_capped = False
        interrupted = False

        ex = ThreadPoolExecutor(max_workers=args.concurrency)
        try:
            futures = {
                ex.submit(
                    process_one, p, prompt_text, prompt_sha, validator,
                    model_name=args.model, pass_name=pass_name,
                    heuristic_skip=not args.no_heuristic_skip,
                    excluded_ids=excluded_ids,
                    log_path=log_path, errors_path=errors_path,
                    include_semantic_context=not args.no_semantic_context,
                    max_linked_assets=int(args.max_linked_assets),
                ): p for p in todo
            }
            try:
                for fut in as_completed(futures):
                    r = fut.result()
                    if r.ok:
                        ok_count += 1
                        if r.skipped_via_heuristic:
                            heuristic_skip_count += 1
                        if r.skipped_excluded:
                            skipped_excluded_count += 1
                    else:
                        fail_count += 1
                    if r.retried:
                        retried_count += 1
                    total_input_tokens += r.input_tokens_est
                    total_output_tokens += r.output_tokens_est

                    done = ok_count + fail_count
                    if done % 25 == 0 or done == len(todo):
                        elapsed = time.time() - run_started
                        rate = done / elapsed if elapsed > 0 else 0
                        print(
                            f"[progress] {done}/{len(todo)}  "
                            f"ok={ok_count} (heur={heuristic_skip_count}, "
                            f"excl={skipped_excluded_count})  "
                            f"fail={fail_count}  "
                            f"rate={rate:.2f} rec/s ({rate*3600:.0f}/hr)  "
                            f"elapsed={elapsed/60:.1f}min  "
                            f"in_toks={total_input_tokens:,}",
                            flush=True,
                        )

                    if not time_capped and (time.time() - run_started > args.max_window_seconds):
                        print(f"[time-cap] >{args.max_window_seconds}s; cancelling pending workers")
                        ex.shutdown(wait=False, cancel_futures=True)
                        time_capped = True
            except KeyboardInterrupt:
                interrupted = True
                print(
                    "\n[interrupt] Ctrl+C; cancelling pending workers, "
                    "in-flight Gemini calls will exit on their own.",
                    flush=True,
                )
                ex.shutdown(wait=False, cancel_futures=True)
        finally:
            if not interrupted:
                ex.shutdown(wait=True)

        elapsed_min = (time.time() - run_started) / 60
        remaining = max(0, len(todo) - ok_count - fail_count)
        # Rough cost estimate only — check current Gemini pricing for your model tier.
        api_input_cost = total_input_tokens * 1.25 / 1_000_000
        api_output_cost = total_output_tokens * 5.0 / 1_000_000
        print("\n=== RUN COMPLETE ===")
        print(f"  processed (ok):              {ok_count}")
        print(f"    via heuristic skip:        {heuristic_skip_count}")
        print(f"    via editor exclusion:      {skipped_excluded_count}")
        print(f"    actually analyzed by LLM:  {ok_count - heuristic_skip_count - skipped_excluded_count}")
        print(f"  retried:                     {retried_count}")
        print(f"  failed:                      {fail_count}")
        print(f"  remaining (this invocation): {remaining}")
        print(f"  elapsed:                     {elapsed_min:.1f} min")
        print(f"  pass dir:                    {run_dir.relative_to(ROOT)}")
        print(f"  input tokens (est):          {total_input_tokens:,}")
        print(f"  output tokens (est):         {total_output_tokens:,}")
        print(
            f"  Gemini API cost (<=200K tier): "
            f"${api_input_cost:.2f} input + ${api_output_cost:.2f} output "
            f"= ${api_input_cost + api_output_cost:.2f}"
        )

        try:
            m = json.loads(manifest_path.read_text(encoding="utf-8"))
            m["validated_pass"] = m.get("validated_pass", 0) + ok_count
            m["skipped_via_heuristic_count"] = m.get("skipped_via_heuristic_count", 0) + heuristic_skip_count
            m["skipped_excluded_count"] = m.get("skipped_excluded_count", 0) + skipped_excluded_count
            m["retried_count"] = m.get("retried_count", 0) + retried_count
            m["final_failure_count"] = m.get("final_failure_count", 0) + fail_count
            m["last_run_completed_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
            m["total_input_tokens_est"] = m.get("total_input_tokens_est", 0) + total_input_tokens
            m["total_output_tokens_est"] = m.get("total_output_tokens_est", 0) + total_output_tokens
            if remaining == 0 and not args.max_records and not time_capped:
                m["completed_at"] = m["last_run_completed_at"]
            atomic_write_json(manifest_path, m)
        except Exception:
            pass

        actually_analyzed = max(0, ok_count - heuristic_skip_count - skipped_excluded_count)
        if args.refresh_indexes and actually_analyzed:
            try:
                from refresh_indexes import refresh_all_indexes  # type: ignore

                refresh_all_indexes()
            except Exception as e:
                print(f"[indexes] refresh failed: {str(e)[:200]}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
