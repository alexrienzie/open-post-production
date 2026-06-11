"""For every pathurl in an xmeml source, classify catalog-resolution mode:
exact / colon-strip / filename-only / true-miss."""
from __future__ import annotations
import sys, sqlite3, urllib.parse
from pathlib import Path
from collections import defaultdict
sys.stdout.reconfigure(encoding="utf-8")
from lxml import etree

SRC = Path(sys.argv[1])
_REPO = Path(__file__).resolve().parents[3]
CATALOG = _REPO / "indexes" / "editorial_catalog.sqlite"


SOURCE_TREE_DIRNAME = "Project"  # the folder your camera originals live under on the source drive

def norm(s: str) -> str:
    return s.lower().replace(":", "").replace("  ", " ").strip()


by_rel_exact: dict[str, tuple[str, str, str]] = {}
by_rel_norm: dict[str, tuple[str, str, str]] = {}
by_filename: dict[str, list[tuple[str, str]]] = defaultdict(list)

con = sqlite3.connect(str(CATALOG))
for aid, sp, fn in con.execute("SELECT asset_id, source_path, filename FROM asset"):
    if sp and SOURCE_TREE_DIRNAME in sp:
        parts = sp.replace("\\", "/").split(SOURCE_TREE_DIRNAME + "/", 1)
        if len(parts) == 2:
            rel = parts[1]
            by_rel_exact[rel.lower()] = (aid, sp, fn)
            by_rel_norm[norm(rel)] = (aid, sp, fn)
    if fn:
        by_filename[fn.lower()].append((aid, sp))

tree = etree.parse(str(SRC))
seq = tree.getroot().find("sequence")

exact = norm_hit = fname_hit = true_miss = 0
norm_ex: list[tuple[str, str]] = []
fname_ex: list[tuple[str, list]] = []
miss_ex: list[str] = []

for f in seq.findall(".//file"):
    purl = f.findtext("pathurl")
    if not purl:
        continue
    decoded = urllib.parse.unquote(purl)
    if SOURCE_TREE_DIRNAME + "/" not in decoded:
        continue
    rel = decoded.split(SOURCE_TREE_DIRNAME + "/", 1)[1]
    rel_lower = rel.lower()
    rel_n = norm(rel)
    fname = rel.rsplit("/", 1)[-1].lower()

    if rel_lower in by_rel_exact:
        exact += 1
    elif rel_n in by_rel_norm:
        norm_hit += 1
        if len(norm_ex) < 6:
            norm_ex.append((rel, by_rel_norm[rel_n][1]))
    elif fname in by_filename:
        fname_hit += 1
        if len(fname_ex) < 8:
            fname_ex.append((rel, by_filename[fname]))
    else:
        true_miss += 1
        if len(miss_ex) < 12:
            miss_ex.append(rel)

total = exact + norm_hit + fname_hit + true_miss
print(f"=== {SRC.name} ({total} file refs) ===")
print(f"  exact catalog match:           {exact}")
print(f"  match after colon-strip:       {norm_hit}  <-- recoverable, no risk")
print(f"  filename-only match elsewhere: {fname_hit}  <-- recoverable, may be ambiguous")
print(f"  no catalog match at all:       {true_miss}")
print()
print("=== Colon-strip recoveries (full list) ===")
for xr, cp in norm_ex:
    print(f"  XML: {xr}")
    print(f"  CAT: {cp}")
print()
print("=== Filename-only recoveries (need disambiguation) ===")
for xr, cands in fname_ex:
    print(f"  XML: {xr}")
    for aid, sp in cands[:3]:
        print(f"  CAT: {sp}")
print()
print("=== True misses ===")
for r in miss_ex:
    print(f"  {r}")
