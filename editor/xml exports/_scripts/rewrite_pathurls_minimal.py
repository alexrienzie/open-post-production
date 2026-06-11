#!/usr/bin/env python3
"""Minimal pathurl substitution — no XML parsing, no serialization.

Reads an xmeml file as raw text, finds every <pathurl>...</pathurl>, resolves
the inner URL to an the workspace proxy or a mirrored-path placeholder, and writes the
result back with byte-level string replacement. Preserves BOM, line endings,
attribute quoting, whitespace, and every other Premiere-export quirk that
breaks when round-tripped through lxml.

Does NOT:
  - truncate the timeline (do that in Premiere with shift-delete or End trim)
  - change sequence format (do that in Premiere project settings)
  - touch any <file> dim metadata, link refs, or empty-tag forms

This is the most conservative possible transformation. If even this doesn't
import, the problem isn't xmeml structure — it's the underlying media or a
Premiere version mismatch.

Usage:
    py rewrite_pathurls_minimal.py --xml <input.xml> --output <output.xml>
"""
from __future__ import annotations
import argparse
import json
import re
import sqlite3
import sys
import urllib.parse
from pathlib import Path

_HERE = Path(__file__).resolve().parent
REPO = _HERE.parent.parent.parent
CATALOG_DB = REPO / "indexes" / "editorial_catalog.sqlite"
ASSET_MAP_JSON = REPO / "derivative media" / "_index" / "asset_map.json"
DERIVATIVE_MEDIA_ROOT = REPO / "derivative media"
DERIVATIVE_MEDIA_PATHURL_ROOT = r"E:\open-post-stack\derivative media"

EXT_TO_PROXY_KINDS = {
    "mp4": ["video_video_proxy"], "mov": ["video_video_proxy"],
    "r3d": ["video_video_proxy"], "m4v": ["video_video_proxy"],
    "wav": ["audio_audio_proxy", "video_audio_proxy"],
    "m4a": ["audio_audio_proxy"], "mp3": ["audio_audio_proxy"],
    "jpg": ["still_still_proxy"], "jpeg": ["still_still_proxy"],
    "heic": ["still_still_proxy"], "png": ["still_still_proxy"],
}

_NTFS_FORBIDDEN_TABLE = str.maketrans({c: "_" for c in ':*?"<>|'})


SOURCE_TREE_DIRNAME = "Project"  # the folder your camera originals live under on the source drive

def _windows_path_to_pathurl(win_path: str) -> str:
    p = str(win_path).replace("\\", "/")
    if len(p) >= 2 and p[1] == ":":
        drive = p[0]
        rest = p[2:].lstrip("/")
        encoded = urllib.parse.quote(rest, safe="/")
        return f"file://localhost/{drive}%3a/{encoded}"
    return "file://localhost/" + urllib.parse.quote(p.lstrip("/"), safe="/")


def _proxy_relpath_to_pathurl(relpath: str) -> str:
    return _windows_path_to_pathurl(
        DERIVATIVE_MEDIA_PATHURL_ROOT + "\\" + relpath.replace("/", "\\").lstrip("\\")
    )


def _norm_for_match(s: str) -> str:
    """Normalize a path-tail for fuzzy matching.

    Handles Apple's U+F022 PUA codepoint (Apple's filesystem-safe stand-in for
    ASCII colon ':'). When a Mac-side path contains a literal ':' the macOS
    filesystem layer replaces it with U+F022 on disk. Catalog source_paths
    captured via os.walk on Mac come back with U+F022, while xmeml pathurls
    encode the original ':' (URL-encoded as %3a). Both decode to visually
    identical strings but with different codepoints, so naive comparison
    misses. Also collapses runs of whitespace and lowercases."""
    s = s.replace("", ":").replace(":", "")
    # also collapse double spaces just in case
    while "  " in s:
        s = s.replace("  ", " ")
    return s.lower().strip()


def _folder_similarity(a: str, b: str) -> int:
    """Length of the longest common token prefix between two folder paths.
    Used to disambiguate filename-only fallback matches (prefer the catalog
    entry whose folder path best matches the XML's intended folder)."""
    at = a.replace("\\", "/").split("/")
    bt = b.replace("\\", "/").split("/")
    common = 0
    for x, y in zip(at, bt):
        if _norm_for_match(x) == _norm_for_match(y):
            common += 1
        else:
            break
    return common


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--xml", required=True, type=Path)
    ap.add_argument("--output", required=True, type=Path)
    args = ap.parse_args()

    # Read raw bytes — preserve BOM, line endings, every byte verbatim.
    raw = args.xml.read_bytes()
    has_bom = raw.startswith(b"\xef\xbb\xbf")
    text = raw.decode("utf-8-sig" if has_bom else "utf-8")

    # Build three resolver indexes from the editorial catalog:
    #   exact     — rel.lower() (handles 95%+ of paths, no encoding issues)
    #   normalized — _norm_for_match(rel) (handles U+F022 vs ASCII ':')
    #   by-filename — list of (rel, asset_id) per lowercased filename
    by_relpath_exact: dict[str, tuple[str, str, str]] = {}
    by_relpath_norm: dict[str, tuple[str, str, str]] = {}
    by_filename: dict[str, list[tuple[str, str, str]]] = {}
    con = sqlite3.connect(str(CATALOG_DB))
    for aid, sp, fn in con.execute("SELECT asset_id, source_path, filename FROM asset"):
        if sp and SOURCE_TREE_DIRNAME in sp:
            parts = sp.replace("\\", "/").split(SOURCE_TREE_DIRNAME + "/", 1)
            if len(parts) == 2:
                rel = parts[1]
                by_relpath_exact[rel.lower()] = (aid, sp, fn)
                by_relpath_norm[_norm_for_match(rel)] = (aid, sp, fn)
        if fn:
            by_filename.setdefault(fn.lower(), []).append((aid, sp or "", fn))
    con.close()
    asset_map = json.loads(ASSET_MAP_JSON.read_text(encoding="utf-8"))["entries"]

    stats = {"exact": 0, "norm": 0, "filename": 0, "miss": 0, "noop": 0}

    def _resolve_to_proxy(aid: str, ext: str) -> str | None:
        pm = asset_map.get(aid, {}) or {}
        for kind in EXT_TO_PROXY_KINDS.get(ext, ["video_video_proxy"]):
            v = pm.get(kind)
            if isinstance(v, dict) and v.get("relative_path"):
                return _proxy_relpath_to_pathurl(v["relative_path"])
        return None

    def resolve(purl: str) -> str:
        decoded = urllib.parse.unquote(purl)
        if "E%3a/open-post-stack" in purl or "E%3A/open-post-stack" in purl:
            stats["noop"] += 1
            return purl  # already a proxy path
        if SOURCE_TREE_DIRNAME + "/" not in decoded:
            stats["noop"] += 1
            return purl  # not a source-tree file — leave alone

        tail = decoded.split(SOURCE_TREE_DIRNAME + "/", 1)[1]
        ext = decoded.rsplit(".", 1)[-1].lower() if "." in decoded else ""

        # 1) exact
        hit = by_relpath_exact.get(tail.lower())
        if hit:
            proxy = _resolve_to_proxy(hit[0], ext)
            if proxy:
                stats["exact"] += 1
                return proxy
            # asset in catalog but proxy missing — fall through to placeholder

        # 2) normalized (handles U+F022 vs ':' and whitespace runs)
        hit = by_relpath_norm.get(_norm_for_match(tail))
        if hit:
            proxy = _resolve_to_proxy(hit[0], ext)
            if proxy:
                stats["norm"] += 1
                return proxy

        # 3) filename-only fallback — disambiguate via folder-path similarity
        fname = tail.rsplit("/", 1)[-1].lower()
        candidates = by_filename.get(fname, [])
        if candidates:
            xml_folder = tail.rsplit("/", 1)[0] if "/" in tail else ""
            best, best_score = None, -1
            for aid, sp, _fn in candidates:
                cat_folder = ""
                if SOURCE_TREE_DIRNAME in sp:
                    cat_folder = sp.replace("\\", "/").split(SOURCE_TREE_DIRNAME + "/", 1)[1]
                    cat_folder = cat_folder.rsplit("/", 1)[0] if "/" in cat_folder else ""
                score = _folder_similarity(xml_folder, cat_folder)
                if score > best_score:
                    best, best_score = (aid, sp), score
            if best and best_score >= 0:
                proxy = _resolve_to_proxy(best[0], ext)
                if proxy:
                    stats["filename"] += 1
                    return proxy

        # No catalog match (or matched but no proxy of expected kind) — emit
        # mirrored placeholder pathurl. Don't create the media here.
        stats["miss"] += 1
        sanitized_tail = tail.translate(_NTFS_FORBIDDEN_TABLE)
        return _proxy_relpath_to_pathurl(sanitized_tail)

    # Find every <pathurl>...</pathurl> and substitute. Use a non-greedy match.
    rewrites = []
    def _sub(m: re.Match) -> str:
        old = m.group(1)
        new = resolve(old)
        if new != old:
            rewrites.append((old, new))
        return f"<pathurl>{new}</pathurl>"

    new_text = re.sub(r"<pathurl>([^<]+)</pathurl>", _sub, text)

    # Re-encode and write. Preserve BOM if source had one.
    out_bytes = new_text.encode("utf-8")
    if has_bom:
        out_bytes = b"\xef\xbb\xbf" + out_bytes
    args.output.write_bytes(out_bytes)

    print(f"in:       {args.xml.name} ({len(raw)} B)")
    print(f"out:      {args.output.name} ({len(out_bytes)} B)")
    print(f"rewrites: {len(rewrites)}")
    print(f"BOM:      {'preserved' if has_bom else 'none in source'}")
    print(f"resolver: exact={stats['exact']}  norm={stats['norm']}  "
          f"filename={stats['filename']}  miss={stats['miss']}  noop={stats['noop']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
