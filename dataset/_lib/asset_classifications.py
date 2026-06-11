"""
Catalog asset classifications (video + audio): editorial bucket + capture/type tag.

`asset_classifications` shape:
  { "bucket": str, "type": str }

`bucket` replaces legacy `asset_bucket`:
  third_party | in_house_priority_ht | in_house_other

`type` (path-derived):
  b_roll | interview | timelapse | archival | third_party | verite | court_recordings

Adapting to your corpus: every `<placeholder>` string in this module (and its
siblings, e.g. `timeline_date.py`) marks a corpus-specific folder, shoot, or
subject name from the original project. The surrounding logic is the reusable
part — replace the placeholders with your own folder-tree names (matching is
against the lowercased source path) and the classifiers work unchanged.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

THIRD_PARTY_TOP_BUCKETS = frozenset({"<news-clips-folder>", "<podcasts-folder>"})  # your third-party top-level folders

ASSET_BUCKET_THIRD_PARTY = "third_party"
ASSET_BUCKET_IN_HOUSE_PRIORITY_HT = "in_house_priority_ht"
ASSET_BUCKET_IN_HOUSE_OTHER = "in_house_other"

ASSET_TYPE_B_ROLL = "b_roll"
ASSET_TYPE_INTERVIEW = "interview"
ASSET_TYPE_TIMELAPSE = "timelapse"
ASSET_TYPE_ARCHIVAL = "archival"
ASSET_TYPE_THIRD_PARTY = "third_party"
ASSET_TYPE_VERITE = "verite"
ASSET_TYPE_COURT_RECORDINGS = "court_recordings"


def _norm_path(source_path: str) -> str:
    return (source_path or "").replace("/", "\\").strip().lower()


SOURCE_TREE_DIRNAME = "Project"  # your RAID source-tree root folder (one level under the drive)


def parse_source_tree_rel(full_path: str) -> str:
    """Relative path under the source tree, lowercased ``\\``-separated, no drive prefix."""
    fp = (full_path or "").replace("/", "\\")
    m = re.match(r"^([a-z]:)\\" + re.escape(SOURCE_TREE_DIRNAME) + r"(?:\\(.*))?$", fp, re.IGNORECASE)
    if not m:
        return ""
    rest = (m.group(2) or "").rstrip("\\")
    return rest.lower() if rest else ""


def top_bucket_from_source_path(source_path: str) -> str:
    gp_rel = parse_source_tree_rel(source_path)
    if gp_rel:
        return gp_rel.split("\\", 1)[0]
    fp = (source_path or "").replace("/", "\\")
    m = re.match(r"^[a-z]:\\([^\\]+)", fp, re.IGNORECASE)
    return (m.group(1) or "").lower() if m else ""


def load_human_transcript_asset_ids(root: Path | None = None) -> set[str]:
    root = root or Path(__file__).resolve().parent.parent
    path = root / "assets/_human transcripts/index.jsonl"
    ids: set[str] = set()
    if not path.is_file():
        return ids
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        aid = rec.get("asset_id")
        if isinstance(aid, str) and len(aid) == 64:
            ids.add(aid)
        for x in rec.get("htr_linked_asset_ids") or []:
            if isinstance(x, str) and len(x) == 64:
                ids.add(x)
    return ids


def _path_indicates_third_party_podcast(source_path: str) -> bool:
    """True when media lives under a podcast-distribution folder (not in-house interview)."""
    pl = _norm_path(source_path)
    if not pl:
        return False
    for seg in pl.split("\\"):
        if seg == "<podcasts-folder>":
            return True
        if seg in ("podcast", "podcasts"):
            return True
        if seg == "adobe podcast":
            return True
    return False


def classify_asset_bucket(asset_id: str, source_path: str, human_ids: set[str]) -> str:
    if _path_indicates_third_party_podcast(source_path or ""):
        return ASSET_BUCKET_THIRD_PARTY
    bucket = top_bucket_from_source_path(source_path or "")
    for tb in THIRD_PARTY_TOP_BUCKETS:
        if tb.casefold() == bucket.casefold():
            return ASSET_BUCKET_THIRD_PARTY
    if str(asset_id) in human_ids:
        return ASSET_BUCKET_IN_HOUSE_PRIORITY_HT
    return ASSET_BUCKET_IN_HOUSE_OTHER


def _matches_explicit_b_roll_paths(pl: str) -> bool:
    """Editorial note: specific shoots tagged as b_roll (path substring, normalized lower). Placeholders — set to your corpus's folder names."""
    if "<broll-shoot-a>" in pl:
        return True
    if "<broll-shoot-b>" in pl:
        return True
    if "<broll-shoot-c>" in pl:
        return True
    if "<broll-shoot-d>" in pl:
        return True
    if "<broll-shoot-e>" in pl or "<broll-shoot-e-misspelled>" in pl:
        return True
    if "<dated-news-shoot>" in pl:
        return True
    if re.search(r"\\[^\\]*_dogs\\", pl):
        return True
    if "stock vids" in pl:
        return True
    if "<travel-broll-folder>" in pl:
        return True
    return False


def _matches_court_recordings(pl: str) -> bool:
    if "<trial-audio-folder>" in pl:
        return True
    if "<hearing-audio-folder>" in pl:
        return True
    return False


def _matches_b_roll(pl: str) -> bool:
    if _matches_explicit_b_roll_paths(pl):
        return True
    if "b-roll" in pl or "b roll" in pl:
        return True
    if re.search(r"(^|[\\/_.-])broll([\\/_.-]|\.|$)", pl):
        return True
    return False


def _matches_interview(pl: str) -> bool:
    if re.search(r"\binterviews?\b", pl):
        return True
    if " int " in pl:
        return True
    # Corpus-specific interview shoots (placeholders — set to your own folder
    # names). Two shapes shown: a plain subject-name substring, and dated-shoot
    # regexes tolerating -/_ separators and zero-padded months.
    if "<interview-subject-a>" in pl:
        return True
    if re.search(r"2024[-_]0?9[-_]26[-_ ]<subject-a>", pl):
        return True
    if re.search(r"2025[-_]0?4[-_]0?8[-_ ]<subject-b>", pl):
        return True
    if re.search(r"2025[-_]7[-_]24[-_ ]<subject-c>", pl) or "<subject-c> int" in pl:
        return True
    if re.search(r"2025[-_]8[-_]8[-_ ]<subject-d>", pl):
        return True
    if re.search(r"2023[-_]8[-_]5[-_ ]<subject-e>", pl):
        return True
    return False


def _matches_timelapse(pl: str) -> bool:
    if "timelapse" in pl:
        return True
    if "time-lapse" in pl or "time lapse" in pl:
        return True
    return False


def _matches_archival(pl: str) -> bool:
    if "archival" in pl:
        return True
    if re.search(r"\bhistory\b", pl):
        return True
    if "<childhood-archival-folder>" in pl:
        return True
    return False


def _matches_type_third_party(pl: str) -> bool:
    markers = (
        "<news-clips-folder>",
        "<podcasts-folder>",
    )
    if any(m in pl for m in markers):
        return True
    if re.search(r"\bpodcasts?\b", pl):
        return True
    if re.search(r"(^|[\\/_. -])news($|[\\/_. -])", pl):
        return True
    return False


def infer_asset_type(source_path: str) -> str:
    """Path-only editorial type. First matching rule wins, else verite."""
    pl = _norm_path(source_path)
    if not pl:
        return ASSET_TYPE_VERITE
    if _matches_court_recordings(pl):
        return ASSET_TYPE_COURT_RECORDINGS
    if _matches_b_roll(pl):
        return ASSET_TYPE_B_ROLL
    if _path_indicates_third_party_podcast(source_path or ""):
        return ASSET_TYPE_THIRD_PARTY
    if _matches_interview(pl):
        return ASSET_TYPE_INTERVIEW
    if _matches_timelapse(pl):
        return ASSET_TYPE_TIMELAPSE
    if _matches_archival(pl):
        return ASSET_TYPE_ARCHIVAL
    if _matches_type_third_party(pl):
        return ASSET_TYPE_THIRD_PARTY
    return ASSET_TYPE_VERITE


def build_asset_classifications(
    asset_id: str,
    source_path: str,
    human_ids: set[str],
) -> dict[str, str]:
    bucket = classify_asset_bucket(asset_id, source_path, human_ids)
    return {
        "bucket": bucket,
        "type": infer_asset_type(source_path),
    }


def primary_asset_bucket(rec: dict) -> str | None:
    """Read bucket from asset_classifications or legacy asset_bucket."""
    ac = rec.get("asset_classifications")
    if isinstance(ac, dict):
        ab = ac.get("bucket")
        if ab is None:
            ab = ac.get("asset_bucket")
        if isinstance(ab, str) and ab:
            return ab
        if isinstance(ab, list) and ab and isinstance(ab[0], str):
            return ab[0]
    leg = rec.get("asset_bucket")
    if isinstance(leg, str) and leg:
        return leg
    return None
