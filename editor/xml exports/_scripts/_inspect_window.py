"""Inspect clipitems in a given timecode window across all V tracks."""
from __future__ import annotations
import sys, os, urllib.parse
sys.stdout.reconfigure(encoding="utf-8")
from lxml import etree
from pathlib import Path

XML = Path(sys.argv[1])
LO_SEC = float(sys.argv[2])
HI_SEC = float(sys.argv[3])

tree = etree.parse(str(XML))
seq = tree.getroot().find("sequence")
fps = 24000 / 1001
lo_f, hi_f = int(LO_SEC * fps), int(HI_SEC * fps)
print(f"window: {LO_SEC}-{HI_SEC}s = frames {lo_f}-{hi_f}")


def pathurl_to_fs(p: str) -> str:
    if p.startswith("file://localhost/"):
        s = p[len("file://localhost/"):]
    else:
        s = p
    s = urllib.parse.unquote(s)
    if len(s) >= 2 and s[1] == ":":
        return s[0] + ":\\" + s[2:].lstrip("/").replace("/", "\\")
    return s


for ti, t in enumerate(seq.findall("media/video/track"), 1):
    items_in_window = []
    for ci in t.findall("clipitem"):
        s = ci.findtext("start")
        e = ci.findtext("end")
        if s and e and s != "-1" and e != "-1":
            sf, ef = int(s), int(e)
            if not (ef < lo_f or sf > hi_f):
                items_in_window.append((sf, ef, ci))
    if items_in_window:
        print(f"\n=== V{ti}: {len(items_in_window)} clipitems in window ===")
        for sf, ef, ci in items_in_window:
            f = ci.find("file")
            fid = f.get("id") if f is not None else "?"
            purl = f.findtext("pathurl") if f is not None else None
            name = (ci.findtext("name") or "?")[:38]
            on_disk = "?"
            if purl:
                fs = pathurl_to_fs(purl)
                if os.path.exists(fs):
                    on_disk = f"OK {os.path.getsize(fs)//1024}KB"
                else:
                    on_disk = "MISSING"
            stub = "" if purl else " (stub-ref to file def)"
            print(f"  [{sf/fps/60:5.2f}-{ef/fps/60:5.2f}min] {name:<40} f={fid:<10} {on_disk}{stub}")
            if purl and on_disk == "MISSING":
                print(f"        URL: {purl}")
                print(f"        FS:  {fs}")
