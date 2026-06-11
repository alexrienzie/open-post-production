"""
Assign parent_id (and optionally relationships) in places/places.json.

Layers (each fills only places still missing a parent):
1) Explicit backbone (Tetons / Wyoming / state -> country / known cities)
2) Comma-suffix hints (", WY" -> pl_wyoming)
3) Keyword inference for GTNP and Yellowstone features
4) Optional Gemini batch pass (--use-llm) for the long tail, constrained to anchor parents

Anchors handed to the LLM: every country/state/county/region in the registry plus key
WY/Tetons nodes (Jackson Hole, Jackson, GTNP, Teton Range, Tetons, Yellowstone NP,
BTNF, Teton County, Snow King). The model can only assign parents from this list.

Usage:
  python _scripts/registries/apply_location_hierarchy.py --apply
  python _scripts/registries/apply_location_hierarchy.py --clear-parents --apply
  python _scripts/registries/apply_location_hierarchy.py --use-llm --apply
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parents[2]
LOC_PATH = ROOT / "places" / "places.json"
PATCH_PATH = ROOT / "_scripts" / "locations_hierarchy_llm_patch.json"

# -----------------------------------------------------------------------------
# Explicit parent_id edges (child_id -> parent_id). Top-level places omitted.
# -----------------------------------------------------------------------------

EXPLICIT_PARENT: dict[str, str] = {
    # --- Wyoming spine ---
    "pl_teton_county": "pl_wyoming",
    "pl_jackson_hole": "pl_wyoming",
    "pl_jackson": "pl_jackson_hole",
    "pl_grand_teton_national_park": "pl_wyoming",
    "pl_bridger_teton_national_forest": "pl_wyoming",
    "pl_yellowstone_national_park": "pl_united_states",
    "pl_grand_teton": "pl_grand_teton_national_park",
    "pl_teton_range": "pl_wyoming",
    "pl_tetons": "pl_teton_range",
    "pl_snow_king": "pl_jackson_hole",
    "pl_national_elk_refuge": "pl_wyoming",
    "pl_clifford_p_hansen_courthouse": "pl_jackson",
    "pl_jenny_lake": "pl_grand_teton_national_park",
    # --- Common corridor / approaches (GTNP or adjacent WY) ---
    "pl_moran_junction": "pl_teton_county",
    "pl_moose": "pl_jackson_hole",
    "pl_teton_village": "pl_jackson_hole",
    "pl_wilson": "pl_jackson_hole",
    "pl_dubois": "pl_wyoming",
    "pl_pinedale": "pl_wyoming",
    "pl_lander": "pl_wyoming",
    "pl_rock_springs": "pl_wyoming",
    "pl_cheyenne": "pl_wyoming",
    "pl_casper": "pl_wyoming",
    "pl_togwotee_pass": "pl_wyoming",
    "pl_togwotee_pass_tetons_area": "pl_wyoming",
    "pl_driggs_idaho": "pl_idaho",
    "pl_driggs": "pl_idaho",
    "pl_victor_idaho": "pl_idaho",
    "pl_victor": "pl_idaho",
    "pl_tetonia": "pl_idaho",
    "pl_ashton": "pl_idaho",
    "pl_island_park": "pl_idaho",
    "pl_west_yellowstone": "pl_montana",
    "pl_alta": "pl_wyoming",
    "pl_star_valley": "pl_wyoming",
    "pl_teton_valley": "pl_idaho",
    "pl_idaho_falls": "pl_idaho",
    "pl_bozeman": "pl_montana",
    "pl_big_sky": "pl_montana",
    "pl_livingston": "pl_montana",
    "pl_cody": "pl_wyoming",
    "pl_salt_lake_city": "pl_utah",
    "pl_salt_lake_city_ut": "pl_utah",
    "pl_park_city": "pl_utah",
    "pl_provo": "pl_utah",
    "pl_logan": "pl_utah",
    "pl_ogden": "pl_utah",
    "pl_denver": "pl_colorado",
    "pl_boulder": "pl_colorado",
    "pl_colorado_springs": "pl_colorado",
    "pl_fort_collins": "pl_colorado",
    "pl_aspen": "pl_colorado",
    "pl_estes_park": "pl_colorado",
    "pl_pikes_peak": "pl_colorado",
    "pl_longs_peak": "pl_colorado",
    "pl_long_s_peak": "pl_colorado",
    "pl_rocky_mountain_national_park": "pl_colorado",
    "pl_portland": "pl_oregon",
    "pl_los_angeles": "pl_california",
    "pl_san_francisco": "pl_california",
    "pl_san_diego": "pl_california",
    "pl_sacramento": "pl_california",
    "pl_yosemite_national_park": "pl_california",
    "pl_mount_whitney": "pl_california",
    "pl_lake_tahoe": "pl_california",
    "pl_tahoe": "pl_california",
    "pl_king_beach": "pl_california",
    "pl_kings_beach": "pl_california",
    "pl_truckee": "pl_california",
    "pl_squaw_valley": "pl_california",
    "pl_palisades_tahoe": "pl_california",
    "pl_broken_arrow": "pl_california",
    "pl_santa_monica": "pl_california",
    "pl_santa_monicas": "pl_california",
    "pl_santa_barbara": "pl_california",
    "pl_marin": "pl_california",
    "pl_marin_county": "pl_california",
    "pl_tamalpais": "pl_california",
    "pl_tamalpa_headlands": "pl_california",
    "pl_pacific_crest_trail": "pl_united_states",
    "pl_phoenix": "pl_arizona",
    "pl_tucson": "pl_arizona",
    "pl_flagstaff": "pl_arizona",
    "pl_grand_canyon": "pl_arizona",
    "pl_grand_canyon_national_park": "pl_arizona",
    "pl_sedona": "pl_arizona",
    "pl_las_vegas": "pl_nevada",
    "pl_reno": "pl_nevada",
    "pl_austin": "pl_texas",
    "pl_dallas": "pl_texas",
    "pl_houston": "pl_texas",
    "pl_chicago": "pl_illinois",
    "pl_new_york": "pl_new_york_state",
    "pl_new_york_city": "pl_new_york_state",
    "pl_brooklyn": "pl_new_york_state",
    "pl_manhattan": "pl_new_york_state",
    "pl_washington_dc": "pl_united_states",
    "pl_miami": "pl_florida",
    "pl_orlando": "pl_florida",
    "pl_tampa": "pl_florida",
    "pl_atlanta": "pl_georgia",
    "pl_nashville": "pl_tennessee",
    "pl_memphis": "pl_tennessee",
    "pl_boston": "pl_massachusetts",
    "pl_philadelphia": "pl_pennsylvania",
    "pl_minneapolis": "pl_minnesota",
    "pl_detroit": "pl_michigan",
    "pl_cleveland": "pl_ohio",
    "pl_columbus": "pl_ohio",
    "pl_cincinnati": "pl_ohio",
    "pl_omaha": "pl_nebraska",
    "pl_new_orleans": "pl_louisiana",
    "pl_baton_rouge": "pl_louisiana",
    "pl_montpelier": "pl_vermont",
    "pl_burlington": "pl_vermont",
    "pl_manchester": "pl_new_hampshire",
    "pl_concord": "pl_new_hampshire",
    "pl_vancouver": "pl_british_columbia",
    "pl_victoria_bc": "pl_british_columbia",
    "pl_calgary": "pl_alberta",
    "pl_edmonton": "pl_alberta",
    "pl_banff": "pl_alberta",
    "pl_jasper": "pl_alberta",
    "pl_toronto": "pl_canada",
    "pl_halifax": "pl_nova_scotia",
    "pl_paris": "pl_france",
    "pl_rome": "pl_italy",
    "pl_venice": "pl_italy",
    "pl_milan": "pl_italy",
    "pl_florence": "pl_italy",
    "pl_naples": "pl_italy",
    "pl_chamonix": "pl_france",
    "pl_geneva": "pl_switzerland",
    "pl_zurich": "pl_switzerland",
    "pl_bern": "pl_switzerland",
    "pl_mexico_city": "pl_mexico",
    "pl_puerto_vallarta": "pl_mexico",
    "pl_cancun": "pl_mexico",
    "pl_denali": "pl_alaska",
    "pl_kodiak_100": "pl_alaska",
    "pl_kodiak": "pl_alaska",
    "pl_anchorage": "pl_alaska",
    "pl_dc": "pl_united_states",
    "pl_jackson_wyoming": "pl_jackson_hole",
    "pl_grand_teton_summit": "pl_grand_teton",
    "pl_enclosure": "pl_grand_teton",
    "pl_the_enclosure": "pl_grand_teton",
    "pl_catwalk": "pl_grand_teton",
    "pl_the_catwalk": "pl_grand_teton",
    "pl_meadows": "pl_grand_teton_national_park",
    "pl_the_meadows": "pl_grand_teton_national_park",
    "pl_glacier_view": "pl_grand_teton_national_park",
    "pl_grand_run": "pl_grand_teton_national_park",
    "pl_teton_crest_trail": "pl_grand_teton_national_park",
    "pl_togwotee": "pl_wyoming",
    "pl_jackson_lake": "pl_grand_teton_national_park",
    "pl_jenny_lake_visitor_center": "pl_grand_teton_national_park",
    "pl_signal_mountain": "pl_grand_teton_national_park",
    "pl_mormon_row": "pl_grand_teton_national_park",
    "pl_oxbow_bend": "pl_grand_teton_national_park",
    "pl_snake_river": "pl_wyoming",
    "pl_string_lake": "pl_grand_teton_national_park",
    "pl_leigh_lake": "pl_grand_teton_national_park",
    "pl_phelps_lake": "pl_grand_teton_national_park",
    "pl_taggart_lake": "pl_grand_teton_national_park",
    "pl_bradley_lake": "pl_grand_teton_national_park",
    "pl_amphitheater_lake": "pl_grand_teton_national_park",
    "pl_delta_lake": "pl_grand_teton_national_park",
    "pl_surprise_lake": "pl_grand_teton_national_park",
    "pl_static_peak": "pl_grand_teton_national_park",
    "pl_buck_mountain": "pl_grand_teton_national_park",
    "pl_cathedral_peak": "pl_grand_teton_national_park",
    "pl_disappointment_peak": "pl_grand_teton_national_park",
    "pl_middle_teton": "pl_grand_teton_national_park",
    "pl_south_teton": "pl_grand_teton_national_park",
    "pl_mount_owen": "pl_grand_teton_national_park",
    "pl_mount_moran": "pl_grand_teton_national_park",
    "pl_mount_tewinot": "pl_grand_teton_national_park",
    "pl_teewinot": "pl_grand_teton_national_park",
    "pl_symmetry_spire": "pl_grand_teton_national_park",
    "pl_storm_point": "pl_grand_teton_national_park",
    "pl_inspiration_point": "pl_grand_teton_national_park",
    "pl_hidden_falls": "pl_grand_teton_national_park",
    "pl_paintbrush_canyon": "pl_grand_teton_national_park",
    "pl_paintbrush_divide": "pl_grand_teton_national_park",
    "pl_hurricane_pass": "pl_grand_teton_national_park",
    "pl_alaska_basin": "pl_grand_teton_national_park",
    "pl_indian_peaks_wilderness": "pl_colorado",
    "pl_mont_blanc": "pl_france",
    "pl_les_houches": "pl_france",
    "pl_courmayeur": "pl_italy",
    "pl_cortina_d_ampezzo": "pl_italy",
    "pl_dolomites": "pl_italy",
    "pl_tre_cime": "pl_italy",
    "pl_zermatt": "pl_switzerland",
    "pl_matterhorn": "pl_switzerland",
    "pl_eiger": "pl_switzerland",
    "pl_jungfrau": "pl_switzerland",
    "pl_andes": "pl_south_america",
    "pl_himalayas": "pl_asia",
    "pl_alps": "pl_europe",
    "pl_appalachians": "pl_united_states",
    "pl_appalachian_trail": "pl_united_states",
    "pl_continental_divide": "pl_united_states",
    "pl_rocky_mountains": "pl_united_states",
    "pl_sierra_nevada_mountains": "pl_california",
    "pl_yosemite": "pl_california",
    "pl_yosemite_valley": "pl_california",
    "pl_southern_california": "pl_california",
    "pl_north_lake_tahoe": "pl_california",
    "pl_long_island": "pl_new_york_state",
    "pl_nassau_county": "pl_new_york_state",
    "pl_suffolk_county": "pl_new_york_state",
    "pl_glacier_national_park": "pl_montana",
    "pl_zion_national_park": "pl_utah",
    "pl_bryce_canyon_national_park": "pl_utah",
    "pl_arches_national_park": "pl_utah",
    "pl_canyonlands_national_park": "pl_utah",
    "pl_capitol_reef_national_park": "pl_utah",
    "pl_city_of_rocks": "pl_idaho",
    "pl_sawtooths": "pl_idaho",
    "pl_sawtooth_national_forest": "pl_idaho",
    "pl_wind_river_reservation": "pl_wyoming",
    "pl_wind_river_range": "pl_wyoming",
    "pl_teton_county_wyoming": "pl_wyoming",
    "pl_togwotee_valley": "pl_wyoming",
    "pl_teton": "pl_wyoming",
    "pl_alberta": "pl_canada",
    "pl_yukon": "pl_canada",
    "pl_nova_scotia": "pl_canada",
    "pl_sicily": "pl_italy",
    "pl_sardinia": "pl_italy",
    "pl_aosta_valley": "pl_italy",
    "pl_north_america": None,
    "pl_south_america": None,
    "pl_central_america": None,
    "pl_east_coast": None,
    "pl_west_coast": None,
    "pl_midwest": None,
    "pl_sierra_nevada": "pl_california",
    "pl_cascades": "pl_united_states",
    "pl_pacific_northwest": "pl_united_states",
    "pl_new_england": "pl_united_states",
    "pl_midwest": "pl_united_states",
    "pl_pacific_ocean": None,
    "pl_atlantic_ocean": None,
}
# Drop sentinel-None entries (kept above only for documentation; tree must omit them).
EXPLICIT_PARENT = {k: v for k, v in EXPLICIT_PARENT.items() if v is not None}

# Substrings (lowercase) -> parent when type matches ALLOWED_GTNP_TYPES and parent still unset.
GTNP_NAME_HINTS: tuple[str, ...] = (
    "grand teton national park",
    "gtnp",
    "garnet canyon",
    "garnett canyon",
    "lupine meadow",
    "death canyon",
    "cascade canyon",
    "jenny lake",
    "string lake",
    "leigh lake",
    "phelps lake",
    "jackson lake",
    "colter bay",
    "taggart lake",
    "bradley lake",
    "paintbrush",
    "hurricane pass",
    "static peak",
    "mount owen",
    "teewinot",
    "middle teton",
    "south teton",
    "cathedral peak",
    "buck mountain",
    "symmetry spire",
    "delta lake",
    "amphitheater",
    "exum ridge",
    "owen spalding",
    "owens spalding",
    "upper saddle",
    "lower saddle",
    "upper exum",
    "lower exum",
    "belly roll",
    "black rock chimney",
    "schoolroom glacier",
    "petzl cave",
    "old climber",
    "grand teton fkt",
    "switchback grand teton",
    "finish grand teton fkt",
    "moraine camp",
    "the moraine",
    "boulder field",
    "rappel station",
    "sargent chimney",
    "sergeant chimney",
)

ALLOWED_GTNP_TYPES = frozenset(
    {"natural_feature", "route", "trailhead", "protected_area", "infrastructure", "establishment", "unknown"}
)

YELLOWSTONE_HINTS = ("yellowstone",)
ALLOWED_YELLOWSTONE_TYPES = ALLOWED_GTNP_TYPES

# US two-letter -> pl_* state slug (only where registry uses predictable slug)
US_ABBR_TO_PL: dict[str, str] = {
    "AL": "pl_alabama",
    "AK": "pl_alaska",
    "AZ": "pl_arizona",
    "AR": "pl_arkansas",
    "CA": "pl_california",
    "CO": "pl_colorado",
    "CT": "pl_connecticut",
    "DE": "pl_delaware",
    "FL": "pl_florida",
    "GA": "pl_georgia",
    "HI": "pl_hawaii",
    "ID": "pl_idaho",
    "IL": "pl_illinois",
    "IN": "pl_indiana",
    "IA": "pl_iowa",
    "KS": "pl_kansas",
    "KY": "pl_kentucky",
    "LA": "pl_louisiana",
    "ME": "pl_maine",
    "MD": "pl_maryland",
    "MA": "pl_massachusetts",
    "MI": "pl_michigan",
    "MN": "pl_minnesota",
    "MS": "pl_mississippi",
    "MO": "pl_missouri",
    "MT": "pl_montana",
    "NE": "pl_nebraska",
    "NV": "pl_nevada",
    "NH": "pl_new_hampshire",
    "NJ": "pl_new_jersey",
    "NM": "pl_new_mexico",
    "NY": "pl_new_york_state",
    "NC": "pl_north_carolina",
    "ND": "pl_north_dakota",
    "OH": "pl_ohio",
    "OK": "pl_oklahoma",
    "OR": "pl_oregon",
    "PA": "pl_pennsylvania",
    "RI": "pl_rhode_island",
    "SC": "pl_south_carolina",
    "SD": "pl_south_dakota",
    "TN": "pl_tennessee",
    "TX": "pl_texas",
    "UT": "pl_utah",
    "VT": "pl_vermont",
    "VA": "pl_virginia",
    "WA": "pl_washington_state",
    "WV": "pl_west_virginia",
    "WI": "pl_wisconsin",
    "WY": "pl_wyoming",
    "DC": "pl_washington_dc",
}

NON_US_STATE_SLUGS: frozenset[str] = frozenset({"pl_british_columbia"})

STATE_SLUG_TO_COUNTRY: dict[str, str] = {
    "pl_british_columbia": "pl_canada",
    "pl_alberta": "pl_canada",
    "pl_ontario": "pl_canada",
    "pl_quebec": "pl_canada",
    "pl_nova_scotia": "pl_canada",
}

_COMMA_SUFFIX_RE = re.compile(
    r",\s*([A-Za-z]{2})\s*(?:\d{5})?\s*$"
)  # ", WY" or ", CA 90210"


def atomic_write_json(path: Path, data: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return s.lower().strip()


def parent_from_comma_suffix(canonical: str, id_set: set[str]) -> str | None:
    m = _COMMA_SUFFIX_RE.search(canonical.strip())
    if not m:
        return None
    abbr = m.group(1).upper()
    cand = US_ABBR_TO_PL.get(abbr)
    if cand and cand in id_set:
        return cand
    return None


def infer_state_country_parent(pid: str, ptype: str, id_set: set[str]) -> str | None:
    if ptype != "state":
        return None
    if pid in NON_US_STATE_SLUGS:
        return STATE_SLUG_TO_COUNTRY.get(pid) or (
            "pl_canada" if "columbia" in pid or "alberta" in pid or "ontario" in pid else None
        )
    if pid in STATE_SLUG_TO_COUNTRY:
        return STATE_SLUG_TO_COUNTRY[pid]
    if pid == "pl_united_states":
        return None
    if "pl_united_states" in id_set:
        return "pl_united_states"
    return None


def infer_gtnp_parent(name: str, ptype: str) -> bool:
    if ptype not in ALLOWED_GTNP_TYPES:
        return False
    n = norm(name)
    return any(h in n for h in GTNP_NAME_HINTS)


def infer_yellowstone_parent(name: str, ptype: str) -> bool:
    if ptype not in ALLOWED_YELLOWSTONE_TYPES:
        return False
    n = norm(name)
    if "grand teton" in n or "gtnp" in n:
        return False
    return any(h in n for h in YELLOWSTONE_HINTS)


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


def call_gemini_json(model: str, prompt: str, *, timeout_sec: int, max_out: int) -> dict[str, Any]:
    import google.generativeai as genai

    keys = gemini_key_candidates()
    if not keys:
        raise RuntimeError("GEMINI_API_KEY not set (env or Windows HKCU\\Environment).")
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
                        "temperature": 0.1,
                        "response_mime_type": "application/json",
                        "max_output_tokens": max_out,
                    },
                    request_options={"timeout": timeout_sec},
                )
                text = getattr(resp, "text", None)
                if not text or not str(text).strip():
                    raise RuntimeError("empty Gemini response")
                s = str(text).strip()
                if s.startswith("{") and s.endswith("}"):
                    obj_text = s
                else:
                    a, b = s.find("{"), s.rfind("}")
                    if a == -1 or b <= a:
                        raise ValueError("No JSON object")
                    obj_text = s[a : b + 1]
                try:
                    return json.loads(obj_text)
                except json.JSONDecodeError:
                    # Common Gemini glitches: missing commas between adjacent objects/arrays
                    # or trailing commas before closing brackets. Repair conservatively.
                    repaired = obj_text
                    repaired = re.sub(r"}\s*{", "},{", repaired)
                    repaired = re.sub(r"]\s*\[", "],[", repaired)
                    # Missing comma between a string value and the next key on a new line:
                    # e.g.  "confidence": "medium"\n    "id": "pl_x"   ->   ...,\n    "id":...
                    repaired = re.sub(r'(\"\s*)\n(\s*\")', r'\1,\n\2', repaired)
                    repaired = re.sub(r",(\s*[}\]])", r"\1", repaired)
                    try:
                        return json.loads(repaired)
                    except json.JSONDecodeError as je2:
                        # Last resort: dump raw response for inspection.
                        debug_path = ROOT / "_scripts" / "locations_hierarchy_llm_lasterror.txt"
                        try:
                            debug_path.write_text(obj_text, encoding="utf-8")
                        except Exception:
                            pass
                        raise je2
            except BaseException as e:
                last_exc = e
                msg = str(e).lower()
                if ("api_key_invalid" in msg or "invalid api key" in msg) and ki + 1 < len(keys):
                    break
                if ("429" in msg or "quota" in msg or "resource has been exhausted" in msg) and attempt + 1 < 8:
                    print(f"[gemini] backoff {backoff}s (try {attempt + 1}/8)", flush=True)
                    time.sleep(min(backoff, 600))
                    backoff = min(int(backoff * 1.45), 600)
                    continue
                raise
    assert last_exc is not None
    raise last_exc


def build_anchor_set(places: list[dict]) -> set[str]:
    """Anchor parents the LLM is allowed to assign.

    Includes geographic containers (countries, states, counties, regions, protected areas)
    plus cities/towns/well-known natural features so establishments and residences can
    attach to a town and routes/peaks can attach to a peak or trailhead.
    """
    by_id = {p["id"]: p for p in places if p.get("id")}
    anchors: set[str] = set()
    for p in places:
        t = p.get("type")
        if t in ("country", "state", "county", "region", "protected_area", "city", "town"):
            anchors.add(p["id"])
        elif t == "natural_feature" and int(p.get("mention_count") or 0) >= 4:
            anchors.add(p["id"])
    forced = [
        "pl_united_states",
        "pl_canada",
        "pl_wyoming",
        "pl_jackson_hole",
        "pl_jackson",
        "pl_teton_county",
        "pl_grand_teton_national_park",
        "pl_grand_teton",
        "pl_teton_range",
        "pl_tetons",
        "pl_yellowstone_national_park",
        "pl_bridger_teton_national_forest",
        "pl_snow_king",
        "pl_idaho",
        "pl_montana",
        "pl_utah",
        "pl_colorado",
        "pl_california",
        "pl_alaska",
        "pl_washington_dc",
        "pl_new_york_state",
        "pl_florida",
        "pl_arizona",
        "pl_nevada",
        "pl_oregon",
        "pl_alberta",
        "pl_british_columbia",
        "pl_france",
        "pl_italy",
        "pl_switzerland",
        "pl_united_kingdom",
        "pl_mexico",
        "pl_spain",
        "pl_germany",
        "pl_japan",
        "pl_australia",
        "pl_new_zealand",
        "pl_europe",
        "pl_south_america",
        "pl_asia",
    ]
    for fid in forced:
        if fid in by_id:
            anchors.add(fid)
    return anchors


def llm_assign_parents(
    unassigned: list[dict],
    anchors: list[dict],
    *,
    model: str,
    batch_size: int,
    timeout_sec: int,
    max_out: int,
) -> dict[str, str]:
    """Returns {place_id: parent_id} for valid model assignments."""
    anchor_lookup = {a["id"]: a for a in anchors}
    anchor_compact = [
        {
            "id": a["id"],
            "name": a.get("canonical_name"),
            "type": a.get("type"),
        }
        for a in anchors
    ]
    instructions = """You assign a parent_id to each place in a documentary registry (Grand Teton FKT story).
Default to ASSIGNING a parent. Use null sparingly.

Pick the single best parent_id from ANCHORS only. Rules of thumb:
- US city/town/establishment -> its US state (pl_<state>). If you cannot identify the state, use pl_united_states.
- Non-US city/town -> its country (pl_<country>) — use the smallest containing anchor available.
- US natural_feature inside Grand Teton National Park or on the Grand Teton -> pl_grand_teton_national_park or pl_grand_teton.
- US natural_feature inside Yellowstone -> pl_yellowstone_national_park.
- Other natural_feature -> the most specific containing protected_area / state / country anchor.
- Routes / trailheads -> the protected_area, peak, or town they're attached to.
- Establishments / residences / infrastructure -> the city/town they're located in (use the closest matching anchor).
- Counties -> their state.
- US states -> pl_united_states.
- Provinces (e.g. British Columbia) -> pl_canada.
- Regions named after a state or country -> that state/country anchor.

Use null ONLY when:
- The place is itself top-level (a country, continent, ocean, or planet), OR
- The string is unidentifiable garbage with no plausible real-world referent.

Disambiguation hints from the corpus:
- "Jackson", "Jackson Hole", "the Tetons", "the Grand", "Grand Teton" all refer to Wyoming/GTNP unless context says otherwise.
- "Salt Lake" usually means Salt Lake City, UT.
- "Tahoe", "Lake Tahoe" -> use pl_lake_tahoe if available; otherwise pl_california or pl_nevada.

Return STRICT JSON only (well-formed, no trailing commas, all object fields comma-separated):
{ "assignments": [ { "id": "pl_*", "parent_id": "pl_* | null", "confidence": "high|medium|low" } ] }
Every input id must appear once. parent_id MUST be a slug from the ANCHORS list, or null."""

    out: dict[str, str] = {}
    batches = [unassigned[i : i + batch_size] for i in range(0, len(unassigned), batch_size)]
    for bi, batch in enumerate(batches):
        items = []
        for p in batch:
            note = (p.get("notes") or "")[:240]
            items.append(
                {
                    "id": p["id"],
                    "name": p.get("canonical_name"),
                    "type": p.get("type"),
                    "aliases": (p.get("aliases") or [])[:4],
                    "mention_count": p.get("mention_count", 0),
                    "notes_excerpt": note,
                }
            )
        payload = {"anchors": anchor_compact, "places": items}
        prompt = instructions + "\n\nDATA:\n" + json.dumps(payload, ensure_ascii=False)
        try:
            resp = call_gemini_json(model, prompt, timeout_sec=timeout_sec, max_out=max_out)
        except BaseException as e:
            print(f"[llm batch {bi + 1}/{len(batches)}] failed: {e}", file=sys.stderr)
            continue
        for r in resp.get("assignments") or []:
            pid = r.get("id")
            par = r.get("parent_id")
            if not isinstance(pid, str) or not pid.startswith("pl_"):
                continue
            if par is None:
                continue
            if not isinstance(par, str) or not par.startswith("pl_"):
                continue
            if par not in anchor_lookup:
                continue
            if par == pid:
                continue
            out[pid] = par
        print(f"[llm] batch {bi + 1}/{len(batches)}: {len(resp.get('assignments') or [])} responses, {len(out)} cumulative")

    return out


def detect_cycles(places: list[dict]) -> list[str]:
    """Detect cycles following parent pointers from each place (starts at id, walks parent_id chain)."""
    by_parent = {p["id"]: (p.get("parent_id") or None) for p in places if p.get("id")}
    errors: list[str] = []
    for start in by_parent:
        seen: set[str] = set()
        cur: str | None = start
        steps = 0
        while cur:
            steps += 1
            if cur in seen:
                errors.append(f"cycle involving {start}")
                break
            seen.add(cur)
            nxt = by_parent.get(cur)
            cur = nxt if nxt else None
            if steps > 40:
                errors.append(f"deep chain from {start}")
                break
    return errors


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument(
        "--clear-parents",
        action="store_true",
        help="Remove all parent_id before assigning (recommended when re-running after manual edits)",
    )
    ap.add_argument("--use-llm", action="store_true", help="Run Gemini batch over remaining unassigned places")
    ap.add_argument("--llm-batch-size", type=int, default=40)
    ap.add_argument("--model", default="gemini-2.5-flash")
    ap.add_argument("--timeout-sec", type=int, default=180)
    ap.add_argument("--max-output-tokens", type=int, default=8192)
    ap.add_argument(
        "--llm-min-mentions",
        type=int,
        default=1,
        help="Skip very low-signal rows (mention_count < N) when calling LLM (default 1 = include all).",
    )
    args = ap.parse_args()
    dry = args.dry_run or not args.apply

    doc = json.loads(LOC_PATH.read_text(encoding="utf-8"))
    places: list[dict] = doc.get("places") or []
    id_set = {p["id"] for p in places if p.get("id")}

    if args.clear_parents:
        for p in places:
            p.pop("parent_id", None)

    assigned = 0
    skipped_bad_parent = 0
    for p in places:
        pid = p.get("id")
        if not pid:
            continue
        if p.get("parent_id"):
            continue
        parent: str | None = None

        if pid in EXPLICIT_PARENT:
            parent = EXPLICIT_PARENT[pid]

        if parent is None:
            parent = infer_state_country_parent(pid, p.get("type") or "", id_set)

        if parent is None:
            cname = p.get("canonical_name") or ""
            if isinstance(cname, str):
                parent = parent_from_comma_suffix(cname, id_set)

        if parent is None:
            cname = p.get("canonical_name") or ""
            if infer_gtnp_parent(cname, p.get("type") or ""):
                if "pl_grand_teton_national_park" in id_set:
                    parent = "pl_grand_teton_national_park"

        if parent is None:
            cname = p.get("canonical_name") or ""
            if infer_yellowstone_parent(cname, p.get("type") or ""):
                if "pl_yellowstone_national_park" in id_set:
                    parent = "pl_yellowstone_national_park"

        if parent is None:
            continue
        if parent not in id_set:
            skipped_bad_parent += 1
            continue
        if parent == pid:
            continue
        p["parent_id"] = parent
        assigned += 1

    print(f"Rules pass: assigned {assigned} parents (bad-target skipped: {skipped_bad_parent})")

    if args.use_llm:
        anchor_ids = build_anchor_set(places)
        anchors = [p for p in places if p["id"] in anchor_ids]
        # NOTE: a place can be both an anchor (valid parent for others) AND unparented itself,
        # e.g. a city that should attach to its state. So we do NOT exclude anchors from input.
        unassigned = [
            p
            for p in places
            if not p.get("parent_id")
            and p.get("type") not in ("country",)
            and int(p.get("mention_count") or 0) >= args.llm_min_mentions
        ]
        print(f"LLM pass: {len(unassigned)} places to classify against {len(anchors)} anchors (model={args.model})")
        if unassigned:
            try:
                llm_out = llm_assign_parents(
                    unassigned,
                    anchors,
                    model=args.model,
                    batch_size=args.llm_batch_size,
                    timeout_sec=args.timeout_sec,
                    max_out=args.max_output_tokens,
                )
            except RuntimeError as e:
                print(f"LLM init error: {e}", file=sys.stderr)
                llm_out = {}
            llm_added = 0
            id_to_row = {p["id"]: p for p in places}
            for pid, par in llm_out.items():
                row = id_to_row.get(pid)
                if not row or row.get("parent_id"):
                    continue
                if par == pid or par not in id_set:
                    continue
                row["parent_id"] = par
                llm_added += 1
            print(f"LLM pass: added {llm_added} parents")

    errs = detect_cycles(places)
    if errs:
        print("WARN cycle/deep checks:", errs[:5])

    n_with_parent = sum(1 for p in places if p.get("parent_id"))
    print(f"Coverage: {n_with_parent}/{len(places)} places have parent_id")

    meta = doc.setdefault("_meta", {})
    meta["source_passes"] = (meta.get("source_passes") or []) + ["apply_location_hierarchy.py"]
    doc["last_updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")

    if dry:
        print("Dry-run: no file written. Pass --apply to save.")
        return 0

    atomic_write_json(LOC_PATH, doc)
    print(f"Wrote {LOC_PATH.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
