"""
Seed organizations/orgs.json from existing string mentions in:
- documents/press/articles/*.json -> analysis.named_entities.organizations[]
- documents/press/social_posts/*.json -> analysis.named_entities.organizations[]
- documents/press/comments/*.json -> (no orgs field; skip)

Output: organizations/orgs.json with same shape conventions as people/people.json.

Slug: o_<lowercase_alnum_underscores>. Aliases collect raw string variants.
"""
from __future__ import annotations

import json
import re
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ARTICLES = ROOT / "documents/press/articles"
SOCIAL = ROOT / "documents/press/social_posts"
OUT_DIR = ROOT / "organizations"
OUT_DIR.mkdir(exist_ok=True)
OUT = OUT_DIR / "orgs.json"


# Manual canonicalization rules — known org variants in your corpus.
# Add to this map as you find more in dryrun output.
CANON_RULES = {
    # NPS
    "national park service": "o_national_park_service",
    "nps": "o_national_park_service",
    "u.s. national park service": "o_national_park_service",
    "us national park service": "o_national_park_service",
    "the national park service": "o_national_park_service",
    # Sponsor / brand
    "the north face": "o_the_north_face",
    "north face": "o_the_north_face",
    "tnf": "o_the_north_face",
    # PLF
    "pacific legal foundation": "o_pacific_legal_foundation",
    "plf": "o_pacific_legal_foundation",
    # Court / DOJ
    "u.s. attorney's office": "o_us_attorney_office_dwy",
    "us attorney's office": "o_us_attorney_office_dwy",
    "department of justice": "o_doj",
    "doj": "o_doj",
    "us doj": "o_doj",
    # FKT.com
    "fkt.com": "o_fkt_com",
    "fastestknowntime.com": "o_fkt_com",
    # Park / locations that get confused with orgs
    "grand teton national park": "o_grand_teton_national_park",
    "yellowstone national park": "o_yellowstone_national_park",
    # Media
    "buckrail": "o_buckrail",
    "jackson hole news&guide": "o_jhnewsandguide",
    "jackson hole news & guide": "o_jhnewsandguide",
    "denver gazette": "o_denver_gazette",
    "outside magazine": "o_outside",
    "outside": "o_outside",
    "deseret news": "o_deseret_news",
    # Athletic / advocacy
    "exum mountain guides": "o_exum",
    "exum": "o_exum",
    # Court entity references
    "us district court": "o_us_district_court_dwy",
    "us district court for the district of wyoming": "o_us_district_court_dwy",
    "district of wyoming": "o_us_district_court_dwy",
    # Government
    "white house": "o_white_house",
    "trump administration": "o_trump_administration",
    "biden administration": "o_biden_administration",
}

# Org type heuristics — regex-on-canonical-name → type
TYPE_RULES = [
    (r"national park service|national park\b", "gov_agency"),
    (r"department|doj|attorney's office|district court|congress|senate|administration|white house", "gov_agency"),
    (r"pacific legal foundation|legal foundation", "nonprofit_legal"),
    (r"news ?& ?guide|gazette|magazine|news|times|post|tribune|daily|press|buckrail|outside\b|jackson hole|new york|deseret|fox|cbs|nbc|abc|cnn|ap\b|reuters|axios|associated press", "media"),
    (r"the north face|tnf|gore|nike|adidas|patagonia|sponsor", "sponsor_brand"),
    (r"fkt\.com|fastestknowntime", "platform"),
    (r"national park\b|park\b", "geographic_entity"),
    (r"\bguides?\b|outfit", "service_co"),
]


def slugify(name: str) -> str:
    n = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    n = n.lower().strip()
    n = re.sub(r"[^a-z0-9]+", "_", n).strip("_")
    n = re.sub(r"^the_", "", n)
    if not n:
        return "o_unknown"
    return f"o_{n}"


def classify_type(canonical: str) -> str:
    s = canonical.lower()
    for pat, t in TYPE_RULES:
        if re.search(pat, s):
            return t
    return "unknown"


def canonical_id(raw: str) -> str:
    key = raw.strip().lower()
    return CANON_RULES.get(key) or slugify(raw)


def main() -> None:
    seen: dict[str, dict] = {}  # id -> record
    counts: Counter = Counter()
    sources_by_id: dict[str, set[str]] = defaultdict(set)
    aliases_by_id: dict[str, set[str]] = defaultdict(set)
    canonical_by_id: dict[str, str] = {}

    def ingest(name: str, source_kind: str) -> None:
        if not name or not name.strip():
            return
        oid = canonical_id(name)
        counts[oid] += 1
        sources_by_id[oid].add(source_kind)
        aliases_by_id[oid].add(name.strip())
        # First-non-trivial wins for canonical_name (longer = often more formal)
        if oid not in canonical_by_id or len(name) > len(canonical_by_id[oid]):
            # Prefer Title Case versions
            canonical_by_id[oid] = name.strip()

    for p in ARTICLES.glob("*.json"):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        an = d.get("analysis")
        if isinstance(an, dict):
            ne = an.get("named_entities") or {}
            for o in ne.get("organizations") or []:
                ingest(o, "press_article")

    if SOCIAL.exists():
        for p in SOCIAL.glob("*.json"):
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                continue
            an = d.get("analysis")
            if isinstance(an, dict):
                ne = an.get("named_entities") or {}
                for o in ne.get("organizations") or []:
                    ingest(o, "press_social")

    # Build registry
    orgs = []
    for oid, raw_canonical in canonical_by_id.items():
        # Apply manual canonical override if available (search CANON_RULES values)
        canonical_name = raw_canonical
        # Use prettiest alias as canonical (most title-cased)
        aliases = sorted(aliases_by_id[oid] - {raw_canonical})
        # Pick best canonical name from aliases
        candidates = [raw_canonical] + list(aliases_by_id[oid])
        # Prefer one with multiple words and no all-lowercase
        candidates.sort(key=lambda s: (-sum(1 for c in s if c.isupper()), len(s)))
        if candidates:
            canonical_name = candidates[0]
        org = {
            "id": oid,
            "canonical_name": canonical_name,
            "aliases": [a for a in sorted(set(aliases_by_id[oid])) if a != canonical_name],
            "type": classify_type(canonical_name),
            "sources": sorted(sources_by_id[oid]),
            "mention_count": counts[oid],
            "relationships": [],
            "notes": "",
            "confidence": "medium" if counts[oid] >= 3 else "low",
        }
        orgs.append(org)

    orgs.sort(key=lambda o: (-o["mention_count"], o["id"]))

    out = {
        "_meta": {
            "registry_version": "v1.0",
            "source_passes": ["seed_from_press_named_entities"],
            "total_count": len(orgs),
            "by_confidence": {
                "medium": sum(1 for o in orgs if o["confidence"] == "medium"),
                "low": sum(1 for o in orgs if o["confidence"] == "low"),
            },
        },
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "organizations": orgs,
    }
    OUT.write_text(json.dumps(out, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Wrote {OUT}: {len(orgs)} orgs")
    print(f"Top 20:")
    for o in orgs[:20]:
        print(f"  {o['mention_count']:>4}  {o['id']:<40}  {o['canonical_name']}")


if __name__ == "__main__":
    main()
