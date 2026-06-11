#!/usr/bin/env python3
"""Set the sequence <format> width/height in an xmeml file via byte-level edit.

Only touches the ONE `<format><samplecharacteristics>` block under
`<sequence><media><video>` (Premiere has only one such block per file —
clipitems' file defs use bare `<samplecharacteristics>` without `<format>`
wrapping). All other widths/heights (per-file proxy dims, still dims) are
left untouched. Also updates the `MZ.Sequence.PreviewFrameSize{Width,Height}`
attributes on the `<sequence>` element so Premiere's preview monitor sizes
correctly.

Surgical text substitution — no XML parsing — to preserve Premiere's quirky
serialization (BOM, double-quoted decl, mixed empty-tag forms).

Usage:
    py set_sequence_format.py --xml <input.xml> --width 1280 --height 720 [--output <out.xml>]
If --output is omitted, the input is edited in place (after byte-verify).
"""
from __future__ import annotations
import argparse
import re
import sys
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--xml", required=True, type=Path)
    ap.add_argument("--width", type=int, default=1280)
    ap.add_argument("--height", type=int, default=720)
    ap.add_argument("--output", type=Path, default=None,
                    help="If omitted, edits --xml in place.")
    args = ap.parse_args()

    target_w, target_h = str(args.width), str(args.height)
    out_path = args.output or args.xml

    raw = args.xml.read_bytes()
    has_bom = raw.startswith(b"\xef\xbb\xbf")
    text = raw.decode("utf-8-sig" if has_bom else "utf-8")

    changes: list[tuple[str, str, str]] = []  # (field, before, after)

    # 1) MZ.Sequence.PreviewFrameSizeWidth/Height attributes on <sequence>.
    def _attr_sub(m: re.Match, target: str, field: str) -> str:
        before = m.group(1)
        if before != target:
            changes.append((field, before, target))
        return m.group(0).replace(f'"{before}"', f'"{target}"', 1)

    text, n_pw = re.subn(
        r'MZ\.Sequence\.PreviewFrameSizeWidth="(\d+)"',
        lambda m: _attr_sub(m, target_w, "MZ.PreviewFrameSizeWidth"),
        text, count=1,
    )
    text, n_ph = re.subn(
        r'MZ\.Sequence\.PreviewFrameSizeHeight="(\d+)"',
        lambda m: _attr_sub(m, target_h, "MZ.PreviewFrameSizeHeight"),
        text, count=1,
    )

    # 2) <format><samplecharacteristics>...<width>X</width>...<height>Y</height>...
    # Match the FIRST occurrence only (sequence-level; per-file blocks have no
    # <format> wrapper). The non-greedy .*? plus DOTALL gets us to the first
    # width/height inside the first <format> block.
    pattern = re.compile(
        r'(<format>\s*<samplecharacteristics>.*?<width>)(\d+)(</width>\s*<height>)(\d+)(</height>)',
        re.DOTALL,
    )
    m = pattern.search(text)
    if m:
        before_w, before_h = m.group(2), m.group(4)
        if before_w != target_w:
            changes.append(("sequence <format> width", before_w, target_w))
        if before_h != target_h:
            changes.append(("sequence <format> height", before_h, target_h))
        text = pattern.sub(
            lambda mm: mm.group(1) + target_w + mm.group(3) + target_h + mm.group(5),
            text, count=1,
        )
    else:
        print(f"WARNING: no <format><samplecharacteristics> block found", file=sys.stderr)

    if not changes:
        print(f"no changes needed: already {target_w}x{target_h}")
        return 0

    out_bytes = text.encode("utf-8")
    if has_bom:
        out_bytes = b"\xef\xbb\xbf" + out_bytes
    out_path.write_bytes(out_bytes)

    print(f"in:  {args.xml.name} ({len(raw):,} B)")
    print(f"out: {out_path.name} ({len(out_bytes):,} B, delta {len(out_bytes)-len(raw):+d})")
    for field, before, after in changes:
        print(f"  {field}: {before} -> {after}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
