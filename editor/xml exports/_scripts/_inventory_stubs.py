"""For each Act, inventory clipitems backed by stub placeholder media
(<200 KB file on disk under derivative media)."""
from __future__ import annotations
import sys, os, urllib.parse
from pathlib import Path
sys.stdout.reconfigure(encoding="utf-8")
from lxml import etree


def pathurl_to_fs(p: str) -> str:
    if p.startswith("file://localhost/"):
        s = p[len("file://localhost/"):]
    else:
        s = p
    s = urllib.parse.unquote(s)
    if len(s) >= 2 and s[1] == ":":
        return s[0] + ":\\" + s[2:].lstrip("/").replace("/", "\\")
    return s


PLACEHOLDER_MAX_KB = 200

for label, path in [
    ("Act I",   "xml exports/project_act I_paths-only.xml"),
    ("Act III", "xml exports/project_act III_paths-only.xml"),
]:
    if not Path(path).exists():
        print(f"{label}: SKIP (file not on disk)")
        continue
    tree = etree.parse(path)
    seq = tree.getroot().find("sequence")
    fps = 24000 / 1001

    file_info = {}
    for f in seq.findall(".//file"):
        fid = f.get("id")
        purl = f.findtext("pathurl")
        if fid and purl:
            fs = pathurl_to_fs(purl)
            sz = os.path.getsize(fs) if os.path.exists(fs) else None
            file_info[fid] = (f.findtext("name") or "?", sz, purl, fs)

    stubs = []
    for ci in seq.findall(".//clipitem"):
        f = ci.find("file")
        if f is None:
            continue
        fid = f.get("id")
        if fid not in file_info:
            continue
        name, sz, purl, fs = file_info[fid]
        if sz is None or sz > PLACEHOLDER_MAX_KB * 1024:
            continue
        s = ci.findtext("start")
        if not s or s == "-1":
            continue
        try:
            sf = int(s)
        except ValueError:
            continue
        # Only count clipitems on the MAIN sequence (skip nested).
        cur = ci.getparent()
        while cur is not None and cur.tag != "sequence":
            cur = cur.getparent()
        if cur is not seq:
            continue
        stubs.append((sf, name, sz, fid, fs))

    print(f"\n=== {label}: stub-backed clipitems on MAIN timeline ===")
    print(f"    (file size < {PLACEHOLDER_MAX_KB} KB on disk)")
    unique_files = {}
    for sf, name, sz, fid, fs in stubs:
        unique_files.setdefault((name, sz, fs), []).append(sf)
    for (name, sz, fs), starts in sorted(unique_files.items(), key=lambda x: min(x[1])):
        starts = sorted(starts)
        times = ", ".join(f"{s/fps/60:.2f}min" for s in starts[:5])
        more = f" ... +{len(starts)-5}" if len(starts) > 5 else ""
        print(f"  {sz:>6}B  {name[:35]:<35} ({len(starts)}x)  at {times}{more}")
        print(f"          {fs}")
    print(f"  TOTAL stub-backed clipitems on main timeline: {len(stubs)}")
