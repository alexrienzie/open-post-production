"""
Compile places/places.json from:
- organizations/orgs.json — geographic_entity orgs migrated to pl_* (see places/CATALOG_DESIGN.md)
- assets/transcripts/*.transcript.json — place_ids[], _unmatched_places[]
- documents/press/articles|comments|social_posts — analysis.named_entities.locations[]

Idempotent: re-running re-aggregates from source corpora. Does not read lat/lon (separate workstream).

Usage:
  python _scripts/registries/build_locations_registry.py
"""
from __future__ import annotations

import json
import os
import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ORGS_JSON = ROOT / "organizations/orgs.json"
OUT = ROOT / "places/places.json"
TRANSCRIPTS = ROOT / "assets/transcripts"
ARTICLES = ROOT / "documents/press/articles"
COMMENTS = ROOT / "documents/press/comments"
SOCIAL_POSTS = ROOT / "documents/press/social_posts"

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

# Raw string (norm) -> fixed pl_* id for known collisions / disambiguation.
CANON_IDS = {
    "grand teton national park": "pl_grand_teton_national_park",
    "gtnp": "pl_grand_teton_national_park",
    "grand teton np": "pl_grand_teton_national_park",
    "jackson hole": "pl_jackson_hole",
    "jackson wy": "pl_jackson",
    "jackson wyoming": "pl_jackson",
    "tetons": "pl_tetons",
    "the tetons": "pl_tetons",
    "teton range": "pl_teton_range",
    "washington d c": "pl_washington_dc",
    "washington dc": "pl_washington_dc",
    "d c": "pl_washington_dc",
    "salt lake": "pl_salt_lake_city",
    "yellowstone national park": "pl_yellowstone_national_park",
}


def atomic_write_json(path: Path, data: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def norm_key(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    s = s.lower().strip()
    s = re.sub(r"\s+", " ", s)
    return s


def slugify(name: str) -> str:
    n = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    n = n.lower().strip()
    n = re.sub(r"[^a-z0-9]+", "_", n).strip("_")
    n = re.sub(r"^the_", "", n)
    if not n:
        return "pl_unknown"
    return f"pl_{n}"


def place_id_for_name(raw: str) -> str:
    nk = norm_key(raw)
    if nk in CANON_IDS:
        return CANON_IDS[nk]
    return slugify(raw)


def slug_to_title(pid: str) -> str:
    if not pid.startswith("pl_"):
        return pid
    parts = pid[3:].split("_")
    small = {"of", "and", "the", "de", "la", "du", "le", "les", "d", "l"}
    words = []
    for i, w in enumerate(parts):
        if not w:
            continue
        if i > 0 and w in small:
            words.append(w)
        else:
            words.append(w[:1].upper() + w[1:] if len(w) > 1 else w.upper())
    return " ".join(words)


def infer_type_from_label(label: str) -> str:
    s = label.strip().lower()
    if s.endswith("national park") or s.endswith("national forest") or s.endswith("national wildlife refuge"):
        return "protected_area"
    if s.endswith("county"):
        return "county"
    if s.endswith("airport") or s in {"lax", "jfk", "sfo", "slc", "den"}:
        return "airport"
    if s.endswith("courthouse") or "refuge" in s:
        return "infrastructure"
    return "unknown"


def rank_confidence(c: str) -> int:
    return {"high": 3, "medium": 2, "low": 1}.get(c, 0)


@dataclass
class Agg:
    id: str
    canonical_name: str = ""
    canon_priority: int = -1
    type: str = "unknown"
    type_priority: int = -1
    aliases: set[str] = field(default_factory=set)
    tags: set[str] = field(default_factory=set)
    sources: set[str] = field(default_factory=set)
    mention_count: int = 0
    relationships: list = field(default_factory=list)
    notes_parts: list[str] = field(default_factory=list)
    confidence: str = "low"
    org_mention_seed: int = 0

    def consider_canonical(self, name: str, priority: int) -> None:
        name = name.strip()
        if not name:
            return
        if priority > self.canon_priority or (
            priority == self.canon_priority and len(name) > len(self.canonical_name)
        ):
            if self.canonical_name and self.canonical_name != name:
                self.aliases.add(self.canonical_name)
            self.canonical_name = name
            self.canon_priority = priority
        elif name != self.canonical_name:
            self.aliases.add(name)

    def consider_type(self, t: str, priority: int) -> None:
        t = (t or "unknown").strip()
        if t not in ALLOWED_TYPES:
            t = "unknown"
        if t == "unknown" and priority < self.type_priority:
            return
        if priority > self.type_priority or (priority == self.type_priority and t != "unknown"):
            self.type = t
            self.type_priority = priority

    def add_note(self, s: str) -> None:
        s = s.strip()
        if s and s not in self.notes_parts:
            self.notes_parts.append(s)
            if len(self.notes_parts) > 12:
                self.notes_parts.pop(0)

    def bump_confidence(self, c: str) -> None:
        if rank_confidence(c) > rank_confidence(self.confidence):
            self.confidence = c


def main() -> int:
    aggs: dict[str, Agg] = {}

    def get(pid: str) -> Agg:
        if pid not in aggs:
            aggs[pid] = Agg(id=pid)
        return aggs[pid]

    # 1) Geographic entities from orgs → pl_* seeds
    if ORGS_JSON.exists():
        orgs_doc = json.loads(ORGS_JSON.read_text(encoding="utf-8"))
        for o in orgs_doc.get("organizations") or []:
            if o.get("type") != "geographic_entity":
                continue
            oid = o.get("id") or ""
            if not oid.startswith("o_"):
                continue
            pid = "pl_" + oid[2:]
            g = get(pid)
            cname = o.get("canonical_name") or slug_to_title(pid)
            g.consider_canonical(cname, 100)
            g.consider_type(infer_type_from_label(cname), 40)
            if o.get("mention_count") is not None:
                g.org_mention_seed = int(o["mention_count"])
            # Omit org aliases here: geographic_entity rows sometimes conflate a park with
            # nearby peaks (e.g. "Grand Teton" vs Grand Teton NP). Aliases accrue from
            # transcripts/press with clearer context.
            g.sources.add("organizations/orgs.json:geographic_entity")
            g.bump_confidence("high")
            if o.get("notes"):
                g.add_note(str(o["notes"])[:500])

    # 2) Transcripts
    for tpath in sorted(TRANSCRIPTS.glob("*.transcript.json")):
        try:
            tr = json.loads(tpath.read_text(encoding="utf-8"))
        except Exception:
            continue
        aid = tr.get("asset_id") or tpath.stem.replace(".transcript", "")

        for pid in tr.get("place_ids") or []:
            if not isinstance(pid, str) or not pid.startswith("pl_"):
                continue
            g = get(pid)
            g.mention_count += 1
            g.sources.add("assets/transcripts:place_ids")
            g.consider_canonical(slug_to_title(pid), 10)
            g.bump_confidence("medium")

        for raw_name in tr.get("_unmatched_places") or []:
            if not isinstance(raw_name, str) or not raw_name.strip():
                continue
            pid = place_id_for_name(raw_name)
            g = get(pid)
            g.mention_count += 1
            g.sources.add("assets/transcripts:_unmatched_places")
            g.consider_canonical(raw_name.strip(), 50)
            g.bump_confidence("medium")

    # 3) Press — articles, comments, social_posts
    def ingest_press_locations(obj: dict, record_id: str, source_label: str) -> None:
        analysis = obj.get("analysis")
        if not isinstance(analysis, dict):
            return
        ne = analysis.get("named_entities")
        if not isinstance(ne, dict):
            return
        locs = ne.get("locations")
        if not isinstance(locs, list):
            return
        seen_in_doc: set[str] = set()
        for loc in locs:
            if not isinstance(loc, str) or not loc.strip():
                continue
            nk = norm_key(loc)
            if nk in seen_in_doc:
                continue
            seen_in_doc.add(nk)
            pid = place_id_for_name(loc)
            g = get(pid)
            g.mention_count += 1
            g.sources.add(source_label)
            g.consider_canonical(loc.strip(), 30)
            inferred = infer_type_from_label(loc)
            g.consider_type(inferred, 35 if inferred != "unknown" else 10)
            g.add_note(f"[{record_id}] press NE")
            g.bump_confidence("medium" if inferred != "unknown" else "low")

    for ap in sorted(ARTICLES.glob("*.json")):
        try:
            ingest_press_locations(json.loads(ap.read_text(encoding="utf-8")), ap.stem, "documents/press/articles")
        except Exception:
            continue

    for cp in sorted(COMMENTS.glob("*.json")):
        try:
            ingest_press_locations(json.loads(cp.read_text(encoding="utf-8")), cp.stem, "documents/press/comments")
        except Exception:
            continue

    for sp in sorted(SOCIAL_POSTS.glob("*.json")):
        try:
            d = json.loads(sp.read_text(encoding="utf-8"))
            rid = d.get("post_id") or sp.stem
            ingest_press_locations(d, rid, "documents/press/social_posts")
        except Exception:
            continue

    # Post-process: aliases should not duplicate canonical_name; trim empty
    places_out: list[dict] = []
    now = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")

    for pid in sorted(aggs.keys()):
        g = aggs[pid]
        if not g.canonical_name:
            g.canonical_name = slug_to_title(pid)
        g.aliases.discard(g.canonical_name)
        aliases_sorted = sorted(g.aliases, key=lambda s: s.lower())
        notes = " | ".join(g.notes_parts) if g.notes_parts else ""
        if len(notes) > 4000:
            notes = notes[:3997] + "..."
        if g.org_mention_seed and "org registry mention_count seed" not in notes.lower():
            notes = (notes + " | " if notes else "") + f"Org registry geographic_entity mention_count seed: {g.org_mention_seed}."

        places_out.append(
            {
                "id": g.id,
                "canonical_name": g.canonical_name,
                "aliases": aliases_sorted,
                "type": g.type,
                "tags": sorted(g.tags),
                "sources": sorted(g.sources),
                "mention_count": g.mention_count,
                "relationships": g.relationships,
                "notes": notes,
                "confidence": g.confidence,
            }
        )

    places_out.sort(key=lambda p: (-p["mention_count"], p["id"]))

    # Preserve hierarchy from the previous registry when re-ingesting (parents survive rebuilds).
    prev_hierarchy: dict[str, dict] = {}
    if OUT.exists():
        try:
            prev_doc = json.loads(OUT.read_text(encoding="utf-8"))
            for pp in prev_doc.get("places") or []:
                pid = pp.get("id")
                if not pid:
                    continue
                blob: dict = {}
                if pp.get("parent_id"):
                    blob["parent_id"] = pp["parent_id"]
                rel = pp.get("relationships")
                if rel:
                    blob["relationships"] = rel
                if blob:
                    prev_hierarchy[pid] = blob
        except Exception:
            pass
    for p in places_out:
        blob = prev_hierarchy.get(p["id"])
        if blob:
            p.update(blob)

    by_conf = {"high": 0, "medium": 0, "low": 0}
    for p in places_out:
        by_conf[p["confidence"]] = by_conf.get(p["confidence"], 0) + 1

    doc = {
        "_meta": {
            "registry_version": "v0.2",
            "source_passes": [
                "build_locations_registry.py:orgs_geographic_entity",
                "build_locations_registry.py:transcripts",
                "build_locations_registry.py:press_articles_comments_social",
            ],
            "total_count": len(places_out),
            "by_confidence": by_conf,
        },
        "schema_version": 1,
        "generated_at": now,
        "last_updated_at": now,
        "name_resolution_rules": [],
        "places": places_out,
    }

    OUT.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(OUT, doc)
    print(f"Wrote {len(places_out)} places to {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
