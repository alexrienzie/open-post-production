"""
Print places/places.json as a hierarchical tree (parent_id graph).

Default: full tree, all roots. Filter or trim with flags below.

Usage:
  python _scripts/registries/print_locations_tree.py                        # full tree
  python _scripts/registries/print_locations_tree.py --root pl_wyoming      # subtree
  python _scripts/registries/print_locations_tree.py --root pl_united_states --max-depth 3
  python _scripts/registries/print_locations_tree.py --orphans              # only places missing a parent
  python _scripts/registries/print_locations_tree.py --min-mentions 3       # hide low-signal leaves
  python _scripts/registries/print_locations_tree.py --out places/_tree.txt
"""
from __future__ import annotations

import argparse
import io
import sys
from collections import defaultdict
from pathlib import Path

import json

ROOT = Path(__file__).resolve().parents[2]
LOC_PATH = ROOT / "places" / "places.json"


def load_places() -> list[dict]:
    return json.loads(LOC_PATH.read_text(encoding="utf-8")).get("places") or []


def render_tree(
    places: list[dict],
    *,
    root: str | None = None,
    max_depth: int | None = None,
    min_mentions: int = 0,
    show_unknown_type: bool = True,
    sink: io.TextIOBase = sys.stdout,
) -> None:
    by_id = {p["id"]: p for p in places if p.get("id")}
    children: dict[str, list[str]] = defaultdict(list)
    roots: list[str] = []
    for p in places:
        pid = p.get("id")
        if not pid:
            continue
        par = p.get("parent_id")
        if par and par in by_id:
            children[par].append(pid)
        else:
            roots.append(pid)

    for k in children:
        children[k].sort(
            key=lambda c: (
                -int(by_id[c].get("mention_count") or 0),
                by_id[c].get("canonical_name") or c,
            )
        )

    def walk(pid: str, depth: int, prefix: str, is_last: bool) -> None:
        if max_depth is not None and depth > max_depth:
            return
        rec = by_id[pid]
        m = int(rec.get("mention_count") or 0)
        if m < min_mentions and not children.get(pid):
            return
        if not show_unknown_type and rec.get("type") == "unknown" and not children.get(pid):
            return
        elbow = "" if depth == 0 else ("`-- " if is_last else "|-- ")
        line = f"{prefix}{elbow}{rec.get('canonical_name') or pid}  ({rec.get('type', '?')}, m={m}, {pid})"
        sink.write(line + "\n")
        next_prefix = "" if depth == 0 else (prefix + ("    " if is_last else "|   "))
        kids = children.get(pid, [])
        for i, c in enumerate(kids):
            walk(c, depth + 1, next_prefix, i == len(kids) - 1)

    if root:
        if root not in by_id:
            sink.write(f"unknown root: {root}\n")
            return
        walk(root, 0, "", True)
        return

    roots_sorted = sorted(
        roots,
        key=lambda c: (
            -int(by_id[c].get("mention_count") or 0),
            by_id[c].get("canonical_name") or c,
        ),
    )
    for i, r in enumerate(roots_sorted):
        walk(r, 0, "", True)


def list_orphans(places: list[dict], sink: io.TextIOBase) -> None:
    by_id = {p["id"]: p for p in places if p.get("id")}
    rows = []
    for p in places:
        if p.get("parent_id"):
            continue
        rows.append(
            (
                int(p.get("mention_count") or 0),
                p.get("type") or "?",
                p["id"],
                p.get("canonical_name") or "",
            )
        )
    rows.sort(reverse=True)
    sink.write(f"orphans (no parent_id): {len(rows)}\n")
    sink.write(f"{'mentions':>8}  {'type':<18}  {'id':<40}  name\n")
    for m, t, pid, name in rows:
        sink.write(f"{m:>8}  {t:<18}  {pid:<40}  {name}\n")


def coverage_summary(places: list[dict], sink: io.TextIOBase) -> None:
    total = len(places)
    with_parent = sum(1 for p in places if p.get("parent_id"))
    sink.write(f"places: {total}\n")
    sink.write(f"with parent_id: {with_parent} ({with_parent / max(total, 1):.0%})\n")
    sink.write(f"orphans: {total - with_parent}\n\n")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root")
    ap.add_argument("--max-depth", type=int, default=None)
    ap.add_argument("--min-mentions", type=int, default=0)
    ap.add_argument("--orphans", action="store_true", help="List orphans instead of tree")
    ap.add_argument("--no-unknown-leaves", action="store_true", help="Hide leaf nodes with type=unknown")
    ap.add_argument("--out", type=str, default=None, help="Write to file (UTF-8) instead of stdout")
    args = ap.parse_args()

    places = load_places()
    sink: io.TextIOBase
    if args.out:
        sink = open(args.out, "w", encoding="utf-8")
    else:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        sink = sys.stdout

    coverage_summary(places, sink)
    if args.orphans:
        list_orphans(places, sink)
    else:
        render_tree(
            places,
            root=args.root,
            max_depth=args.max_depth,
            min_mentions=args.min_mentions,
            show_unknown_type=not args.no_unknown_leaves,
            sink=sink,
        )

    if args.out:
        sink.close()
        print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
