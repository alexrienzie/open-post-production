"""
Bulk-fill `parent_id` on places/places.json using Gemini.

Why a separate pass: `apply_location_hierarchy.py` only handles the explicit / state-country
backbone and a small keyword set. This script asks an LLM to place each remaining
unparented record into the existing container catalog (countries, states, regions,
counties, protected_areas, cities, towns).

Container catalog (allowed parent ids) is built from the registry itself, so the
model can only pick targets that already exist.

Usage:
  python _scripts/registries/enrich_location_parents.py                  # dry run (writes patch)
  python _scripts/registries/enrich_location_parents.py --apply          # write parents back
  python _scripts/registries/enrich_location_parents.py --limit 60 --batch-size 20 --apply
  python _scripts/registries/enrich_location_parents.py --include-already-parented --apply

Requires GEMINI_API_KEY (env or HKCU\\Environment).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import unicodedata
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parents[2]
LOC_PATH = ROOT / "places" / "places.json"
PATCH_PATH = ROOT / "_scripts" / "locations_parents_patch.json"

CONTAINER_TYPES = ("country", "state", "region", "county", "protected_area", "city", "town")


def atomic_write_json(path: Path, data: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return s.lower().strip()


def read_hkcu_gemini_key() -> str:
    if os.name != "nt":
        return ""
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Environment") as h:
            v, _ = winreg.QueryValueEx(h, "GEMINI_API_KEY")
            return str(v).strip()
    except OSError:
        return ""


def gemini_keys() -> list[str]:
    out: list[str] = []
    p = (os.getenv("GEMINI_API_KEY") or "").strip()
    if p:
        out.append(p)
    if os.name == "nt":
        h = read_hkcu_gemini_key()
        if h and h not in out:
            out.append(h)
    return out


def looks_quota(e: BaseException) -> bool:
    s = str(e).lower()
    return "429" in s or "quota" in s or "resource has been exhausted" in s


def looks_bad_key(e: BaseException) -> bool:
    s = str(e).lower()
    return "api_key_invalid" in s or "invalid api key" in s


DEBUG_DIR = ROOT / "_scripts" / "_gemini_debug"


def _dump_debug(prompt: str, response_text: str | None, exc: BaseException | None = None) -> None:
    try:
        DEBUG_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        (DEBUG_DIR / f"prompt_{ts}.txt").write_text(prompt, encoding="utf-8")
        if response_text is not None:
            (DEBUG_DIR / f"response_{ts}.txt").write_text(response_text, encoding="utf-8")
        if exc is not None:
            (DEBUG_DIR / f"error_{ts}.txt").write_text(repr(exc), encoding="utf-8")
    except Exception:
        pass


def call_gemini(model: str, prompt: str, *, timeout_sec: int, max_out: int) -> dict[str, Any]:
    import google.generativeai as genai

    keys = gemini_keys()
    if not keys:
        raise RuntimeError("GEMINI_API_KEY not set (env or Windows HKCU\\Environment).")
    last_exc: Optional[BaseException] = None
    for ki, key in enumerate(keys):
        genai.configure(api_key=key)
        m = genai.GenerativeModel(model)
        backoff = 25
        for attempt in range(8):
            try:
                resp = m.generate_content(
                    prompt,
                    generation_config={
                        "temperature": 0.1,
                        "response_mime_type": "application/json",
                        "max_output_tokens": max_out,
                    },
                    request_options={"timeout": timeout_sec},
                )
                txt = getattr(resp, "text", None)
                if not txt or not str(txt).strip():
                    finish = None
                    try:
                        cand = (resp.candidates or [None])[0]
                        finish = getattr(cand, "finish_reason", None)
                    except Exception:
                        pass
                    _dump_debug(prompt, "", RuntimeError(f"empty response, finish_reason={finish}"))
                    raise RuntimeError(f"empty Gemini response (finish_reason={finish})")
                try:
                    parsed = parse_json(str(txt).strip())
                except Exception as parse_exc:
                    _dump_debug(prompt, str(txt), parse_exc)
                    raise
                return parsed
            except BaseException as e:
                last_exc = e
                if looks_bad_key(e) and ki + 1 < len(keys):
                    break
                if looks_quota(e) and attempt + 1 < 8:
                    sleep_for = min(backoff, 600)
                    print(f"[gemini] backoff {sleep_for}s (attempt {attempt + 1}/8)", flush=True)
                    time.sleep(sleep_for)
                    backoff = min(int(backoff * 1.45), 600)
                    continue
                raise
    assert last_exc is not None
    raise last_exc


def parse_json(text: str) -> dict[str, Any]:
    s = text.strip()
    if s.startswith("{") and s.endswith("}"):
        return json.loads(s)
    a, b = s.find("{"), s.rfind("}")
    if a == -1 or b <= a:
        raise ValueError("Model output had no JSON object")
    return json.loads(s[a : b + 1])


def build_container_catalog(places: list[dict]) -> dict[str, list[dict]]:
    """Group container-type places by type. Excludes ids that already chain through their parent
    so the model still gets enough scope.
    """
    by_type: dict[str, list[dict]] = defaultdict(list)
    for p in places:
        t = p.get("type") or "unknown"
        if t not in CONTAINER_TYPES:
            continue
        entry = {
            "id": p["id"],
            "name": p.get("canonical_name") or p["id"],
            "parent_id": p.get("parent_id"),
        }
        by_type[t].append(entry)
    for t in by_type:
        by_type[t].sort(key=lambda x: x["name"].lower())
    return by_type


def render_catalog(catalog: dict[str, list[dict]], compact: bool = True) -> str:
    """Compact form: `pl_id|Name|parent` per line, grouped by type."""
    lines: list[str] = []
    order = ["country", "state", "region", "county", "protected_area", "city", "town"]
    for t in order:
        rows = catalog.get(t) or []
        if not rows:
            continue
        lines.append(f"## {t} ({len(rows)})")
        for r in rows:
            par = r.get("parent_id") or "-"
            if compact:
                lines.append(f"{r['id']}|{r['name']}|{par}")
            else:
                lines.append(f"  - {r['id']} :: {r['name']}  parent={par}")
    return "\n".join(lines).strip()


def candidate_payload(p: dict) -> dict:
    notes = (p.get("notes") or "")[:280]
    return {
        "id": p["id"],
        "canonical_name": p.get("canonical_name") or p["id"],
        "type": p.get("type") or "unknown",
        "aliases": (p.get("aliases") or [])[:6],
        "mention_count": p.get("mention_count") or 0,
        "notes_excerpt": notes,
    }


SYSTEM_PROMPT = """You assign geographic parents (`parent_id`) to places in a documentary registry rooted in
the Greater Yellowstone region. For each place in INPUTS, pick the BEST parent from the
allowed catalog, or return null when the right parent is not in the catalog.

Hard rules:
- `parent_id` must be one of the ids listed in CATALOG (country / state / region / county / protected_area / city / town). NEVER invent slugs.
- A place can NEVER be its own parent.
- A country has no parent (return null).
- Prefer the SMALLEST containing entity that exists in CATALOG. Examples:
    - A trail/peak inside Grand Teton National Park -> pl_grand_teton_national_park.
    - A neighborhood inside a city -> the city id, not the state.
    - A town in Wyoming with no county available -> the state pl_wyoming.
- Use the `notes_excerpt` and `aliases` to disambiguate. The corpus is centered on Wyoming/Idaho/Utah/Montana, but every continent appears.
- US states (NY, WY, ...) -> pl_united_states. Canadian provinces -> pl_canada.
- If a place is itself a state/region/county/etc., pick its containing country/state.
- If genuinely ambiguous or unrelated to any catalog entry, return null. Do NOT guess.

Output JSON only:
{
  "assignments": [
    {"id": "pl_xxx", "parent_id": "pl_yyy_or_null", "confidence": "high|medium|low", "reason": "<= 140 chars"}
  ]
}
Return one assignment per input id; no extras, no missing ids."""


def gemini_assign_parents(
    inputs: list[dict],
    catalog_text: str,
    *,
    model: str,
    batch_size: int,
    timeout_sec: int,
    max_out: int,
) -> list[dict]:
    out: list[dict] = []
    for start in range(0, len(inputs), batch_size):
        batch = inputs[start : start + batch_size]
        prompt = (
            SYSTEM_PROMPT
            + "\n\nCATALOG:\n"
            + catalog_text
            + "\n\nINPUTS_JSON:\n"
            + json.dumps(batch, ensure_ascii=False)
        )
        print(
            f"[gemini] batch {start // batch_size + 1}/"
            f"{(len(inputs) + batch_size - 1) // batch_size} ({len(batch)} places)",
            flush=True,
        )
        try:
            data = call_gemini(model, prompt, timeout_sec=timeout_sec, max_out=max_out)
        except RuntimeError as e:
            print(f"  ERROR in batch: {e}", flush=True)
            continue
        assigns = data.get("assignments") if isinstance(data, dict) else None
        if assigns is None:
            print(f"  WARN: response missing 'assignments'. keys={list(data.keys()) if isinstance(data, dict) else type(data)}", flush=True)
            _dump_debug(prompt, json.dumps(data, ensure_ascii=False)[:8000], None)
            continue
        if not isinstance(assigns, list):
            print(f"  WARN: assignments is {type(assigns).__name__}, expected list", flush=True)
            _dump_debug(prompt, json.dumps(data, ensure_ascii=False)[:8000], None)
            continue
        if not assigns:
            print("  WARN: empty assignments[]; dumping prompt+response", flush=True)
            _dump_debug(prompt, json.dumps(data, ensure_ascii=False)[:8000], None)
        else:
            non_null = sum(1 for a in assigns if isinstance(a, dict) and a.get("parent_id") not in (None, "", "null"))
            print(f"  got {len(assigns)} assignments ({non_null} non-null parents)", flush=True)
            if non_null == 0:
                print("  -> all null; dumping prompt+response for inspection", flush=True)
                _dump_debug(prompt, json.dumps(data, ensure_ascii=False)[:8000], None)
        out.extend(assigns)
    return out


def detect_cycles(by_parent: dict[str, str | None]) -> list[str]:
    errs: list[str] = []
    for start in by_parent:
        cur = start
        seen: set[str] = set()
        steps = 0
        while cur:
            if cur in seen:
                errs.append(f"cycle starting at {start}")
                break
            seen.add(cur)
            steps += 1
            nxt = by_parent.get(cur)
            cur = nxt if nxt else None
            if steps > 60:
                errs.append(f"deep chain from {start}")
                break
    return errs


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="Write parent_ids back to places/places.json")
    ap.add_argument("--limit", type=int, default=0, help="Process at most N unparented (0 = all)")
    ap.add_argument("--batch-size", type=int, default=25)
    ap.add_argument("--model", default="gemini-2.5-flash")
    ap.add_argument("--timeout-sec", type=int, default=240)
    ap.add_argument("--max-output-tokens", type=int, default=12000)
    ap.add_argument(
        "--include-already-parented",
        action="store_true",
        help="Re-evaluate places that already have parent_id (to fix bad rule-based assignments)",
    )
    ap.add_argument(
        "--min-confidence",
        choices=("high", "medium", "low"),
        default="medium",
        help="Minimum confidence required to apply an assignment.",
    )
    args = ap.parse_args()

    if not LOC_PATH.exists():
        print(f"Missing {LOC_PATH}", file=sys.stderr)
        return 1

    doc = json.loads(LOC_PATH.read_text(encoding="utf-8"))
    places: list[dict] = doc.get("places") or []
    by_id = {p["id"]: p for p in places if p.get("id")}

    catalog = build_container_catalog(places)
    catalog_text = render_catalog(catalog)
    catalog_ids = {r["id"] for rows in catalog.values() for r in rows}

    candidates: list[dict] = []
    for p in places:
        if p.get("type") == "country":
            continue
        if p.get("parent_id") and not args.include_already_parented:
            continue
        candidates.append(candidate_payload(p))

    candidates.sort(key=lambda x: -int(x.get("mention_count") or 0))
    if args.limit > 0:
        candidates = candidates[: args.limit]

    print(
        f"Catalog containers: {sum(len(v) for v in catalog.values())} | "
        f"candidates to (re)assign: {len(candidates)}"
    )
    if not candidates:
        print("Nothing to do.")
        return 0

    try:
        assigns = gemini_assign_parents(
            candidates,
            catalog_text,
            model=args.model,
            batch_size=args.batch_size,
            timeout_sec=args.timeout_sec,
            max_out=args.max_output_tokens,
        )
    except RuntimeError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    rank = {"high": 3, "medium": 2, "low": 1}
    threshold = rank[args.min_confidence]
    proposals: dict[str, dict] = {}
    rejects: list[dict] = []

    for a in assigns:
        if not isinstance(a, dict):
            continue
        pid = a.get("id")
        parent = a.get("parent_id")
        conf = (a.get("confidence") or "low").lower()
        reason = (a.get("reason") or "")[:200]
        if not isinstance(pid, str) or pid not in by_id:
            rejects.append({"reason": "unknown_id", "raw": a})
            continue
        if parent in (None, "", "null"):
            rejects.append({"reason": "null_parent", "raw": a})
            continue
        if not isinstance(parent, str) or parent not in catalog_ids:
            rejects.append({"reason": "unknown_or_invalid_parent", "raw": a})
            continue
        if parent == pid:
            rejects.append({"reason": "self_parent", "raw": a})
            continue
        if rank.get(conf, 0) < threshold:
            rejects.append({"reason": f"confidence<{args.min_confidence}", "raw": a})
            continue
        proposals[pid] = {"parent_id": parent, "confidence": conf, "reason": reason}

    # Cycle check on the proposed graph
    by_parent_after: dict[str, str | None] = {p["id"]: p.get("parent_id") for p in places}
    for pid, info in proposals.items():
        by_parent_after[pid] = info["parent_id"]
    cycle_errs = detect_cycles(by_parent_after)
    if cycle_errs:
        # Drop any assignments that participate in a cycle to keep the tree sane
        bad = set()
        for err in cycle_errs:
            for pid in proposals:
                if pid in err:
                    bad.add(pid)
        for b in bad:
            rejects.append({"reason": "would_create_cycle", "raw": proposals.pop(b, None)})
        print(f"Dropped {len(bad)} cycle-creating assignments")

    patch = {
        "_review_meta": {
            "reviewer": "enrich_location_parents.py",
            "model": args.model,
            "min_confidence": args.min_confidence,
            "reviewed_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
            "total_candidates": len(candidates),
            "applied": len(proposals),
            "rejected": len(rejects),
        },
        "assignments": [
            {"id": pid, "parent_id": v["parent_id"], "confidence": v["confidence"], "reason": v["reason"]}
            for pid, v in sorted(proposals.items())
        ],
        "rejects": rejects[:200],
    }
    atomic_write_json(PATCH_PATH, patch)
    print(f"Wrote audit -> {PATCH_PATH.relative_to(ROOT)}")

    if not args.apply:
        print("Dry run. Re-run with --apply to update locations.json.")
        return 0

    changed = 0
    for pid, v in proposals.items():
        cur = by_id[pid].get("parent_id")
        if cur != v["parent_id"]:
            by_id[pid]["parent_id"] = v["parent_id"]
            changed += 1

    meta = doc.setdefault("_meta", {})
    meta["source_passes"] = (meta.get("source_passes") or []) + ["enrich_location_parents.py"]
    doc["last_updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    atomic_write_json(LOC_PATH, doc)
    print(f"Applied {changed} parent_id updates -> {LOC_PATH.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
