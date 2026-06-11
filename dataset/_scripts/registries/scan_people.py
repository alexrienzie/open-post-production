#!/usr/bin/env python3
"""
Heuristic scan of people/people.json for likely cleanup candidates.

Categories scanned:
  1. Near-duplicate canonical names (fuzzy match >= 88%)
  2. Title artifacts in canonical (Sen., Hon., AUSA, Mr., Magistrate Judge, etc.)
  3. Missing aliases (e.g., "John A. Smith" but no "John Smith" alias)
  4. Surname clusters (>= 2 entries sharing surname, no family relationship)
  5. Confidence stale (low confidence but multiple sources OR multiple roles)
  6. Role/source mismatch (e.g., role="attorney" but only press_index source)
  7. Orphan entries (no relationships, no notes, single source, single role)
  8. Email parser noise (suspicious casing, concatenated words)
  9. Pet/non-human entries (sanity flag, not necessarily wrong)

Output: _review_drafts/people_review.md grouped by category.
Each entry has a suggested action you can accept/reject in a follow-up pass.
"""
import json, re, sys
from pathlib import Path
from collections import defaultdict

try:
    from rapidfuzz import fuzz
    def fuzz_score(a, b): return fuzz.token_set_ratio(a, b)
    HAS_FUZZ = True
except ImportError:
    from difflib import SequenceMatcher
    def fuzz_score(a, b):
        ta, tb = set(a.lower().split()), set(b.lower().split())
        if not ta or not tb: return 0
        if ta == tb: return 100
        return int(SequenceMatcher(None, " ".join(sorted(ta)), " ".join(sorted(tb))).ratio() * 100)
    HAS_FUZZ = True
except ImportError:
    HAS_FUZZ = False

ROOT = Path(__file__).resolve().parents[2]
PEOPLE = ROOT / "people" / "people.json"
OUT = ROOT / "_review_drafts" / "people_review.md"

TITLE_RE = re.compile(r"\b(Sen\.|Senator|Rep\.|Representative|Hon\.|Honorable|Magistrate Judge|Magistrate|Judge|AUSA|Mr\.|Ms\.|Mrs\.|Dr\.|Sgt\.|Lt\.|Capt\.|Col\.|Gen\.|Adm\.|Pres\.|President|Gov\.|Governor|Atty\.|Attorney|U\.S\. Attorney|U\.S\.A\.O\.|Acting U\.S\. Attorney|Congressman|Congresswoman)\b")

def load():
    return json.loads(PEOPLE.read_text())

def get_surname(name):
    parts = name.replace(",", "").split()
    if not parts: return None
    # crude: skip suffixes, take last alphabetic word
    suffixes = {"Jr.", "Sr.", "II", "III", "IV", "Esq."}
    for w in reversed(parts):
        if w not in suffixes and re.match(r"^[A-Z][a-z]", w):
            return w
    return parts[-1]

def get_first(name):
    parts = name.split()
    return parts[0] if parts else None

def main():
    d = load()
    ppl = d["people"]
    by_id = {p["id"]: p for p in ppl}

    out = []
    out.append(f"# People Registry Review — Heuristic Scan\n")
    out.append(f"Generated: {__import__('datetime').date.today().isoformat()}\n")
    out.append(f"Total people: {len(ppl)}\n\n")
    out.append("Each section lists candidates. Mark each as ✅ (apply) or ❌ (skip) and we'll process in a follow-up.\n\n---\n\n")

    # 1. Near-duplicate canonical names
    out.append("## 1. Near-duplicate canonical names (fuzzy ≥88%)\n\n")
    out.append("Likely the same person under slightly different spellings/formats.\n\n")
    if HAS_FUZZ:
        names = [(p["id"], p["canonical_name"]) for p in ppl]
        seen = set()
        dupes = []
        for i, (id1, n1) in enumerate(names):
            for id2, n2 in names[i+1:]:
                if (id1, id2) in seen: continue
                if abs(len(n1) - len(n2)) > 8: continue
                ratio = fuzz_score(n1, n2)
                if ratio >= 88:
                    dupes.append((ratio, id1, n1, id2, n2))
                    seen.add((id1, id2))
        dupes.sort(reverse=True)
        if not dupes:
            out.append("_(none flagged)_\n\n")
        else:
            out.append(f"Found {len(dupes)} pairs.\n\n")
            for ratio, id1, n1, id2, n2 in dupes[:50]:
                out.append(f"- [ ] **{ratio}%** — `{id1}` ({n1!r}) vs `{id2}` ({n2!r})\n")
            if len(dupes) > 50:
                out.append(f"\n_(...and {len(dupes)-50} more, truncated)_\n")
    else:
        out.append("_(rapidfuzz not installed — skipped)_\n")
    out.append("\n")

    # 2. Title artifacts in canonical
    out.append("## 2. Title artifacts in canonical_name\n\n")
    out.append("Titles like 'Sen.', 'Magistrate Judge' should be in `aliases`, not the canonical.\n\n")
    title_hits = []
    for p in ppl:
        m = TITLE_RE.search(p["canonical_name"])
        if m:
            title_hits.append((p["id"], p["canonical_name"], m.group(0)))
    if not title_hits:
        out.append("_(none)_\n")
    else:
        out.append(f"Found {len(title_hits)}.\n\n")
        for id_, name, title in title_hits:
            stripped = TITLE_RE.sub("", name).strip().rstrip(",")
            out.append(f"- [ ] `{id_}` — `{name}` → strip `{title}` → `{stripped}`, add `{title} {stripped}` as alias\n")
    out.append("\n")

    # 3. Missing aliases — middle initial / suffix variants
    out.append("## 3. Missing common-form aliases\n\n")
    out.append("Canonical has middle initial or suffix; common 2-word form not in aliases.\n\n")
    miss = []
    for p in ppl:
        parts = p["canonical_name"].split()
        if len(parts) >= 3:
            # detect middle initial like "A." or middle word
            short = f"{parts[0]} {parts[-1]}"
            aliases_lower = [a.lower() for a in (p.get("aliases") or [])]
            if short.lower() != p["canonical_name"].lower() and short.lower() not in aliases_lower:
                miss.append((p["id"], p["canonical_name"], short))
    if not miss:
        out.append("_(none)_\n")
    else:
        out.append(f"Found {len(miss)} entries.\n\n")
        for id_, full, short in miss[:50]:
            out.append(f"- [ ] `{id_}` — `{full}` → add alias `{short}`\n")
        if len(miss) > 50:
            out.append(f"\n_(...and {len(miss)-50} more)_\n")
    out.append("\n")

    # 4. Surname clusters
    out.append("## 4. Surname clusters with no family relationship\n\n")
    out.append("Multiple people share a surname but have no parent/sibling/spouse relationship recorded. Possibly family.\n\n")
    by_surname = defaultdict(list)
    for p in ppl:
        sn = get_surname(p["canonical_name"])
        if sn and len(sn) > 3:  # skip "Mr" etc.
            by_surname[sn].append(p)
    family_rel = {"parent_of", "child_of", "sibling", "spouse_of", "former_partner", "spouse", "former_attorney_for"}
    clusters = []
    for sn, group in by_surname.items():
        if len(group) < 2: continue
        # check if any pair has family rel
        ids = {g["id"] for g in group}
        has_rel = False
        for g in group:
            for r in (g.get("relationships") or []):
                if r.get("to_id") in ids and r.get("type") in family_rel:
                    has_rel = True
                    break
            if has_rel: break
        if not has_rel:
            clusters.append((sn, group))
    if not clusters:
        out.append("_(all family clusters have relationships)_\n")
    else:
        out.append(f"Found {len(clusters)} surname clusters without family rel.\n\n")
        for sn, group in clusters[:30]:
            ids_str = ", ".join(f"`{g['id']}`" for g in group)
            names_str = " / ".join(g["canonical_name"] for g in group)
            out.append(f"- [ ] **{sn}**: {names_str} ({ids_str}) — confirm family or coincidence\n")
        if len(clusters) > 30:
            out.append(f"\n_(...and {len(clusters)-30} more)_\n")
    out.append("\n")

    # 5. Confidence stale
    out.append("## 5. Confidence stale (low but multi-source OR multi-role)\n\n")
    stale = []
    for p in ppl:
        if p.get("confidence") == "low":
            srcs = p.get("sources") or []
            roles = p.get("roles") or []
            if len(srcs) >= 2 or len(roles) >= 2:
                stale.append(p)
    if not stale:
        out.append("_(none)_\n")
    else:
        out.append(f"Found {len(stale)} entries.\n\n")
        for p in stale[:50]:
            out.append(f"- [ ] `{p['id']}` ({p['canonical_name']}) — sources={p.get('sources')}, roles={p.get('roles')[:3]} → promote to medium\n")
        if len(stale) > 50:
            out.append(f"\n_(...and {len(stale)-50} more)_\n")
    out.append("\n")

    # 6. Role/source mismatch
    out.append("## 6. Role/source mismatch (legal role from press-only source)\n\n")
    legal_roles = {"attorney", "judge", "magistrate", "ausa", "u.s. attorney", "us attorney", "counsel", "magistrate judge", "acting u.s. attorney"}
    mismatch = []
    for p in ppl:
        roles_lower = [r.lower() for r in (p.get("roles") or [])]
        srcs = set(p.get("sources") or [])
        if any(r in legal_roles for r in roles_lower):
            if srcs and srcs.issubset({"press_index"}):
                mismatch.append(p)
    if not mismatch:
        out.append("_(none)_\n")
    else:
        out.append(f"Found {len(mismatch)} entries.\n\n")
        for p in mismatch[:30]:
            out.append(f"- [ ] `{p['id']}` ({p['canonical_name']}) — roles={p.get('roles')}, sources={p.get('sources')} → maybe role tag is wrong, or add case_records source\n")
    out.append("\n")

    # 7. Orphan entries
    out.append("## 7. Orphan candidates (no relationships, no notes, single source, single role)\n\n")
    out.append("Low-signal entries — candidates for delete unless they're real but minor.\n\n")
    orphans = []
    for p in ppl:
        rels = p.get("relationships") or []
        notes = p.get("notes") or ""
        srcs = p.get("sources") or []
        roles = p.get("roles") or []
        if not rels and not notes and len(srcs) <= 1 and len(roles) <= 1 and p.get("confidence") == "low":
            orphans.append(p)
    if not orphans:
        out.append("_(none)_\n")
    else:
        out.append(f"Found {len(orphans)} entries (showing first 50).\n\n")
        for p in orphans[:50]:
            out.append(f"- [ ] `{p['id']}` ({p['canonical_name']}) — sources={p.get('sources')}, roles={p.get('roles')}\n")
        if len(orphans) > 50:
            out.append(f"\n_(...and {len(orphans)-50} more — full list in JSON output)_\n")
    out.append("\n")

    # 8. Email parser noise — suspicious patterns
    out.append("## 8. Email parser noise (suspicious casing/structure)\n\n")
    noise = []
    for p in ppl:
        n = p["canonical_name"]
        # all-lowercase, all-uppercase, contains digits, contains @, single word with mixed case
        if (n.islower() and " " in n) or n.isupper() or any(c.isdigit() for c in n) or "@" in n:
            noise.append(p)
        elif " " not in n and n != n.title() and len(n) > 4:
            noise.append(p)
    if not noise:
        out.append("_(none)_\n")
    else:
        out.append(f"Found {len(noise)} entries.\n\n")
        for p in noise[:30]:
            out.append(f"- [ ] `{p['id']}` — `{p['canonical_name']}` (sources={p.get('sources')})\n")
    out.append("\n")

    # 9. Pet/non-human sanity
    out.append("## 9. Non-human entries (sanity flag — not necessarily wrong)\n\n")
    pets = [p for p in ppl if "pet" in (p.get("roles") or [])]
    if not pets:
        out.append("_(none)_\n")
    else:
        for p in pets:
            out.append(f"- ✅ `{p['id']}` — {p['canonical_name']} ({p.get('notes','')[:60]})\n")
    out.append("\n")

    out.append("---\n\n## Summary\n\n")
    out.append(f"- Near-duplicates: {len(dupes) if HAS_FUZZ else '?'}\n")
    out.append(f"- Title artifacts: {len(title_hits)}\n")
    out.append(f"- Missing aliases: {len(miss)}\n")
    out.append(f"- Surname clusters: {len(clusters)}\n")
    out.append(f"- Confidence stale: {len(stale)}\n")
    out.append(f"- Role/source mismatch: {len(mismatch)}\n")
    out.append(f"- Orphan candidates: {len(orphans)}\n")
    out.append(f"- Parser noise: {len(noise)}\n")
    out.append(f"- Non-human: {len(pets)}\n")

    OUT.write_text("".join(out))
    print(f"Wrote {OUT}")
    print(f"Total flags: dup={len(dupes) if HAS_FUZZ else '?'} title={len(title_hits)} alias={len(miss)} surname={len(clusters)} stale={len(stale)} role_mismatch={len(mismatch)} orphan={len(orphans)} noise={len(noise)} pet={len(pets)}")

if __name__ == "__main__":
    main()
