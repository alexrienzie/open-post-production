"""Generate the story-filter library — one JSON per narrative-structure framework.

Why a generator and not 25 hand-edited JSONs:

- Cross-framework equivalence claims need to stay consistent. If Vogler's "Crossing
  the First Threshold" maps to Campbell's "Crossing the First Threshold" and to
  Snyder's "Break Into Two", that claim should be expressed once and re-emitted
  into both files' cross_framework_equivalence blocks.
- All filters share the same JSON schema (documented in
  editor/story/_resources/macro_structure/macro_structure_README.md). Keeping the schema centralized
  in this script makes it easy to add fields later without 25 separate edits.
- The stage_id namespace is small (two-char prefix per framework). Keeping
  prefixes in one file prevents collisions.

Run:
    cd editor
    uv run python "story/_sidecar scripts/_build_story_filters.py"

Outputs to: editor/story/_resources/macro_structure/<framework_id>.json

Each filter follows the v1 schema documented in macro_structure/macro_structure_README.md:
- identity (framework_id, title, author, year, source)
- taxonomic placement (tradition, engine, act_envelope, resolution)
- lineage (predecessors, descendants, siblings, note)
- stages[] (stage_id, position, movement?, name, function)
- cross_framework_equivalence — keyed by other framework_id
- applicability (best_for, weak_for, limitations)
- project_relevance — ships null; fill with your application notes
"""

from __future__ import annotations
import json
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

OUT_DIR = Path(__file__).resolve().parent.parent / "_resources" / "macro_structure"
OUT_DIR.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
#  Framework definitions
# ---------------------------------------------------------------------------
#
#  Each framework returns a dict matching the v1 schema (see macro_structure/macro_structure_README.md).
#  Order is roughly the order in the macro_structure_guide.md so the
#  poster's argument is preserved in the registry.
# ---------------------------------------------------------------------------


def aristotle_poetics():
    return {
        "framework_id": "aristotle_poetics",
        "title": "Poetics",
        "author": "Aristotle",
        "year": -335,
        "source": "Poetics, c. 335 BCE.",
        "tradition": "dramatic",
        "engine": "external_conflict",
        "act_envelope": "three_act",
        "resolution": 3,
        "lineage": {
            "predecessors": [],
            "descendants": ["donatus_protasis", "horace_five_acts", "freytag_pyramid", "field_paradigm"],
            "siblings": [],
            "note": "The root of Western dramatic theory. Plot (mythos) is supreme over character; the engine is peripeteia (reversal) plus anagnorisis (recognition) producing catharsis. Aristotle also split plot into desis (complication/tying) and lysis (denouement/untying), the literal ancestor of 'rising action / falling action.'",
        },
        "stages": [
            {
                "stage_id": "ar01_beginning",
                "position": 1,
                "name": "Beginning (desis)",
                "function": "An action that does not itself necessarily follow from anything else, but from which something else naturally follows. Stable initial state with the seeds of complication.",
            },
            {
                "stage_id": "ar02_middle",
                "position": 2,
                "name": "Middle",
                "function": "Complication tightens. Includes peripeteia (reversal of fortune) and anagnorisis (recognition — a shift from ignorance to knowledge).",
            },
            {
                "stage_id": "ar03_end",
                "position": 3,
                "name": "End (lysis)",
                "function": "Catastrophe and resolution. The action follows necessarily from what came before and nothing else follows it. Catharsis is produced.",
            },
        ],
        "cross_framework_equivalence": {
            "freytag_pyramid": {
                "ar01_beginning": "fp01_exposition",
                "ar02_middle": "fp03_climax_apex",
                "ar03_end": "fp05_catastrophe",
            },
            "field_paradigm": {
                "ar01_beginning": "fd01_setup",
                "ar02_middle": "fd03_confrontation",
                "ar03_end": "fd07_resolution",
            },
            "snyder_save_the_cat": {
                "ar01_beginning": "sn03_setup",
                "ar02_middle": "sn09_midpoint",
                "ar03_end": "sn14_finale",
            },
        },
        "applicability": {
            "best_for": ["tragedy", "any_dramatic_form", "first_principles_review"],
            "weak_for": ["fine_grained_assembly_decisions", "documentary_observational"],
            "limitations": "3-stage resolution is too coarse for scene-by-scene editorial work. Useful for sanity-checking the macro shape, not for assembly.",
        },
        "project_relevance": None,  # your application notes — how this lens maps to YOUR film
    }


def horace_five_acts():
    return {
        "framework_id": "horace_five_acts",
        "title": "Ars Poetica — five-act rule",
        "author": "Horace",
        "year": -19,
        "source": "Ars Poetica, c. 19 BCE.",
        "tradition": "dramatic",
        "engine": "external_conflict",
        "act_envelope": "five_act",
        "resolution": 5,
        "lineage": {
            "predecessors": ["aristotle_poetics"],
            "descendants": ["freytag_pyramid", "yorke_into_the_woods"],
            "siblings": ["donatus_protasis"],
            "note": "Prescriptive Roman rule: a play should have 'neither less nor more than five acts.' Less a theory than a convention that hardened into law and recurs every time someone (Freytag, Yorke) wants a five-act spine.",
        },
        "stages": [
            {"stage_id": "ho01_act_i", "position": 1, "name": "Act I", "function": "Exposition; introduce protagonist and situation."},
            {"stage_id": "ho02_act_ii", "position": 2, "name": "Act II", "function": "Rising complications; the action thickens."},
            {"stage_id": "ho03_act_iii", "position": 3, "name": "Act III", "function": "Climax or turning point at the structural center."},
            {"stage_id": "ho04_act_iv", "position": 4, "name": "Act IV", "function": "Falling action; consequences play out."},
            {"stage_id": "ho05_act_v", "position": 5, "name": "Act V", "function": "Catastrophe or resolution; closure."},
        ],
        "cross_framework_equivalence": {
            "freytag_pyramid": {
                "ho01_act_i": "fp01_exposition",
                "ho02_act_ii": "fp02_rising_action",
                "ho03_act_iii": "fp03_climax_apex",
                "ho04_act_iv": "fp04_falling_action",
                "ho05_act_v": "fp05_catastrophe",
            },
            "yorke_into_the_woods": {
                "ho01_act_i": "yo01_act_i_setup",
                "ho02_act_ii": "yo02_act_ii_doubt",
                "ho03_act_iii": "yo03_act_iii_midpoint",
                "ho04_act_iv": "yo04_act_iv_regression",
                "ho05_act_v": "yo05_act_v_reawakening",
            },
        },
        "applicability": {
            "best_for": ["theatre", "tv_series_episode_structure", "classical_form"],
            "weak_for": ["short_films", "documentary"],
            "limitations": "Numerical convention rather than a theory of meaning. The five acts exist because Horace said so, not because the story demands it.",
        },
        "project_relevance": None,  # your application notes — how this lens maps to YOUR film
    }


def donatus_protasis():
    return {
        "framework_id": "donatus_protasis",
        "title": "Three-part Comedic Structure",
        "author": "Aelius Donatus",
        "year": 350,
        "source": "Commentary on Terence, 4th c. CE. Renaissance critics later inserted 'catastasis' between epitasis and catastrophe.",
        "tradition": "dramatic",
        "engine": "external_conflict",
        "act_envelope": "three_act",
        "resolution": 3,
        "lineage": {
            "predecessors": ["aristotle_poetics", "horace_five_acts"],
            "descendants": ["freytag_pyramid", "field_paradigm"],
            "siblings": [],
            "note": "Roman grammarian commenting on Terence; divided comedy into protasis (exposition), epitasis (rising complication), catastrophe (resolution). The three-act idea in embryo, predating Field by 1,600 years.",
        },
        "stages": [
            {"stage_id": "do01_protasis", "position": 1, "name": "Protasis", "function": "Exposition. Introduce the characters and situation."},
            {"stage_id": "do02_epitasis", "position": 2, "name": "Epitasis", "function": "Rising complication; entanglements multiply."},
            {"stage_id": "do03_catastrophe", "position": 3, "name": "Catastrophe", "function": "Resolution — for Donatus, the unraveling and outcome, not necessarily disaster."},
        ],
        "cross_framework_equivalence": {
            "aristotle_poetics": {
                "do01_protasis": "ar01_beginning",
                "do02_epitasis": "ar02_middle",
                "do03_catastrophe": "ar03_end",
            },
            "field_paradigm": {
                "do01_protasis": "fd01_setup",
                "do02_epitasis": "fd03_confrontation",
                "do03_catastrophe": "fd07_resolution",
            },
        },
        "applicability": {
            "best_for": ["comedy", "classical_drama"],
            "weak_for": ["fine_grained_screenwriting"],
            "limitations": "Originally for Terence's comedy; the 'catastrophe' has lost its Roman meaning (resolution) and become 'disaster' in English. Confusing on its own.",
        },
        "project_relevance": None,  # your application notes — how this lens maps to YOUR film
    }


def freytag_pyramid():
    return {
        "framework_id": "freytag_pyramid",
        "title": "Freytag's Pyramid",
        "author": "Gustav Freytag",
        "year": 1863,
        "source": "Die Technik des Dramas, 1863.",
        "tradition": "dramatic",
        "engine": "external_conflict",
        "act_envelope": "five_act",
        "resolution": 5,
        "lineage": {
            "predecessors": ["aristotle_poetics", "horace_five_acts"],
            "descendants": ["field_paradigm", "yorke_into_the_woods"],
            "siblings": ["donatus_protasis"],
            "note": "Built to describe five-act Classical and Shakespearean tragedy, NOT contemporary film. The pyramid's symmetry (equal fall after the central climax) fits almost no modern movie.",
        },
        "stages": [
            {"stage_id": "fp01_exposition", "position": 1, "name": "Exposition", "function": "Introduction of setting, characters, and initial conditions."},
            {"stage_id": "fp02_rising_action", "position": 2, "name": "Rising Action", "function": "Inciting incident (exciting force) plus escalating complications."},
            {"stage_id": "fp03_climax_apex", "position": 3, "name": "Climax (apex)", "function": "Structural center; turning point at maximum tension. For Freytag this is at the middle, NOT the end."},
            {"stage_id": "fp04_falling_action", "position": 4, "name": "Falling Action", "function": "Consequences of the central reversal play out; the protagonist's fate clarifies."},
            {"stage_id": "fp05_catastrophe", "position": 5, "name": "Catastrophe", "function": "Final outcome — disaster (tragedy) or restoration (comedy). What modern audiences usually call 'the climax.'"},
        ],
        "cross_framework_equivalence": {
            "_note": "Freytag's 'climax' ≠ modern 'climax.' His apex-climax is at the center (STC Midpoint / Field Midpoint / Vogler Ordeal). His 'catastrophe' is what modern usage calls the climax. Do not equate Freytag-climax with Snyder-Finale or Field-Climax.",
            "field_paradigm": {
                "fp03_climax_apex": "fd04_midpoint",
                "fp05_catastrophe": "fd06_climax",
            },
            "vogler_writers_journey": {
                "fp03_climax_apex": "v08_the_ordeal",
                "fp05_catastrophe": "v11_the_resurrection",
            },
            "snyder_save_the_cat": {
                "fp03_climax_apex": "sn09_midpoint",
                "fp05_catastrophe": "sn14_finale",
            },
        },
        "applicability": {
            "best_for": ["five_act_tragedy", "shakespeare", "classical_drama_review"],
            "weak_for": ["modern_film", "documentary", "asymmetric_arcs"],
            "limitations": "Demands symmetric rise and fall, which almost no modern movie obeys. Modern climaxes sit at ~90%, not at the center. Useful as a lens on tragedy; misleading as a screenwriting template.",
        },
        "project_relevance": None,  # your application notes — how this lens maps to YOUR film
    }


def kishotenketsu_four_part():
    return {
        "framework_id": "kishotenketsu_four_part",
        "title": "Kishōtenketsu — four-part structure",
        "author": "(classical East Asian; literary tradition)",
        "year": None,
        "source": "Classical Chinese/Japanese/Korean four-part structure (起承転結 qǐ-chéng-zhuǎn-hé). Originally a poetic form (quatrains); generalized to narrative.",
        "tradition": "non_western",
        "engine": "change",
        "act_envelope": "four_part",
        "resolution": 4,
        "lineage": {
            "predecessors": [],
            "descendants": [],
            "siblings": ["natyashastra_sandhi"],
            "note": "Independent tradition; not derived from Aristotle. Crucially: no conflict required. Engine is juxtaposition and recontextualization, not antagonism.",
        },
        "stages": [
            {"stage_id": "k01_ki", "position": 1, "name": "Ki (introduction)", "function": "Introduce the elements: a place, a person, a situation. No problem yet."},
            {"stage_id": "k02_sho", "position": 2, "name": "Shō (development)", "function": "Develop what was introduced; deepen, elaborate, accumulate detail. Still no conflict required."},
            {"stage_id": "k03_ten", "position": 3, "name": "Ten (twist)", "function": "An apparently unrelated new element appears. Disjunction, surprise, a left turn. The connection is not yet clear."},
            {"stage_id": "k04_ketsu", "position": 4, "name": "Ketsu (reconciliation)", "function": "The twist retroactively unites all four parts — the meaning of the whole becomes visible. Closure through synthesis, not resolution of conflict."},
        ],
        "cross_framework_equivalence": {
            "_note": "Cross-framework equivalences here are intentionally sparse. Kishōtenketsu's twist ('ten') has no clean Western analog — it's NOT a plot twist, NOT a reversal, NOT a midpoint complication. Forcing equivalences corrupts the framework's value.",
            "vogler_writers_journey": {
                "k02_sho": "v06_tests_allies_enemies",
            },
        },
        "applicability": {
            "best_for": ["documentary_observational", "ensemble_portraits", "essay_film", "no_conflict_stories", "slice_of_life"],
            "weak_for": ["thriller", "epic", "single_protagonist_transformation"],
            "limitations": "Western audiences trained on conflict-engine stories may read the 'ten' as a structural failure (random tangent) rather than a deliberate juxtaposition. Use sparingly with audiences unaccustomed to the form, or make the ketsu-reconciliation strong enough to retroactively justify the ten.",
        },
        "project_relevance": None,  # your application notes — how this lens maps to YOUR film
    }


def propp_morphology():
    return {
        "framework_id": "propp_morphology",
        "title": "Morphology of the Folktale",
        "author": "Vladimir Propp",
        "year": 1928,
        "source": "Morphology of the Folktale (1928); Russian Formalist analysis of ~100 Russian fairy tales.",
        "tradition": "mythic",
        "engine": "external_conflict",
        "act_envelope": "free",
        "resolution": 31,
        "lineage": {
            "predecessors": [],
            "descendants": ["campbell_monomyth"],
            "siblings": [],
            "note": "Russian Formalist; the scientific, bottom-up counterpart to Campbell's mystical, top-down monomyth. Direct ancestor of computational/structuralist story analysis. Propp extracted 31 narrative functions and 7 character roles. Stable: the SEQUENCE of functions, not the surface content.",
        },
        "stages": [
            {"stage_id": "pr01_absentation", "position": 1, "movement": "Preparation", "name": "Absentation", "function": "A member of the family leaves home."},
            {"stage_id": "pr02_interdiction", "position": 2, "movement": "Preparation", "name": "Interdiction", "function": "A prohibition is addressed to the hero."},
            {"stage_id": "pr03_violation", "position": 3, "movement": "Preparation", "name": "Violation", "function": "The prohibition is violated."},
            {"stage_id": "pr04_reconnaissance", "position": 4, "movement": "Preparation", "name": "Reconnaissance", "function": "The villain seeks information."},
            {"stage_id": "pr05_delivery", "position": 5, "movement": "Preparation", "name": "Delivery", "function": "The villain receives information about the victim."},
            {"stage_id": "pr06_trickery", "position": 6, "movement": "Preparation", "name": "Trickery", "function": "Villain attempts to deceive."},
            {"stage_id": "pr07_complicity", "position": 7, "movement": "Preparation", "name": "Complicity", "function": "Victim falls for trick or unknowingly helps."},
            {"stage_id": "pr08_villainy_lack", "position": 8, "movement": "Complication", "name": "Villainy or Lack", "function": "Villain causes harm OR a family member lacks something. (This is the inciting incident.)"},
            {"stage_id": "pr09_mediation", "position": 9, "movement": "Complication", "name": "Mediation", "function": "Misfortune made known; hero is dispatched."},
            {"stage_id": "pr10_counteraction", "position": 10, "movement": "Complication", "name": "Beginning Counteraction", "function": "Hero decides to act."},
            {"stage_id": "pr11_departure", "position": 11, "movement": "Complication", "name": "Departure", "function": "Hero leaves home."},
            {"stage_id": "pr12_first_function_donor", "position": 12, "movement": "Donor sequence", "name": "First Function of the Donor", "function": "Hero is tested, attacked, or interrogated; prepares to receive a magical agent."},
            {"stage_id": "pr13_hero_reaction", "position": 13, "movement": "Donor sequence", "name": "Hero's Reaction", "function": "Hero reacts to the donor's test."},
            {"stage_id": "pr14_receipt_magical_agent", "position": 14, "movement": "Donor sequence", "name": "Receipt of Magical Agent", "function": "Hero acquires the gift, helper, or knowledge."},
            {"stage_id": "pr15_guidance", "position": 15, "movement": "Donor sequence", "name": "Guidance", "function": "Hero is led to the object of search."},
            {"stage_id": "pr16_struggle", "position": 16, "movement": "Climax", "name": "Struggle", "function": "Hero and villain join in direct combat."},
            {"stage_id": "pr17_branding", "position": 17, "movement": "Climax", "name": "Branding", "function": "Hero is marked (wound, ring, scar)."},
            {"stage_id": "pr18_victory", "position": 18, "movement": "Climax", "name": "Victory", "function": "Villain is defeated."},
            {"stage_id": "pr19_liquidation", "position": 19, "movement": "Climax", "name": "Liquidation", "function": "Initial misfortune or lack is resolved."},
            {"stage_id": "pr20_return", "position": 20, "movement": "Return", "name": "Return", "function": "Hero returns."},
            {"stage_id": "pr21_pursuit", "position": 21, "movement": "Return", "name": "Pursuit", "function": "Hero is chased."},
            {"stage_id": "pr22_rescue", "position": 22, "movement": "Return", "name": "Rescue", "function": "Hero is rescued from pursuit."},
            {"stage_id": "pr23_unrecognized_arrival", "position": 23, "movement": "Recognition", "name": "Unrecognized Arrival", "function": "Hero arrives home or elsewhere, unrecognized."},
            {"stage_id": "pr24_unfounded_claims", "position": 24, "movement": "Recognition", "name": "Unfounded Claims", "function": "A false hero presents claims."},
            {"stage_id": "pr25_difficult_task", "position": 25, "movement": "Recognition", "name": "Difficult Task", "function": "A difficult task is proposed to the hero."},
            {"stage_id": "pr26_solution", "position": 26, "movement": "Recognition", "name": "Solution", "function": "The task is resolved."},
            {"stage_id": "pr27_recognition", "position": 27, "movement": "Recognition", "name": "Recognition", "function": "Hero is recognized by the mark."},
            {"stage_id": "pr28_exposure", "position": 28, "movement": "Recognition", "name": "Exposure", "function": "False hero or villain is exposed."},
            {"stage_id": "pr29_transfiguration", "position": 29, "movement": "Recognition", "name": "Transfiguration", "function": "Hero is given a new appearance."},
            {"stage_id": "pr30_punishment", "position": 30, "movement": "Recognition", "name": "Punishment", "function": "Villain is punished."},
            {"stage_id": "pr31_wedding", "position": 31, "movement": "Recognition", "name": "Wedding", "function": "Hero marries and ascends the throne (or equivalent reward)."},
        ],
        "cross_framework_equivalence": {
            "campbell_monomyth": {
                "pr11_departure": "c04_crossing_the_first_threshold",
                "pr14_receipt_magical_agent": "c05_belly_of_the_whale",
                "pr16_struggle": "c07_meeting_the_goddess",
                "pr27_recognition": "c16_master_of_two_worlds",
            },
        },
        "applicability": {
            "best_for": ["folktale_analysis", "screenplay_diagnostics", "computational_narrative", "ensemble_character_role_review"],
            "weak_for": ["modern_literary_fiction", "documentary"],
            "limitations": "Propp generalized from Russian fairy tales specifically; the 31-function sequence doesn't survive intact in stories outside that genre. Most useful as a checklist of FUNCTION TYPES rather than a strict sequence.",
        },
        "project_relevance": None,  # your application notes — how this lens maps to YOUR film
    }


def campbell_monomyth():
    return {
        "framework_id": "campbell_monomyth",
        "title": "The Hero with a Thousand Faces — Monomyth",
        "author": "Joseph Campbell",
        "year": 1949,
        "source": "The Hero with a Thousand Faces, 1949. Synthesized from Jung (archetypes, the collective unconscious), Frazer, and especially Van Gennep's rites of passage (1909).",
        "tradition": "mythic",
        "engine": "external_conflict",
        "act_envelope": "three_movement",
        "resolution": 17,
        "lineage": {
            "predecessors": ["propp_morphology"],
            "descendants": ["vogler_writers_journey", "harmon_story_circle", "watts_eight_point"],
            "siblings": [],
            "note": "Synthesized from Jung, Frazer, Van Gennep. Hugely influential, also fairly criticized for cherry-picking and flattening cultural specificity. Vogler's adaptation (1992) is what actually runs Hollywood.",
        },
        "stages": [
            {"stage_id": "c01_call_to_adventure_world", "position": 1, "movement": "Departure", "name": "The Call to Adventure", "function": "Hero is called to leave the ordinary world."},
            {"stage_id": "c02_refusal_of_the_call", "position": 2, "movement": "Departure", "name": "Refusal of the Call", "function": "Hero hesitates or refuses."},
            {"stage_id": "c03_supernatural_aid", "position": 3, "movement": "Departure", "name": "Supernatural Aid", "function": "A mentor or guide appears, often with a gift."},
            {"stage_id": "c04_crossing_the_first_threshold", "position": 4, "movement": "Departure", "name": "Crossing the First Threshold", "function": "Hero commits to the adventure; enters the special world."},
            {"stage_id": "c05_belly_of_the_whale", "position": 5, "movement": "Departure", "name": "Belly of the Whale", "function": "The final separation from the known world; metaphorical death."},
            {"stage_id": "c06_road_of_trials", "position": 6, "movement": "Initiation", "name": "The Road of Trials", "function": "A series of tests, challenges, and ordeals."},
            {"stage_id": "c07_meeting_the_goddess", "position": 7, "movement": "Initiation", "name": "Meeting with the Goddess", "function": "Hero encounters a powerful figure of love or unconditional acceptance."},
            {"stage_id": "c08_woman_as_temptress", "position": 8, "movement": "Initiation", "name": "Woman as Temptress", "function": "Temptation to abandon the quest. (Note: the name is Campbell's; the function is 'temptation toward easy gratification' and is not gendered.)"},
            {"stage_id": "c09_atonement_with_the_father", "position": 9, "movement": "Initiation", "name": "Atonement with the Father", "function": "Confrontation with the ultimate authority figure — often the source of the hero's fear."},
            {"stage_id": "c10_apotheosis", "position": 10, "movement": "Initiation", "name": "Apotheosis", "function": "Hero achieves a higher state of being or understanding."},
            {"stage_id": "c11_the_ultimate_boon", "position": 11, "movement": "Initiation", "name": "The Ultimate Boon", "function": "The goal of the quest is achieved."},
            {"stage_id": "c12_refusal_of_the_return", "position": 12, "movement": "Return", "name": "Refusal of the Return", "function": "Hero reluctant to return to the ordinary world."},
            {"stage_id": "c13_the_magic_flight", "position": 13, "movement": "Return", "name": "The Magic Flight", "function": "Escape with the boon, often pursued."},
            {"stage_id": "c14_rescue_from_without", "position": 14, "movement": "Return", "name": "Rescue from Without", "function": "Hero needs help to return; the ordinary world reaches in."},
            {"stage_id": "c15_crossing_the_return_threshold", "position": 15, "movement": "Return", "name": "The Crossing of the Return Threshold", "function": "Hero returns; integration of the two worlds."},
            {"stage_id": "c16_master_of_two_worlds", "position": 16, "movement": "Return", "name": "Master of Two Worlds", "function": "Hero comfortable in both special and ordinary worlds."},
            {"stage_id": "c17_freedom_to_live", "position": 17, "movement": "Return", "name": "Freedom to Live", "function": "Hero lives in the present, freed from fear of death; community is restored."},
        ],
        "cross_framework_equivalence": {
            "vogler_writers_journey": {
                "c01_call_to_adventure_world": "v02_call_to_adventure",
                "c02_refusal_of_the_call": "v03_refusal_of_the_call",
                "c03_supernatural_aid": "v04_meeting_the_mentor",
                "c04_crossing_the_first_threshold": "v05_crossing_the_first_threshold",
                "c05_belly_of_the_whale": "v05_crossing_the_first_threshold",
                "c06_road_of_trials": "v06_tests_allies_enemies",
                "c10_apotheosis": "v08_the_ordeal",
                "c11_the_ultimate_boon": "v09_the_reward",
                "c13_the_magic_flight": "v10_the_road_back",
                "c16_master_of_two_worlds": "v11_the_resurrection",
                "c17_freedom_to_live": "v12_return_with_the_elixir",
            },
            "harmon_story_circle": {
                "c04_crossing_the_first_threshold": "ha03_go",
                "c10_apotheosis": "ha05_find_it",
                "c17_freedom_to_live": "ha08_changed",
            },
        },
        "applicability": {
            "best_for": ["mythic_films", "epic_transformation", "fantasy", "adventure"],
            "weak_for": ["ensemble_documentary", "no_conflict_stories", "anti_heroic_narratives"],
            "limitations": "17 stages is too many for routine assembly. The gendered names ('Meeting with the Goddess', 'Woman as Temptress') are dated and read awkwardly today — translate them by FUNCTION not by NAME. Critics (notably feminist + cultural-studies scholarship) argue Campbell flattened culturally specific myths into one Western-coded shape.",
        },
        "project_relevance": None,  # your application notes — how this lens maps to YOUR film
    }


def harmon_story_circle():
    return {
        "framework_id": "harmon_story_circle",
        "title": "Story Circle (8-step)",
        "author": "Dan Harmon",
        "year": 2009,
        "source": "Developed for Community and Rick and Morty episode breaking, c. 2009. Documented in Harmon's Channel 101 essays.",
        "tradition": "mythic",
        "engine": "change",
        "act_envelope": "free",
        "resolution": 8,
        "lineage": {
            "predecessors": ["campbell_monomyth", "vogler_writers_journey"],
            "descendants": [],
            "siblings": ["watts_eight_point"],
            "note": "An 8-step radical simplification of Campbell built for fast, repeatable episode breaking. Its genius is compression: the monomyth you can hold in your head.",
        },
        "stages": [
            {"stage_id": "ha01_you", "position": 1, "name": "You", "function": "A character is in a zone of comfort."},
            {"stage_id": "ha02_need", "position": 2, "name": "Need", "function": "But they want something."},
            {"stage_id": "ha03_go", "position": 3, "name": "Go", "function": "They enter an unfamiliar situation."},
            {"stage_id": "ha04_search", "position": 4, "name": "Search", "function": "Adapt to it."},
            {"stage_id": "ha05_find_it", "position": 5, "name": "Find", "function": "Get what they wanted."},
            {"stage_id": "ha06_take", "position": 6, "name": "Take", "function": "Pay a heavy price for it."},
            {"stage_id": "ha07_return", "position": 7, "name": "Return", "function": "Return to their familiar situation."},
            {"stage_id": "ha08_changed", "position": 8, "name": "Change", "function": "Having changed."},
        ],
        "cross_framework_equivalence": {
            "vogler_writers_journey": {
                "ha01_you": "v01_ordinary_world",
                "ha03_go": "v05_crossing_the_first_threshold",
                "ha05_find_it": "v09_the_reward",
                "ha06_take": "v08_the_ordeal",
                "ha07_return": "v10_the_road_back",
                "ha08_changed": "v12_return_with_the_elixir",
            },
            "snyder_save_the_cat": {
                "ha03_go": "sn06_break_into_two",
                "ha05_find_it": "sn09_midpoint",
                "ha08_changed": "sn15_final_image",
            },
        },
        "applicability": {
            "best_for": ["tv_episode", "short_form", "scene_level_microstructure", "ensemble_with_rotating_protagonist"],
            "weak_for": ["feature_film_macrostructure", "documentary_long_form"],
            "limitations": "8 steps is too coarse for a 90-120 minute feature; works best inside Act II as a scene-level reusable shape. Fractal use is encouraged — Harmon applies the circle to scenes and to episodes simultaneously.",
        },
        "project_relevance": None,  # your application notes — how this lens maps to YOUR film
    }


def watts_eight_point():
    return {
        "framework_id": "watts_eight_point",
        "title": "Eight-Point Arc",
        "author": "Nigel Watts",
        "year": 1996,
        "source": "Writing a Novel and Getting Published, 1996.",
        "tradition": "mythic",
        "engine": "external_conflict",
        "act_envelope": "free",
        "resolution": 8,
        "lineage": {
            "predecessors": ["campbell_monomyth"],
            "descendants": [],
            "siblings": ["harmon_story_circle", "wells_seven_point"],
            "note": "A clean, novel-oriented restatement of the same monomyth spine. Distinctive contribution: 'Critical Choice' as an active decision, not a passive sweeping-along.",
        },
        "stages": [
            {"stage_id": "wa01_stasis", "position": 1, "name": "Stasis", "function": "The everyday world before the story starts."},
            {"stage_id": "wa02_trigger", "position": 2, "name": "Trigger", "function": "An event sets the protagonist's quest in motion."},
            {"stage_id": "wa03_quest", "position": 3, "name": "The Quest", "function": "The protagonist sets out to resolve the disturbance."},
            {"stage_id": "wa04_surprise", "position": 4, "name": "Surprise", "function": "Obstacles, challenges, and revelations along the way."},
            {"stage_id": "wa05_critical_choice", "position": 5, "name": "Critical Choice", "function": "The protagonist must actively choose — under pressure — between competing goods or evils. (Distinctive Watts emphasis.)"},
            {"stage_id": "wa06_climax", "position": 6, "name": "Climax", "function": "Highest stakes confrontation; consequences of the critical choice play out."},
            {"stage_id": "wa07_reversal", "position": 7, "name": "Reversal", "function": "The change brought about by the climax — a new state."},
            {"stage_id": "wa08_resolution", "position": 8, "name": "Resolution", "function": "A new stasis is established."},
        ],
        "cross_framework_equivalence": {
            "harmon_story_circle": {
                "wa01_stasis": "ha01_you",
                "wa02_trigger": "ha02_need",
                "wa03_quest": "ha03_go",
                "wa05_critical_choice": "ha06_take",
                "wa08_resolution": "ha08_changed",
            },
        },
        "applicability": {
            "best_for": ["novel", "character_decision_review", "moral_dilemma_stories"],
            "weak_for": ["pure_action", "documentary_without_protagonist_choice"],
            "limitations": "Hinges entirely on a single 'Critical Choice' — stories without a clear single decision moment fit poorly.",
        },
        "project_relevance": None,  # your application notes — how this lens maps to YOUR film
    }


def field_paradigm():
    return {
        "framework_id": "field_paradigm",
        "title": "The Paradigm",
        "author": "Syd Field",
        "year": 1979,
        "source": "Screenplay, 1979. Later refined with the Midpoint and two Pinches.",
        "tradition": "dramatic",
        "engine": "external_conflict",
        "act_envelope": "three_act",
        "resolution": 5,
        "lineage": {
            "predecessors": ["aristotle_poetics", "donatus_protasis"],
            "descendants": ["mckee_story", "seger_diagnostics", "snyder_save_the_cat", "hauge_two_journeys"],
            "siblings": [],
            "note": "The foundational modern screenwriting text. Everything prescriptive after this is a reaction to or elaboration of Field. Tied to page count (~120 pages = 120 minutes; Plot Point I near p.25, Plot Point II near p.85).",
        },
        "stages": [
            {"stage_id": "fd01_setup", "position": 1, "movement": "Act I", "name": "Set-Up", "function": "Establish protagonist, world, and dramatic premise. Pages 1-25."},
            {"stage_id": "fd02_plot_point_1", "position": 2, "movement": "Act I/II hinge", "name": "Plot Point 1", "function": "Page ~25. An event that spins the action into Act II and gives the story its new direction."},
            {"stage_id": "fd03_confrontation", "position": 3, "movement": "Act II", "name": "Confrontation", "function": "Protagonist faces escalating obstacles. The long middle. Pages 25-85."},
            {"stage_id": "fd04_midpoint", "position": 4, "movement": "Act II center", "name": "Midpoint", "function": "Page ~60. A major reversal or new piece of information that changes the protagonist's relationship to the goal. Flanked by two Pinches (~37 and ~75) that re-apply pressure."},
            {"stage_id": "fd05_plot_point_2", "position": 5, "movement": "Act II/III hinge", "name": "Plot Point 2", "function": "Page ~85. Another event that spins into Act III."},
            {"stage_id": "fd06_climax", "position": 6, "movement": "Act III", "name": "Climax", "function": "Highest-stakes confrontation. Page ~110-115."},
            {"stage_id": "fd07_resolution", "position": 7, "movement": "Act III", "name": "Resolution", "function": "Aftermath and new equilibrium. Pages 115-120."},
        ],
        "cross_framework_equivalence": {
            "snyder_save_the_cat": {
                "fd02_plot_point_1": "sn06_break_into_two",
                "fd04_midpoint": "sn09_midpoint",
                "fd05_plot_point_2": "sn13_break_into_three",
                "fd06_climax": "sn14_finale",
            },
            "vogler_writers_journey": {
                "fd02_plot_point_1": "v05_crossing_the_first_threshold",
                "fd04_midpoint": "v08_the_ordeal",
                "fd05_plot_point_2": "v10_the_road_back",
                "fd06_climax": "v11_the_resurrection",
            },
            "hauge_two_journeys": {
                "fd02_plot_point_1": "hg02_change_of_plans",
                "fd04_midpoint": "hg03_point_of_no_return",
                "fd05_plot_point_2": "hg04_major_setback",
                "fd06_climax": "hg05_climax",
            },
        },
        "applicability": {
            "best_for": ["feature_film", "screenplay_diagnostics", "page_count_pacing"],
            "weak_for": ["serial_tv", "documentary_without_protagonist", "experimental_form"],
            "limitations": "Tied to feature-page math; doesn't translate cleanly to TV or to documentaries that don't have a 120-minute target. The midpoint is real but the two Pinches are often forced.",
        },
        "project_relevance": None,  # your application notes — how this lens maps to YOUR film
    }


def gulino_sequence():
    return {
        "framework_id": "gulino_sequence",
        "title": "The Sequence Approach (8 sequences)",
        "author": "Paul Joseph Gulino",
        "year": 2004,
        "source": "Screenwriting: The Sequence Approach, 2004. A legacy of silent-era practice of shipping films on physical reels.",
        "tradition": "dramatic",
        "engine": "external_conflict",
        "act_envelope": "free",
        "resolution": 8,
        "lineage": {
            "predecessors": ["field_paradigm"],
            "descendants": [],
            "siblings": [],
            "note": "Argues the three-act model is too coarse and that films are really built from eight 10-15 minute sequences, each a mini-movie with its own tension and partial resolution. The most useful corrective to 'what do I do in the sagging middle of Act II.'",
        },
        "stages": [
            {"stage_id": "gu01_seq_a", "position": 1, "name": "Sequence A — Status quo + inciting incident", "function": "Establish protagonist and world; the inciting incident lands."},
            {"stage_id": "gu02_seq_b", "position": 2, "name": "Sequence B — Predicament and lock-in", "function": "Protagonist commits to a course of action; the new direction is locked in."},
            {"stage_id": "gu03_seq_c", "position": 3, "name": "Sequence C — First obstacle, raising stakes", "function": "Initial attempt to solve the problem fails; stakes raised."},
            {"stage_id": "gu04_seq_d", "position": 4, "name": "Sequence D — First culmination / midpoint", "function": "A second, partial culmination — what looked like the goal turns out to be something else, or the real problem becomes visible."},
            {"stage_id": "gu05_seq_e", "position": 5, "name": "Sequence E — Subplot and rising action", "function": "Subplot intensifies; the protagonist tries new tactics."},
            {"stage_id": "gu06_seq_f", "position": 6, "name": "Sequence F — Main culmination, end of Act II", "function": "Major setback / all is lost / point of decision."},
            {"stage_id": "gu07_seq_g", "position": 7, "name": "Sequence G — New tension and twist", "function": "Act III opens with new conditions; final approach to the climax."},
            {"stage_id": "gu08_seq_h", "position": 8, "name": "Sequence H — Resolution", "function": "Climax and aftermath."},
        ],
        "cross_framework_equivalence": {
            "field_paradigm": {
                "gu02_seq_b": "fd02_plot_point_1",
                "gu04_seq_d": "fd04_midpoint",
                "gu06_seq_f": "fd05_plot_point_2",
                "gu08_seq_h": "fd06_climax",
            },
        },
        "applicability": {
            "best_for": ["feature_film_assembly", "act_ii_diagnostics", "long_form_documentary", "reels_thinking"],
            "weak_for": ["short_form", "experimental"],
            "limitations": "Requires identifying eight clear local tension/resolution arcs; if the material has only three or four, forcing eight feels artificial.",
        },
        "project_relevance": None,  # your application notes — how this lens maps to YOUR film
    }


def mckee_story():
    return {
        "framework_id": "mckee_story",
        "title": "Story — Principles of Screenwriting",
        "author": "Robert McKee",
        "year": 1997,
        "source": "Story: Substance, Structure, Style, and the Principles of Screenwriting, 1997.",
        "tradition": "dramatic",
        "engine": "external_conflict",
        "act_envelope": "three_act",
        "resolution": 5,
        "lineage": {
            "predecessors": ["aristotle_poetics", "field_paradigm"],
            "descendants": [],
            "siblings": ["seger_diagnostics", "snyder_save_the_cat"],
            "note": "The philosophical heavyweight. Core concept: the GAP between expectation and result is where meaning is generated. Conflict is non-negotiable for McKee, which is exactly where Kishōtenketsu pushes back.",
        },
        "stages": [
            {"stage_id": "mc01_inciting_incident", "position": 1, "name": "Inciting Incident", "function": "Upsets the balance of life and triggers the spine. The protagonist now has an object of desire."},
            {"stage_id": "mc02_progressive_complications", "position": 2, "name": "Progressive Complications", "function": "Escalating forces of antagonism. Each complication harder than the last; the protagonist's tools become inadequate."},
            {"stage_id": "mc03_crisis", "position": 3, "name": "Crisis", "function": "The ultimate decision under maximum pressure. The protagonist must choose between irreconcilable goods or the lesser of two evils."},
            {"stage_id": "mc04_climax", "position": 4, "name": "Climax", "function": "The result of the crisis decision plays out; story value tips definitively."},
            {"stage_id": "mc05_resolution", "position": 5, "name": "Resolution", "function": "Aftermath and new equilibrium."},
        ],
        "cross_framework_equivalence": {
            "field_paradigm": {
                "mc01_inciting_incident": "fd02_plot_point_1",
                "mc03_crisis": "fd05_plot_point_2",
                "mc04_climax": "fd06_climax",
                "mc05_resolution": "fd07_resolution",
            },
            "snyder_save_the_cat": {
                "mc01_inciting_incident": "sn04_catalyst",
                "mc03_crisis": "sn12_dark_night_of_the_soul",
                "mc04_climax": "sn14_finale",
            },
        },
        "applicability": {
            "best_for": ["screenwriting_review", "moral_dilemma_films", "philosophical_assessment"],
            "weak_for": ["observational_documentary", "no_conflict_stories"],
            "limitations": "Insists on conflict as the engine; bad fit for material that resists antagonism (Kishōtenketsu's domain). McKee's seminars are also famous for being prescriptive about the 'crisis decision' moment — not every film has one.",
        },
        "project_relevance": None,  # your application notes — how this lens maps to YOUR film
    }


def seger_diagnostics():
    return {
        "framework_id": "seger_diagnostics",
        "title": "Making a Good Script Great",
        "author": "Linda Seger",
        "year": 1987,
        "source": "Making a Good Script Great, 1987.",
        "tradition": "dramatic",
        "engine": "external_conflict",
        "act_envelope": "three_act",
        "resolution": 5,
        "lineage": {
            "predecessors": ["field_paradigm"],
            "descendants": [],
            "siblings": ["mckee_story"],
            "note": "From the development/script-doctor side rather than the auteur side. Three acts, two turning points, a clear central question, and an emphasis on REWRITING as the real work. Where McKee is theory, Seger is diagnostics.",
        },
        "stages": [
            {"stage_id": "sg01_act_one_setup", "position": 1, "movement": "Act I", "name": "Setup", "function": "Establish protagonist, central question, world. End with a turning point that locks in the central question."},
            {"stage_id": "sg02_turning_point_1", "position": 2, "movement": "Act I/II hinge", "name": "Turning Point 1", "function": "Spins the action into Act II."},
            {"stage_id": "sg03_act_two_development", "position": 3, "movement": "Act II", "name": "Development", "function": "Pursue the central question through escalating obstacles. Subplot ('B-story') develops alongside."},
            {"stage_id": "sg04_turning_point_2", "position": 4, "movement": "Act II/III hinge", "name": "Turning Point 2", "function": "Spins the action into Act III; the central question is reframed."},
            {"stage_id": "sg05_act_three_resolution", "position": 5, "movement": "Act III", "name": "Resolution", "function": "Answer the central question. Climax and aftermath."},
        ],
        "cross_framework_equivalence": {
            "field_paradigm": {
                "sg02_turning_point_1": "fd02_plot_point_1",
                "sg04_turning_point_2": "fd05_plot_point_2",
            },
        },
        "applicability": {
            "best_for": ["script_diagnostics", "rewrite_passes", "central_question_clarification"],
            "weak_for": ["theory_first_work"],
            "limitations": "Doesn't add structural innovation over Field; the value is in the diagnostic vocabulary (central question, scene goal, rewrite pass).",
        },
        "project_relevance": None,  # your application notes — how this lens maps to YOUR film
    }


def snyder_save_the_cat():
    return {
        "framework_id": "snyder_save_the_cat",
        "title": "Save the Cat — 15-beat sheet",
        "author": "Blake Snyder",
        "year": 2005,
        "source": "Save the Cat! The Last Book on Screenwriting You'll Ever Need, 2005.",
        "tradition": "dramatic",
        "engine": "external_conflict",
        "act_envelope": "three_act",
        "resolution": 15,
        "lineage": {
            "predecessors": ["field_paradigm", "mckee_story"],
            "descendants": [],
            "siblings": ["hauge_two_journeys", "wells_seven_point"],
            "note": "The most prescriptive and most commercially dominant modern screenwriting framework. 15 beats with literal page numbers. Loved for usability, blamed for formulaic sameness. The B-story idea is worth stealing for any doc with parallel arcs.",
        },
        "stages": [
            {"stage_id": "sn01_opening_image", "position": 1, "movement": "Act I", "name": "Opening Image", "function": "First visual statement of the film's world and tone."},
            {"stage_id": "sn02_theme_stated", "position": 2, "movement": "Act I", "name": "Theme Stated", "function": "Theme voiced (often indirectly) so later payoffs can resonate."},
            {"stage_id": "sn03_setup", "position": 3, "movement": "Act I", "name": "Set-Up", "function": "Relationships, flaws, routines, stakes."},
            {"stage_id": "sn04_catalyst", "position": 4, "movement": "Act I", "name": "Catalyst", "function": "Inciting incident."},
            {"stage_id": "sn05_debate", "position": 5, "movement": "Act I", "name": "Debate", "function": "Hesitation; the question 'should I?'"},
            {"stage_id": "sn06_break_into_two", "position": 6, "movement": "Act II hinge", "name": "Break into Two", "function": "Commitment to Act II — the upside-down world."},
            {"stage_id": "sn07_b_story", "position": 7, "movement": "Act II", "name": "B Story", "function": "Relationship/theme parallel arc begins."},
            {"stage_id": "sn08_fun_and_games", "position": 8, "movement": "Act II", "name": "Fun and Games", "function": "The premise delivered — 'the promise of the premise.'"},
            {"stage_id": "sn09_midpoint", "position": 9, "movement": "Act II center", "name": "Midpoint", "function": "False victory or false defeat; stakes raised."},
            {"stage_id": "sn10_bad_guys_close_in", "position": 10, "movement": "Act II", "name": "Bad Guys Close In", "function": "External and internal pressure mounts."},
            {"stage_id": "sn11_all_is_lost", "position": 11, "movement": "Act II", "name": "All Is Lost", "function": "Lowest point; whiff of death."},
            {"stage_id": "sn12_dark_night_of_the_soul", "position": 12, "movement": "Act II", "name": "Dark Night of the Soul", "function": "Wallowing in defeat; the protagonist must change to survive."},
            {"stage_id": "sn13_break_into_three", "position": 13, "movement": "Act III hinge", "name": "Break into Three", "function": "Insight from B-story plus A-story converge into a new plan."},
            {"stage_id": "sn14_finale", "position": 14, "movement": "Act III", "name": "Finale", "function": "Climax — five-point payoff."},
            {"stage_id": "sn15_final_image", "position": 15, "movement": "Act III", "name": "Final Image", "function": "Mirror or contrast to the Opening Image — proof of change."},
        ],
        "cross_framework_equivalence": {
            "_note": "Equivalence with most other modern frameworks is well-established; see field_paradigm, mckee_story, vogler_writers_journey, hauge_two_journeys for the canonical cross-table.",
        },
        "applicability": {
            "best_for": ["commercial_features", "story_pitching", "page_count_planning", "documentary_long_form"],
            "weak_for": ["experimental", "anti_formula", "kishōtenketsu_material"],
            "limitations": "Famously prescriptive — applied rigidly, produces sameness. Treat the beats as places to ASK QUESTIONS, not slots to fill.",
        },
        "project_relevance": None,  # your application notes — how this lens maps to YOUR film
    }


def hauge_two_journeys():
    return {
        "framework_id": "hauge_two_journeys",
        "title": "Six-Stage Plot Structure with Two Journeys",
        "author": "Michael Hauge",
        "year": 1988,
        "source": "Writing Screenplays That Sell, 1988; The Hero's 2 Journeys (with Christopher Vogler), 2003.",
        "tradition": "dramatic",
        "engine": "external_conflict",
        "act_envelope": "free",
        "resolution": 6,
        "lineage": {
            "predecessors": ["field_paradigm"],
            "descendants": [],
            "siblings": ["snyder_save_the_cat"],
            "note": "Six-stage plot structure hinged on five turning points (Opportunity 10%, Change of Plans 25%, Point of No Return 50%, Major Setback 75%, Climax 90%). Real contribution: TWO PARALLEL JOURNEYS — the outer plot goal AND the inner arc from Identity (false self / mask) to Essence (true self).",
        },
        "stages": [
            {"stage_id": "hg01_setup", "position": 1, "movement": "Act I", "name": "Setup", "function": "Protagonist in Identity (the mask). Outer plot world established. Inner arc seeded — what the protagonist is hiding from."},
            {"stage_id": "hg02_change_of_plans", "position": 2, "movement": "Act I/II hinge", "name": "Change of Plans", "function": "10-25%. Opportunity, then a hard commitment. Identity begins to be threatened."},
            {"stage_id": "hg03_point_of_no_return", "position": 3, "movement": "Act II center", "name": "Point of No Return", "function": "~50%. Outer commitment irrevocable; inner self begins to emerge."},
            {"stage_id": "hg04_major_setback", "position": 4, "movement": "Act II late", "name": "Major Setback", "function": "~75%. Outer plot crisis; inner self is fully exposed."},
            {"stage_id": "hg05_climax", "position": 5, "movement": "Act III", "name": "Climax", "function": "~90%. Outer goal resolved (won or lost); inner journey completes — Essence revealed."},
            {"stage_id": "hg06_aftermath", "position": 6, "movement": "Act III", "name": "Aftermath", "function": "New equilibrium; protagonist is now living in Essence."},
        ],
        "cross_framework_equivalence": {
            "field_paradigm": {
                "hg02_change_of_plans": "fd02_plot_point_1",
                "hg03_point_of_no_return": "fd04_midpoint",
                "hg04_major_setback": "fd05_plot_point_2",
                "hg05_climax": "fd06_climax",
            },
            "snyder_save_the_cat": {
                "hg02_change_of_plans": "sn06_break_into_two",
                "hg03_point_of_no_return": "sn09_midpoint",
                "hg04_major_setback": "sn11_all_is_lost",
                "hg05_climax": "sn14_finale",
            },
            "vogler_writers_journey": {
                "hg02_change_of_plans": "v05_crossing_the_first_threshold",
                "hg03_point_of_no_return": "v08_the_ordeal",
                "hg04_major_setback": "v10_the_road_back",
                "hg05_climax": "v11_the_resurrection",
            },
        },
        "applicability": {
            "best_for": ["transformation_arcs", "character_driven_film", "documentary_with_protagonist", "dual_arc_films"],
            "weak_for": ["pure_plot_thrillers", "ensemble_without_protagonist"],
            "limitations": "The Identity/Essence framing assumes a single protagonist with a clear internal transformation. Ensemble films need separate Hauge passes per character.",
        },
        "project_relevance": None,  # your application notes — how this lens maps to YOUR film
    }


def truby_anatomy():
    return {
        "framework_id": "truby_anatomy",
        "title": "The Anatomy of Story — 22 Steps",
        "author": "John Truby",
        "year": 2007,
        "source": "The Anatomy of Story: 22 Steps to Becoming a Master Storyteller, 2007.",
        "tradition": "dramatic",
        "engine": "change",
        "act_envelope": "free",
        "resolution": 22,
        "lineage": {
            "predecessors": ["field_paradigm", "mckee_story"],
            "descendants": [],
            "siblings": [],
            "note": "The deliberate anti-template. 22 steps organized around the character's MORAL and PSYCHOLOGICAL NEED, not act breaks. Structure should grow organically from the protagonist's desire line and moral argument; rigid 3-act counting kills stories.",
        },
        "stages": [
            {"stage_id": "tr01_self_revelation_need_desire", "position": 1, "name": "Self-revelation, need, and desire", "function": "What the character will learn and the surface goal. Plant these first."},
            {"stage_id": "tr02_ghost_and_story_world", "position": 2, "name": "Ghost and story world", "function": "The past wound (ghost) and the present-day world that embodies it."},
            {"stage_id": "tr03_weakness_and_need", "position": 3, "name": "Weakness and need", "function": "Psychological and moral weaknesses. Moral need = a wrong the character does to others."},
            {"stage_id": "tr04_inciting_event", "position": 4, "name": "Inciting event", "function": "The event that disrupts and forces the desire to crystallize."},
            {"stage_id": "tr05_desire", "position": 5, "name": "Desire", "function": "The specific, concrete goal the protagonist now pursues."},
            {"stage_id": "tr06_ally_or_allies", "position": 6, "name": "Ally or allies", "function": "Companions; foils that highlight the protagonist's traits."},
            {"stage_id": "tr07_opponent_and_or_mystery", "position": 7, "name": "Opponent and/or mystery", "function": "A figure (or system) competing for the same goal."},
            {"stage_id": "tr08_fake_ally_opponent", "position": 8, "name": "Fake-ally opponent", "function": "A character who appears to help but secretly opposes."},
            {"stage_id": "tr09_first_revelation_decision", "position": 9, "name": "First revelation and decision", "function": "New information forces a changed plan."},
            {"stage_id": "tr10_plan", "position": 10, "name": "Plan", "function": "The strategy the protagonist will execute against the opponent."},
            {"stage_id": "tr11_opponents_plan_main_counterattack", "position": 11, "name": "Opponent's plan and main counterattack", "function": "Antagonist's strategy is revealed; first major counterattack."},
            {"stage_id": "tr12_drive", "position": 12, "name": "Drive", "function": "Series of actions in pursuit of the goal; protagonist may become increasingly immoral."},
            {"stage_id": "tr13_attack_by_ally", "position": 13, "name": "Attack by ally", "function": "An ally calls out the protagonist's flaws."},
            {"stage_id": "tr14_apparent_defeat", "position": 14, "name": "Apparent defeat", "function": "Major loss — appears unrecoverable."},
            {"stage_id": "tr15_second_revelation_decision_obsessive_drive_changed_desire", "position": 15, "name": "Second revelation and decision", "function": "New insight; obsessive drive replaces or transforms the original desire."},
            {"stage_id": "tr16_audience_revelation", "position": 16, "name": "Audience revelation", "function": "Audience learns something the protagonist doesn't yet know."},
            {"stage_id": "tr17_third_revelation_decision", "position": 17, "name": "Third revelation and decision", "function": "Another insight forces another plan change."},
            {"stage_id": "tr18_gate_gauntlet_visit_to_death", "position": 18, "name": "Gate, gauntlet, visit to death", "function": "Crossing into the climactic space; metaphorical death-encounter."},
            {"stage_id": "tr19_battle", "position": 19, "name": "Battle", "function": "Climactic confrontation."},
            {"stage_id": "tr20_self_revelation", "position": 20, "name": "Self-revelation", "function": "Protagonist sees their own moral need clearly — the heart of the story."},
            {"stage_id": "tr21_moral_decision", "position": 21, "name": "Moral decision", "function": "Acting on the self-revelation; the character chooses rightly under pressure."},
            {"stage_id": "tr22_new_equilibrium", "position": 22, "name": "New equilibrium", "function": "Aftermath; new state expresses the protagonist's transformation."},
        ],
        "cross_framework_equivalence": {
            "mckee_story": {
                "tr04_inciting_event": "mc01_inciting_incident",
                "tr14_apparent_defeat": "mc03_crisis",
                "tr19_battle": "mc04_climax",
                "tr20_self_revelation": "mc04_climax",
                "tr22_new_equilibrium": "mc05_resolution",
            },
        },
        "applicability": {
            "best_for": ["character_driven_film", "moral_argument_stories", "ensemble_documentary", "complex_subjects"],
            "weak_for": ["pure_plot_thrillers", "short_form"],
            "limitations": "22 steps is too many for routine assembly. Best used as a DIAGNOSTIC checklist: is the moral need present? Is there a self-revelation moment? Has the protagonist's desire transformed? Not as a slot-filling template.",
        },
        "project_relevance": None,  # your application notes — how this lens maps to YOUR film
    }


def wells_seven_point():
    return {
        "framework_id": "wells_seven_point",
        "title": "Seven-Point Story Structure",
        "author": "Dan Wells",
        "year": 2011,
        "source": "Dan Wells lecture series, c. 2011. Explicitly derived from Lester Dent's pulp-fiction master formula.",
        "tradition": "dramatic",
        "engine": "external_conflict",
        "act_envelope": "free",
        "resolution": 7,
        "lineage": {
            "predecessors": ["field_paradigm"],
            "descendants": [],
            "siblings": ["watts_eight_point", "harmon_story_circle"],
            "note": "Hook, Plot Turn 1, Pinch 1, Midpoint, Pinch 2, Plot Turn 2, Resolution. Distinctive method: plot BACKWARD from the resolution so the hook is the mirror-image of the ending.",
        },
        "stages": [
            {"stage_id": "we01_hook", "position": 1, "name": "Hook", "function": "Starting state — chosen to mirror or contrast the Resolution. Where the protagonist is at the beginning of their arc."},
            {"stage_id": "we02_plot_turn_1", "position": 2, "name": "Plot Turn 1", "function": "Disruption; the story is set in motion. End of Act I."},
            {"stage_id": "we03_pinch_1", "position": 3, "name": "Pinch 1", "function": "First pressure point — antagonist applies force; protagonist must commit further."},
            {"stage_id": "we04_midpoint", "position": 4, "name": "Midpoint", "function": "Protagonist shifts from reactive to proactive. The pivot."},
            {"stage_id": "we05_pinch_2", "position": 5, "name": "Pinch 2", "function": "Second pressure point — much harder than Pinch 1. The wheels start coming off."},
            {"stage_id": "we06_plot_turn_2", "position": 6, "name": "Plot Turn 2", "function": "Protagonist obtains the final piece needed to solve the central problem. End of Act II."},
            {"stage_id": "we07_resolution", "position": 7, "name": "Resolution", "function": "Final state — mirror or contrast to Hook. The arc completes."},
        ],
        "cross_framework_equivalence": {
            "field_paradigm": {
                "we02_plot_turn_1": "fd02_plot_point_1",
                "we04_midpoint": "fd04_midpoint",
                "we06_plot_turn_2": "fd05_plot_point_2",
            },
            "snyder_save_the_cat": {
                "we02_plot_turn_1": "sn06_break_into_two",
                "we04_midpoint": "sn09_midpoint",
                "we06_plot_turn_2": "sn13_break_into_three",
            },
        },
        "applicability": {
            "best_for": ["novelists_outlining", "pulp_fiction", "tight_genre", "endings_first_planning"],
            "weak_for": ["discovery_writing", "documentary_without_known_ending"],
            "limitations": "Backwards-planning requires knowing the ending — bad fit for documentary where the ending is discovered. But the Hook/Resolution mirror is still useful retrospectively in assembly.",
        },
        "project_relevance": None,  # your application notes — how this lens maps to YOUR film
    }


def yorke_into_the_woods():
    return {
        "framework_id": "yorke_into_the_woods",
        "title": "Into the Woods — Fractal Five-Act Change",
        "author": "John Yorke",
        "year": 2013,
        "source": "Into the Woods: A Five-Act Journey Into Story, 2013.",
        "tradition": "dramatic",
        "engine": "change",
        "act_envelope": "five_act",
        "resolution": 5,
        "lineage": {
            "predecessors": ["horace_five_acts", "freytag_pyramid", "field_paradigm", "campbell_monomyth"],
            "descendants": [],
            "siblings": [],
            "note": "The synthesizer. A BBC drama executive who argues every story is a FRACTAL five-act structure of CHANGE: the protagonist moves from no-knowledge through doubt, reluctance, and regression to reawakening and mastery — and the same shape recurs at the level of the whole film, the act, the scene, and the beat.",
        },
        "stages": [
            {"stage_id": "yo01_act_i_setup", "position": 1, "movement": "Act I", "name": "Act I — No Knowledge", "function": "Protagonist unaware of their flaw or true situation. The 'mask' is on."},
            {"stage_id": "yo02_act_ii_doubt", "position": 2, "movement": "Act II", "name": "Act II — Awakening / Doubt", "function": "Doubt about the old self enters; first attempts to act on new knowledge."},
            {"stage_id": "yo03_act_iii_midpoint", "position": 3, "movement": "Act III", "name": "Act III — Midpoint Knowledge / Acceptance", "function": "Protagonist accepts what they couldn't see before. Pivots from reactive to proactive."},
            {"stage_id": "yo04_act_iv_regression", "position": 4, "movement": "Act IV", "name": "Act IV — Reluctance / Regression", "function": "Backslide; the old self reasserts under pressure; deepest test."},
            {"stage_id": "yo05_act_v_reawakening", "position": 5, "movement": "Act V", "name": "Act V — Reawakening / Mastery", "function": "Knowledge integrated; mastery demonstrated; new equilibrium."},
        ],
        "cross_framework_equivalence": {
            "horace_five_acts": {
                "yo01_act_i_setup": "ho01_act_i",
                "yo02_act_ii_doubt": "ho02_act_ii",
                "yo03_act_iii_midpoint": "ho03_act_iii",
                "yo04_act_iv_regression": "ho04_act_iv",
                "yo05_act_v_reawakening": "ho05_act_v",
            },
        },
        "applicability": {
            "best_for": ["any_form_unified_theory", "fractal_review", "scene_level_diagnostics", "tv_series"],
            "weak_for": ["short_form_only"],
            "limitations": "The fractal claim is strong — verifying it at every level requires care. Best used as a lens that asks 'is THIS scene also a five-act change?'",
        },
        "project_relevance": None,  # your application notes — how this lens maps to YOUR film
    }


def vonnegut_shapes():
    return {
        "framework_id": "vonnegut_shapes",
        "title": "Shapes of Stories",
        "author": "Kurt Vonnegut",
        "year": 1947,
        "source": "From Vonnegut's rejected University of Chicago anthropology master's thesis (1947); popularized in lectures.",
        "tradition": "heuristic",
        "engine": "felt_trajectory",
        "act_envelope": "free",
        "resolution": None,
        "lineage": {
            "predecessors": [],
            "descendants": [],
            "siblings": ["kubler_ross_grief", "scientific_method"],
            "note": "Plots graphed as lines on a good-fortune / ill-fortune axis. Philosophy: emotional TRAJECTORY matters more than event taxonomy. The most useful lens for documentary because it's about felt shape, not plot mechanics.",
        },
        "stages": [
            {"stage_id": "vn01_man_in_hole", "position": 1, "name": "Man in Hole", "function": "Protagonist starts at neutral, falls into trouble, climbs out, ends higher than they started."},
            {"stage_id": "vn02_boy_meets_girl", "position": 2, "name": "Boy Meets Girl", "function": "Finds something wonderful, loses it, regains it."},
            {"stage_id": "vn03_cinderella", "position": 3, "name": "Cinderella", "function": "Step-rise (incremental gains), sudden collapse, then off-the-chart happiness."},
            {"stage_id": "vn04_from_bad_to_worse", "position": 4, "name": "From Bad to Worse", "function": "Starts low and gets worse — Kafka's territory."},
            {"stage_id": "vn05_creation_story", "position": 5, "name": "Creation Story", "function": "Starts in chaos / void and ends in stable order."},
            {"stage_id": "vn06_old_testament", "position": 6, "name": "Old Testament", "function": "Series of falls and partial recoveries; non-monotonic."},
            {"stage_id": "vn07_new_testament", "position": 7, "name": "New Testament", "function": "Creation Story plus a Cinderella spike at the end — off-the-chart happiness after order is established."},
        ],
        "cross_framework_equivalence": {
            "_note": "Vonnegut shapes are orthogonal to beat-ladder frameworks; they describe emotional trajectory and can be overlaid on any of them.",
        },
        "applicability": {
            "best_for": ["documentary", "emotional_pacing_review", "earned_descent_check", "non_western_material"],
            "weak_for": ["beat_by_beat_assembly"],
            "limitations": "Doesn't tell you WHAT scenes to use — only whether the cumulative emotional shape is what you wanted. A diagnostic, not a template.",
        },
        "project_relevance": None,  # your application notes — how this lens maps to YOUR film
    }


def scientific_method():
    return {
        "framework_id": "scientific_method",
        "title": "The Scientific Method as Story Shape",
        "author": "(borrowed framework)",
        "year": None,
        "source": "The standard scientific-inquiry sequence (problem → research → hypothesis → experiment → analysis → conclusion). Inclusion in the chart is semi-tongue-in-cheek but makes the deeper point explicit: inquiry has the same dramatic shape as story.",
        "tradition": "borrowed",
        "engine": "inquiry",
        "act_envelope": "free",
        "resolution": 6,
        "lineage": {
            "predecessors": [],
            "descendants": [],
            "siblings": ["kubler_ross_grief", "vonnegut_shapes"],
            "note": "Not narrative theory; an investigative shape borrowed for stories where the engine is question-and-discovery rather than antagonist-and-conflict. Genuinely useful for investigative and 'uncovering the truth' documentaries.",
        },
        "stages": [
            {"stage_id": "sm01_problem", "position": 1, "name": "Problem", "function": "Initial disturbance — an injustice, a mystery, an anomaly. The thing that demands explanation."},
            {"stage_id": "sm02_research", "position": 2, "name": "Research", "function": "Background gathering. Reading, interviewing, surveying the landscape."},
            {"stage_id": "sm03_hypothesis", "position": 3, "name": "Hypothesis", "function": "A tentative explanation forms. 'I think it's X because Y.'"},
            {"stage_id": "sm04_experiment", "position": 4, "name": "Experiment", "function": "Test the hypothesis. In documentary: confront a source, request a record, attempt an action that will produce evidence."},
            {"stage_id": "sm05_analysis", "position": 5, "name": "Analysis", "function": "Read the result. Did the experiment confirm or refute? What does it mean?"},
            {"stage_id": "sm06_conclusion", "position": 6, "name": "Conclusion", "function": "The answer to the original problem. The 'aha.' May be public-facing (publication / verdict) or internal (the protagonist now KNOWS)."},
        ],
        "cross_framework_equivalence": {
            "vonnegut_shapes": {
                "_note": "Scientific Method threads typically trace a 'Man in Hole' or 'Cinderella' shape — descent into uncertainty, then breakthrough.",
            },
        },
        "applicability": {
            "best_for": ["investigative_documentary", "mystery_thread", "uncovering_truth_films", "legal_revelation_stories"],
            "weak_for": ["pure_character_drama", "no_inquiry_required"],
            "limitations": "Only useful where there is a real INVESTIGATION — a question that begins ambiguous and resolves. Don't impose this on material that isn't shaped this way.",
        },
        "project_relevance": None,  # your application notes — how this lens maps to YOUR film
    }


def kubler_ross_grief():
    return {
        "framework_id": "kubler_ross_grief",
        "title": "Five Stages of Grief",
        "author": "Elisabeth Kübler-Ross",
        "year": 1969,
        "source": "On Death and Dying, 1969.",
        "tradition": "borrowed",
        "engine": "grief",
        "act_envelope": "free",
        "resolution": 5,
        "lineage": {
            "predecessors": [],
            "descendants": [],
            "siblings": ["scientific_method", "vonnegut_shapes"],
            "note": "Not a story theory at all; a clinical model of dying that writers co-opted as a ready-made emotional arc. Kübler-Ross herself never meant it as linear or universal, which is the standard caution. The chart sometimes adds an initiating 'trauma' beat.",
        },
        "stages": [
            {"stage_id": "kr00_trauma", "position": 0, "name": "Trauma", "function": "The precipitating loss or shock that initiates the process. (Some charts include this; original Kübler-Ross does not.)"},
            {"stage_id": "kr01_denial", "position": 1, "name": "Denial", "function": "Refusing to accept the loss. 'This isn't happening.'"},
            {"stage_id": "kr02_anger", "position": 2, "name": "Anger", "function": "Outrage at the situation, at others, at fate."},
            {"stage_id": "kr03_bargaining", "position": 3, "name": "Bargaining", "function": "Attempts to negotiate the loss away — pleading, deal-making."},
            {"stage_id": "kr04_depression", "position": 4, "name": "Depression", "function": "Recognition of the loss; withdrawal and despair."},
            {"stage_id": "kr05_acceptance", "position": 5, "name": "Acceptance", "function": "Integrating the loss; a new equilibrium that includes the loss."},
        ],
        "cross_framework_equivalence": {
            "_note": "Kübler-Ross stages don't map to plot frameworks; they map to emotional state of a specific character at a specific moment. Useful overlay, not equivalent vocabulary.",
        },
        "applicability": {
            "best_for": ["grief_arc_characters", "loss_thread", "documentary_following_bereavement"],
            "weak_for": ["plot_macrostructure"],
            "limitations": "Real grief is non-linear, recursive, and skips stages. Used rigidly, this framework misrepresents how loss actually feels. Use the vocabulary, not the order.",
        },
        "project_relevance": None,  # your application notes — how this lens maps to YOUR film
    }


def boston_fourfold_state():
    return {
        "framework_id": "boston_fourfold_state",
        "title": "Human Nature in Its Fourfold State",
        "author": "Thomas Boston",
        "year": 1720,
        "source": "Human Nature in Its Fourfold State, 1720.",
        "tradition": "borrowed",
        "engine": "salvation",
        "act_envelope": "four_part",
        "resolution": 4,
        "lineage": {
            "predecessors": [],
            "descendants": [],
            "siblings": ["augustinian_redemptive_history"],
            "note": "Scottish Calvinist theologian's four states of humanity. Adjacent on the chart to the Augustinian redemptive-historical arc. The Bible's macro-structure as four-act story (paradise, loss, redemption, restoration) is why redemption arcs feel archetypal.",
        },
        "stages": [
            {"stage_id": "bo01_primitive_integrity", "position": 1, "name": "Primitive Integrity", "function": "Original innocence / Eden state."},
            {"stage_id": "bo02_entire_depravity", "position": 2, "name": "Entire Depravity", "function": "The fall; fallen nature."},
            {"stage_id": "bo03_begun_recovery", "position": 3, "name": "Begun Recovery", "function": "Grace enters; partial restoration begins."},
            {"stage_id": "bo04_consummate_happiness", "position": 4, "name": "Consummate Happiness", "function": "Glory; full restoration."},
        ],
        "cross_framework_equivalence": {
            "augustinian_redemptive_history": {
                "bo01_primitive_integrity": "au01_creation",
                "bo02_entire_depravity": "au02_fall",
                "bo03_begun_recovery": "au03_salvation",
                "bo04_consummate_happiness": "au04_eternity",
            },
        },
        "applicability": {
            "best_for": ["redemption_arc_films", "religious_documentary", "spiritual_transformation"],
            "weak_for": ["secular_documentary_without_redemption_frame"],
            "limitations": "Explicitly theological; secularizing it changes its meaning.",
        },
        "project_relevance": None,  # your application notes — how this lens maps to YOUR film
    }


def augustinian_redemptive_history():
    return {
        "framework_id": "augustinian_redemptive_history",
        "title": "Augustinian Redemptive History",
        "author": "Augustine (drawn from his theological corpus)",
        "year": 410,
        "source": "Augustine of Hippo's four states of human will (posse peccare / non posse peccare etc.) and the Creation–Fall–Salvation–Eternity arc — the spine of the Christian metanarrative.",
        "tradition": "borrowed",
        "engine": "salvation",
        "act_envelope": "four_part",
        "resolution": 4,
        "lineage": {
            "predecessors": [],
            "descendants": ["boston_fourfold_state"],
            "siblings": [],
            "note": "The Bible's macro-structure read as a four-act story. The deepest single reason redemption arcs feel archetypal in Western storytelling.",
        },
        "stages": [
            {"stage_id": "au01_creation", "position": 1, "name": "Creation", "function": "Original order and goodness."},
            {"stage_id": "au02_fall", "position": 2, "name": "Fall", "function": "Rupture; sin enters; original order broken."},
            {"stage_id": "au03_salvation", "position": 3, "name": "Salvation", "function": "Redemption offered; broken order under repair."},
            {"stage_id": "au04_eternity", "position": 4, "name": "Eternity", "function": "Restored order; the new heaven and new earth."},
        ],
        "cross_framework_equivalence": {
            "boston_fourfold_state": {
                "au01_creation": "bo01_primitive_integrity",
                "au02_fall": "bo02_entire_depravity",
                "au03_salvation": "bo03_begun_recovery",
                "au04_eternity": "bo04_consummate_happiness",
            },
        },
        "applicability": {
            "best_for": ["redemption_arc_review", "metanarrative_diagnostics"],
            "weak_for": ["secular_observational"],
            "limitations": "Theological framework; using it editorially requires being aware of its baggage.",
        },
        "project_relevance": None,  # your application notes — how this lens maps to YOUR film
    }


def stage_musical_two_act():
    return {
        "framework_id": "stage_musical_two_act",
        "title": "Stage Musical Two-Act Structure",
        "author": "(theatrical convention)",
        "year": None,
        "source": "Theatrical-exhibition convention; structure bent around the practical need for an interval. The Act I 'button' and the Act II '11 o'clock number' are the canonical landmarks.",
        "tradition": "dramatic",
        "engine": "external_conflict",
        "act_envelope": "free",
        "resolution": 8,
        "lineage": {
            "predecessors": [],
            "descendants": [],
            "siblings": [],
            "note": "Form following the building, not the story — the act structure exists because the audience needs to use the bathroom. But the conventions (the strong Act I button, the late Act II low point) are real structural lessons.",
        },
        "stages": [
            {"stage_id": "sm01_normal_world", "position": 1, "movement": "Act I", "name": "Normal World", "function": "Opening number establishes setting and ensemble."},
            {"stage_id": "sm02_inciting_incident", "position": 2, "movement": "Act I", "name": "Inciting Incident", "function": "The 'I want' song; the disruption that launches the story."},
            {"stage_id": "sm03_point_of_no_return", "position": 3, "movement": "Act I", "name": "Point of No Return", "function": "Commitment; the Act I closing 'button' that sends audience to intermission wanting more."},
            {"stage_id": "sm04_intermission", "position": 4, "movement": "interval", "name": "Intermission", "function": "Audience break. The cliff-hanger has 15 minutes to land."},
            {"stage_id": "sm05_midpoint_resumes", "position": 5, "movement": "Act II", "name": "Midpoint Resumes", "function": "Act II opener; restate stakes, reorient the audience."},
            {"stage_id": "sm06_big_gloom", "position": 6, "movement": "Act II", "name": "The Big Gloom", "function": "All-is-lost moment; the late Act II low point. Often the '11 o'clock number.'"},
            {"stage_id": "sm07_climax_into_resolution", "position": 7, "movement": "Act II", "name": "Climax into Resolution", "function": "Final confrontation; the answer to the central question."},
            {"stage_id": "sm08_new_normal", "position": 8, "movement": "Act II", "name": "New Normal", "function": "Closing number; the new equilibrium."},
        ],
        "cross_framework_equivalence": {
            "snyder_save_the_cat": {
                "sm03_point_of_no_return": "sn06_break_into_two",
                "sm06_big_gloom": "sn11_all_is_lost",
                "sm07_climax_into_resolution": "sn14_finale",
            },
        },
        "applicability": {
            "best_for": ["stage_musicals", "long_runtime_films_with_natural_break", "tv_series_pilot_act_breaks"],
            "weak_for": ["short_form", "single_sitting_films"],
            "limitations": "Built around an exhibition convention that mostly doesn't apply to documentary distribution. The Act I button is still useful as a structural test ('does Act I end on a hook?').",
        },
        "project_relevance": None,  # your application notes — how this lens maps to YOUR film
    }


def vonnegut_shapes_alias():
    """vonnegut_shapes is already used as the framework_id; this is the alias used in project_beats.json.frameworks.vonnegut_shape."""
    return None  # not emitted — placeholder note


# ---------------------------------------------------------------------------
#  Beyond the poster — non-Western and abstract additions
# ---------------------------------------------------------------------------


def natyashastra_sandhi():
    return {
        "framework_id": "natyashastra_sandhi",
        "title": "Nāṭyaśāstra — Five Sandhis",
        "author": "Bharata Muni (attributed)",
        "year": 200,
        "source": "Nāṭyaśāstra, c. 200 BCE–200 CE. The foundational Sanskrit treatise on dramaturgy, performance, and aesthetics.",
        "tradition": "non_western",
        "engine": "rasa",
        "act_envelope": "five_act",
        "resolution": 5,
        "lineage": {
            "predecessors": [],
            "descendants": [],
            "siblings": ["kishotenketsu_four_part"],
            "note": "Independent of Aristotle and predates most Western theory. The five 'sandhis' (junctures) divide a play into segments oriented around RASA (aesthetic emotion / flavor) rather than plot conflict. The engine is the audience's emotional response, not the protagonist's struggle.",
        },
        "stages": [
            {"stage_id": "ns01_mukha_opening", "position": 1, "name": "Mukha — Opening juncture", "function": "Establish the seed of the action; introduce the principal flavor (rasa) that will dominate."},
            {"stage_id": "ns02_pratimukha_progression", "position": 2, "name": "Pratimukha — Progression juncture", "function": "Develop the seed; partial actions and rising involvement; obstacles emerge."},
            {"stage_id": "ns03_garbha_development", "position": 3, "name": "Garbha — Development juncture (the 'womb')", "function": "Concealment, hidden meaning, and apparent setbacks that gestate the resolution. Midpoint of psychological depth."},
            {"stage_id": "ns04_vimarsha_pause", "position": 4, "name": "Vimarśa — Reflection juncture", "function": "Pause for consideration; the protagonist (and audience) reflect on what has happened; emotional consolidation before the close."},
            {"stage_id": "ns05_nirvahana_conclusion", "position": 5, "name": "Nirvahana — Conclusion juncture", "function": "Resolution; the principal rasa is fully realized in the audience."},
        ],
        "cross_framework_equivalence": {
            "_note": "Rough alignment with Western five-act structures (Horace, Freytag, Yorke) but the engine is different. Don't equate Garbha with Freytag's climax-apex even though both sit at the structural center.",
            "yorke_into_the_woods": {
                "ns01_mukha_opening": "yo01_act_i_setup",
                "ns03_garbha_development": "yo03_act_iii_midpoint",
                "ns05_nirvahana_conclusion": "yo05_act_v_reawakening",
            },
        },
        "applicability": {
            "best_for": ["meditative_documentary", "rasa_oriented_films", "non_western_subject_matter", "essay_film"],
            "weak_for": ["thriller", "action", "plot_driven_genre"],
            "limitations": "Requires the editor to think in terms of dominant emotional flavors rather than plot beats. Hard to apply to films that audiences expect to follow Western conflict-engine conventions.",
        },
        "project_relevance": None,  # your application notes — how this lens maps to YOUR film
    }


def ma_negative_space():
    return {
        "framework_id": "ma_negative_space",
        "title": "Ma — Negative Space as Structure",
        "author": "(Japanese aesthetic tradition)",
        "year": None,
        "source": "The classical Japanese concept of 'ma' (間) — interval, pause, the meaningful gap between elements. A structural principle in traditional Japanese arts (architecture, ikebana, noh theater, Ozu's cinema).",
        "tradition": "non_western",
        "engine": "rhythm",
        "act_envelope": "free",
        "resolution": None,
        "lineage": {
            "predecessors": [],
            "descendants": [],
            "siblings": ["kishotenketsu_four_part"],
            "note": "Not a stage framework; a structural principle that argues meaning lives in the GAPS between events as much as in the events themselves. Ozu's 'pillow shots' (still images between scenes) and Tarkovsky's long takes are Western-readable analogs. Documentary editors can use ma to structure breath into a cut.",
        },
        "stages": [
            {"stage_id": "ma01_event", "position": 1, "name": "Event", "function": "An action, moment, or emotional beat."},
            {"stage_id": "ma02_ma_pause", "position": 2, "name": "Ma (interval)", "function": "The deliberate pause AFTER the event. Stillness, contemplation, an empty shot. The audience is given time to feel."},
            {"stage_id": "ma03_event", "position": 3, "name": "Event", "function": "Next action. Recontextualized by the preceding ma."},
        ],
        "cross_framework_equivalence": {
            "_note": "No equivalent in Western beat frameworks (which treat space-between-events as transition to be minimized, not content to be cultivated).",
        },
        "applicability": {
            "best_for": ["observational_documentary", "essay_film", "meditative_pacing", "trauma_processing_films"],
            "weak_for": ["thriller", "tight_plot_genre", "broadcast_television"],
            "limitations": "Audiences trained on dense Western pacing may read ma as 'slow' or 'boring' unless the surrounding context legitimizes the pause. Used sparingly, devastating; used heavily, alienating.",
        },
        "project_relevance": None,  # your application notes — how this lens maps to YOUR film
    }


def direct_cinema_observational():
    return {
        "framework_id": "direct_cinema_observational",
        "title": "Direct Cinema — Observational Mode",
        "author": "Maysles, Wiseman, Pennebaker, Drew (founding figures)",
        "year": 1960,
        "source": "1960s American documentary movement: Robert Drew, the Maysles brothers, Frederick Wiseman, D.A. Pennebaker. Sister tradition: French cinéma vérité (Rouch).",
        "tradition": "non_western",
        "engine": "observation",
        "act_envelope": "free",
        "resolution": None,
        "lineage": {
            "predecessors": [],
            "descendants": [],
            "siblings": ["mosaic_portrait"],
            "note": "Not a Western tradition geographically (it's American), but methodologically non-Western: rejects imposed narrative structure in favor of observed reality. The editor's role is to find shape in what was filmed, not to impose Snyder beats on it. Often called 'fly-on-the-wall.'",
        },
        "stages": [
            {"stage_id": "dc01_observation", "position": 1, "name": "Observation", "function": "What was actually present in the world during shooting. No imposed dramatic arc."},
            {"stage_id": "dc02_emergence", "position": 2, "name": "Emergence", "function": "Patterns, repetitions, and turns that the FOOTAGE reveals — not the ones the editor brings."},
            {"stage_id": "dc03_juxtaposition", "position": 3, "name": "Juxtaposition", "function": "Meaning produced by adjacency: two scenes next to each other generate understanding the editor didn't pre-script."},
            {"stage_id": "dc04_rhythm", "position": 4, "name": "Rhythm", "function": "Pacing arises from observed reality — durations of action, natural pauses — not from imposed beat slots."},
            {"stage_id": "dc05_release", "position": 5, "name": "Release", "function": "Closure comes from a moment that feels final in the world that was filmed, not from a beat-prescribed climax."},
        ],
        "cross_framework_equivalence": {
            "_note": "Direct cinema is methodologically opposed to imposed-structure frameworks. The frameworks below describe similar non-imposition philosophies, not equivalent stages.",
        },
        "applicability": {
            "best_for": ["observational_documentary", "verite_subject", "institutional_films", "essay_film"],
            "weak_for": ["legal_thriller_documentary_arc", "investigative_with_revelation", "documentaries_with_clear_protagonist_quest"],
            "limitations": "Documentary subjects with strong external structure (a trial, a campaign, a race) resist pure observation because the external structure imposes its own beats whether the editor wants them or not. Direct cinema works best when the SUBJECT itself doesn't have a built-in dramatic arc.",
        },
        "project_relevance": None,  # your application notes — how this lens maps to YOUR film
    }


def mosaic_portrait():
    return {
        "framework_id": "mosaic_portrait",
        "title": "Mosaic Portrait — Ensemble Without Protagonist",
        "author": "(implicit in films like Koyaanisqatsi, Manakamana, Faces Places; theorized by Bill Nichols, Michael Renov)",
        "year": None,
        "source": "Documentary tradition of building meaning from many small portraits rather than following a single protagonist's arc. Theoretical underpinning in Nichols's modes of documentary (poetic, expository, participatory, reflexive).",
        "tradition": "non_western",
        "engine": "accumulation",
        "act_envelope": "free",
        "resolution": None,
        "lineage": {
            "predecessors": [],
            "descendants": [],
            "siblings": ["direct_cinema_observational"],
            "note": "Where Western narrative wants one protagonist with an arc, mosaic portrait builds a collective picture from many small pieces. Each piece may not have an arc; the WHOLE acquires shape through accumulation, contrast, and pattern. Common in ensemble documentaries and films about communities.",
        },
        "stages": [
            {"stage_id": "mo01_tile", "position": 1, "name": "Tile", "function": "A small unit: a portrait, a vignette, an interaction. Self-contained, often without arc."},
            {"stage_id": "mo02_juxtaposition", "position": 2, "name": "Juxtaposition", "function": "Adjacent tiles generate meaning. Similarities, contrasts, echoes."},
            {"stage_id": "mo03_pattern", "position": 3, "name": "Pattern", "function": "Across many tiles, themes emerge. The audience starts to see what the film is about without being told."},
            {"stage_id": "mo04_completion", "position": 4, "name": "Completion", "function": "Final tile lands. The audience understands the picture as a whole."},
        ],
        "cross_framework_equivalence": {
            "_note": "Mosaic portrait is methodologically distinct from beat-driven frameworks; it shares philosophy with Kishōtenketsu (accumulation rather than conflict) and direct cinema (no imposed structure).",
        },
        "applicability": {
            "best_for": ["ensemble_documentary", "community_films", "tribute_films", "essay_films"],
            "weak_for": ["single_protagonist_quest", "legal_thriller", "investigative_with_clear_question"],
            "limitations": "Audiences trained on protagonist-driven story may feel a mosaic film 'doesn't go anywhere' until they recalibrate. Requires confident editorial restraint not to force a single thread.",
        },
        "project_relevance": None,  # your application notes — how this lens maps to YOUR film
    }


def dual_dramatic_question():
    return {
        "framework_id": "dual_dramatic_question",
        "title": "Dual Dramatic Question — Parallel A/B Arcs",
        "author": "(implicit in many ensemble films; theorized variously)",
        "year": None,
        "source": "The dual-dramatic-question structure: two stacked goals resolved in reverse order. Closest theoretical antecedent in Snyder's A-story/B-story logic, but where Snyder treats B as a relationship-and-theme subplot, dual dramatic question treats both A and B as substantive external goals.",
        "tradition": "hybrid",
        "engine": "external_conflict",
        "act_envelope": "free",
        "resolution": 6,
        "lineage": {
            "predecessors": ["snyder_save_the_cat", "hauge_two_journeys"],
            "descendants": [],
            "siblings": [],
            "note": "Two stacked dramatic questions (a global Problem A and a local Problem B; Solution B resolves before Solution A). Two parallel arcs that mutually inform each other; resolution of one provides the conditions or insight for resolving the other.",
        },
        "stages": [
            {"stage_id": "dq01_problem_a_global", "position": 1, "name": "Problem A — Global", "function": "The larger, systemic question. Often the one the audience can't immediately act on."},
            {"stage_id": "dq02_problem_b_local", "position": 2, "name": "Problem B — Local", "function": "The smaller, character-scale question. The one that gets the protagonist into the story."},
            {"stage_id": "dq03_pursuit_b", "position": 3, "name": "Pursuit of B", "function": "Protagonist pursues B; encounters A through that pursuit."},
            {"stage_id": "dq04_solution_b", "position": 4, "name": "Solution B", "function": "B is resolved — but in resolving B, A becomes both clearer and harder."},
            {"stage_id": "dq05_pursuit_a", "position": 5, "name": "Pursuit of A", "function": "With B resolved, protagonist now pursues A — armed with what B taught."},
            {"stage_id": "dq06_solution_a", "position": 6, "name": "Solution A", "function": "A is resolved, often differently than the protagonist expected. The nested structure makes both resolutions feel earned."},
        ],
        "cross_framework_equivalence": {
            "snyder_save_the_cat": {
                "_note": "Dual-DQ A-story ≈ Snyder A-story; Dual-DQ B-story ≈ Snyder B-story, but with B treated as a substantive external goal, not just a theme/relationship subplot.",
                "dq02_problem_b_local": "sn07_b_story",
                "dq04_solution_b": "sn09_midpoint",
                "dq06_solution_a": "sn14_finale",
            },
        },
        "applicability": {
            "best_for": ["legal_films", "investigative_documentary", "two_question_narratives", "personal_into_systemic"],
            "weak_for": ["single_question_films", "pure_character_drama"],
            "limitations": "Requires two genuinely substantive questions. If B is just a relationship subplot, use Snyder directly. If A and B aren't actually distinct, the structure collapses.",
        },
        "project_relevance": None,  # your application notes — how this lens maps to YOUR film
    }


def character_up_a_tree():
    return {
        "framework_id": "character_up_a_tree",
        "title": "Character Up a Tree (folk maxim)",
        "author": "(folk attribution disputed)",
        "year": None,
        "source": "Folk writing maxim: 'Put your character up a tree, throw rocks at him, get him down.' Variants include 'set the tree on fire.' Attributed variously to Mark Twain, James M. Cain, screenwriting teachers — no reliable single source.",
        "tradition": "heuristic",
        "engine": "external_conflict",
        "act_envelope": "three_act",
        "resolution": 3,
        "lineage": {
            "predecessors": ["aristotle_poetics"],
            "descendants": [],
            "siblings": [],
            "note": "The entire dramatic tradition compressed to a joke: establish a goal, escalate obstacles, resolve. Useful as a quick gut-check.",
        },
        "stages": [
            {"stage_id": "ut01_up_the_tree", "position": 1, "name": "Up the tree", "function": "Establish the protagonist's predicament — a clear goal or stuck position."},
            {"stage_id": "ut02_throw_rocks", "position": 2, "name": "Throw rocks", "function": "Escalate obstacles. McKee's 'progressive complications' in folk form."},
            {"stage_id": "ut03_get_them_down", "position": 3, "name": "Get them down", "function": "Resolve the situation. The protagonist either makes it down or doesn't — either way, the story ends."},
        ],
        "cross_framework_equivalence": {
            "aristotle_poetics": {
                "ut01_up_the_tree": "ar01_beginning",
                "ut02_throw_rocks": "ar02_middle",
                "ut03_get_them_down": "ar03_end",
            },
        },
        "applicability": {
            "best_for": ["gut_check", "pitching", "diagnosing_lack_of_pressure"],
            "weak_for": ["assembly_decisions", "fine_grained_work"],
            "limitations": "Folk maxim, not a craft tool. Best used as a smell-test: 'is anyone throwing any rocks?' If not, the middle is probably sagging.",
        },
        "project_relevance": None,  # your application notes — how this lens maps to YOUR film
    }


def comedy_structure():
    return {
        "framework_id": "comedy_structure",
        "title": "Comedy structure — modes, engines, and the dramatic inversion",
        "author": "Northrop Frye + the modern dramatic-comedy tradition",
        "year": 1957,
        "source": "Northrop Frye's Anatomy of Criticism (1957) for the mythos-of-spring comedy theory; modern instantiations from Lubitsch, Hawks, Christopher Guest, the Coen brothers, Adam McKay, Last Week Tonight. The poster ignores comedy almost entirely (it's a story-shape chart focused on dramatic conflict); this filter fills that gap.",
        "tradition": "dramatic",
        "engine": "foolishness_vs_virtue",
        "act_envelope": "free",
        "resolution": 7,
        "lineage": {
            "predecessors": ["aristotle_poetics", "donatus_protasis"],
            "descendants": [],
            "siblings": ["kishotenketsu_four_part"],
            "note": "Comedy as a STRUCTURAL mode (Frye), not a vibe. Comedy inverts the dramatic engine — instead of antagonist vs. protagonist, the engine is foolishness vs. virtue OR rigidity vs. flexibility. Comedy's ending is restoration / marriage / community renewed, not the dramatic ending of dragon-slain / hero-transformed-alone. Comedy and tragedy share the same beat positions (Aristotle's beginning-middle-end), but the value at each beat inverts. The seventh mode (`co07_meta_subversive_comedy`) is the modern outlier: comedy where the FORM itself is part of the joke, traced from Brecht through Mel Brooks to Deadpool and Fleabag.",
        },
        "stages": [
            {
                "stage_id": "co01_old_comedy",
                "position": 1,
                "movement": "ancient",
                "name": "Old Comedy (Aristophanes, c. 425 BCE)",
                "function": "Satirical political comedy. Targets the powerful through ridicule; comic engine is exposure of folly. Modern echoes: Last Week Tonight, The Daily Show, satirical documentary in the Adam McKay vein.",
            },
            {
                "stage_id": "co02_new_comedy",
                "position": 2,
                "movement": "ancient",
                "name": "New Comedy (Menander → Plautus → Terence, c. 320 BCE-160 BCE)",
                "function": "Domestic and romantic comedy. The 'boy meets girl, obstacles, marriage' template. Engine is order vs. obstacle to order (usually parental, sometimes social). Source of essentially all modern romantic comedy.",
            },
            {
                "stage_id": "co03_romantic_comedy",
                "position": 3,
                "movement": "early_modern",
                "name": "Romantic Comedy (Shakespeare, c. 1595-1605)",
                "function": "New Comedy elaborated with the romantic-pastoral and the disguise plot. Characters leave the city for the green world (forest, country), undergo transformation, return to a restored society. Engine: love overcomes social or familial obstacle. Modern: When Harry Met Sally and descendants.",
            },
            {
                "stage_id": "co04_comedy_of_manners",
                "position": 4,
                "movement": "modern",
                "name": "Comedy of Manners (Restoration → Wilde → Whit Stillman)",
                "function": "Comedy generated by social rules and the cost of violating them. Characters defined by class register, vocabulary, manners. Engine: social code vs. individual desire. Modern documentary instances: the cringe-from-class-collision moments in many docuseries.",
            },
            {
                "stage_id": "co05_dark_comedy",
                "position": 5,
                "movement": "modern",
                "name": "Dark Comedy / Black Humor (Kubrick, Coens, Beckett)",
                "function": "Comedy that lives in proximity to grief, violence, or absurdity. The laugh is uncomfortable; the discomfort IS the point. Strangelove, Fargo, Burn After Reading. Documentary instance: Tickled, the parts of Tiger King that work, much of Errol Morris.",
            },
            {
                "stage_id": "co06_dramatic_comedy",
                "position": 6,
                "movement": "modern",
                "name": "Dramatic Comedy (Apatow, Payne, McKay)",
                "function": "Films and docs with serious dramatic stakes that use comedy as both pressure-release and editorial argument. Sideways, The Big Short, Don't Look Up, Last Week Tonight as documentary form. Fits films with serious stakes delivered through comic absurdity.",
            },
            {
                "stage_id": "co07_meta_subversive_comedy",
                "position": 7,
                "movement": "postmodern",
                "name": "Meta-Subversive Comedy (Brooks → Python → Deadpool → Fleabag)",
                "function": "Comedy where the FORM itself is part of the joke. The work knows it's a work, the genre is mocked from within the genre, and the audience is acknowledged as audience. Traces from Brecht's Verfremdungseffekt through Mel Brooks (Blazing Saddles, Spaceballs, Young Frankenstein), Monty Python (the show interrupting itself), Ferris Bueller's direct address, Scream and Cabin in the Woods (slasher commenting on slashers), Community / 30 Rock / Modern Family (TV mocking TV form), Fleabag (the look-to-camera), Deadpool (superhero film mocking superhero films). Three intensities: light (Mel Brooks acknowledgment-wink the audience enjoys), medium (Fleabag-style sustained fourth-wall complicity), heavy (full Deadpool genre-violation). The intensity is the editorial dial.",
            },
        ],
        "cross_framework_equivalence": {
            "_note": "Comedy inverts specific dramatic-tradition beats. The Save-the-Cat moment in dramatic mode is a likability beat; in comic mode it's a banana-peel-and-pratfall moment. All Is Lost in drama is loss; in comedy it's humiliation. Finale in drama is dragon-slain; in comedy it's wedding-or-community-restored. The mapping is structural inversion, not equivalence.",
            "aristotle_poetics": {
                "co06_dramatic_comedy": "ar03_end",
            },
            "kishotenketsu_four_part": {
                "co04_comedy_of_manners": "k03_ten",
                "co05_dark_comedy": "k03_ten",
            },
        },
        "applicability": {
            "best_for": ["dramatic_comedy_features", "satirical_documentary", "stranger_than_fiction_docs", "stories_with_absurd_real_elements", "films_where_seriousness_alone_would_be_grim"],
            "weak_for": ["pure_tragedy", "verite_observational_without_imposed_tone", "films_where_subjects_should_not_be_made_to_seem_ridiculous"],
            "limitations": "Comedy requires editorial JUDGMENT calls about which moments are absurd and which are sincere. The same footage can be cut as either; the editor's choice is the tone. Mishandled comedy can read as cruelty (when the subject doesn't deserve ridicule) or as undermining stakes (when the audience stops believing the seriousness matters). The 'dramatic comedy' mode specifically is the hardest to pull off because it requires holding BOTH tones simultaneously.",
        },
        "project_relevance": None,  # your application notes — how this lens maps to YOUR film
    }


def arndt_endings():
    return {
        "framework_id": "arndt_endings",
        "title": "Endings — The Good, The Bad, and the Insanely Great",
        "author": "Michael Arndt",
        "year": 2014,
        "source": "Lecture/essay 'Endings: The Good, The Bad, and the Insanely Great' (Pixar talk + Aerogramme Writers' Studio essay, c. 2014). Arndt wrote Little Miss Sunshine (2006), Toy Story 3 (2010), and shares story credit on Star Wars: The Force Awakens (2015).",
        "tradition": "dramatic",
        "engine": "external_conflict",
        "act_envelope": "free",
        "resolution": 3,
        "lineage": {
            "predecessors": ["mckee_story", "truby_anatomy", "hauge_two_journeys"],
            "descendants": [],
            "siblings": ["dual_dramatic_question"],
            "note": "Diagnostic specifically for endings. Argues great endings layer THREE climaxes that detonate in rapid sequence: external (the plot's stated goal), internal (the character's wound/lack), and philosophical (the worldview the antagonist embodied being overturned or vindicated). The 'insanely great' ending fires all three in the same moment.",
        },
        "stages": [
            {
                "stage_id": "ae01_external_climax",
                "position": 1,
                "name": "External climax",
                "function": "The protagonist achieves or definitively fails the STATED goal — the plot question the audience has been tracking since Act I. This is the surface answer. In Toy Story 3 it's escaping the incinerator. In Little Miss Sunshine it's the pageant performance. Crucially, the protagonist often REJECTS this goal at the crisis moment in order to win the internal — and the rejection IS the win.",
            },
            {
                "stage_id": "ae02_internal_climax",
                "position": 2,
                "name": "Internal climax",
                "function": "The protagonist resolves the internal wound or lack that was hidden from them at the start. Hauge's Identity-to-Essence move, Truby's self-revelation. This climax is invisible without the external climax to land it against — the audience reads internal change BY watching what the protagonist does at the moment of external crisis.",
            },
            {
                "stage_id": "ae03_philosophical_climax",
                "position": 3,
                "name": "Philosophical climax",
                "function": "The antagonist embodied an opposing worldview; the antagonist's defeat (or surrender) is the defeat of that worldview. Star Wars: the Empire's order-through-force loses to the Rebellion's freedom-through-community. Toy Story 3: Lotso's self-protective bitterness loses to Woody's loyalty. Without this layer, the ending is plot resolution but not meaning resolution; the audience leaves entertained but not changed.",
            },
        ],
        "cross_framework_equivalence": {
            "_note": "Arndt's three climaxes overlap with Truby's late steps (self-revelation, moral decision, new equilibrium) and Hauge's outer/inner journey resolution, but Arndt's distinctive contribution is the THIRD (philosophical) climax and the requirement that all three fire in tight sequence.",
            "truby_anatomy": {
                "ae01_external_climax": "tr19_battle",
                "ae02_internal_climax": "tr20_self_revelation",
                "ae03_philosophical_climax": "tr21_moral_decision",
            },
            "hauge_two_journeys": {
                "ae01_external_climax": "hg05_climax",
                "ae02_internal_climax": "hg05_climax",
            },
            "mckee_story": {
                "ae01_external_climax": "mc04_climax",
                "ae02_internal_climax": "mc04_climax",
            },
        },
        "applicability": {
            "best_for": ["features_with_clear_ending", "transformation_arcs", "character_studies_with_philosophical_stakes", "documentary_with_a_resolution"],
            "weak_for": ["observational_documentary", "open_ended_films", "ensemble_without_protagonist"],
            "limitations": "Requires three things to be ALREADY PLANTED in Act I: a stated goal, an internal lack, and an antagonist with a coherent opposing worldview. If any of the three are missing at setup, the corresponding climax cannot land. Arndt's diagnostic is therefore as much about Act I planting as about Act III payoff. Also assumes a single protagonist — ensemble films need separate three-climax passes per character.",
        },
        "project_relevance": None,  # your application notes — how this lens maps to YOUR film
    }


# ---------------------------------------------------------------------------
#  Emit
# ---------------------------------------------------------------------------


BUILDERS = [
    aristotle_poetics,
    horace_five_acts,
    donatus_protasis,
    freytag_pyramid,
    kishotenketsu_four_part,
    propp_morphology,
    campbell_monomyth,
    harmon_story_circle,
    watts_eight_point,
    field_paradigm,
    gulino_sequence,
    mckee_story,
    seger_diagnostics,
    snyder_save_the_cat,
    hauge_two_journeys,
    truby_anatomy,
    wells_seven_point,
    yorke_into_the_woods,
    vonnegut_shapes,
    scientific_method,
    kubler_ross_grief,
    boston_fourfold_state,
    augustinian_redemptive_history,
    stage_musical_two_act,
    natyashastra_sandhi,
    ma_negative_space,
    direct_cinema_observational,
    mosaic_portrait,
    dual_dramatic_question,
    character_up_a_tree,
    arndt_endings,
    comedy_structure,
]


def main():
    print(f"Writing filters to: {OUT_DIR}")
    written = []
    for builder in BUILDERS:
        data = builder()
        if data is None:
            continue
        fid = data["framework_id"]
        path = OUT_DIR / f"{fid}.json"
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write("\n")
        n_stages = len(data.get("stages", []))
        n_eq = sum(1 for k in data.get("cross_framework_equivalence", {}) if not k.startswith("_"))
        print(f"  {fid:<38} stages={n_stages:>2}  cross-eq={n_eq}")
        written.append(fid)
    print(f"\nWrote {len(written)} filters.")
    return written


if __name__ == "__main__":
    main()
