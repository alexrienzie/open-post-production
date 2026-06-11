#!/usr/bin/env python3
"""
Emit catalog link edges touching human-manifest components with a coarse **tier**:

- tier 1: both endpoints are human clip manifest `asset_id`s (strong human anchor).
- tier 2: symmetric catalog neighbor (each lists the other) but not both manifest.
- tier 3: directed catalog edge only (asymmetric).

Output: `_audit/link_edge_tiers_<timestamp>.jsonl` (no transcript or catalog mutation).

Usage:
  python _scripts/links/build_link_edge_tiers.py
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
AUDIT_DIR = ROOT / "_audit"

sys.path.insert(0, str(ROOT / "_scripts"))
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))  # shared modules live at _scripts root
from human_link_components import (  # noqa: E402
    discover_components,
    is_symmetric_neighbor,
    linked_neighbors,
    load_manifest_asset_ids,
)


def main() -> int:
    manifest = load_manifest_asset_ids()
    if not manifest:
        print("empty manifest", file=sys.stderr)
        return 1
    run_id = datetime.now(timezone.utc).strftime("link_edge_tiers_%Y%m%dT%H%M%SZ")
    out_path = AUDIT_DIR / f"{run_id}.jsonl"

    comps = discover_components(manifest)
    seen_edges: set[tuple[str, str]] = set()
    n = 0
    with out_path.open("w", encoding="utf-8") as f:
        for comp in comps:
            for aid in sorted(comp):
                for nb in sorted(linked_neighbors(aid)):
                    if nb not in comp:
                        continue
                    a, b = (aid, nb) if aid < nb else (nb, aid)
                    if (a, b) in seen_edges:
                        continue
                    seen_edges.add((a, b))
                    m1 = a in manifest
                    m2 = b in manifest
                    sym = is_symmetric_neighbor(a, b)
                    if m1 and m2:
                        tier = 1
                    elif sym:
                        tier = 2
                    else:
                        tier = 3
                    rec = {
                        "run_id": run_id,
                        "asset_a": a,
                        "asset_b": b,
                        "tier": tier,
                        "manifest_a": m1,
                        "manifest_b": m2,
                        "symmetric": sym,
                    }
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                    n += 1

    print(json.dumps({"run_id": run_id, "edges_written": n, "out": str(out_path.relative_to(ROOT))}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
