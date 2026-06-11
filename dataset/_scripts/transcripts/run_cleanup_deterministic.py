"""
Phase A of transcript cleanup — deterministic (free) cluster match.

For clusters from `_runs/cleanup_candidates_<ts>/candidate_clusters.json` with
`occurrences >= threshold`, search transcripts for each cluster's mishearing
strings using a conservative word-boundary regex match, and emit additive
`corrections[]` entries (high confidence) without ever mutating Whisper text.

Corrections are validated and merged via CleanupValidator. Per-record commits
are atomic. Re-runs are idempotent (no duplicate correction entries; unchanged
records are not rewritten).
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Iterable, Optional

ROOT = Path(__file__).resolve().parents[2]
RUNS_DIR = ROOT / "_runs"
TRANSCRIPTS = ROOT / "assets/transcripts"
LOCK_PATH = RUNS_DIR / ".cleanup_deterministic.lock"

# Reuse analysis runner's helpers — no duplication
sys.path.insert(0, str(ROOT / "_scripts"))
from run_transcript_analysis_skeleton import (  # noqa: E402
    SingleRunner,
    atomic_write_json,
    append_log,
    cleanup_stale_tmps,
)
from validate_transcript_cleanup import CleanupValidator  # noqa: E402


def _utc_ts_slug() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d_%H%M")


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def find_most_recent_candidates_dir(runs_dir: Path = RUNS_DIR) -> Optional[Path]:
    """Pick the newest `_runs/cleanup_candidates_*` directory by mtime."""
    cands = [p for p in runs_dir.glob("cleanup_candidates_*") if p.is_dir()]
    if not cands:
        return None
    cands.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return cands[0]


def load_clusters(candidates_dir: Path) -> list[dict]:
    clusters_path = candidates_dir / "candidate_clusters.json"
    if not clusters_path.exists():
        raise FileNotFoundError(f"missing {clusters_path}")
    return json.loads(clusters_path.read_text(encoding="utf-8"))


def _entity_fields_for_slug(slug: str) -> tuple[str, str, str]:
    """Return (type, id_field_name, other_null_fields_csv) for a target slug."""
    if slug.startswith("p_"):
        return "name_substitution", "people_id", "org_id,place_id"
    if slug.startswith("o_"):
        return "term_substitution", "org_id", "people_id,place_id"
    if slug.startswith("pl_"):
        return "term_substitution", "place_id", "people_id,org_id"
    raise ValueError(f"unrecognized target_slug prefix: {slug!r}")


def _word_boundary_pattern(text: str) -> re.Pattern[str]:
    """
    Conservative match: use \\b boundaries when the ends are word characters.

    This mitigates substring false-positives like 'Ann' matching 'cannot'.
    """
    if not text:
        return re.compile(r"(?!x)x")  # never matches
    escaped = re.escape(text)
    start_word = bool(re.match(r"^\w", text))
    end_word = bool(re.match(r".*\w$", text))
    if start_word and end_word:
        return re.compile(rf"\b{escaped}\b")
    return re.compile(escaped)


def _iter_match_indices(pattern: re.Pattern[str], text: str) -> Iterable[int]:
    for m in pattern.finditer(text or ""):
        yield m.start()


def _is_short_single_token(s: str) -> bool:
    """Heuristic safety check for high-false-positive first-name tokens."""
    if not isinstance(s, str):
        return False
    t = s.strip()
    if not t:
        return False
    if " " in t:
        return False
    # ignore obvious abbreviations like "Mr." etc as still single token
    bare = re.sub(r"[^\w]", "", t)
    return 0 < len(bare) <= 4


def _transcript_mentions_target_id(transcript: dict, target_slug: str) -> bool:
    if target_slug.startswith("p_"):
        return target_slug in (transcript.get("people_ids") or [])
    if target_slug.startswith("o_"):
        return target_slug in (transcript.get("org_ids") or [])
    if target_slug.startswith("pl_"):
        return target_slug in (transcript.get("place_ids") or [])
    return False


def _purge_unsafe_deterministic_short_token_corrections(transcript: dict) -> tuple[dict, int]:
    """
    Remove previously-emitted deterministic corrections that are short single-token
    matches *and* whose target slug isn't already mentioned in this record's ids.
    This keeps Phase A conservative and avoids semantically-wrong short-name style
    mappings that still pass validator.
    """
    removed = 0
    out = dict(transcript)
    corrs = list(out.get("corrections") or [])
    kept: list[dict] = []
    for c in corrs:
        if c.get("model") != "deterministic-cluster-match":
            kept.append(c)
            continue
        original = c.get("original") or ""
        if not _is_short_single_token(original):
            kept.append(c)
            continue
        # Determine the target slug (exactly one of these should be non-null)
        target = c.get("people_id") or c.get("org_id") or c.get("place_id")
        if isinstance(target, str) and target and not _transcript_mentions_target_id(out, target):
            removed += 1
            continue
        kept.append(c)
    if removed:
        out["corrections"] = kept
    return out, removed


def build_corrections_for_cluster(
    transcript: dict,
    *,
    target_slug: str,
    target_canonical: str,
    mishearings: list[str],
) -> list[dict]:
    """Return a list of correction objects (one per mishearing with ≥1 hit)."""
    corrections: list[dict] = []
    full_text = transcript.get("full_text") or ""
    segments = transcript.get("segments") or []

    ctype, id_field, other_nulls = _entity_fields_for_slug(target_slug)
    null_a, null_b = other_nulls.split(",")

    short_token_guard = _is_short_single_token
    target_is_prementioned = _transcript_mentions_target_id(transcript, target_slug)

    for original in mishearings:
        # Safety guard: for very short single-token mishearings (e.g. Ann, Mike, Kai),
        # only auto-apply if the record already mentions that entity id. Otherwise
        # leave for Phase B / review.
        if short_token_guard(original) and not target_is_prementioned:
            continue

        pat = _word_boundary_pattern(original)

        hits_full = list(_iter_match_indices(pat, full_text))
        hit_seg_indices: list[int] = []
        for i, seg in enumerate(segments):
            if not isinstance(seg, dict):
                continue
            seg_text = seg.get("text") or ""
            if any(True for _ in _iter_match_indices(pat, seg_text)):
                hit_seg_indices.append(i)

        if not hits_full and not hit_seg_indices:
            continue

        spans: list[dict] = []
        if hits_full:
            spans.append({"field": "full_text"})
        for i in hit_seg_indices:
            seg = segments[i]
            spans.append({
                "field": f"segments[{i}].text",
                "start_sec": seg.get("start_sec"),
                "end_sec": seg.get("end_sec"),
            })

        corr = {
            "type": ctype,
            "original": original,
            "corrected": target_canonical,
            "people_id": None,
            "org_id": None,
            "place_id": None,
            "spans": spans,
            "confidence": "high",
            "rationale": (
                "Deterministic cluster match: word-boundary regex hit for a corpus-confirmed "
                "mishearing→canonical mapping (Phase A)."
            ),
        }
        corr[id_field] = target_slug
        corr[null_a] = None
        corr[null_b] = None
        corrections.append(corr)

    return corrections


def _record_path_for_asset(asset_id: str) -> Path:
    return TRANSCRIPTS / f"{asset_id}.transcript.json"


def _would_change(transcript: dict, merged: dict) -> bool:
    return merged != transcript


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--candidates",
        default=None,
        help=(
            "Path to _runs/cleanup_candidates_<ts>/ directory. "
            "Default: most recent cleanup_candidates_* dir."
        ),
    )
    ap.add_argument("--threshold", type=int, default=10, help="Min cluster occurrences for Phase A.")
    ap.add_argument("--dry-run", action="store_true", help="Print estimates only; no writes.")
    ap.add_argument(
        "--refresh-indexes",
        action="store_true",
        help="After successful mutations, rebuild MANIFEST.json and ../indexes/editorial_catalog.sqlite.",
    )
    ap.add_argument(
        "--purge-unsafe-short-tokens",
        action="store_true",
        help=(
            "Before generating new deterministic corrections, remove prior "
            "deterministic-cluster-match corrections whose original is a short single token "
            "and whose target slug isn't already present in this record's ids."
        ),
    )
    args = ap.parse_args()

    candidates_dir = Path(args.candidates).resolve() if args.candidates else None
    if candidates_dir is None:
        candidates_dir = find_most_recent_candidates_dir()
        if candidates_dir is None:
            print("ERROR: no _runs/cleanup_candidates_* directory found.", file=sys.stderr)
            return 1
    if not candidates_dir.exists():
        print(f"ERROR: candidates dir not found: {candidates_dir}", file=sys.stderr)
        return 1

    clusters_path = candidates_dir / "candidate_clusters.json"
    clusters = load_clusters(candidates_dir)

    eligible = [c for c in clusters if int(c.get("occurrences") or 0) >= args.threshold]
    skipped = len(clusters) - len(eligible)

    # Precompute run metadata
    ts = _utc_ts_slug()
    run_id = f"cleanup_deterministic_{ts}"
    run_dir = RUNS_DIR / run_id
    manifest_path = run_dir / "manifest.json"
    log_path = run_dir / "log.jsonl"
    errors_path = run_dir / "errors.jsonl"

    # Validator + workspace registries
    validator = CleanupValidator.from_workspace()

    # Estimate / worklist build (single-threaded)
    touched_asset_ids: set[str] = set()
    for c in eligible:
        for aid in (c.get("asset_ids") or []):
            touched_asset_ids.add(aid)

    # Quick estimate pass (read-only) that also pre-filters to migrated schema v5
    migrated_assets: list[str] = []
    missing_assets: list[str] = []
    for aid in sorted(touched_asset_ids):
        p = _record_path_for_asset(aid)
        if not p.exists():
            missing_assets.append(aid)
            continue
        try:
            rec = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        if (rec.get("schema_version") or 0) >= 5:
            migrated_assets.append(aid)

    print("[plan] deterministic cleanup (Phase A)")
    print(f"[plan] candidates dir: {candidates_dir.relative_to(ROOT) if ROOT in candidates_dir.parents else candidates_dir}")
    print(f"[plan] threshold: {args.threshold}")
    print(f"[plan] clusters: total={len(clusters)} eligible={len(eligible)} skipped(<threshold)={skipped}")
    print(f"[plan] asset_ids touched by eligible clusters: {len(touched_asset_ids)}")
    print(f"[plan] migrated (schema>=5) records present: {len(migrated_assets)} (missing files: {len(missing_assets)})")
    if args.dry_run:
        return 0

    with SingleRunner(LOCK_PATH):
        # Sweep stale .tmp files left by a previous interrupted run
        n_stale = cleanup_stale_tmps()
        if n_stale:
            print(f"[cleanup] removed {n_stale} stale .tmp files from previous run")

        run_dir.mkdir(parents=True, exist_ok=True)
        candidates_sha = _sha256_file(clusters_path)

        if not manifest_path.exists():
            atomic_write_json(manifest_path, {
                "run_id": run_id,
                "pass_name": "cleanup_deterministic",
                "started_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                "script_path": str(Path(__file__).relative_to(ROOT)),
                "candidates_dir": str(candidates_dir.relative_to(ROOT)) if ROOT in candidates_dir.parents else str(candidates_dir),
                "candidate_clusters_sha256": candidates_sha,
                "threshold": args.threshold,
                "model": "deterministic-cluster-match",
                "domain": "transcripts",
                "eligible_clusters": len(eligible),
                "skipped_clusters": skipped,
                "input_asset_ids": len(migrated_assets),
                "records_touched": 0,
                "records_unchanged": 0,
                "high_conf_committed": 0,
                "validation_failed_count": 0,
                "missing_record_files": len(missing_assets),
                "completed_at": None,
            })

        touched = 0
        unchanged = 0
        high_conf_total = 0
        validation_failed = 0

        # Build per-asset cluster list (keeps Phase A single-threaded + deterministic)
        by_asset: dict[str, list[dict]] = {}
        for c in eligible:
            for aid in (c.get("asset_ids") or []):
                by_asset.setdefault(aid, []).append(c)

        for aid in migrated_assets:
            p = _record_path_for_asset(aid)
            try:
                rec = json.loads(p.read_text(encoding="utf-8"))
            except Exception as e:
                append_log(errors_path, {"asset_id": aid, "ok": False, "error": f"read_failed: {str(e)[:300]}"})
                continue

            if args.purge_unsafe_short_tokens:
                purged, removed = _purge_unsafe_deterministic_short_token_corrections(rec)
                if removed:
                    atomic_write_json(p, purged)
                    append_log(log_path, {
                        "asset_id": aid, "ok": True, "touched": True,
                        "high_conf_count": 0, "candidate_count": 0,
                        "reason": "purged_unsafe_short_token_deterministic_corrections",
                        "removed_count": removed,
                    })
                    touched += 1
                    # refresh record for subsequent generation step
                    rec = purged

            all_corrs: list[dict] = []
            for c in (by_asset.get(aid) or []):
                target_slug = c.get("target_slug") or ""
                target_canonical = c.get("target_canonical") or ""
                mishearings = [m.get("text") for m in (c.get("mishearings") or []) if isinstance(m, dict) and m.get("text")]
                if not target_slug or not target_canonical or not mishearings:
                    continue
                try:
                    all_corrs.extend(build_corrections_for_cluster(
                        rec,
                        target_slug=target_slug,
                        target_canonical=target_canonical,
                        mishearings=mishearings,
                    ))
                except Exception as e:
                    append_log(errors_path, {
                        "asset_id": aid, "ok": False,
                        "error": f"cluster_build_failed: {str(e)[:300]}",
                        "target_slug": target_slug,
                    })

            if not all_corrs:
                append_log(log_path, {"asset_id": aid, "ok": True, "touched": False, "reason": "no_word_boundary_hits"})
                unchanged += 1
                continue

            out = {"corrections": all_corrs}
            v = validator.validate(out, rec)
            if not v.ok:
                validation_failed += 1
                append_log(errors_path, {
                    "asset_id": aid,
                    "ok": False,
                    "error": "validation_failed",
                    "details": v.errors,
                    "generated_corrections": len(all_corrs),
                })
                append_log(log_path, {"asset_id": aid, "ok": False, "error": "validation_failed"})
                continue

            applied_at = dt.datetime.now(dt.timezone.utc).isoformat()
            merged = validator.merge(
                rec,
                v,
                applied_at=applied_at,
                model_str="deterministic-cluster-match",
            )

            if not _would_change(rec, merged):
                append_log(log_path, {
                    "asset_id": aid, "ok": True, "touched": False,
                    "reason": "idempotent_no_change",
                    "high_conf_count": len(v.high_confidence),
                    "candidate_count": len(v.candidates),
                })
                unchanged += 1
                continue

            atomic_write_json(p, merged)
            hc = len(v.high_confidence)
            high_conf_total += hc
            touched += 1
            append_log(log_path, {
                "asset_id": aid, "ok": True, "touched": True,
                "high_conf_count": hc,
                "candidate_count": len(v.candidates),
                "warnings": v.warnings,
            })

        try:
            m = json.loads(manifest_path.read_text(encoding="utf-8"))
            m["records_touched"] = m.get("records_touched", 0) + touched
            m["records_unchanged"] = m.get("records_unchanged", 0) + unchanged
            m["high_conf_committed"] = m.get("high_conf_committed", 0) + high_conf_total
            m["validation_failed_count"] = m.get("validation_failed_count", 0) + validation_failed
            m["completed_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
            atomic_write_json(manifest_path, m)
        except Exception:
            pass

        print("\n=== DETERMINISTIC CLEANUP COMPLETE ===")
        print(f"  records touched:         {touched}")
        print(f"  records unchanged:       {unchanged}")
        print(f"  high-conf committed:     {high_conf_total}")
        print(f"  validation failed:       {validation_failed}")
        print(f"  run dir:                 {run_dir.relative_to(ROOT)}")

        # Keep derived SQL/JSON indexes in sync with any transcript mutations.
        if args.refresh_indexes and touched:
            try:
                from refresh_indexes import refresh_all_indexes  # type: ignore

                refresh_all_indexes()
            except Exception as e:
                print(f"[indexes] refresh failed: {str(e)[:200]}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

