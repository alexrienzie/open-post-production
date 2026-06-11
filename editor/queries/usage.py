"""editor.queries.usage — cross-Act "already-used" media dedup.

When pulling b-roll / stills for a new scene, you want to know what a prior Act
export already uses so you don't duplicate a clip across acts (e.g. Act I burned
5/7 Bryce archival photos for his "1983 Record Holder" backstory). This module
parses the committed Act XML exports, resolves every ``<pathurl>`` back to an
``asset_id``, and reports what's used + where.

Resolution: pathurl -> derivative-media-relative path -> ``asset_id`` via the
``derivative media/_index/asset_map.json`` reverse index (covers video + still +
audio proxies), with a catalog ``source_path`` fallback for anything the map
misses.

Public API (all return plain dicts/lists — JSON-serializable):

    find_act_exports(include_scene_workspace=False) -> list[Path]
    used_assets(xmls=None, ...) -> dict[asset_id, list[usage]]
    is_used(asset_id, used=None) -> bool
    filter_unused(asset_ids, used=None) -> list[str]
    annotate_usage(results, used=None, key="asset_id") -> same list (adds 'used_in')

CLI:
    py usage.py                         # summary: per-act clip/asset counts
    py usage.py --check <aid> [<aid>..] # is each (10-char prefix OK) used? where?
    py usage.py --list [--act NAME]     # dump used asset_ids (optionally one act)
    py usage.py --unresolved            # pathurls that didn't resolve (data quality)
"""
from __future__ import annotations

import json
import sqlite3
import sys
import urllib.parse
from pathlib import Path
from xml.etree import ElementTree as ET

_QUERIES_DIR = Path(__file__).resolve().parent
_REPO = _QUERIES_DIR.parent.parent
XML_EXPORTS = _REPO / "editor" / "xml exports"
ASSET_MAP = _REPO / "derivative media" / "_index" / "asset_map.json"
EDITORIAL_DB = _REPO / "indexes" / "editorial_catalog.sqlite"
_DM_MARKER = "derivative media/"


# ---------------- path / resolution helpers ----------------

def _norm(p: str) -> str:
    return p.replace("\\", "/").strip().lower()


def find_act_exports(include_scene_workspace: bool = False) -> list[Path]:
    """Committed Act exports: top-level ``editor/xml exports/*.xml`` (excludes
    ``_archive/``). Scene-workspace WIP XMLs are off by default."""
    out = [p for p in XML_EXPORTS.glob("*.xml") if p.is_file()]
    if include_scene_workspace:
        out += [p for p in (XML_EXPORTS / "scene_workspace").glob("*.xml") if p.is_file()]
    return sorted(out)


_RELIDX_CACHE: dict[str, str] | None = None


def _stem(p: str) -> str:
    """Strip the final extension from a normalized path (folder dots preserved)."""
    head, sep, tail = p.rpartition("/")
    base = tail.rsplit(".", 1)[0] if "." in tail else tail
    return f"{head}{sep}{base}"


def _relpath_index() -> dict[str, str]:
    """normalized derivative-media relative_path -> asset_id (from asset_map).

    Indexed BOTH by full normalized relpath AND by extension-stripped stem, because
    asset_map stores the *source* relative_path (e.g. ``…CANON.MXF``) while the Act
    XML references the transcoded *proxy* (``…CANON.mp4``) — same asset, different
    extension. Stem keys are added only when they don't clobber a full-path key.
    """
    global _RELIDX_CACHE
    if _RELIDX_CACHE is not None:
        return _RELIDX_CACHE
    idx: dict[str, str] = {}
    stem_idx: dict[str, str] = {}
    if ASSET_MAP.exists():
        entries = json.loads(ASSET_MAP.read_text(encoding="utf-8")).get("entries", {})
        for aid, e in entries.items():
            for v in e.values():
                if isinstance(v, dict) and v.get("relative_path"):
                    rp = _norm(v["relative_path"])
                    idx.setdefault(rp, aid)
                    stem_idx.setdefault(_stem(rp), aid)
    # fold stems in without clobbering exact keys
    for k, aid in stem_idx.items():
        idx.setdefault(k, aid)
    _RELIDX_CACHE = idx
    return idx


def pathurl_to_relpath(pathurl: str) -> str | None:
    """``file://localhost/E%3a/.../derivative%20media/<rel>`` -> ``<rel>`` (decoded)."""
    if not pathurl:
        return None
    body = pathurl.split("file://localhost/")[-1]
    dec = urllib.parse.unquote(body).replace("\\", "/")
    i = dec.lower().find(_DM_MARKER)
    if i < 0:
        return None
    return dec[i + len(_DM_MARKER):]


def _catalog_fallback(rel: str, con: sqlite3.Connection | None) -> str | None:
    """Resolve a relative path via catalog source_path tail-match (handles
    filename reuse across shoots by requiring the full relative tail)."""
    if con is None:
        return None
    fn = rel.replace("\\", "/").split("/")[-1]
    reln = _norm(rel)
    rows = con.execute("SELECT asset_id, source_path FROM asset WHERE filename = ?", (fn,)).fetchall()
    for aid, sp in rows:
        if _norm(sp).endswith(reln):
            return aid
    return None


def resolve_pathurl(pathurl: str, relidx: dict | None = None, con: sqlite3.Connection | None = None) -> str | None:
    rel = pathurl_to_relpath(pathurl)
    if rel is None:
        return None
    relidx = relidx if relidx is not None else _relpath_index()
    reln = _norm(rel)
    aid = relidx.get(reln) or relidx.get(_stem(reln))
    if aid:
        return aid
    return _catalog_fallback(rel, con)


# ---------------- XML parsing ----------------

def _clip_seconds(clipitem: ET.Element, frame_str: str | None) -> float | None:
    if frame_str is None:
        return None
    try:
        fr = int(frame_str)
    except ValueError:
        return None
    rate = clipitem.find("rate")
    tb, ntsc = 24, True
    if rate is not None:
        tb = int((rate.findtext("timebase") or "24"))
        ntsc = (rate.findtext("ntsc") or "TRUE").upper() == "TRUE"
    fps = tb * (1000.0 / 1001.0) if ntsc else float(tb)
    return round(fr / fps, 2) if fps else None


def parse_export(xml_path: Path, relidx: dict | None = None, con: sqlite3.Connection | None = None) -> tuple[list[dict], list[str]]:
    """Return (uses, unresolved_pathurls) for one Act export.

    Each use: {asset_id, clip_name, pathurl, src_in_sec, src_out_sec}.
    """
    relidx = relidx if relidx is not None else _relpath_index()
    root = ET.parse(str(xml_path)).getroot()
    # file-id -> pathurl (full <file> defs carry pathurl; later refs are stubs)
    fid_to_url: dict[str, str] = {}
    for f in root.iter("file"):
        url = f.findtext("pathurl")
        if f.get("id") and url:
            fid_to_url.setdefault(f.get("id"), url)
    uses: list[dict] = []
    unresolved: list[str] = []
    for ci in root.iter("clipitem"):
        fel = ci.find("file")
        if fel is None:
            continue  # title / generator / nested-sequence item
        url = fel.findtext("pathurl") or fid_to_url.get(fel.get("id") or "")
        if not url:
            continue
        aid = resolve_pathurl(url, relidx, con)
        if aid is None:
            unresolved.append(url)
            continue
        uses.append({
            "asset_id": aid,
            "clip_name": ci.findtext("name"),
            "pathurl": url,
            "src_in_sec": _clip_seconds(ci, ci.findtext("in")),
            "src_out_sec": _clip_seconds(ci, ci.findtext("out")),
        })
    return uses, unresolved


# ---------------- public API ----------------

def used_assets(xmls: list[Path] | None = None, *, include_scene_workspace: bool = False,
                use_catalog_fallback: bool = True) -> dict[str, list[dict]]:
    """Map asset_id -> list of usages across the given (or all committed) Act exports.

    Each usage: {act, clip_name, src_in_sec, src_out_sec}.
    """
    xmls = xmls if xmls is not None else find_act_exports(include_scene_workspace)
    relidx = _relpath_index()
    con = None
    if use_catalog_fallback and EDITORIAL_DB.exists():
        con = sqlite3.connect(f"file:{EDITORIAL_DB}?mode=ro", uri=True)
    try:
        out: dict[str, list[dict]] = {}
        for x in xmls:
            uses, _ = parse_export(x, relidx, con)
            for u in uses:
                out.setdefault(u["asset_id"], []).append({
                    "act": x.name,
                    "clip_name": u["clip_name"],
                    "src_in_sec": u["src_in_sec"],
                    "src_out_sec": u["src_out_sec"],
                })
        return out
    finally:
        if con is not None:
            con.close()


def is_used(asset_id: str, used: dict | None = None) -> bool:
    used = used if used is not None else used_assets()
    return asset_id in used


def filter_unused(asset_ids, used: dict | None = None) -> list[str]:
    """Return the subset of asset_ids NOT used in any committed Act export."""
    used = used if used is not None else used_assets()
    return [a for a in asset_ids if a not in used]


def annotate_usage(results: list[dict], used: dict | None = None, key: str = "asset_id") -> list[dict]:
    """Add a ``used_in`` field (list of act filenames, [] if unused) to each
    composer result dict in place. Lets you eyeball dedup on any composer output."""
    used = used if used is not None else used_assets()
    for r in results:
        aid = r.get(key)
        r["used_in"] = sorted({u["act"] for u in used.get(aid, [])}) if aid else []
    return results


# ---------------- CLI ----------------

def _resolve_short(prefix: str, used: dict) -> list[str]:
    if prefix in used:
        return [prefix]
    return [a for a in used if a.startswith(prefix)]


def main(argv: list[str]) -> int:
    # Windows consoles default to cp1252; Act paths contain non-cp1252 chars
    # (e.g. U+202F in folder names) — make stdout robust rather than crash.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass
    args = argv[1:]
    xmls = find_act_exports("--scene" in args)
    if "--scene" in args:
        args.remove("--scene")

    if "--check" in args:
        used = used_assets(xmls)
        targets = [a for a in args if a != "--check"]
        # also resolve any full asset_ids not yet in `used` (report as unused)
        for t in targets:
            hits = _resolve_short(t, used) or [t]
            for aid in hits:
                where = used.get(aid)
                if where:
                    locs = sorted({u["act"] for u in where})
                    print(f"USED     {aid[:12]}  in {len(locs)} export(s): {', '.join(locs)}")
                    for u in where[:6]:
                        rng = f"{u['src_in_sec']}-{u['src_out_sec']}s" if u["src_in_sec"] is not None else ""
                        print(f"           - {u['act']}: {u['clip_name']} {rng}")
                else:
                    print(f"UNUSED   {aid[:12]}")
        return 0

    if "--unresolved" in args:
        relidx = _relpath_index()
        con = sqlite3.connect(f"file:{EDITORIAL_DB}?mode=ro", uri=True) if EDITORIAL_DB.exists() else None
        total = 0
        for x in xmls:
            _, unres = parse_export(x, relidx, con)
            uniq = sorted(set(unres))
            if uniq:
                print(f"\n{x.name}: {len(uniq)} unresolved pathurl(s)")
                for u in uniq[:40]:
                    print("   ", pathurl_to_relpath(u) or u)
            total += len(uniq)
        if con:
            con.close()
        print(f"\ntotal unresolved (distinct per-act): {total}")
        return 0

    if "--list" in args:
        act = None
        if "--act" in args:
            i = args.index("--act")
            act = args[i + 1] if i + 1 < len(args) else None
        sel = [x for x in xmls if (act is None or act.lower() in x.name.lower())]
        used = used_assets(sel)
        for aid in sorted(used):
            print(aid)
        print(f"# {len(used)} distinct assets used across {len(sel)} export(s)", file=sys.stderr)
        return 0

    # default: summary
    relidx = _relpath_index()
    con = sqlite3.connect(f"file:{EDITORIAL_DB}?mode=ro", uri=True) if EDITORIAL_DB.exists() else None
    print(f"Act exports scanned ({len(xmls)}):  [+{0} scene-workspace]")
    grand: dict[str, int] = {}
    for x in xmls:
        uses, unres = parse_export(x, relidx, con)
        distinct = {u["asset_id"] for u in uses}
        for a in distinct:
            grand[a] = grand.get(a, 0) + 1
        print(f"  {x.name:52s} clips={len(uses):4d}  distinct_assets={len(distinct):4d}  unresolved={len(set(unres)):3d}")
    if con:
        con.close()
    shared = {a: c for a, c in grand.items() if c > 1}
    print(f"\nTotal distinct assets used: {len(grand)}")
    print(f"Assets used in >1 export:   {len(shared)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
