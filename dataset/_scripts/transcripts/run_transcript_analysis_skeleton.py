"""
DEPRECATED — kept on disk for emergency Claude fallback only.

The active transcript analysis runner is now
`_scripts/run_transcript_analysis_via_gemini.py` (Gemini API default model, single-call
per record, no chunking / deferral). This Claude skeleton remains because
several helpers (`SingleRunner`, `atomic_write_json`, `append_log`,
`build_user_message`, `parse_response_json`, `is_trivial_record`,
`trivial_analysis_output`, `needs_processing`, `cleanup_stale_tmps`) are
imported by the Gemini runner and a few other scripts.

Do not start a new analysis pass with this script unless the Gemini runner is
unavailable.

----- original docstring follows -----

Reference runner for the transcript analysis pass — local foreground, Claude Max plan.

Calls the `claude` CLI (`claude --print --model …`) for each transcript record,
validates output against the controlled-vocabulary registries via Validator,
and commits merged records atomically.

Lock + idempotency + atomic commits are platform-agnostic. Rate-limit sleep is
heuristic (the CLI doesn't expose `retry-after` headers like the API; we
pattern-match stderr) plus exponential backoff with jitter to avoid concurrent
workers thundering-herding the same window.

Concurrency: ThreadPoolExecutor with N workers (default 4). Each worker spawns
its own `claude --print` subprocess. The shared write paths (log.jsonl,
errors.jsonl) are protected by a threading.Lock; per-record JSON commits go to
distinct files and don't need locking.

Heuristic skip: records with playback_duration_sec < 10s, word count < 10, and
≤1 speaker get auto-classified as utility content (slate, camera check, ambient
b-roll) — no LLM call. Saves ~5-15% of corpus depending on shoot composition.
Disable with --no-heuristic-skip.

Designed for local foreground invocation per `_runs/RESUME_STRATEGY.md`.

Usage:
    python _scripts/run_transcript_analysis_skeleton.py
    python _scripts/run_transcript_analysis_skeleton.py --concurrency 4
    python _scripts/run_transcript_analysis_skeleton.py --concurrency 1 --no-heuristic-skip   # serial, no skip
    python _scripts/run_transcript_analysis_skeleton.py --max-records 5
    python _scripts/run_transcript_analysis_skeleton.py --pass-name transcript_analysis_take2
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import random
import re
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[2]
TRANSCRIPTS = ROOT / "assets/transcripts"
VIDEO = ROOT / "assets/video"
PROMPT_PATH = ROOT / "_prompts/transcript_analysis_prompt.md"
RUNS_DIR = ROOT / "_runs"
LOCK_PATH = ROOT / "_runs" / ".transcript_analysis.lock"

DEFAULT_MODEL = "claude-sonnet-4-6"
DEFAULT_TIMEOUT_SEC = 600                        # generous; sonnet typically 60-150s/record
DEFAULT_INITIAL_BACKOFF_SEC = 300                # 5 min initial; escalates 1.5x per repeat
DEFAULT_MAX_BACKOFF_SEC = 5 * 3600               # cap at one full window-reset
DEFAULT_BACKOFF_JITTER_SEC = 30                  # +0-30s random; breaks thundering herd
DEFAULT_CONCURRENCY = 4

# Heuristic-skip thresholds — must ALL match for a record to skip the LLM call
TRIVIAL_DURATION_SEC = 10.0
TRIVIAL_WORD_COUNT = 10
TRIVIAL_SPEAKER_COUNT = 1

# Long-transcript deferral thresholds — EITHER triggers deferral to a future
# chunked-analysis pass. `claude --print` reliably errors (exit 1, empty stderr)
# on inputs that exceed some CLI-side limit; rather than retry forever, we mark
# these records deferred and move on.
LONG_TRANSCRIPT_DURATION_SEC = 1800.0    # 30 min
LONG_TRANSCRIPT_CHAR_THRESHOLD = 30000   # ~7,500 words ≈ 10K input tokens before prompt

# Wire in the validator
sys.path.insert(0, str(ROOT / "_scripts"))
from validate_transcript_analysis import Validator  # noqa: E402


# ----------------------------------------------------------------------
# Cross-platform single-instance lock (POSIX + Windows)
# ----------------------------------------------------------------------
def _pid_alive(pid: int) -> bool:
    """Best-effort liveness check. Fail-closed: ambiguous means treat as alive."""
    if os.name == "nt":
        try:
            out = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}"],
                capture_output=True, text=True, timeout=5,
            )
            return str(pid) in (out.stdout or "")
        except Exception:
            return True
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


class SingleRunner:
    """Atomic exclusive-create lockfile. Works on Windows + POSIX without ifdefs."""

    def __init__(self, lock_path: Path):
        self.lock_path = lock_path
        self.fd: Optional[int] = None

    def __enter__(self):
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.fd = os.open(str(self.lock_path), os.O_CREAT | os.O_EXCL | os.O_RDWR)
        except FileExistsError:
            stale = False
            try:
                content = self.lock_path.read_text(encoding="utf-8")
                pid_line = content.splitlines()[0].strip() if content else ""
                if pid_line.isdigit() and not _pid_alive(int(pid_line)):
                    stale = True
                    print(f"[lock] stale lockfile (pid {pid_line} not alive); reclaiming")
            except Exception:
                pass
            if stale:
                try:
                    self.lock_path.unlink()
                except Exception:
                    pass
                self.fd = os.open(str(self.lock_path), os.O_CREAT | os.O_EXCL | os.O_RDWR)
            else:
                print(f"[lock] another runner is active at {self.lock_path}; exiting cleanly")
                sys.exit(0)
        body = f"{os.getpid()}\n{dt.datetime.now(dt.timezone.utc).isoformat()}\n"
        os.write(self.fd, body.encode("utf-8"))
        return self

    def __exit__(self, *args):
        if self.fd is not None:
            try:
                os.close(self.fd)
            except Exception:
                pass
            try:
                self.lock_path.unlink()
            except Exception:
                pass


# ----------------------------------------------------------------------
# Atomic write
# ----------------------------------------------------------------------
def atomic_write_json(path: Path, data: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


# Thread-safe append helpers for the shared log + errors files.
_log_lock = threading.Lock()


def append_log(log_path: Path, entry: dict) -> None:
    """Single-line JSONL append. Safe for concurrent callers — protected by _log_lock."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with _log_lock:
        with log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def cleanup_stale_tmps() -> int:
    """Remove orphan .tmp files left by a previous run that was killed mid-write.
    Safe at startup since SingleRunner already established that no other runner holds the lock."""
    stale = list(TRANSCRIPTS.glob("*.tmp"))
    for t in stale:
        try:
            t.unlink()
        except Exception:
            pass
    return len(stale)


# ----------------------------------------------------------------------
# Rate-limit detection for `claude --print`
# ----------------------------------------------------------------------
RATE_LIMIT_PATTERNS = [
    re.compile(r"usage limit", re.I),
    re.compile(r"rate[_ ]?limit", re.I),
    re.compile(r"\b429\b"),
    re.compile(r"\b529\b"),
    re.compile(r"too many requests", re.I),
    re.compile(r"5-hour", re.I),
    re.compile(r"window reset", re.I),
]


def looks_like_rate_limit(stderr: str, stdout: str = "") -> bool:
    blob = (stderr or "") + "\n" + (stdout or "")
    return any(p.search(blob) for p in RATE_LIMIT_PATTERNS)


_AUTH_ERROR_PATTERNS = [
    re.compile(r"login", re.I),
    re.compile(r"authenticat", re.I),
    re.compile(r"unauthorized", re.I),
    re.compile(r"\bauth\b", re.I),
    re.compile(r"session expired", re.I),
    re.compile(r"please sign in", re.I),
    re.compile(r"invalid credential", re.I),
]


def looks_like_silent_systemic_fail(stderr: str, stdout: str, exit_code: int) -> bool:
    """Detect systemic CLI failures that should pause all workers, not be
    treated as record-specific errors. Two patterns:

      1. Truly silent: exit != 0 with empty stderr AND empty stdout.
      2. Auth-shaped: exit != 0 with auth-related text in stderr or stdout
         (e.g., prompt to /login, "session expired", etc.). These appear when
         the OAuth token refresh fails across a reboot.

    Observed in production when:
      - Max-plan window cap fires (silent variant)
      - Auth token expires (auth-shaped variant)
      - Weekly cap fires (varies by CLI version)
    """
    if exit_code == 0:
        return False
    blob = (stderr or "") + "\n" + (stdout or "")
    if not blob.strip():
        return True  # truly silent — pattern 1
    if any(p.search(blob) for p in _AUTH_ERROR_PATTERNS):
        return True  # auth-shaped — pattern 2
    return False


# ----------------------------------------------------------------------
# Cross-worker systemic-failure coordination.
# When one worker detects systemic failure (rate limit OR silent exit), all
# workers pause together until the backoff window ends. Without this, 4
# concurrent workers each independently log per-record failures and burn
# through the queue at zero seconds per record.
# ----------------------------------------------------------------------
_systemic_state_lock = threading.Lock()
_systemic_pause_until: Optional[float] = None   # epoch seconds; None when not paused


def _check_systemic_pause() -> None:
    """If global systemic-pause is active, sleep until it expires."""
    while True:
        with _systemic_state_lock:
            until = _systemic_pause_until
        if until is None:
            return
        remaining = until - time.time()
        if remaining <= 0:
            return
        time.sleep(min(remaining, 60))  # re-check every 60s in case it's cleared


def _set_systemic_pause(until: float) -> None:
    global _systemic_pause_until
    with _systemic_state_lock:
        if _systemic_pause_until is None or until > _systemic_pause_until:
            _systemic_pause_until = until


def _clear_systemic_pause() -> None:
    global _systemic_pause_until
    with _systemic_state_lock:
        _systemic_pause_until = None


def estimate_tokens(text: str) -> int:
    """Char-based token estimate.

    Anthropic doesn't publish a Python tokenizer, and `claude --print` doesn't
    emit usage metadata. Char/4 is a conservative approximation for English
    mixed with structured content (markdown tables, JSON). Real token counts
    are typically 5–15% lower than this estimate."""
    if not text:
        return 0
    return len(text) // 4


DEFAULT_MAX_SYSTEMIC_RETRIES = 8  # caps total sleep at ~4h cumulative


def call_with_resume(
    prompt_input: str,
    *,
    model: str = DEFAULT_MODEL,
    timeout_sec: int = DEFAULT_TIMEOUT_SEC,
    initial_backoff_sec: int = DEFAULT_INITIAL_BACKOFF_SEC,
    max_backoff_sec: int = DEFAULT_MAX_BACKOFF_SEC,
    jitter_sec: int = DEFAULT_BACKOFF_JITTER_SEC,
    max_systemic_retries: int = DEFAULT_MAX_SYSTEMIC_RETRIES,
) -> str:
    """
    Pipe `prompt_input` to `claude --print --model <model>` and return stdout.

    Systemic failures (rate-limit text in stderr OR exit 1 with empty
    stdout/stderr — the silent-cap-hit pattern) trigger exponential backoff
    with random jitter, capped at one window-reset. The pause is shared across
    all worker threads via `_systemic_pause_until`, so when one worker detects
    systemic failure all four pause together rather than each independently
    logging per-record fails.

    After `max_systemic_retries` consecutive systemic-shaped failures on the
    same call, gives up and raises — protects against infinite loop on a
    record that genuinely fails this way (vs. a transient cap).

    Other non-zero exits with stderr text are treated as record-specific and
    raise immediately.
    """
    cmd = ["claude", "--print", "--model", model]
    backoff = initial_backoff_sec
    systemic_retries = 0
    while True:
        # Wait if another worker has set a global systemic pause.
        _check_systemic_pause()

        try:
            proc = subprocess.run(
                cmd,
                input=prompt_input,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=timeout_sec,
                check=False,
            )
        except subprocess.TimeoutExpired as e:
            raise RuntimeError(f"claude --print timed out after {timeout_sec}s") from e

        if proc.returncode == 0:
            # Success — any systemic pause this thread set should be cleared
            # so other paused workers can also resume.
            _clear_systemic_pause()
            return proc.stdout

        explicit_rl = looks_like_rate_limit(proc.stderr, proc.stdout)
        silent_fail = looks_like_silent_systemic_fail(proc.stderr, proc.stdout, proc.returncode)

        if explicit_rl or silent_fail:
            if systemic_retries >= max_systemic_retries:
                kind = "rate-limit" if explicit_rl else "silent-systemic"
                raise RuntimeError(
                    f"claude --print failed ({kind}, exit {proc.returncode}) "
                    f"after {max_systemic_retries} retries; giving up on this call. "
                    f"stderr: {(proc.stderr or '').strip()[:200]}"
                )
            base = min(backoff, max_backoff_sec)
            jitter = random.uniform(0, jitter_sec)
            sleep_for = base + jitter
            kind = "rate-limit" if explicit_rl else "silent-systemic"
            print(
                f"[{kind}] thread={threading.get_ident()} exit={proc.returncode}; "
                f"sleeping {sleep_for:.0f}s ({sleep_for/60:.1f} min). "
                f"All workers will pause until then. "
                f"stderr: {(proc.stderr or '').strip()[:200]}",
                flush=True,
            )
            # Set global pause so concurrent workers don't burn through the queue.
            _set_systemic_pause(time.time() + sleep_for)
            time.sleep(sleep_for)
            backoff = min(int(backoff * 1.5), max_backoff_sec)
            systemic_retries += 1
            continue

        # Non-systemic non-zero exit → record-specific. Capture BOTH streams so
        # we can diagnose ambiguous failures (e.g., auth prompts that print to
        # stdout, not stderr, and so don't trip the silent-systemic check).
        stderr_snip = (proc.stderr or '').strip()[:300]
        stdout_snip = (proc.stdout or '').strip()[:300]
        raise RuntimeError(
            f"claude --print failed (exit {proc.returncode}) | "
            f"stderr: {stderr_snip!r} | stdout: {stdout_snip!r}"
        )


# ----------------------------------------------------------------------
# Idempotency check — has this transcript been analyzed under the current prompt?
# ----------------------------------------------------------------------
def needs_processing(record: dict, prompt_sha: str) -> bool:
    a = record.get("analysis") or {}
    if a.get("analyzed_at") is None:
        return True
    if not a.get("summary_one_line"):
        return True
    if a.get("prompt_sha256") != prompt_sha:
        return True
    return False


# ----------------------------------------------------------------------
# Heuristic skip — utility / slate clips don't need an LLM call
# ----------------------------------------------------------------------
def is_trivial_record(rec: dict) -> tuple[bool, str]:
    """Return (is_trivial, reason). Two paths to trivial:

    1. Short utility clip: duration < 10s AND words < 10 AND speakers ≤ 1.
    2. Empty Whisper output: regardless of duration, if Whisper produced fewer
       than 3 meaningful (alphanumeric) words. Catches inaudible/silent
       segments where Whisper emits '...' or near-empty text.
    """
    full_text = (rec.get("full_text") or "").strip()
    duration = rec.get("playback_duration_sec") or 0.0
    word_count = len(full_text.split()) if full_text else 0
    # Count alphanumeric word tokens — strips punctuation/ellipsis-only content
    meaningful_words = re.findall(r"\b\w+\b", full_text)
    meaningful_word_count = len(meaningful_words)
    spk = rec.get("speakers_raw")
    if isinstance(spk, dict):
        speaker_count = len(spk)
    elif isinstance(spk, list):
        speaker_count = len(spk)
    else:
        speaker_count = 0

    # Path 1: short utility clip
    if (0 < duration < TRIVIAL_DURATION_SEC and
            word_count < TRIVIAL_WORD_COUNT and
            speaker_count <= TRIVIAL_SPEAKER_COUNT):
        return True, f"dur={duration:.1f}s words={word_count} speakers={speaker_count}"

    # Path 2: empty-content Whisper output (e.g., "...", "[inaudible]", silence)
    if meaningful_word_count < 3:
        snippet = full_text[:30].replace("\n", " ")
        return True, f"empty_content: meaningful_words={meaningful_word_count} text={snippet!r}"

    return False, ""


def trivial_analysis_output() -> dict:
    """Synthesize the analysis block for a trivial record — bypasses the LLM call.
    Conservative defaults pass the validator. Distinguished from LLM output via
    `analysis.analyzer = 'heuristic-skip ...'`."""
    return {
        "subject_of_interview": None,
        "people_ids": [],
        "org_ids": [],
        "place_ids": [],
        "moment_ids": [],
        "analysis": {
            "summary_one_line": "Trivial utility clip (auto-classified): too short, too few words, ≤1 speaker.",
            "summary_paragraph": (
                "Auto-classified by heuristic — duration < 10s, word count < 10, ≤1 speaker. "
                "Likely a slate, camera check, or ambient B-roll snippet. No LLM analysis "
                "performed. Re-run with --no-heuristic-skip to override."
            ),
            "topics": [],
            "themes": [],
            "tone": {"mood": "analytical", "energy": "low", "formality": "casual"},
            "key_quotes": [],
            "key_moments": [],
            "storylines": [],
        },
        "craft": {
            "shot_kind": "b-roll",
            "audio_quality": "clean",
        },
        "_unmatched_people": [],
        "_unmatched_orgs": [],
        "_unmatched_places": [],
    }


# ----------------------------------------------------------------------
# Long-transcript deferral — too big for a single claude --print invocation
# ----------------------------------------------------------------------
def is_long_transcript(rec: dict, *,
                       duration_threshold: float = LONG_TRANSCRIPT_DURATION_SEC,
                       char_threshold: int = LONG_TRANSCRIPT_CHAR_THRESHOLD) -> tuple[bool, str]:
    """Return (is_long, reason). Deferred records skip the LLM call and get a
    placeholder analysis stamped with `analysis.deferred_reason = 'long_transcript'`,
    queued for a future chunked-analysis pass."""
    duration = rec.get("playback_duration_sec") or 0.0
    full_text = rec.get("full_text") or ""
    char_count = len(full_text)
    if duration > duration_threshold:
        return True, f"duration={duration:.0f}s (>{duration_threshold:.0f}s threshold)"
    if char_count > char_threshold:
        return True, f"chars={char_count} (>{char_threshold} threshold)"
    return False, ""


def deferred_long_transcript_output(rec: dict) -> dict:
    """Synthesize a deferred-placeholder analysis block. Conservative defaults
    pass the validator. Distinguished via `analysis.deferred_reason` and a
    `deferred-long-transcript ...` analyzer string. A future chunked-analysis
    pass replaces these with real analyses."""
    duration = rec.get("playback_duration_sec") or 0.0
    spk = rec.get("speakers_raw")
    if isinstance(spk, dict):
        speaker_count = len(spk)
    elif isinstance(spk, list):
        speaker_count = len(spk)
    else:
        speaker_count = 0
    shot_kind = "verite" if speaker_count > 3 else "interview"
    return {
        "subject_of_interview": None,
        "people_ids": [],
        "org_ids": [],
        "place_ids": [],
        "moment_ids": [],
        "analysis": {
            "summary_one_line": (
                f"Deferred: long transcript ({duration/60:.0f} min, {speaker_count} speakers) "
                f"awaiting chunked-pass analysis."
            ),
            "summary_paragraph": (
                f"Auto-deferred: transcript exceeded the single-call threshold "
                f"(duration {duration/60:.1f} min or full_text > 30K chars). "
                f"`claude --print` reliably errors on inputs of this size; rather than "
                f"retry indefinitely, the record is marked deferred. A future chunked-"
                f"analysis pass will re-process by splitting into windows. Until then, "
                f"no real analysis is available; topics/themes/key_quotes are empty."
            ),
            "topics": [],
            "themes": [],
            "tone": {"mood": "analytical", "energy": "low", "formality": "conversational"},
            "key_quotes": [],
            "key_moments": [],
            "storylines": [],
            "deferred_reason": "long_transcript",
        },
        "craft": {
            "shot_kind": shot_kind,
            "audio_quality": "clean",
        },
        "_unmatched_people": [],
        "_unmatched_orgs": [],
        "_unmatched_places": [],
    }


# ----------------------------------------------------------------------
# User message construction + response parsing
# ----------------------------------------------------------------------
def _compact_segments(segments: list) -> list:
    """Drop word-level timing; keep speaker_raw + text + start/end. Saves ~80% input tokens."""
    out = []
    for s in segments or []:
        if not isinstance(s, dict):
            continue
        out.append({
            "start_sec": s.get("start_sec"),
            "end_sec": s.get("end_sec"),
            "speaker_raw": s.get("speaker_raw"),
            "speaker": s.get("speaker"),
            "text": s.get("text"),
        })
    return out


def load_video_record(asset_id: str) -> Optional[dict]:
    """Companion video record holds path_metadata (shoot_date, shoot_label, camera_id, scene)."""
    p = VIDEO / f"{asset_id}.video.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def build_user_message(transcript: dict, *, extra_context: dict | None = None) -> str:
    aid = transcript.get("asset_id") or ""
    video = load_video_record(aid) or {}
    ffprobe = video.get("ffprobe") or {}
    payload = {
        "asset_id": aid,
        "transcript_manifest": transcript.get("manifest"),
        "playback_duration_sec": transcript.get("playback_duration_sec"),
        "speakers_raw": transcript.get("speakers_raw"),
        "path_metadata": video.get("path_metadata"),
        "ffprobe_summary": {
            k: ffprobe.get(k) for k in
            ("duration_sec", "width", "height", "fps", "codec", "audio_channels")
        } if ffprobe else None,
        "full_text": transcript.get("full_text"),
        "people_ids": transcript.get("people_ids") or [],
        "org_ids": transcript.get("org_ids") or [],
        "place_ids": transcript.get("place_ids") or [],
        "segments": _compact_segments(transcript.get("segments") or []),
    }
    if extra_context:
        # Keep in a dedicated namespace so we don't collide with schema fields.
        payload["_context"] = extra_context
    instruction = (
        "Analyze the transcript record below against the controlled-vocabulary prompt "
        "context provided above. Return a SINGLE JSON object matching the output schema. "
        "No prose, no commentary, no markdown fences — JSON only.\n\n"
        "TRANSCRIPT_RECORD:\n"
    )
    return instruction + json.dumps(payload, ensure_ascii=False, indent=2)


_FENCE_RE = re.compile(r"^```(?:json)?\s*\n?(.*?)\n?```\s*$", re.S)


def parse_response_json(stdout: str) -> dict:
    """Extract the JSON object from claude --print stdout. Strips fences; falls back to first balanced {…}."""
    s = (stdout or "").strip()
    m = _FENCE_RE.match(s)
    if m:
        s = m.group(1).strip()
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass
    start = s.find("{")
    if start < 0:
        raise ValueError(f"no JSON object in response (first 200 chars: {s[:200]!r})")
    depth = 0
    for i in range(start, len(s)):
        c = s[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return json.loads(s[start:i + 1])
    raise ValueError("unbalanced JSON in response")


def analyze_one(prompt_text: str, transcript_record: dict, *, model: str = DEFAULT_MODEL
                ) -> tuple[dict, dict]:
    """Single LLM call wrapping the prompt + record + parsing.
    Returns (parsed_analysis_dict, metrics_dict). Metrics include char counts
    and token estimates for the input/output of this call only (caller is
    responsible for aggregating across retries if needed)."""
    user_msg = build_user_message(transcript_record)
    full_input = prompt_text + "\n\n---\n\n" + user_msg
    stdout = call_with_resume(full_input, model=model)
    metrics = {
        "input_chars": len(full_input),
        "input_tokens_est": estimate_tokens(full_input),
        "output_chars": len(stdout) if stdout else 0,
        "output_tokens_est": estimate_tokens(stdout),
    }
    return parse_response_json(stdout), metrics


# ----------------------------------------------------------------------
# Per-record processor — runs in worker threads
# ----------------------------------------------------------------------
@dataclass
class ProcessResult:
    asset_id: str
    ok: bool
    error: str = ""
    duration_ms: int = 0
    skipped_via_heuristic: bool = False
    deferred_long: bool = False
    retried: bool = False
    input_tokens_est: int = 0
    output_tokens_est: int = 0


def _stamp_analysis(out: dict, prompt_sha: str, analyzer_str: str) -> None:
    """Set authoritative metadata on the analysis block. Always overwrites
    whatever the model emitted — never trust LLM-supplied analyzer/sha/timestamp."""
    out.setdefault("analysis", {})
    out["analysis"]["analyzed_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
    out["analysis"]["prompt_sha256"] = prompt_sha
    out["analysis"]["analyzer"] = analyzer_str


def process_one(p: Path, prompt_text: str, prompt_sha: str, validator: Validator,
                *, model: str, pass_name: str, log_path: Path, errors_path: Path,
                heuristic_skip: bool, defer_long: bool,
                long_duration_threshold: float = LONG_TRANSCRIPT_DURATION_SEC,
                long_chars_threshold: int = LONG_TRANSCRIPT_CHAR_THRESHOLD) -> ProcessResult:
    aid_fallback = p.stem.replace(".transcript", "")
    try:
        rec = json.loads(p.read_text(encoding="utf-8"))
        aid = rec.get("asset_id") or aid_fallback

        # ---- Heuristic skip path ----
        if heuristic_skip:
            trivial, reason = is_trivial_record(rec)
            if trivial:
                analysis_out = trivial_analysis_output()
                _stamp_analysis(analysis_out, prompt_sha,
                                f"heuristic-skip (transcript-batch {pass_name})")
                v = validator.validate(analysis_out, rec)
                if not v.ok:
                    raise RuntimeError(f"trivial output failed validator: {v.errors}")
                merged = validator.merge(rec, analysis_out)
                atomic_write_json(p, merged)
                append_log(log_path, {
                    "asset_id": aid, "ok": True, "duration_ms": 0,
                    "skipped_via_heuristic": True, "reason": reason,
                })
                return ProcessResult(asset_id=aid, ok=True, skipped_via_heuristic=True)

        # ---- Long-transcript deferral path ----
        if defer_long:
            is_long, long_reason = is_long_transcript(
                rec,
                duration_threshold=long_duration_threshold,
                char_threshold=long_chars_threshold,
            )
            if is_long:
                analysis_out = deferred_long_transcript_output(rec)
                _stamp_analysis(analysis_out, prompt_sha,
                                f"deferred-long-transcript (transcript-batch {pass_name})")
                v = validator.validate(analysis_out, rec)
                if not v.ok:
                    raise RuntimeError(f"deferred output failed validator: {v.errors}")
                merged = validator.merge(rec, analysis_out)
                atomic_write_json(p, merged)
                append_log(log_path, {
                    "asset_id": aid, "ok": True, "duration_ms": 0,
                    "deferred_long": True, "reason": long_reason,
                })
                return ProcessResult(asset_id=aid, ok=True, deferred_long=True)

        # ---- Normal LLM path ----
        t0 = time.time()
        analysis_out, m1 = analyze_one(prompt_text, rec, model=model)
        duration_ms = int((time.time() - t0) * 1000)
        total_input_tokens = m1["input_tokens_est"]
        total_output_tokens = m1["output_tokens_est"]

        retried = False
        v = validator.validate(analysis_out, rec)
        if not v.ok:
            retried = True
            retry_prompt = validator.build_retry_prompt(analysis_out, v.errors)
            analysis_out, m2 = analyze_one(prompt_text + "\n\n" + retry_prompt, rec, model=model)
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
                return ProcessResult(asset_id=aid, ok=False, error="validation_failed_after_retry",
                                     retried=True,
                                     input_tokens_est=total_input_tokens,
                                     output_tokens_est=total_output_tokens)

        _stamp_analysis(analysis_out, prompt_sha,
                        f"{model} (transcript-batch {pass_name})")
        merged = validator.merge(rec, analysis_out)
        atomic_write_json(p, merged)

        append_log(log_path, {
            "asset_id": aid, "ok": True, "duration_ms": duration_ms,
            "warnings": v.warnings, "retried": retried,
            "input_tokens_est": total_input_tokens,
            "output_tokens_est": total_output_tokens,
        })
        return ProcessResult(asset_id=aid, ok=True, duration_ms=duration_ms, retried=retried,
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
                    help="Run subdirectory name in _runs/. Defaults to transcript_analysis_<timestamp>.")
    ap.add_argument("--max-records", type=int, default=0,
                    help="Process at most N records (0 = no limit; useful for testing)")
    ap.add_argument("--max-window-seconds", type=int, default=18000,
                    help="Soft cap on total runtime. If exceeded, stop submitting new work.")
    ap.add_argument("--model", default=DEFAULT_MODEL,
                    help=f"Model passed to `claude --print --model`. Default: {DEFAULT_MODEL}")
    ap.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY,
                    help=f"Number of worker threads. Default: {DEFAULT_CONCURRENCY}. Use 1 for serial.")
    ap.add_argument("--no-heuristic-skip", action="store_true",
                    help="Disable heuristic skip for trivially-short utility records.")
    ap.add_argument("--no-defer-long", action="store_true",
                    help="Disable long-transcript deferral. Records that hit the threshold "
                         "will be sent to claude --print and likely fail with exit 1.")
    ap.add_argument(
        "--refresh-indexes",
        action="store_true",
        help="After successful mutations, rebuild MANIFEST.json and ../indexes/editorial_catalog.sqlite.",
    )
    ap.add_argument("--long-duration-threshold", type=float,
                    default=LONG_TRANSCRIPT_DURATION_SEC,
                    help=f"Defer transcripts with playback_duration_sec > N. "
                         f"Default: {LONG_TRANSCRIPT_DURATION_SEC:.0f}s.")
    ap.add_argument("--long-chars-threshold", type=int,
                    default=LONG_TRANSCRIPT_CHAR_THRESHOLD,
                    help=f"Defer transcripts with len(full_text) > N. "
                         f"Default: {LONG_TRANSCRIPT_CHAR_THRESHOLD}.")
    args = ap.parse_args()

    with SingleRunner(LOCK_PATH):
        if not PROMPT_PATH.exists():
            print(f"ERROR: prompt context not found at {PROMPT_PATH}", file=sys.stderr)
            print("Run _scripts/build_transcript_prompt_context.py first.", file=sys.stderr)
            return 1

        # Sweep stale .tmp files left by a previous interrupted run
        n_stale = cleanup_stale_tmps()
        if n_stale:
            print(f"[cleanup] removed {n_stale} stale .tmp files from previous run")

        prompt_text = PROMPT_PATH.read_text(encoding="utf-8")
        # Use canonical SHA so timestamp-only regenerations don't churn the
        # worklist. Stays in sync with the active Gemini runner.
        from build_transcript_prompt_context import canonical_prompt_sha  # noqa: E402
        prompt_sha = canonical_prompt_sha(prompt_text)

        ts = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d_%H%M")
        pass_name = args.pass_name or f"transcript_analysis_{ts}"
        run_dir = RUNS_DIR / pass_name
        run_dir.mkdir(parents=True, exist_ok=True)

        manifest_path = run_dir / "manifest.json"
        log_path = run_dir / "log.jsonl"
        errors_path = run_dir / "errors.jsonl"

        if not manifest_path.exists():
            atomic_write_json(manifest_path, {
                "run_id": pass_name,
                "pass_name": "transcript_analysis",
                "started_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                "prompt_path": str(PROMPT_PATH.relative_to(ROOT)),
                "prompt_sha256": prompt_sha,
                "script_path": str(Path(__file__).relative_to(ROOT)),
                "model": args.model,
                "model_invocation": "claude --print",
                "concurrency": args.concurrency,
                "heuristic_skip_enabled": not args.no_heuristic_skip,
                "defer_long_enabled": not args.no_defer_long,
                "long_duration_threshold_sec": args.long_duration_threshold,
                "long_chars_threshold": args.long_chars_threshold,
                "domain": "transcripts",
                "input_count": None,
                "validated_pass": 0,
                "heuristic_skip_count": 0,
                "deferred_long_count": 0,
                "retried_count": 0,
                "final_failure_count": 0,
                "estimated_cost_usd": 0.0,
                "registries_at_run_time": {},
                "completed_at": None,
            })

        validator = Validator.from_workspace()

        all_records = sorted(TRANSCRIPTS.glob("*.json"))
        todo: list[Path] = []
        for p in all_records:
            try:
                rec = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                continue
            if needs_processing(rec, prompt_sha):
                todo.append(p)
        print(f"[plan] {len(todo)} of {len(all_records)} records need processing this run")
        print(f"[plan] concurrency={args.concurrency}, "
              f"heuristic_skip={'on' if not args.no_heuristic_skip else 'off'}")

        if args.max_records:
            todo = todo[:args.max_records]
            print(f"[plan] capping at --max-records={args.max_records}")

        run_started = time.time()
        ok_count = 0
        fail_count = 0
        retried_count = 0
        heuristic_skip_count = 0
        deferred_long_count = 0
        total_input_tokens_est = 0
        total_output_tokens_est = 0
        time_capped = False
        interrupted = False

        ex = ThreadPoolExecutor(max_workers=args.concurrency)
        try:
            futures = {
                ex.submit(
                    process_one, p, prompt_text, prompt_sha, validator,
                    model=args.model, pass_name=pass_name,
                    log_path=log_path, errors_path=errors_path,
                    heuristic_skip=not args.no_heuristic_skip,
                    defer_long=not args.no_defer_long,
                    long_duration_threshold=args.long_duration_threshold,
                    long_chars_threshold=args.long_chars_threshold,
                ): p for p in todo
            }

            try:
                for fut in as_completed(futures):
                    result = fut.result()
                    if result.ok:
                        ok_count += 1
                        if result.skipped_via_heuristic:
                            heuristic_skip_count += 1
                        if result.deferred_long:
                            deferred_long_count += 1
                    else:
                        fail_count += 1
                    if result.retried:
                        retried_count += 1
                    total_input_tokens_est += result.input_tokens_est
                    total_output_tokens_est += result.output_tokens_est

                    # Periodic progress line
                    done = ok_count + fail_count
                    if done % 50 == 0 or done == len(todo):
                        elapsed = time.time() - run_started
                        rate = done / elapsed if elapsed > 0 else 0
                        print(
                            f"[progress] {done}/{len(todo)}  "
                            f"ok={ok_count} (heur={heuristic_skip_count}, defer={deferred_long_count})  "
                            f"fail={fail_count}  "
                            f"rate={rate:.2f} rec/s ({rate*3600:.0f}/hr)  "
                            f"elapsed={elapsed/60:.1f}min",
                            flush=True,
                        )

                    # Time-cap: stop submitting new work
                    if not time_capped and (time.time() - run_started > args.max_window_seconds):
                        print(f"[time-cap] elapsed > {args.max_window_seconds}s; cancelling pending workers")
                        ex.shutdown(wait=False, cancel_futures=True)
                        time_capped = True
            except KeyboardInterrupt:
                interrupted = True
                print(
                    "\n[interrupt] Ctrl+C received; cancelling pending workers. "
                    "In-flight subprocess calls (up to ~4) will exit on their own; "
                    "the script will not wait for them.",
                    flush=True,
                )
                ex.shutdown(wait=False, cancel_futures=True)

        finally:
            # If interrupted, we already called shutdown(wait=False); don't block on workers.
            # Otherwise, normal completion path waits for remaining workers.
            if not interrupted:
                ex.shutdown(wait=True)

        elapsed_min = (time.time() - run_started) / 60
        remaining_after = max(0, len(todo) - ok_count - fail_count)
        llm_ok = ok_count - heuristic_skip_count - deferred_long_count
        # Sonnet 4.6 pricing: $3/MTok input, $15/MTok output (uncached). Cached prefix
        # would be ~$0.30/MTok input but `claude --print` is uncached.
        api_input_cost = total_input_tokens_est * 3.0 / 1_000_000
        api_output_cost = total_output_tokens_est * 15.0 / 1_000_000
        print("\n=== RUN COMPLETE ===")
        print(f"  processed (ok):              {ok_count}")
        print(f"    via heuristic skip:        {heuristic_skip_count}")
        print(f"    via long-deferral:         {deferred_long_count}")
        print(f"    via LLM:                   {llm_ok}")
        print(f"  retried:                     {retried_count}")
        print(f"  failed:                      {fail_count}")
        print(f"  remaining (this invocation): {remaining_after}")
        print(f"  elapsed:                     {elapsed_min:.1f} min")
        print(f"  pass dir:                    {run_dir.relative_to(ROOT)}")
        print(f"  input tokens (est):          {total_input_tokens_est:,}")
        print(f"  output tokens (est):         {total_output_tokens_est:,}")
        print(f"  hypothetical API cost (uncached): "
              f"${api_input_cost:.2f} input + ${api_output_cost:.2f} output "
              f"= ${api_input_cost + api_output_cost:.2f}")

        try:
            m = json.loads(manifest_path.read_text(encoding="utf-8"))
            m["validated_pass"] = m.get("validated_pass", 0) + ok_count
            m["heuristic_skip_count"] = m.get("heuristic_skip_count", 0) + heuristic_skip_count
            m["deferred_long_count"] = m.get("deferred_long_count", 0) + deferred_long_count
            m["retried_count"] = m.get("retried_count", 0) + retried_count
            m["final_failure_count"] = m.get("final_failure_count", 0) + fail_count
            m["last_run_completed_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
            if remaining_after == 0 and not args.max_records and not time_capped:
                m["completed_at"] = m["last_run_completed_at"]
            atomic_write_json(manifest_path, m)
        except Exception:
            pass

        # Keep derived SQL/JSON indexes in sync with any transcript mutations.
        if args.refresh_indexes and ok_count:
            try:
                from refresh_indexes import refresh_all_indexes  # type: ignore

                refresh_all_indexes()
            except Exception as e:
                print(f"[indexes] refresh failed: {str(e)[:200]}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
