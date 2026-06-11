"""
Phase 0 of the transcript cleanup pass — cross-transcript candidate clustering.

Sweeps every transcript's `_unmatched_people` and `_unmatched_orgs`, runs
phonetic + edit-distance matching against the people/orgs registries, and
emits cluster artifacts. No LLM calls — pure DSP, runs in seconds against
3,878 records.

Outputs:
    _runs/cleanup_candidates_<ts>/
    ├── candidate_clusters.json    — phonetic clusters with proposed registry targets
    ├── unclustered.json           — unmatched candidates with no plausible registry hit
    └── stats.json                 — counts + top mishearings

Phonetic algorithm: Double Metaphone + normalized Levenshtein distance. Pure
stdlib + a tiny in-file metaphone implementation; no external packages.

Usage (from PowerShell or any local terminal):

    python _scripts/transcripts/find_correction_candidates.py
    python _scripts/transcripts/find_correction_candidates.py --max-distance 0.30
    python _scripts/transcripts/find_correction_candidates.py --min-occurrences 2
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TRANSCRIPTS = ROOT / "assets/transcripts"
RUNS_DIR = ROOT / "_runs"
PEOPLE_PATH = ROOT / "people/people.json"
ORGS_PATH = ROOT / "organizations/orgs.json"


# ----------------------------------------------------------------------
# Tiny Metaphone-like phonetic encoder.
#
# This is NOT full Double Metaphone — it's a compact rule set that's good
# enough for English proper-noun matching of the kind Whisper produces. For
# English film transcripts: catches common ASR mishearings like Sinceri↔Sunseri,
# Damon↔Damien, Schiff↔Shift, etc. Swap in the `metaphone` PyPI package for
# stricter results if needed; this avoids the dependency.
# ----------------------------------------------------------------------
_VOWELS = set("AEIOU")


def _normalize(s: str) -> str:
    s = s.upper()
    s = re.sub(r"[^A-Z\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def metaphone(word: str) -> str:
    """Compact metaphone-style phonetic key for a single word."""
    w = _normalize(word).replace(" ", "")
    if not w:
        return ""

    # Early-letter substitutions
    sub_starts = (
        ("AE", "E"), ("GN", "N"), ("KN", "N"), ("PN", "N"),
        ("WR", "R"), ("PS", "S"), ("WH", "W"),
    )
    for old, new in sub_starts:
        if w.startswith(old):
            w = new + w[len(old):]
            break
    if w.startswith("X"):
        w = "S" + w[1:]

    # Mid-word substitutions
    w = w.replace("PH", "F").replace("GH", "")
    w = re.sub(r"DGE|DGI|DGY", "J", w)
    w = w.replace("CK", "K").replace("CC", "K").replace("CH", "X")
    w = re.sub(r"C(?=[IEY])", "S", w)
    w = w.replace("C", "K")
    w = re.sub(r"SH|SCH|SCI|SCE|SCY", "X", w)
    w = w.replace("TH", "0")  # voiceless dental
    w = re.sub(r"G(?=[IEY])", "J", w)
    w = w.replace("Q", "K")
    w = w.replace("Z", "S")
    w = w.replace("V", "F").replace("W", "").replace("Y", "")
    w = w.replace("MB", "M")

    # Drop double letters
    w = re.sub(r"(.)\1+", r"\1", w)

    # Drop vowels except leading
    if not w:
        return ""
    out = w[0] + "".join(c for c in w[1:] if c not in _VOWELS)
    return out


def phonetic_key(name: str) -> str:
    """Phonetic key for a multi-word name — concatenated metaphones."""
    parts = _normalize(name).split()
    return "".join(metaphone(p) for p in parts)


def levenshtein(a: str, b: str) -> int:
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i] + [0] * len(b)
        for j, cb in enumerate(b, 1):
            cost = 0 if ca == cb else 1
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
        prev = cur
    return prev[-1]


def normalized_distance(a: str, b: str) -> float:
    if not a and not b:
        return 0.0
    return levenshtein(a, b) / max(len(a), len(b))


# ----------------------------------------------------------------------
# Registry loading
# ----------------------------------------------------------------------
def load_registry_targets() -> tuple[list[dict], list[dict]]:
    """Returns (people_targets, org_targets), each entry has id+canonical_name+aliases."""
    people = json.loads(PEOPLE_PATH.read_text(encoding="utf-8"))
    orgs = json.loads(ORGS_PATH.read_text(encoding="utf-8"))

    p_targets = []
    for p in people.get("people") or []:
        if not p.get("id") or not p.get("canonical_name"):
            continue
        names = [p["canonical_name"]] + (p.get("aliases") or [])
        p_targets.append({
            "id": p["id"],
            "canonical_name": p["canonical_name"],
            "names": names,
        })

    o_targets = []
    for o in orgs.get("organizations") or []:
        if not o.get("id") or not o.get("canonical_name"):
            continue
        names = [o["canonical_name"]] + (o.get("aliases") or [])
        o_targets.append({
            "id": o["id"],
            "canonical_name": o["canonical_name"],
            "names": names,
        })

    return p_targets, o_targets


# ----------------------------------------------------------------------
# Candidate extraction
# ----------------------------------------------------------------------
_LEADING_NAME_RE = re.compile(r"^([A-Z][A-Za-z'\.\-]+(?:\s+[A-Z][A-Za-z'\.\-]+){0,4})")


def extract_candidate_string(unmatched_entry) -> str:
    """Extract the leading proper-noun-looking phrase from an _unmatched_* entry.

    Accepts both shapes:
      - New-format object: {"name": "...", "context": "..."}
      - Legacy string: "Nick Daniel Sinceri (second-place finisher, ...)" or
                       "Damon Shift — context: ..."
    """
    if isinstance(unmatched_entry, dict):
        return (unmatched_entry.get("name") or "").strip()
    if not isinstance(unmatched_entry, str):
        return ""
    s = unmatched_entry.strip()
    m = _LEADING_NAME_RE.match(s)
    if m:
        return m.group(1)
    return re.split(r"[\(\—\-—:]", s, maxsplit=1)[0].strip()


def best_match(candidate: str, targets: list[dict],
               max_distance: float) -> tuple[dict, float] | None:
    """Find the best registry target for a candidate string.

    Strategy:
      1. Phonetic-key exact match → distance 0.
      2. Otherwise, normalized Levenshtein on phonetic keys; accept if ≤ max_distance.
    """
    cand_key = phonetic_key(candidate)
    if not cand_key:
        return None

    best: tuple[dict, float] | None = None
    for t in targets:
        for n in t["names"]:
            t_key = phonetic_key(n)
            if not t_key:
                continue
            if t_key == cand_key:
                return (t, 0.0)
            d = normalized_distance(cand_key, t_key)
            if d <= max_distance and (best is None or d < best[1]):
                best = (t, d)
    return best


# ----------------------------------------------------------------------
# Main sweep
# ----------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-distance", type=float, default=0.30,
                    help="Max normalized phonetic Levenshtein distance to accept (default 0.30)")
    ap.add_argument("--min-occurrences", type=int, default=1,
                    help="Min cluster occurrence count to include in output (default 1)")
    args = ap.parse_args()

    if not TRANSCRIPTS.exists():
        print(f"ERROR: transcripts dir not found at {TRANSCRIPTS}", file=sys.stderr)
        return 1

    p_targets, o_targets = load_registry_targets()
    print(f"[load] {len(p_targets)} people targets, {len(o_targets)} org targets")

    files = sorted(TRANSCRIPTS.glob("*.json"))
    print(f"[scan] {len(files)} transcript files")

    # cluster_key: (kind, target_id) -> {"target": dict, "mishearings": Counter, "asset_ids": set}
    clusters: dict[tuple[str, str], dict] = {}
    unclustered: list[dict] = []

    for p in files:
        try:
            rec = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        aid = rec.get("asset_id") or p.stem.replace(".transcript", "")

        for kind, key, targets in (
            ("people", "_unmatched_people", p_targets),
            ("orgs", "_unmatched_orgs", o_targets),
        ):
            for entry in (rec.get(key) or []):
                cand = extract_candidate_string(entry)
                if not cand:
                    continue
                # Preserve original entry text for the cluster output regardless of shape
                raw_for_log = entry if isinstance(entry, str) else json.dumps(entry, ensure_ascii=False)
                m = best_match(cand, targets, args.max_distance)
                if m is None:
                    unclustered.append({
                        "kind": kind,
                        "asset_id": aid,
                        "raw_unmatched": raw_for_log,
                        "candidate_string": cand,
                    })
                    continue
                target, dist = m
                cluster_key = (kind, target["id"])
                cl = clusters.setdefault(cluster_key, {
                    "kind": kind,
                    "target_slug": target["id"],
                    "target_canonical": target["canonical_name"],
                    "mishearings": defaultdict(int),
                    "asset_ids": set(),
                    "min_distance": 1.0,
                })
                cl["mishearings"][cand] += 1
                cl["asset_ids"].add(aid)
                cl["min_distance"] = min(cl["min_distance"], dist)

    # Materialize
    cluster_list = []
    for (kind, target_id), cl in clusters.items():
        total = sum(cl["mishearings"].values())
        if total < args.min_occurrences:
            continue
        cluster_list.append({
            "kind": cl["kind"],
            "target_slug": cl["target_slug"],
            "target_canonical": cl["target_canonical"],
            "occurrences": total,
            "asset_count": len(cl["asset_ids"]),
            "min_phonetic_distance": round(cl["min_distance"], 4),
            "mishearings": [
                {"text": k, "count": v}
                for k, v in sorted(cl["mishearings"].items(), key=lambda kv: -kv[1])
            ],
            "asset_ids": sorted(cl["asset_ids"]),
        })
    cluster_list.sort(key=lambda c: -c["occurrences"])

    # Output dir
    ts = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d_%H%M")
    out_dir = RUNS_DIR / f"cleanup_candidates_{ts}"
    out_dir.mkdir(parents=True, exist_ok=True)

    (out_dir / "candidate_clusters.json").write_text(
        json.dumps(cluster_list, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    (out_dir / "unclustered.json").write_text(
        json.dumps(unclustered, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    stats = {
        "transcripts_scanned": len(files),
        "clusters_found": len(cluster_list),
        "total_clustered_occurrences": sum(c["occurrences"] for c in cluster_list),
        "unclustered_count": len(unclustered),
        "max_distance_used": args.max_distance,
        "top_clusters_by_occurrence": [
            {"slug": c["target_slug"], "canonical": c["target_canonical"], "occurrences": c["occurrences"]}
            for c in cluster_list[:20]
        ],
    }
    (out_dir / "stats.json").write_text(json.dumps(stats, indent=2), encoding="utf-8")

    print(f"\n=== CLUSTERING COMPLETE ===")
    print(f"  clusters:        {len(cluster_list)}")
    print(f"  total occurrences: {stats['total_clustered_occurrences']}")
    print(f"  unclustered:     {len(unclustered)}")
    print(f"  output:          {out_dir.relative_to(ROOT)}")
    if cluster_list[:5]:
        print("\n  top 5 clusters:")
        for c in cluster_list[:5]:
            top = c["mishearings"][0] if c["mishearings"] else {}
            print(f"    {c['target_slug']:40s} {c['occurrences']:>4}× "
                  f"(top: {top.get('text','')!r:30s} ×{top.get('count',0)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
