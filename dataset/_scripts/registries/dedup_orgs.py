"""
Comprehensive orgs.json cleanup.

Three operations:
1. Merge confirmed duplicates per the curated MERGE_MAP. Mention_counts sum,
   aliases union, sources union. The "winner" id stays; losers' aliases roll
   into the winner's aliases list (preserving original spellings).

2. Add curated aliases to high-mention orgs that lack them.

3. Fix type misclassification on the high-signal orgs.

Then walks all FK consumers (articles, comments, social_posts, transcripts, timeline JSONL) and
rewrites any org_id matching a merged-loser id to the canonical winner id.
Atomic writes everywhere.

Idempotent: if MERGE_MAP says A → B and we re-run, A is already gone, no-op.
"""
from __future__ import annotations

import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ORGS_PATH = ROOT / "organizations" / "orgs.json"


# Merge map: {loser_id: winner_id}. Loser is removed; mention_count, aliases,
# sources roll into winner. The losers' canonical_names get added as aliases.
MERGE_MAP: dict[str, str] = {
    # Department of Justice
    "o_u_s_department_of_justice": "o_doj",
    "o_justice_department": "o_doj",
    # Department of the Interior
    "o_department_of_interior": "o_department_of_the_interior",
    "o_u_s_department_of_the_interior": "o_department_of_the_interior",
    "o_interior_department": "o_department_of_the_interior",
    "o_u_s_interior_department": "o_department_of_the_interior",
    # FastestKnownTime
    "o_fastestknowntime": "o_fkt_com",
    "o_fastest_known_time": "o_fkt_com",
    # Jackson Hole News & Guide
    "o_jackson_hole_news_and_guide": "o_jhnewsandguide",
    # House Judiciary Committee
    "o_u_s_house_judiciary_committee": "o_house_judiciary_committee",
    # US Attorney's Office DWY (5 variants → canonical)
    "o_u_s_attorney_s_office_for_the_district_of_wyoming": "o_us_attorney_office_dwy",
    "o_u_s_attorney_s_office_district_of_wyoming": "o_us_attorney_office_dwy",
    "o_u_s_attorney_s_office_for_wyoming": "o_us_attorney_office_dwy",
    "o_u_s_attorney_s_office_wyoming": "o_us_attorney_office_dwy",
    "o_u_s_district_attorney_s_office": "o_us_attorney_office_dwy",
    # Reason Magazine
    "o_reason": "o_reason_magazine",
    # Supreme Court (US)
    "o_supreme_court": "o_u_s_supreme_court",
    # Teton Gravity Research
    "o_teton_gravity": "o_teton_gravity_research",
    # US District Court (Wyoming-specific only — keep generic separate)
    "o_u_s_district_court_for_wyoming": "o_u_s_district_court_for_the_district_of_wyoming",
    "o_wyoming_district_court": "o_u_s_district_court_for_the_district_of_wyoming",
    # Clifford P. Hansen Courthouse
    "o_clifford_p_hansen_federal_courthouse": "o_clifford_p_hansen_courthouse",
    # Exum
    "o_exum_guides": "o_exum",
    # Chicago Sun-Times
    "o_sun_times": "o_chicago_sun_times",
    # USA Track & Field
    "o_usa_track_field": "o_usa_track_and_field",
    # N.A. Nature Photographers Association
    "o_north_american_nature_photographers_association": "o_north_american_nature_photography_association",
    # The Steep Stuff Podcast
    "o_steep_stuff": "o_steep_stuff_podcast",
    # US House of Representatives (bare US House → full name)
    "o_u_s_house": "o_u_s_house_of_representatives",
    # USA Mountain Running Team variants
    "o_u_s_world_mountain_running_team": "o_usa_mountain_running_team",
    "o_us_world_mountain_running_team": "o_usa_mountain_running_team",
    "o_team_usa": "o_usa_mountain_running_team",
    # Jackson Hole Radio variants
    "o_jackson_hole_community_radio": "o_jackson_hole_radio",
}


# Curated aliases for high-mention orgs missing them.
# {org_id: [aliases_to_add]}
ADD_ALIASES: dict[str, list[str]] = {
    "o_national_park_service": ["NPS", "U.S. National Park Service", "National Park Service", "the Park Service", "Park Service"],
    "o_the_north_face": ["TNF", "North Face"],
    "o_pacific_legal_foundation": ["PLF"],
    "o_grand_teton_national_park": ["Grand Teton", "GTNP", "Grand Teton Park", "the park"],
    "o_doj": ["Justice Department", "U.S. Department of Justice", "U.S. DOJ", "the Department of Justice"],
    "o_fkt_com": ["FastestKnownTime", "FKT", "Fastest Known Time", "FKT.com", "fastestknowntime.com"],
    "o_foundation_for_individual_rights_and_expression": ["FIRE"],
    "o_jhnewsandguide": ["Jackson Hole News and Guide", "News & Guide", "JHNG"],
    "o_cato_institute": ["Cato"],
    "o_house_judiciary_committee": ["U.S. House Judiciary Committee", "House Judiciary", "Judiciary Committee"],
    "o_us_attorney_office_dwy": [
        "U.S. Attorney's Office for the District of Wyoming",
        "U.S. Attorney's Office District of Wyoming",
        "U.S. Attorney's Office for Wyoming",
        "U.S. Attorney's Office (Wyoming)",
        "U.S. District Attorney's Office",
        "USAO",
        "the U.S. Attorney's Office",
    ],
    "o_white_house": ["The White House"],
    "o_department_of_the_interior": [
        "Department of Interior",
        "U.S. Department of the Interior",
        "U.S. Department of Interior",
        "Interior Department",
        "U.S. Interior Department",
        "DOI",
    ],
    "o_yellowstone_national_park": ["Yellowstone", "YNP", "Yellowstone Park"],
    "o_us_district_court_dwy": ["U.S. District Court for the District of Wyoming", "District Court of Wyoming"],
    "o_u_s_supreme_court": ["Supreme Court", "SCOTUS", "the Supreme Court"],
    "o_u_s_senate": ["Senate", "the Senate"],
    "o_u_s_congress": ["Congress", "the Congress", "U.S. Congress"],
    "o_u_s_house_of_representatives": ["U.S. House", "House of Representatives", "the House"],
    "o_reason_magazine": ["Reason"],
    "o_outside": ["Outside Magazine", "outsideonline.com"],
    "o_jackson_hole_mountain_resort": ["JHMR", "Jackson Hole Resort"],
    "o_grand_targhee": ["Grand Targhee Resort"],
    "o_la_sportiva": ["Sportiva"],
    "o_exum": ["Exum Mountain Guides", "Exum Guides"],
    "o_teton_gravity_research": ["TGR", "Teton Gravity"],
    "o_usa_track_and_field": ["USATF", "USA Track & Field"],
    "o_buckrail": ["Buckrail.com"],
    "o_wyofile": ["WyoFile.com"],
    "o_gearjunkie": ["Gear Junkie", "GearJunkie.com"],
    "o_bridger_teton_national_forest": ["Bridger-Teton", "BTNF"],
    "o_jenny_lake_rangers": ["Jenny Lake Climbing Rangers", "Jenny Lake Ranger District"],
    "o_steep_stuff_podcast": ["Steep Stuff", "The Steep Stuff Podcast", "Steep Stuff Podcast"],
    "o_chicago_sun_times": ["Sun-Times", "Chicago Sun Times"],
    "o_north_american_nature_photography_association": ["NANPA", "North American Nature Photographers Association"],
    "o_clifford_p_hansen_courthouse": ["Clifford P. Hansen Federal Courthouse", "Hansen Courthouse"],
    "o_usa_mountain_running_team": ["U.S. World Mountain Running Team", "US World Mountain Running Team", "Team USA"],
    "o_jackson_hole_radio": ["Jackson Hole Community Radio", "JH Radio", "KHOL"],
}


# Fix type misclassification on high-signal orgs (only correcting from `unknown`).
TYPE_FIXES: dict[str, str] = {
    "o_national_park_service": "gov_agency",
    "o_the_north_face": "sponsor_brand",
    "o_pacific_legal_foundation": "nonprofit_legal",
    "o_grand_teton_national_park": "geographic_entity",
    "o_yellowstone_national_park": "geographic_entity",
    "o_doj": "gov_agency",
    "o_fkt_com": "platform",
    "o_foundation_for_individual_rights_and_expression": "nonprofit_legal",
    "o_jhnewsandguide": "media",
    "o_cato_institute": "nonprofit_advocacy",
    "o_house_judiciary_committee": "gov_agency",
    "o_us_attorney_office_dwy": "gov_agency",
    "o_white_house": "gov_agency",
    "o_department_of_the_interior": "gov_agency",
    "o_us_district_court_dwy": "gov_agency",
    "o_u_s_supreme_court": "gov_agency",
    "o_u_s_senate": "gov_agency",
    "o_u_s_congress": "gov_agency",
    "o_u_s_house_of_representatives": "gov_agency",
    "o_u_s_district_court": "gov_agency",
    "o_reason_magazine": "media",
    "o_outside": "media",
    "o_buckrail": "media",
    "o_wyofile": "media",
    "o_gearjunkie": "media",
    "o_jackson_hole_mountain_resort": "service_co",
    "o_grand_targhee": "service_co",
    "o_la_sportiva": "sponsor_brand",
    "o_exum": "service_co",
    "o_teton_gravity_research": "media",
    "o_fior_productions": "service_co",
    "o_bridger_teton_national_forest": "geographic_entity",
    "o_bridger_teton_avalanche_center": "gov_agency",
    "o_jenny_lake_rangers": "gov_agency",
    "o_jenny_lake": "geographic_entity",
    "o_clifford_p_hansen_courthouse": "geographic_entity",
    "o_steep_stuff_podcast": "media",
    "o_new_york_times": "media",
    "o_new_york_post": "media",
    "o_new_york_sun": "media",
    "o_chicago_sun_times": "media",
    "o_denver_gazette": "media",
    "o_deseret_news": "media",
    "o_bozeman_chronicle": "media",
    "o_axios": "media",
    "o_associated_press": "media",
    "o_reuters": "media",
    "o_artnews": "media",
    "o_blm": "gov_agency",
    "o_american_alpine_club": "nonprofit_advocacy",
    "o_american_avalanche_association": "nonprofit_advocacy",
    "o_avalanche_canada": "nonprofit_advocacy",
    "o_teton_county_search_and_rescue": "gov_agency",
    "o_inyo_county_search_and_rescue": "gov_agency",
    "o_teton_county": "geographic_entity",
    "o_usa_track_and_field": "nonprofit_advocacy",
    "o_usa_mountain_running_team": "nonprofit_advocacy",
    "o_national_press_photographers_association": "nonprofit_advocacy",
    "o_north_american_nature_photography_association": "nonprofit_advocacy",
    "o_jackson_hole_radio": "media",
    "o_jackson_hole_daily": "media",
    "o_trump_administration": "gov_agency",
    "o_biden_administration": "gov_agency",
    "o_u_s_department_of_agriculture": "gov_agency",
}


def atomic_write_json(path: Path, data) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def merge_orgs(orgs_doc: dict) -> tuple[dict, dict]:
    """Returns (new_orgs_doc, merge_count_dict)."""
    by_id = {o["id"]: o for o in orgs_doc["organizations"]}

    # Resolve transitive merges (in case a winner is itself a loser elsewhere)
    def resolve(oid: str) -> str:
        seen = set()
        while oid in MERGE_MAP and oid not in seen:
            seen.add(oid)
            oid = MERGE_MAP[oid]
        return oid

    merge_counts: dict[str, int] = defaultdict(int)
    losers_seen = set()

    for loser_id, winner_id in MERGE_MAP.items():
        winner_id = resolve(winner_id)
        if loser_id == winner_id or loser_id not in by_id:
            continue
        if winner_id not in by_id:
            print(f"  WARN: winner {winner_id} not in registry (skip merge of {loser_id})", file=sys.stderr)
            continue
        loser = by_id[loser_id]
        winner = by_id[winner_id]

        # Add loser's canonical_name + aliases as winner's aliases
        existing_aliases = set(winner.get("aliases") or [])
        existing_aliases.add(loser["canonical_name"])
        for a in loser.get("aliases") or []:
            existing_aliases.add(a)
        # Don't add winner's own canonical_name as alias
        existing_aliases.discard(winner["canonical_name"])
        winner["aliases"] = sorted(existing_aliases)

        # Sum mention_count
        winner["mention_count"] = (winner.get("mention_count", 0) or 0) + (loser.get("mention_count", 0) or 0)

        # Union sources
        winner["sources"] = sorted(set((winner.get("sources") or []) + (loser.get("sources") or [])))

        # Bump confidence to medium if winner had only single-mention before
        if winner["mention_count"] >= 3 and winner.get("confidence") == "low":
            winner["confidence"] = "medium"

        losers_seen.add(loser_id)
        merge_counts[winner_id] += 1

    # Drop losers
    new_orgs = [o for o in orgs_doc["organizations"] if o["id"] not in losers_seen]

    # Add curated aliases
    by_id_new = {o["id"]: o for o in new_orgs}
    aliases_added = 0
    for oid, new_aliases in ADD_ALIASES.items():
        if oid not in by_id_new:
            continue
        org = by_id_new[oid]
        existing = set(org.get("aliases") or [])
        before = len(existing)
        existing.update(new_aliases)
        existing.discard(org["canonical_name"])
        org["aliases"] = sorted(existing)
        aliases_added += len(existing) - before

    # Apply type fixes
    type_fixes_applied = 0
    for oid, new_type in TYPE_FIXES.items():
        if oid in by_id_new:
            old_type = by_id_new[oid].get("type")
            if old_type != new_type:
                by_id_new[oid]["type"] = new_type
                type_fixes_applied += 1

    # Re-sort by mention_count desc
    new_orgs.sort(key=lambda o: (-o.get("mention_count", 0), o["id"]))

    # Update _meta
    orgs_doc["organizations"] = new_orgs
    meta = orgs_doc.setdefault("_meta", {})
    meta["registry_version"] = "v2.0"
    meta["total_count"] = len(new_orgs)
    meta["by_confidence"] = {
        c: sum(1 for o in new_orgs if o.get("confidence") == c)
        for c in ("high", "medium", "low")
    }
    meta["source_passes"] = (meta.get("source_passes") or []) + ["dedup_2026-05-04"]
    orgs_doc["last_updated_at"] = datetime.now(timezone.utc).isoformat()

    print(f"  losers merged:    {len(losers_seen)}  ({sum(merge_counts.values())} merge ops)")
    print(f"  new total orgs:   {len(new_orgs)}")
    print(f"  aliases added:    {aliases_added}")
    print(f"  type fixes:       {type_fixes_applied}")

    return orgs_doc, dict(merge_counts)


def rewrite_refs(merge_resolution: dict[str, str]) -> dict:
    """Walk all FK consumers and rewrite org_ids using merge_resolution map."""

    def remap_list(ids: list) -> tuple[list, int]:
        if not ids:
            return ids, 0
        new = []
        changed = 0
        seen = set()
        for o in ids:
            target = merge_resolution.get(o, o)
            if target != o:
                changed += 1
            if target not in seen:
                new.append(target)
                seen.add(target)
        return sorted(new), changed

    catalogs = [
        ROOT / "documents/press/articles",
        ROOT / "documents/press/comments",
        ROOT / "documents/press/social_posts",
        ROOT / "assets/transcripts",
        ROOT / "timeline/us_events.jsonl",
    ]

    def _atomic_write_jsonl(path, recs):
        tmp = path.with_suffix(path.suffix + ".tmp")
        body = "\n".join(json.dumps(r, ensure_ascii=False) for r in recs) + "\n"
        tmp.write_text(body, encoding="utf-8")
        os.replace(tmp, path)

    totals = defaultdict(int)
    for d in catalogs:
        if not d.exists():
            continue
        domain_changed = 0
        domain_records = 0
        rel = str(d.relative_to(ROOT))

        # JSONL path: load all, modify, write whole file
        if d.is_file() and d.suffix == ".jsonl":
            records = []
            for line in d.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except Exception:
                    totals[f"{rel}_errors"] += 1
            any_changed = False
            for rec in records:
                domain_records += 1
                if "org_ids" not in rec:
                    continue
                new_list, changes = remap_list(rec["org_ids"])
                if changes > 0:
                    rec["org_ids"] = new_list
                    domain_changed += 1
                    any_changed = True
            if any_changed:
                _atomic_write_jsonl(d, records)
            print(f"  {rel:<40} examined={domain_records:>5}  rewritten={domain_changed:>5}")
            totals[rel] = domain_changed
            continue

        # Per-file path
        for p in d.glob("*.json"):
            try:
                rec = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                totals[f"{d.name}_errors"] += 1
                continue
            if "org_ids" not in rec:
                continue
            new_list, changes = remap_list(rec["org_ids"])
            if changes > 0:
                rec["org_ids"] = new_list
                atomic_write_json(p, rec)
                domain_changed += 1
            domain_records += 1
        print(f"  {rel:<40} examined={domain_records:>5}  rewritten={domain_changed:>5}")
        totals[d.name] = domain_changed
    return dict(totals)


def main() -> int:
    print("=== orgs.json dedup pass ===")
    orgs_doc = json.loads(ORGS_PATH.read_text(encoding="utf-8"))
    print(f"  orgs before:   {len(orgs_doc['organizations'])}")

    new_doc, merge_counts = merge_orgs(orgs_doc)

    # Build flat resolution map (handles transitive)
    resolution: dict[str, str] = {}
    for loser, winner in MERGE_MAP.items():
        # Resolve transitive
        target = winner
        seen = {loser}
        while target in MERGE_MAP and target not in seen:
            seen.add(target)
            target = MERGE_MAP[target]
        resolution[loser] = target

    atomic_write_json(ORGS_PATH, new_doc)
    print(f"  written:       {ORGS_PATH.relative_to(ROOT)}")

    print("\n=== rewriting org_ids across catalogs ===")
    totals = rewrite_refs(resolution)
    print(f"\n=== TOTAL records rewritten: {sum(v for k, v in totals.items() if not k.endswith('_errors'))} ===")
    return 0



if __name__ == "__main__":
    sys.exit(main())


def main() -> int:
    print("=== orgs.json dedup pass ===")
    orgs_doc = json.loads(ORGS_PATH.read_text(encoding="utf-8"))
    print(f"  orgs before:   {len(orgs_doc['organizations'])}")

    new_doc, merge_counts = merge_orgs(orgs_doc)

    resolution: dict[str, str] = {}
    for loser, winner in MERGE_MAP.items():
        target = winner
        seen = {loser}
        while target in MERGE_MAP and target not in seen:
            seen.add(target)
            target = MERGE_MAP[target]
        resolution[loser] = target

    atomic_write_json(ORGS_PATH, new_doc)
    print(f"  written:       {ORGS_PATH.relative_to(ROOT)}")

    print("\n=== rewriting org_ids across catalogs ===")
    totals = rewrite_refs(resolution)
    print(f"\n=== TOTAL records rewritten: {sum(v for k, v in totals.items() if not k.endswith('_errors'))} ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
