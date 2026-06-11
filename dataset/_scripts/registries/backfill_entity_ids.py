"""
Heuristic backfill of people_ids[] and org_ids[] across:
- assets/transcripts/*.json (match against full_text)
- documents/press/articles/*.json (match against analysis.named_entities + content if available)
- documents/press/comments/*.json (match against comment.text)
- documents/press/social_posts/*.json (caption, transcript, analysis.named_entities)
- timeline/us_events.jsonl (match against title + summary + key_figures + sources)

Strategy:
- Build a single token→id table from people.json (canonical_name + aliases)
  and orgs.json (canonical_name + aliases).
- Skip ambiguous tokens listed in name_resolution_rules (Mike, Jackson, etc.)
  unless paired with a disambiguating word — for now we just skip them and
  let a later context-aware pass handle them.
- Match whole-word, case-insensitive.
- Confidence: low (heuristic). User should manually verify high-leverage records.

Idempotent: only adds NEW IDs that weren't already in people_ids/org_ids.
"""
from __future__ import annotations

import json
import os
import re
import unicodedata
from collections import defaultdict
from pathlib import Path


def atomic_write_json(path: Path, data: dict) -> None:
    """Atomic JSON write — write to .tmp, then os.replace.

    Fixes the failure mode where a kill mid-write truncates the destination.
    A killed run leaves the original untouched (or absent), never half-written.
    """
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)

ROOT = Path(__file__).resolve().parents[2]
PEOPLE_JSON = ROOT / "people/people.json"
ORGS_JSON = ROOT / "organizations/orgs.json"
LOCATIONS_JSON = ROOT / "places/places.json"

TRANSCRIPTS = ROOT / "assets/transcripts"
ARTICLES = ROOT / "documents/press/articles"
COMMENTS = ROOT / "documents/press/comments"
SOCIAL_POSTS = ROOT / "documents/press/social_posts"
US_EVENTS = ROOT / "timeline/us_events.jsonl"


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return s.lower().strip()


def build_matcher() -> tuple[dict[str, str], set[str], dict[str, str]]:
    """
    Returns (term_lower -> id, ambiguous_tokens, id -> kind) where kind is 'p'|'o'.
    """
    term_to_id: dict[str, str] = {}
    id_kind: dict[str, str] = {}
    ambiguous: set[str] = set()

    people = json.loads(PEOPLE_JSON.read_text(encoding="utf-8"))
    for r in people.get("name_resolution_rules") or []:
        ambiguous.add(norm(r.get("pattern", "")))

    for p in people.get("people") or []:
        pid = p.get("id")
        if not pid:
            continue
        id_kind[pid] = "p"
        terms = [p.get("canonical_name", "")] + list(p.get("aliases") or [])
        for t in terms:
            tn = norm(t)
            if not tn or len(tn) < 3:
                continue
            # Skip if first-name-only and matches an ambiguous rule
            if tn in ambiguous:
                continue
            # Don't overwrite an existing higher-precedence mapping
            term_to_id.setdefault(tn, pid)

    if ORGS_JSON.exists():
        orgs = json.loads(ORGS_JSON.read_text(encoding="utf-8"))
        for o in orgs.get("organizations") or []:
            oid = o.get("id")
            if not oid:
                continue
            id_kind[oid] = "o"
            terms = [o.get("canonical_name", "")] + list(o.get("aliases") or [])
            for t in terms:
                tn = norm(t)
                if not tn or len(tn) < 3:
                    continue
                # Don't override a person mapping with an org if collision
                if tn in term_to_id and id_kind.get(term_to_id[tn]) == "p":
                    continue
                term_to_id.setdefault(tn, oid)

    # Locations registry — placeholder until populated. No-op when empty.
    # Match precedence: person > org > location (so a place collision with a
    # person/org keeps the person/org).
    if LOCATIONS_JSON.exists():
        try:
            locs = json.loads(LOCATIONS_JSON.read_text(encoding="utf-8"))
            for pl in locs.get("places") or []:
                pl_id = pl.get("id")
                if not pl_id:
                    continue
                id_kind[pl_id] = "pl"
                terms = [pl.get("canonical_name", "")] + list(pl.get("aliases") or [])
                for t in terms:
                    tn = norm(t)
                    if not tn or len(tn) < 3:
                        continue
                    # Don't override existing person/org mapping
                    if tn in term_to_id and id_kind.get(term_to_id[tn]) in ("p", "o"):
                        continue
                    term_to_id.setdefault(tn, pl_id)
        except Exception:
            pass

    return term_to_id, ambiguous, id_kind


def build_compiled_regex(term_to_id: dict[str, str]) -> re.Pattern:
    """Build a single alternation regex over all terms (sorted longest-first)."""
    terms = sorted(term_to_id.keys(), key=lambda s: -len(s))
    pat = r"\b(?:" + "|".join(re.escape(t) for t in terms) + r")\b"
    return re.compile(pat)


def find_matches(text: str, compiled: re.Pattern, term_to_id: dict[str, str], id_kind: dict[str, str]) -> tuple[set[str], set[str], set[str]]:
    """Whole-word, case-insensitive matching via single compiled alternation.
    Returns (people_ids_set, org_ids_set, place_ids_set)."""
    if not text:
        return set(), set(), set()
    text_norm = norm(text)
    pids: set[str] = set()
    oids: set[str] = set()
    plids: set[str] = set()
    for m in compiled.finditer(text_norm):
        eid = term_to_id.get(m.group(0))
        if not eid:
            continue
        kind = id_kind.get(eid)
        if kind == "p":
            pids.add(eid)
        elif kind == "o":
            oids.add(eid)
        elif kind == "pl":
            plids.add(eid)
    return pids, oids, plids


def update_record(rec: dict, pids: set[str], oids: set[str], plids: set[str] = None) -> bool:
    """Add new IDs to people_ids, org_ids, place_ids fields. Return True if changed."""
    changed = False
    if pids:
        existing = set(rec.get("people_ids") or [])
        new = sorted(existing | pids)
        if new != sorted(existing):
            rec["people_ids"] = new
            changed = True
    if oids:
        existing = set(rec.get("org_ids") or [])
        new = sorted(existing | oids)
        if new != sorted(existing):
            rec["org_ids"] = new
            changed = True
    elif "org_ids" not in rec:
        rec["org_ids"] = []
        changed = True
    if plids:
        existing = set(rec.get("place_ids") or [])
        new = sorted(existing | plids)
        if new != sorted(existing):
            rec["place_ids"] = new
            changed = True
    return changed


def _atomic_write_jsonl(path: Path, records: list[dict]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    body = "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n"
    tmp.write_text(body, encoding="utf-8")
    os.replace(tmp, path)


def process_dir(catalog_dir: Path, text_extractor, label: str, max_records: int = 0) -> dict:
    stats = {"examined": 0, "updated": 0, "skipped_no_text": 0, "errors": 0,
             "p_added": 0, "o_added": 0}
    term_to_id, ambiguous, id_kind = build_matcher()
    compiled = build_compiled_regex(term_to_id)

    # JSONL path: load all, modify in memory, write whole file atomically.
    if catalog_dir.is_file() and catalog_dir.suffix == ".jsonl":
        records = []
        for line in catalog_dir.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except Exception:
                stats["errors"] += 1
        any_changed = False
        for rec in records:
            stats["examined"] += 1
            text = text_extractor(rec)
            if not text:
                stats["skipped_no_text"] += 1
                continue
            pids, oids, plids = find_matches(text, compiled, term_to_id, id_kind)
            before_p = len(rec.get("people_ids") or [])
            before_o = len(rec.get("org_ids") or [])
            if update_record(rec, pids, oids, plids):
                after_p = len(rec.get("people_ids") or [])
                after_o = len(rec.get("org_ids") or [])
                stats["p_added"] += max(0, after_p - before_p)
                stats["o_added"] += max(0, after_o - before_o)
                stats["updated"] += 1
                any_changed = True
        if any_changed:
            _atomic_write_jsonl(catalog_dir, records)
        return stats

    # Per-file JSON path
    paths = sorted(catalog_dir.glob("*.json"))
    if max_records:
        paths = paths[:max_records]
    for p in paths:
        stats["examined"] += 1
        try:
            rec = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            stats["errors"] += 1
            continue
        text = text_extractor(rec)
        if not text:
            stats["skipped_no_text"] += 1
            continue
        pids, oids, plids = find_matches(text, compiled, term_to_id, id_kind)
        before_p = len(rec.get("people_ids") or [])
        before_o = len(rec.get("org_ids") or [])
        if update_record(rec, pids, oids, plids):
            atomic_write_json(p, rec)
            after_p = len(rec.get("people_ids") or [])
            after_o = len(rec.get("org_ids") or [])
            stats["p_added"] += max(0, after_p - before_p)
            stats["o_added"] += max(0, after_o - before_o)
            stats["updated"] += 1
    return stats


def transcript_text(rec: dict) -> str:
    return rec.get("full_text") or ""


def article_text(rec: dict) -> str:
    parts = []
    md = rec.get("metadata") or {}
    parts.append(md.get("title") or "")
    parts.append(md.get("subtitle") or "")
    an = rec.get("analysis") or {}
    parts.append(an.get("summary_one_line") or "")
    parts.append(an.get("summary_paragraph") or "")
    ne = (an.get("named_entities") or {})
    parts.extend(ne.get("people") or [])
    parts.extend(ne.get("organizations") or [])
    parts.extend(ne.get("locations") or [])
    pq = an.get("pull_quotes") or []
    for q in pq:
        if isinstance(q, dict):
            parts.append(q.get("text") or "")
            parts.append(q.get("speaker") or "")
            parts.append(q.get("context") or "")
    # Article body if accessible
    content = rec.get("content") or {}
    if isinstance(content, dict) and content.get("text"):
        parts.append(content["text"])
    return "\n".join(p for p in parts if p)


def comment_text(rec: dict) -> str:
    c = rec.get("comment") or {}
    parts = [c.get("text") or ""]
    cm = rec.get("commenter") or {}
    parts.append(cm.get("username") or "")
    an = rec.get("analysis") or {}
    parts.append(an.get("summary_one_line") or "")
    parts.append(an.get("key_phrase") or "")
    return "\n".join(p for p in parts if p)


def social_post_text(rec: dict) -> str:
    post = rec.get("post") or {}
    acct = rec.get("account") or {}
    parts = [
        post.get("caption") or "",
        post.get("transcript") or "",
        post.get("alt_text") or "",
        acct.get("username") or "",
        acct.get("display_name") or "",
        acct.get("tracker_label") or "",
    ]
    an = rec.get("analysis") or {}
    parts.extend([
        an.get("summary_one_line") or "",
        an.get("key_phrase") or "",
    ])
    topics = an.get("topics") or []
    if isinstance(topics, list):
        parts.extend(str(t) for t in topics if t)
    ne = an.get("named_entities") or {}
    if isinstance(ne, dict):
        parts.extend(ne.get("people") or [])
        parts.extend(ne.get("organizations") or [])
        parts.extend(ne.get("locations") or [])
    return "\n".join(p for p in parts if p)


def event_text(rec: dict) -> str:
    parts = [rec.get("title") or "", rec.get("summary") or "", rec.get("description_brief") or ""]
    for kf in rec.get("key_figures") or []:
        if isinstance(kf, dict):
            parts.append(kf.get("name") or "")
        elif isinstance(kf, str):
            parts.append(kf)
    for s in rec.get("sources") or []:
        if isinstance(s, dict):
            parts.append(s.get("title") or "")
            parts.append(s.get("publication") or "")
    return "\n".join(p for p in parts if p)


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--domain",
        choices=[
            "transcripts", "articles", "comments", "social_posts", "us_events",
            "all",
        ],
        default="all",
    )
    ap.add_argument("--max-records", type=int, default=0)
    args = ap.parse_args()

    targets = []
    if args.domain in ("transcripts", "all"):
        targets.append(("transcripts", TRANSCRIPTS, transcript_text))
    if args.domain in ("articles", "all"):
        targets.append(("articles", ARTICLES, article_text))
    if args.domain in ("comments", "all"):
        targets.append(("comments", COMMENTS, comment_text))
    if args.domain in ("social_posts", "all"):
        targets.append(("social_posts", SOCIAL_POSTS, social_post_text))
    if args.domain in ("us_events", "all"):
        targets.append(("us_events", US_EVENTS, event_text))
    for name, d, fn in targets:
        if not d.exists():
            print(f"\n=== {name} (skipped, dir not found) ===")
            continue
        s = process_dir(d, fn, name, args.max_records)
        print(f"\n=== {name} ===")
        for k, v in s.items():
            print(f"  {k}: {v}")


if __name__ == "__main__":
    main()