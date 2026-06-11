"""
Curate places/places.json: merge duplicates (aliases), fix types, optional deletes.

Pipeline:
1) Deterministic: merge places sharing the same normalized canonical_name.
2) Similarity clusters: union-find on adjacent near-duplicates (sorted-by-name window + string ratio).
3) Optional Gemini: resolve each multi-member cluster (winner, absorbs, canonical_name, type).

Outputs `_scripts/locations_curation_patch.json` (audit). Use --apply to write locations.json
and rewrite `place_ids` on transcript records.

Requires GEMINI_API_KEY (env or Windows HKCU Environment) for --use-llm.

Usage:
  python _scripts/registries/curate_locations_registry.py --deterministic-only --apply
  python _scripts/registries/curate_locations_registry.py --use-llm --apply
  python _scripts/registries/curate_locations_registry.py --use-llm   # dry-run: writes patch only
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import unicodedata
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parents[2]
LOC_PATH = ROOT / "places" / "places.json"
PATCH_PATH = ROOT / "_scripts" / "locations_curation_patch.json"
TRANSCRIPTS = ROOT / "assets/transcripts"

ALLOWED_TYPES = frozenset(
    {
        "country",
        "state",
        "region",
        "county",
        "city",
        "town",
        "protected_area",
        "natural_feature",
        "route",
        "trailhead",
        "establishment",
        "residence",
        "infrastructure",
        "airport",
        "transport",
        "unknown",
    }
)


def atomic_write_json(path: Path, data: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def norm_key(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    s = s.lower().strip()
    s = re.sub(r"\s+", " ", s)
    return s


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


def gemini_key_candidates() -> list[str]:
    out: list[str] = []
    proc = (os.getenv("GEMINI_API_KEY") or "").strip()
    if proc:
        out.append(proc)
    if os.name == "nt":
        hk = read_hkcu_gemini_key()
        if hk and hk not in out:
            out.append(hk)
    return out


def looks_quota(exc: BaseException) -> bool:
    s = str(exc).lower()
    return "429" in s or "resource has been exhausted" in s or "quota" in s


def looks_bad_key(exc: BaseException) -> bool:
    s = str(exc).lower()
    return "api_key_invalid" in s or "invalid api key" in s


def call_gemini_json(model: str, prompt: str, *, timeout_sec: int, max_out: int) -> dict[str, Any]:
    import google.generativeai as genai

    keys = gemini_key_candidates()
    if not keys:
        raise RuntimeError(
            "GEMINI_API_KEY not set (env or Windows HKCU\\Environment). "
            "Use --deterministic-only to skip LLM."
        )
    last_exc: Optional[BaseException] = None
    for ki, api_key in enumerate(keys):
        genai.configure(api_key=api_key)
        m = genai.GenerativeModel(model)
        backoff = 25
        for attempt in range(8):
            try:
                resp = m.generate_content(
                    prompt,
                    generation_config={
                        "temperature": 0.15,
                        "response_mime_type": "application/json",
                        "max_output_tokens": max_out,
                    },
                    request_options={"timeout": timeout_sec},
                )
                text = getattr(resp, "text", None)
                if not text or not str(text).strip():
                    raise RuntimeError("empty Gemini response")
                return extract_json_object(str(text).strip())
            except BaseException as e:
                last_exc = e
                if looks_bad_key(e) and ki + 1 < len(keys):
                    break
                if looks_quota(e) and attempt + 1 < 8:
                    print(f"[gemini] quota/backoff sleep {backoff}s (attempt {attempt + 1}/8)", flush=True)
                    time.sleep(min(backoff, 600))
                    backoff = min(int(backoff * 1.45), 600)
                    continue
                raise
    assert last_exc is not None
    raise last_exc


def extract_json_object(text: str) -> dict[str, Any]:
    s = text.strip()
    # Gemini sometimes returns JSON with extra trailing text / markdown fences.
    # Parse the *first* JSON object and ignore trailing junk.
    if s.startswith("{") and s.endswith("}"):
        return json.loads(s)
    a = s.find("{")
    if a == -1:
        raise ValueError("No JSON object in model output")
    # Use raw_decode so we tolerate "Extra data" after the first object.
    decoder = json.JSONDecoder()
    obj, _ = decoder.raw_decode(s[a:])
    return obj


def collapse_merges(merges: dict[str, str]) -> dict[str, str]:
    """Each key (loser) maps to ultimate winner after chaining."""
    mm = dict(merges)
    changed = True
    while changed:
        changed = False
        for k, v in list(mm.items()):
            if v in mm and mm[v] != v:
                mm[k] = mm[v]
                changed = True
    return mm


def ratio(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


@dataclass
class PlaceRow:
    raw: dict

    @property
    def id(self) -> str:
        return self.raw["id"]

    @property
    def name(self) -> str:
        return (self.raw.get("canonical_name") or "").strip()

    @property
    def m(self) -> int:
        return int(self.raw.get("mention_count") or 0)


class UnionFind:
    def __init__(self, ids: list[str]) -> None:
        self.p = {i: i for i in ids}

    def find(self, x: str) -> str:
        if self.p[x] != x:
            self.p[x] = self.find(self.p[x])
        return self.p[x]

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[rb] = ra

    def clusters(self) -> dict[str, list[str]]:
        buck: dict[str, list[str]] = defaultdict(list)
        for i in self.p:
            buck[self.find(i)].append(i)
        return dict(buck)


def deterministic_norm_merges(rows: list[PlaceRow]) -> dict[str, str]:
    """loser_id -> winner_id for identical norm_key(canonical_name)."""
    groups: dict[str, list[PlaceRow]] = defaultdict(list)
    for r in rows:
        groups[norm_key(r.name) or r.id].append(r)

    merges: dict[str, str] = {}
    for _, g in groups.items():
        if len(g) < 2:
            continue
        g.sort(key=lambda x: (-x.m, x.id))
        winner = g[0].id
        for r in g[1:]:
            merges[r.id] = winner
    return merges


def deterministic_comma_parent_city_merges(rows: list[PlaceRow]) -> dict[str, str]:
    """
    Merge city/town records when one canonical_name is just the base name (e.g. "Afton")
    and another canonical_name is the same base plus ", <State>" (e.g. "Afton, Wyoming"),
    assuming both records share the same `parent_id` (state).

    This catches the high-frequency "city vs city+state-suffix" duplicates that similarity
    thresholds often miss when commas and state words lower string similarity.
    """
    allowed_types = {"city", "town"}
    # Index exact canonical_name for (type, parent_id).
    by_key: dict[tuple[str, Optional[str], str], PlaceRow] = {}
    for r in rows:
        if (r.raw.get("type") not in allowed_types) or not r.name:
            continue
        key = (r.raw.get("type"), r.raw.get("parent_id"), r.name)
        by_key.setdefault(key, r)
        # Prefer highest mention_count when multiple entries share the same key.
        if by_key[key].m < r.m:
            by_key[key] = r

    merges: dict[str, str] = {}
    for r in rows:
        if r.raw.get("type") not in allowed_types:
            continue
        if not r.name or "," not in r.name:
            continue
        base = r.name.split(",", 1)[0].strip()
        if not base:
            continue
        key = (r.raw.get("type"), r.raw.get("parent_id"), base)
        winner = by_key.get(key)
        if not winner:
            continue
        if winner.id != r.id:
            # Deterministic winner chosen by apply_merge_map later; here we just seed.
            merges[r.id] = winner.id
    return merges


def similarity_edges(rows: list[PlaceRow], *, window: int, min_ratio: float) -> list[tuple[str, str]]:
    """Compare each row to next `window` neighbors in sorted normalized name order."""
    sorted_r = sorted(rows, key=lambda r: norm_key(r.name) or r.id)
    edges: list[tuple[str, str]] = []
    for i, a in enumerate(sorted_r):
        na = norm_key(a.name)
        if not na:
            continue
        for b in sorted_r[i + 1 : i + 1 + window]:
            nb = norm_key(b.name)
            if not nb:
                continue
            if na[0] != nb[0]:
                break
            if ratio(na, nb) >= min_ratio:
                edges.append((a.id, b.id))
    return edges


def clusters_from_edges(ids: list[str], edges: list[tuple[str, str]]) -> list[list[str]]:
    uf = UnionFind(ids)
    for a, b in edges:
        if a in uf.p and b in uf.p:
            uf.union(a, b)
    cl = uf.clusters()
    return [sorted(v) for v in cl.values() if len(v) > 1]


def row_by_id(rows: list[PlaceRow]) -> dict[str, PlaceRow]:
    return {r.id: r for r in rows}


def apply_merge_map(rows: list[PlaceRow], merges: dict[str, str]) -> list[PlaceRow]:
    """Collapse losers into winners; returns new PlaceRow list."""
    if not merges:
        return rows
    collapsed = collapse_merges(merges)
    absorbed = {lid for lid, wid in collapsed.items() if lid != wid}
    by_id_raw = {r.id: json.loads(json.dumps(r.raw)) for r in rows}

    out: dict[str, dict] = {}
    for r in rows:
        rid = r.id
        if rid in absorbed:
            continue
        out[rid] = by_id_raw[rid]

    for loser, winner in collapsed.items():
        if loser == winner or loser not in by_id_raw:
            continue
        if winner not in out:
            continue
        L, W = by_id_raw[loser], out[winner]
        W["mention_count"] = int(W.get("mention_count") or 0) + int(L.get("mention_count") or 0)
        for a in [L.get("canonical_name")] + (L.get("aliases") or []):
            if isinstance(a, str) and a.strip() and a.strip() != W.get("canonical_name"):
                W.setdefault("aliases", [])
                if a.strip() not in W["aliases"]:
                    W["aliases"].append(a.strip())
        W["sources"] = sorted(set((W.get("sources") or []) + (L.get("sources") or [])))
        ln = (L.get("notes") or "").strip()
        if ln:
            prev = (W.get("notes") or "").strip()
            W["notes"] = (prev + " | merged " + loser + ": " + ln[:600]).strip(" |")[:4000]

    for w in out.values():
        c = w.get("canonical_name")
        w["aliases"] = sorted({x for x in (w.get("aliases") or []) if x and x != c}, key=str.lower)

    return [PlaceRow(out[i]) for i in sorted(out.keys())]


def gemini_resolve_clusters(
    clusters: list[list[str]],
    by_id: dict[str, PlaceRow],
    *,
    model: str,
    timeout_sec: int,
    max_out: int,
    batch_size: int,
) -> tuple[dict[str, str], dict[str, str], dict[str, str], list[str]]:
    """
    Returns merges (loser->winner), type_fixes, canonical_overrides, drop_ids
    """
    merges: dict[str, str] = {}
    type_fixes: dict[str, str] = {}
    canonical_overrides: dict[str, str] = {}
    drop_ids: list[str] = []

    system = """You curate a documentary film locations registry for the sample film (Grand Teton region).
For each cluster, the entries may be duplicates (same real-world place) or may need to stay separate.

Rules:
- Prefer ONE winning pl_* id per duplicate group: the one with the highest mention_count (m) unless a clearly wrong/junk id should not win.
- NEVER merge different real places (e.g. Jackson WY vs Jackson MS; Grand Teton peak vs Grand Teton National Park are DIFFERENT).
- Absorb true duplicates: all other ids in the cluster merge into winner; their canonical names become aliases of the winner.
- If a cluster mixes unrelated places, split by outputting multiple resolution objects with disjoint absorb_ids each with its own winner_id, OR mark unrelated ids with drop_ids only if they are clearly not geographic (garbage strings).
- Types must be one of: country, state, region, county, city, town, protected_area, natural_feature, route, trailhead, establishment, residence, infrastructure, airport, transport, unknown.

Return JSON only:
{
  "resolutions": [
    {
      "winner_id": "pl_*",
      "absorb_ids": ["pl_*", ...],
      "canonical_name": "Preferred display name",
      "type": "city",
      "drop_ids": [],
      "reason": "short"
    }
  ]
}

Every pl_* id in the input clusters must appear exactly once across winner_id, absorb_ids, and drop_ids combined (either absorbed or dropped or winner). No hallucinated ids."""

    for start in range(0, len(clusters), batch_size):
        batch = clusters[start : start + batch_size]
        payload = []
        for ci, c in enumerate(batch):
            payload.append(
                {
                    "cluster_index": start + ci,
                    "places": [
                        {
                            "id": x,
                            "canonical_name": by_id[x].name,
                            "type": by_id[x].raw.get("type"),
                            "m": by_id[x].m,
                            "aliases": (by_id[x].raw.get("aliases") or [])[:8],
                        }
                        for x in c
                    ],
                }
            )
        prompt = system + "\n\nCLUSTERS_JSON:\n" + json.dumps(payload, ensure_ascii=False)
        out = call_gemini_json(model, prompt, timeout_sec=timeout_sec, max_out=max_out)
        for res in out.get("resolutions") or []:
            wid = res.get("winner_id")
            absorbs = [x for x in (res.get("absorb_ids") or []) if isinstance(x, str)]
            drops = [x for x in (res.get("drop_ids") or []) if isinstance(x, str)]
            if not isinstance(wid, str) or not wid.startswith("pl_"):
                continue
            cname = res.get("canonical_name")
            t = res.get("type")
            cluster_ids = set([wid] + absorbs + drops)
            for d in drops:
                if d in by_id:
                    drop_ids.append(d)
            for a in absorbs:
                if a != wid and a in by_id:
                    merges[a] = wid
            if isinstance(cname, str) and cname.strip():
                canonical_overrides[wid] = cname.strip()
            if isinstance(t, str) and t in ALLOWED_TYPES:
                type_fixes[wid] = t
            # validate coverage
            missing = [i for i in cluster_ids if i not in by_id and i.startswith("pl_")]
            if missing:
                print(f"[warn] model referenced unknown ids: {missing[:5]}", file=sys.stderr)

    return merges, type_fixes, canonical_overrides, drop_ids


def rewrite_transcript_place_ids(collapsed_merges: dict[str, str], deletes: set[str]) -> dict[str, int]:
    def remap(ids: list) -> tuple[list, int]:
        out, seen = [], set()
        ch = 0
        for x in ids or []:
            if not isinstance(x, str):
                continue
            t = collapsed_merges.get(x, x)
            if t in deletes:
                ch += 1
                continue
            if t != x:
                ch += 1
            if t not in seen:
                out.append(t)
                seen.add(t)
        return sorted(out), ch

    stats = {"examined": 0, "rewritten": 0, "changes": 0}
    for p in TRANSCRIPTS.glob("*.transcript.json"):
        try:
            rec = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        stats["examined"] += 1
        if "place_ids" not in rec:
            continue
        new_list, ch = remap(rec.get("place_ids") or [])
        if ch > 0:
            rec["place_ids"] = new_list
            atomic_write_json(p, rec)
            stats["rewritten"] += 1
            stats["changes"] += ch
    return stats


def main() -> int:
    ap = argparse.ArgumentParser(description="Curate locations registry (dedupe + optional Gemini).")
    ap.add_argument("--apply", action="store_true", help="Write places/places.json + rewrite transcript place_ids")
    ap.add_argument("--deterministic-only", action="store_true", help="Skip Gemini")
    ap.add_argument("--use-llm", action="store_true", help="Resolve similarity clusters with Gemini")
    ap.add_argument(
        "--apply-from-patch",
        action="store_true",
        help="Apply the already-computed _scripts/locations_curation_patch.json (skips LLM / similarity work). "
             "Rewrites places/places.json + transcript place_ids based on the patch content.",
    )
    ap.add_argument(
        "--patch-path",
        type=Path,
        default=PATCH_PATH,
        help="Patch JSON to apply when --apply-from-patch is set.",
    )
    ap.add_argument("--similarity-window", type=int, default=24)
    ap.add_argument("--similarity-ratio", type=float, default=0.91)
    ap.add_argument("--llm-batch-size", type=int, default=6, help="Clusters per Gemini request")
    ap.add_argument("--model", default="gemini-2.5-flash")
    ap.add_argument("--timeout-sec", type=int, default=180)
    ap.add_argument("--max-output-tokens", type=int, default=8192)
    args = ap.parse_args()

    if not LOC_PATH.exists():
        print(f"Missing {LOC_PATH}", file=sys.stderr)
        return 1

    doc = json.loads(LOC_PATH.read_text(encoding="utf-8"))
    places = doc.get("places") or []

    # Fast path: apply an existing audit patch without re-running Gemini.
    if args.apply_from_patch:
        if not args.patch_path.exists():
            print(f"Missing patch file: {args.patch_path}", file=sys.stderr)
            return 1
        patch = json.loads(args.patch_path.read_text(encoding="utf-8"))
        rows = [PlaceRow(dict(p)) for p in places]
        patch_merges = patch.get("merges") or {}
        delete_set = set(patch.get("deletes") or [])

        # Collapse loser ids into winners.
        rows = apply_merge_map(rows, patch_merges)

        final_by = {r.id: r.raw for r in rows}
        # Apply canonical + type fixes.
        for pid, name in (patch.get("canonical_names") or {}).items():
            if pid in final_by and isinstance(name, str) and name.strip():
                old = final_by[pid].get("canonical_name")
                if old and old != name.strip():
                    final_by[pid].setdefault("aliases", [])
                    if old not in final_by[pid]["aliases"]:
                        final_by[pid]["aliases"].append(old)
                final_by[pid]["canonical_name"] = name.strip()
        for pid, t in (patch.get("type_fixes") or {}).items():
            if pid in final_by and t in ALLOWED_TYPES:
                final_by[pid]["type"] = t

        # Apply deletes.
        for d in delete_set:
            final_by.pop(d, None)

        # Normalize aliases list.
        for w in final_by.values():
            c = w.get("canonical_name")
            w["aliases"] = sorted({x for x in (w.get("aliases") or []) if x and x != c}, key=str.lower)

        # Keep same ordering logic as dry-run/apply.
        places_out = sorted(final_by.values(), key=lambda p: (-int(p.get("mention_count") or 0), p["id"]))

        collapsed = collapse_merges(patch_merges)
        st = rewrite_transcript_place_ids(collapsed, delete_set)

        # Update registry metadata and write.
        doc["places"] = places_out
        meta = doc.setdefault("_meta", {})
        by_conf = {"high": 0, "medium": 0, "low": 0}
        for p in places_out:
            by_conf[p.get("confidence", "low")] = by_conf.get(p.get("confidence", "low"), 0) + 1
        meta["registry_version"] = "v0.3-curated"
        meta["total_count"] = len(places_out)
        meta["by_confidence"] = by_conf
        meta["source_passes"] = (meta.get("source_passes") or []) + ["curate_locations_registry.py:apply-from-patch"]
        meta["last_updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        atomic_write_json(LOC_PATH, doc)

        print(
            f"Applied patch -> {LOC_PATH}\n"
            f"Transcripts: examined={st['examined']} rewritten={st['rewritten']} slot_changes~={st['changes']}"
        )
        return 0

    # Default path: compute patch (deterministic + optional Gemini), write it,
    # and optionally apply it.
    rows = [PlaceRow(dict(p)) for p in places]
    n0 = len(rows)

    patch: dict[str, Any] = {
        "_review_meta": {
            "reviewer": "deterministic+similarity" + ("+gemini" if args.use_llm and not args.deterministic_only else ""),
            "reviewed_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
            "similarity_window": args.similarity_window,
            "similarity_ratio": args.similarity_ratio,
        },
        "merges": {},
        "canonical_names": {},
        "type_fixes": {},
        "deletes": [],
    }

    # 1) deterministic norm merges
    # 0) deterministic city/town comma-state suffix merges
    m0 = deterministic_comma_parent_city_merges(rows)
    patch["merges"].update(m0)
    rows = apply_merge_map(rows, m0)
    if m0:
        print(f"[0] comma-state city/town merges: {len(m0)} losers removed -> {len(rows)} places")
    else:
        print("[0] comma-state city/town merges: 0 losers removed")

    # 1) deterministic norm merges
    m1 = deterministic_norm_merges(rows)
    patch["merges"].update(m1)
    rows = apply_merge_map(rows, m1)
    print(f"[1] norm merges: {len(m1)} losers removed -> {len(rows)} places")

    # 2) similarity clusters
    edges = similarity_edges(rows, window=args.similarity_window, min_ratio=args.similarity_ratio)
    ids = [r.id for r in rows]
    clusters = clusters_from_edges(ids, edges)
    print(f"[2] similarity edges: {len(edges)} -> multi-id clusters: {len(clusters)}")

    m2: dict[str, str] = {}
    type_fixes: dict[str, str] = {}
    canon_over: dict[str, str] = {}
    drops: list[str] = []

    if args.use_llm and not args.deterministic_only and clusters:
        by_id = row_by_id(rows)
        try:
            m2, type_fixes, canon_over, drops = gemini_resolve_clusters(
                clusters,
                by_id,
                model=args.model,
                timeout_sec=args.timeout_sec,
                max_out=args.max_output_tokens,
                batch_size=args.llm_batch_size,
            )
        except RuntimeError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            print("Tip: run with --deterministic-only or set GEMINI_API_KEY.", file=sys.stderr)
            return 1
        patch["merges"].update(m2)
        patch["type_fixes"].update(type_fixes)
        patch["canonical_names"].update(canon_over)
        patch["deletes"] = sorted(set(drops))
        print(f"[3] llm: merge edges {len(m2)}, type fixes {len(type_fixes)}, drops {len(set(drops))}")
        rows = apply_merge_map(rows, m2)

    # Apply canonical + type fixes on final rows
    final_by = {r.id: r.raw for r in rows}
    for pid, name in patch["canonical_names"].items():
        if pid in final_by and isinstance(name, str) and name.strip():
            old = final_by[pid].get("canonical_name")
            if old and old != name.strip():
                final_by[pid].setdefault("aliases", [])
                if old not in final_by[pid]["aliases"]:
                    final_by[pid]["aliases"].append(old)
            final_by[pid]["canonical_name"] = name.strip()
    for pid, t in patch["type_fixes"].items():
        if pid in final_by and t in ALLOWED_TYPES:
            final_by[pid]["type"] = t

    delete_set = set(patch["deletes"])
    for d in delete_set:
        final_by.pop(d, None)

    for w in final_by.values():
        c = w.get("canonical_name")
        w["aliases"] = sorted({x for x in (w.get("aliases") or []) if x and x != c}, key=str.lower)

    places_out = sorted(final_by.values(), key=lambda p: (-int(p.get("mention_count") or 0), p["id"]))

    collapsed = collapse_merges(patch["merges"])

    patch["_stats"] = {
        "places_before": n0,
        "places_after": len(places_out),
        "merge_pairs": len(collapsed),
    }
    atomic_write_json(PATCH_PATH, patch)
    print(f"Wrote audit patch -> {PATCH_PATH.relative_to(ROOT)}")

    if not args.apply:
        print("Dry-run only. Re-run with --apply to write locations.json + rewrite transcripts.")
        return 0

    by_conf = {"high": 0, "medium": 0, "low": 0}
    for p in places_out:
        by_conf[p.get("confidence", "low")] = by_conf.get(p.get("confidence", "low"), 0) + 1

    doc["places"] = places_out
    meta = doc.setdefault("_meta", {})
    meta["registry_version"] = "v0.3-curated"
    meta["total_count"] = len(places_out)
    meta["by_confidence"] = by_conf
    meta["source_passes"] = (meta.get("source_passes") or []) + [
        "curate_locations_registry.py",
    ]
    doc["last_updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")

    atomic_write_json(LOC_PATH, doc)
    print(f"Wrote {len(places_out)} places -> {LOC_PATH.relative_to(ROOT)}")

    st = rewrite_transcript_place_ids(collapsed, delete_set)
    print(
        f"Transcripts: examined={st['examined']} rewritten={st['rewritten']} slot_changes~={st['changes']}",
    )
    print("DONE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
