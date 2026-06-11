"""
Phase 1 of the transcript cleanup pass — per-record correction generation.

Reads `_runs/cleanup_candidates_<ts>/candidate_clusters.json` produced by
`find_correction_candidates.py`, builds an asset_id -> [relevant cluster
contexts] index, and for each transcript record with at least one cluster
member, calls Sonnet via `claude --print` with the cleanup prompt + the
record + the cluster context.

Reuses the analysis runner's plumbing (lock, atomic commit, call_with_resume,
JSON parser) via direct import — no duplication.

Usage:
    python _scripts/transcripts/run_transcript_cleanup_skeleton.py --candidates _runs/cleanup_candidates_<ts>
    python _scripts/transcripts/run_transcript_cleanup_skeleton.py --candidates ... --max-records 5
    python _scripts/transcripts/run_transcript_cleanup_skeleton.py --candidates ... --pass-name cleanup_take2

Per `_runs/RESUME_STRATEGY.md`: run from PowerShell foreground, not Cowork's
bash sandbox. Same 5-hour-window rate-limit behavior as the analysis pass.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[2]
TRANSCRIPTS = ROOT / "assets/transcripts"
PROMPT_PATH = ROOT / "_prompts/transcript_cleanup_prompt.md"
RUNS_DIR = ROOT / "_runs"
LOCK_PATH = ROOT / "_runs" / ".transcript_cleanup.lock"

DEFAULT_MODEL = "claude-sonnet-4-6"
DEFAULT_CONCURRENCY = 4

# Reuse analysis runner's helpers — no duplication
sys.path.insert(0, str(ROOT / "_scripts"))
from run_transcript_analysis_skeleton import (  # noqa: E402
    SingleRunner,
    atomic_write_json,
    append_log,
    call_with_resume,
    parse_response_json,
    load_video_record,
    _compact_segments,
)
from validate_transcript_cleanup import CleanupValidator  # noqa: E402


# ----------------------------------------------------------------------
# Output sanitization — make validator passing more reliable
# ----------------------------------------------------------------------
_SEG_FIELD_RE = re.compile(r"^segments\[(\d+)\]\.text$")


def _span_text(span_field: str, transcript: dict) -> str:
    if span_field == "full_text":
        return transcript.get("full_text") or ""
    m = _SEG_FIELD_RE.match(span_field or "")
    if not m:
        return ""
    idx = int(m.group(1))
    segs = transcript.get("segments") or []
    if 0 <= idx < len(segs) and isinstance(segs[idx], dict):
        return segs[idx].get("text") or ""
    return ""


def _sanitize_spans(original: str, spans: list, transcript: dict) -> list[dict]:
    kept: list[dict] = []
    for s in spans or []:
        if not isinstance(s, dict):
            continue
        field = s.get("field")
        if not field:
            continue
        text = _span_text(field, transcript)
        if original and original in text:
            # Normalize segment spans to use segment timestamps (model sometimes emits wrong bounds)
            m = _SEG_FIELD_RE.match(field)
            if m:
                idx = int(m.group(1))
                seg = (transcript.get("segments") or [])[idx]
                kept.append({
                    "field": field,
                    "start_sec": seg.get("start_sec"),
                    "end_sec": seg.get("end_sec"),
                })
            else:
                kept.append({"field": field})
    # Ensure full_text span exists if possible
    if original and original in (transcript.get("full_text") or ""):
        if not any(s.get("field") == "full_text" for s in kept):
            kept.insert(0, {"field": "full_text"})
    return kept


def _try_fill_slug_from_cluster_context(c: dict, cluster_contexts: list[dict]) -> None:
    """
    If model omitted people_id/org_id/place_id but the `original` uniquely matches
    one cluster context mishearing, fill target_slug + corrected canonical.
    """
    if not isinstance(c, dict):
        return
    if c.get("people_id") or c.get("org_id") or c.get("place_id"):
        return
    original = c.get("original")
    if not isinstance(original, str) or not original:
        return
    def norm(s: str) -> str:
        return re.sub(r"\W+", "", (s or "")).casefold()

    o_norm = norm(original)
    matches = []
    for ctx in cluster_contexts or []:
        mis = ctx.get("mishearings_observed") or []
        for m in mis:
            if isinstance(m, str) and norm(m) == o_norm:
                matches.append(ctx)
                break
    if len(matches) != 1:
        return
    ctx = matches[0]
    target_slug = ctx.get("target_slug")
    target_canonical = ctx.get("target_canonical")
    if not isinstance(target_slug, str) or not target_slug:
        return
    if isinstance(target_canonical, str) and target_canonical:
        c["corrected"] = target_canonical
    if target_slug.startswith("p_"):
        c["people_id"] = target_slug
        c["org_id"] = None
        c["place_id"] = None
        c["type"] = "name_substitution"
    elif target_slug.startswith("o_"):
        c["org_id"] = target_slug
        c["people_id"] = None
        c["place_id"] = None
        # keep type as emitted (term_substitution expected), don't force
    elif target_slug.startswith("pl_"):
        c["place_id"] = target_slug
        c["people_id"] = None
        c["org_id"] = None


def sanitize_cleanup_output(
    cleanup_out: dict,
    transcript: dict,
    cluster_contexts: list[dict],
    *,
    validator: Optional[CleanupValidator] = None,
) -> dict:
    out = cleanup_out if isinstance(cleanup_out, dict) else {}
    corrs = out.get("corrections")
    if not isinstance(corrs, list):
        return {"corrections": []}
    sanitized: list[dict] = []
    for c in corrs:
        if not isinstance(c, dict):
            continue
        _try_fill_slug_from_cluster_context(c, cluster_contexts)
        if validator:
            pid, oid, plid = c.get("people_id"), c.get("org_id"), c.get("place_id")
            if pid and pid not in validator.people_ids:
                continue
            if oid and oid not in validator.org_ids:
                continue
            if plid and validator.place_ids and plid not in validator.place_ids:
                continue
        # Drop structurally-invalid name substitutions (prevents whole-record failure).
        if c.get("type") == "name_substitution" and not c.get("people_id"):
            continue
        original = c.get("original")
        if not isinstance(original, str) or not original:
            continue
        spans = _sanitize_spans(original, c.get("spans") or [], transcript)
        if not spans:
            # Can't validate without a valid span; drop this correction.
            continue
        c2 = dict(c)
        c2["spans"] = spans
        sanitized.append(c2)
    return {"corrections": sanitized}


@dataclass
class ProcessResult:
    asset_id: str
    ok: bool
    duration_ms: int = 0
    high_conf_count: int = 0
    candidate_count: int = 0
    retried: bool = False
    error: str = ""


def process_one(
    p: Path,
    *,
    prompt_text: str,
    cluster_contexts: list[dict],
    validator: CleanupValidator,
    model: str,
    pass_name: str,
    log_path: Path,
    errors_path: Path,
) -> ProcessResult:
    aid_fallback = p.stem.replace(".transcript", "")
    try:
        rec = json.loads(p.read_text(encoding="utf-8"))
        aid = rec.get("asset_id") or aid_fallback

        t0 = time.time()
        cleanup_out = cleanup_one(prompt_text, rec, cluster_contexts, model=model)
        duration_ms = int((time.time() - t0) * 1000)

        cleanup_out = sanitize_cleanup_output(
            cleanup_out, rec, cluster_contexts, validator=validator,
        )
        retried = False
        v = validator.validate(cleanup_out, rec)
        if not v.ok:
            retried = True
            retry_prompt = validator.build_retry_prompt(cleanup_out, v.errors)
            cleanup_out = cleanup_one(prompt_text + "\n\n" + retry_prompt, rec, cluster_contexts, model=model)
            cleanup_out = sanitize_cleanup_output(
                cleanup_out, rec, cluster_contexts, validator=validator,
            )
            v = validator.validate(cleanup_out, rec)
            if not v.ok:
                append_log(errors_path, {
                    "asset_id": aid, "ok": False,
                    "error": "validation_failed_after_retry",
                    "details": v.errors,
                })
                append_log(log_path, {"asset_id": aid, "ok": False, "error": "validation_failed_after_retry"})
                return ProcessResult(asset_id=aid, ok=False, duration_ms=duration_ms, retried=True,
                                     error="validation_failed_after_retry")

        applied_at = dt.datetime.now(dt.timezone.utc).isoformat()
        model_str = f"{model} (transcript-cleanup {pass_name})"
        merged = validator.merge(rec, v, applied_at=applied_at, model_str=model_str)
        atomic_write_json(p, merged)

        hc = len(v.high_confidence)
        cd = len(v.candidates)
        append_log(log_path, {
            "asset_id": aid, "ok": True, "duration_ms": duration_ms,
            "high_conf_count": hc, "candidate_count": cd, "warnings": v.warnings,
            "retried": retried,
        })
        return ProcessResult(asset_id=aid, ok=True, duration_ms=duration_ms,
                             high_conf_count=hc, candidate_count=cd, retried=retried)
    except Exception as e:
        msg = str(e)[:500]
        append_log(errors_path, {"asset_id": aid_fallback, "ok": False, "error": msg})
        append_log(log_path, {"asset_id": aid_fallback, "ok": False, "error": msg})
        return ProcessResult(asset_id=aid_fallback, ok=False, error=msg)


# ----------------------------------------------------------------------
# Cluster index — asset_id -> list[cluster_context]
# ----------------------------------------------------------------------
def build_cluster_index(
    candidates_dir: Path,
    *,
    skip_deterministic_clusters: bool,
    threshold: int,
    min_occurrences: int,
) -> dict[str, list[dict]]:
    clusters_path = candidates_dir / "candidate_clusters.json"
    if not clusters_path.exists():
        raise FileNotFoundError(f"missing {clusters_path}; run find_correction_candidates.py first")
    clusters = json.loads(clusters_path.read_text(encoding="utf-8"))

    by_asset: dict[str, list[dict]] = {}
    for c in clusters:
        occ = int(c.get("occurrences") or 0)
        if occ < min_occurrences:
            continue
        if skip_deterministic_clusters and occ >= threshold:
            continue
        ctx = {
            "kind": c["kind"],
            "target_slug": c["target_slug"],
            "target_canonical": c["target_canonical"],
            "occurrences_corpus_wide": c["occurrences"],
            "asset_count_corpus_wide": c["asset_count"],
            "mishearings_observed": [m["text"] for m in c.get("mishearings") or []],
        }
        for aid in c.get("asset_ids") or []:
            by_asset.setdefault(aid, []).append(ctx)
    return by_asset


# ----------------------------------------------------------------------
# Idempotency
# ----------------------------------------------------------------------
def _has_correction_for(rec: dict, target_slug: str) -> bool:
    field_name = "people_id" if target_slug.startswith("p_") else \
                 "org_id" if target_slug.startswith("o_") else \
                 "place_id" if target_slug.startswith("pl_") else None
    if field_name is None:
        return False
    for c in (rec.get("corrections") or []):
        if c.get(field_name) == target_slug:
            return True
    for c in (rec.get("_correction_candidates") or []):
        if c.get(field_name) == target_slug:
            return True
    return False


def needs_processing(rec: dict, cluster_contexts: list[dict], prompt_sha: str) -> bool:
    if rec.get("schema_version", 0) < 5:
        return False  # not yet migrated; skip
    # Has every cluster target already produced a correction or candidate? If so skip.
    for ctx in cluster_contexts:
        if not _has_correction_for(rec, ctx["target_slug"]):
            return True
    return False


# ----------------------------------------------------------------------
# User message construction
# ----------------------------------------------------------------------
def _cluster_needles(cluster_contexts: list[dict]) -> list[str]:
    needles: list[str] = []
    for ctx in cluster_contexts or []:
        for m in ctx.get("mishearings_observed") or []:
            if isinstance(m, str) and len(m.strip()) >= 2:
                needles.append(m.strip())
    return needles


def _excerpt_full_text(full_text: str, needles: list[str], *, max_chars: int = 52000) -> tuple[str, bool]:
    if len(full_text) <= max_chars:
        return full_text, False
    ft_lower = full_text.casefold()
    ranges: list[tuple[int, int]] = []
    for n in needles:
        nl = n.casefold()
        if len(nl) < 2:
            continue
        start = 0
        while True:
            i = ft_lower.find(nl, start)
            if i < 0:
                break
            ranges.append((max(0, i - 280), min(len(full_text), i + len(n) + 280)))
            start = i + max(1, len(nl))
    if not ranges:
        return full_text[:max_chars] + (
            "\n\n[NOTE: full_text truncated — first chunk only; prefer spans in segments if needed.]"
        ), True
    ranges.sort()
    merged: list[list[int]] = []
    for a, b in ranges:
        if not merged or a > merged[-1][1] + 40:
            merged.append([a, b])
        else:
            merged[-1][1] = max(merged[-1][1], b)
    parts: list[str] = []
    total = 0
    for a, b in merged:
        chunk = full_text[a:b]
        overhead = len(f"[offset {a}-{b}]\n") + 8
        if total + len(chunk) + overhead > max_chars:
            break
        parts.append(f"[offset {a}-{b}]\n{chunk}")
        total += len(chunk) + overhead
    note = (
        "\n\n[NOTE: full_text is excerpted around cluster mishearings (CLI size limit). "
        "Only propose corrections for `original` substrings that appear in this excerpt or "
        "in the provided segments.]"
    )
    return "\n\n".join(parts) + note, True


def _filter_segments_for_needles(segments: list, needles: list[str], *, max_segments: int = 420) -> list[dict]:
    if not needles:
        return _compact_segments((segments or [])[: min(500, len(segments or []))])
    nlow = [n.casefold() for n in needles]
    out: list[dict] = []
    for s in segments or []:
        if not isinstance(s, dict):
            continue
        t = (s.get("text") or "").casefold()
        if any(n in t for n in nlow):
            out.append({
                "start_sec": s.get("start_sec"),
                "end_sec": s.get("end_sec"),
                "speaker_raw": s.get("speaker_raw"),
                "speaker": s.get("speaker"),
                "text": s.get("text"),
            })
        if len(out) >= max_segments:
            break
    if len(out) < 6:
        return _compact_segments((segments or [])[:200])
    return out


def build_user_message(transcript: dict, cluster_contexts: list[dict]) -> str:
    aid = transcript.get("asset_id") or ""
    video = load_video_record(aid) or {}
    needles = _cluster_needles(cluster_contexts)
    full_text = transcript.get("full_text") or ""
    segments = transcript.get("segments") or []

    excerpted = False
    if len(full_text) > 55000 or len(segments) > 900:
        full_text_payload, excerpted = _excerpt_full_text(full_text, needles)
        seg_payload = _filter_segments_for_needles(segments, needles)
    else:
        full_text_payload = full_text
        seg_payload = _compact_segments(segments)

    payload = {
        "asset_id": aid,
        "transcript_manifest": transcript.get("manifest"),
        "playback_duration_sec": transcript.get("playback_duration_sec"),
        "speakers_raw": transcript.get("speakers_raw"),
        "path_metadata": video.get("path_metadata"),
        "full_text": full_text_payload,
        "segments": seg_payload,
        "analysis_output": {
            "people_ids": transcript.get("people_ids") or [],
            "org_ids": transcript.get("org_ids") or [],
            "_unmatched_people": transcript.get("_unmatched_people") or [],
            "_unmatched_orgs": transcript.get("_unmatched_orgs") or [],
            "_proposed_places": transcript.get("_proposed_places") or [],
        },
        "cluster_context": cluster_contexts,
    }
    if excerpted:
        payload["_cleanup_mode"] = "excerpted_for_cli_limits"
    instruction = (
        "Identify Whisper transcription errors on the transcript record below "
        "and propose structured corrections per the cleanup prompt above. The "
        "`cluster_context` field lists the corpus-wide phonetic clusters that "
        "touch this transcript — treat each as a strong prior. Return a SINGLE "
        "JSON object: {\"corrections\": [...]}. No prose, no commentary, no "
        "markdown fences — JSON only.\n\nTRANSCRIPT_RECORD:\n"
    )
    return instruction + json.dumps(payload, ensure_ascii=False, indent=2)


# ----------------------------------------------------------------------
# The single-record cleanup call
# ----------------------------------------------------------------------
def cleanup_one(prompt_text: str, transcript: dict, cluster_contexts: list[dict],
                *, model: str = DEFAULT_MODEL) -> dict:
    user_msg = build_user_message(transcript, cluster_contexts)
    full_input = prompt_text + "\n\n---\n\n" + user_msg
    stdout = call_with_resume(full_input, model=model)
    return parse_response_json(stdout)


# ----------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--candidates", required=True,
                    help="Path to _runs/cleanup_candidates_<ts>/ produced by find_correction_candidates.py")
    ap.add_argument("--pass-name", default=None)
    ap.add_argument("--max-records", type=int, default=0)
    ap.add_argument("--max-window-seconds", type=int, default=18000)
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY)
    ap.add_argument(
        "--refresh-indexes",
        action="store_true",
        help="After successful mutations, rebuild MANIFEST.json and ../indexes/editorial_catalog.sqlite.",
    )
    ap.add_argument(
        "--skip-deterministic-clusters",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="When enabled (default), skip clusters with occurrences >= --threshold (handled by Phase A).",
    )
    ap.add_argument(
        "--threshold",
        type=int,
        default=10,
        help="Deterministic Phase A threshold; Phase B skips clusters with occurrences >= N when skipping is enabled.",
    )
    ap.add_argument(
        "--min-occurrences",
        type=int,
        default=2,
        help="Only include clusters with occurrences >= N (default 2; yields the 2–9 tail after skipping).",
    )
    ap.add_argument(
        "--only-asset-ids",
        default=None,
        help="Comma-separated asset_id hex strings; only these records are eligible this run.",
    )
    args = ap.parse_args()

    candidates_dir = Path(args.candidates).resolve()

    with SingleRunner(LOCK_PATH):
        if not PROMPT_PATH.exists():
            print(f"ERROR: cleanup prompt not found at {PROMPT_PATH}", file=sys.stderr)
            return 1

        prompt_text = PROMPT_PATH.read_text(encoding="utf-8")
        prompt_sha = hashlib.sha256(prompt_text.encode("utf-8")).hexdigest()

        cluster_index = build_cluster_index(
            candidates_dir,
            skip_deterministic_clusters=args.skip_deterministic_clusters,
            threshold=args.threshold,
            min_occurrences=args.min_occurrences,
        )
        print(f"[plan] cluster index: {len(cluster_index)} asset_ids touched by clusters")

        only_ids: Optional[set[str]] = None
        if args.only_asset_ids:
            only_ids = {x.strip().lower() for x in args.only_asset_ids.split(",") if x.strip()}
            print(f"[plan] restricting to {len(only_ids)} asset_id(s) via --only-asset-ids")

        ts = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d_%H%M")
        pass_name = args.pass_name or f"transcript_cleanup_{ts}"
        run_dir = RUNS_DIR / pass_name
        run_dir.mkdir(parents=True, exist_ok=True)

        manifest_path = run_dir / "manifest.json"
        log_path = run_dir / "log.jsonl"
        errors_path = run_dir / "errors.jsonl"

        if not manifest_path.exists():
            atomic_write_json(manifest_path, {
                "run_id": pass_name,
                "pass_name": "transcript_cleanup",
                "started_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                "prompt_path": str(PROMPT_PATH.relative_to(ROOT)),
                "prompt_sha256": prompt_sha,
                "script_path": str(Path(__file__).relative_to(ROOT)),
                "candidates_dir": str(candidates_dir.relative_to(ROOT)) if ROOT in candidates_dir.parents else str(candidates_dir),
                "model": args.model,
                "model_invocation": "claude --print",
                "domain": "transcripts",
                "input_count": None,
                "validated_pass": 0,
                "high_conf_committed": 0,
                "candidates_committed": 0,
                "retried_count": 0,
                "final_failure_count": 0,
                "completed_at": None,
            })

        validator = CleanupValidator.from_workspace()

        # Build worklist — records touched by clusters that don't already have corrections for those targets.
        todo: list[tuple[Path, list[dict]]] = []
        for aid, ctxs in cluster_index.items():
            if only_ids is not None and str(aid).lower() not in only_ids:
                continue
            p = TRANSCRIPTS / f"{aid}.transcript.json"
            if not p.exists():
                continue
            try:
                rec = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                continue
            if needs_processing(rec, ctxs, prompt_sha):
                todo.append((p, ctxs))
        print(f"[plan] {len(todo)} records need cleanup processing this run")

        if args.max_records:
            todo = todo[:args.max_records]
            print(f"[plan] capping at --max-records={args.max_records}")

        run_started = time.time()
        ok_count = 0
        fail_count = 0
        retried_count = 0
        high_conf_total = 0
        cand_total = 0

        interrupted = False
        time_capped = False

        ex = ThreadPoolExecutor(max_workers=args.concurrency)
        try:
            futures = {
                ex.submit(
                    process_one,
                    p,
                    prompt_text=prompt_text,
                    cluster_contexts=ctxs,
                    validator=validator,
                    model=args.model,
                    pass_name=pass_name,
                    log_path=log_path,
                    errors_path=errors_path,
                ): p for (p, ctxs) in todo
            }

            try:
                for fut in as_completed(futures):
                    r = fut.result()
                    if r.ok:
                        ok_count += 1
                        high_conf_total += r.high_conf_count
                        cand_total += r.candidate_count
                    else:
                        fail_count += 1
                    if r.retried:
                        retried_count += 1

                    done = ok_count + fail_count
                    if done % 25 == 0 or done == len(todo):
                        elapsed = time.time() - run_started
                        rate = done / elapsed if elapsed > 0 else 0
                        print(
                            f"[progress] {done}/{len(todo)}  ok={ok_count}  fail={fail_count}  "
                            f"high={high_conf_total}  cand={cand_total}  "
                            f"rate={rate:.2f} rec/s  elapsed={elapsed/60:.1f}min",
                            flush=True,
                        )

                    if not time_capped and (time.time() - run_started > args.max_window_seconds):
                        print(f"[time-cap] elapsed > {args.max_window_seconds}s; cancelling pending workers")
                        ex.shutdown(wait=False, cancel_futures=True)
                        time_capped = True
                        break
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
            if not interrupted and not time_capped:
                ex.shutdown(wait=True)

        elapsed_min = (time.time() - run_started) / 60
        remaining = max(0, len(todo) - ok_count - fail_count)
        print("\n=== CLEANUP RUN COMPLETE ===")
        print(f"  processed (ok):            {ok_count}")
        print(f"  retried:                   {retried_count}")
        print(f"  failed:                    {fail_count}")
        print(f"  high-conf corrections:     {high_conf_total}")
        print(f"  review candidates:         {cand_total}")
        print(f"  remaining (this invocation): {remaining}")
        print(f"  elapsed:                   {elapsed_min:.1f} min")
        print(f"  pass dir:                  {run_dir.relative_to(ROOT)}")

        try:
            m = json.loads(manifest_path.read_text(encoding="utf-8"))
            m["validated_pass"] = m.get("validated_pass", 0) + ok_count
            m["high_conf_committed"] = m.get("high_conf_committed", 0) + high_conf_total
            m["candidates_committed"] = m.get("candidates_committed", 0) + cand_total
            m["retried_count"] = m.get("retried_count", 0) + retried_count
            m["final_failure_count"] = m.get("final_failure_count", 0) + fail_count
            m["last_run_completed_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
            if remaining == 0 and not args.max_records:
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
