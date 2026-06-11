"""
Add or normalize `shoot_location` on catalog video / audio / still JSON.

Shape (always an object):
  { "place": str | null, "source": str | null }
  (`place`: `pl_*` slug or free-text; `source`: how it was set, e.g. `path`, `gps`, `manual`.)

Legacy scalar `null` or string values are normalized to this object.

- Key order: `shoot_location` immediately before `location` when `location` exists;
  otherwise immediately before `linked_assets` (typical layout), else append
  before `asset_classifications` if present, else at end.

Idempotent; atomic writes.

Usage:
  python _scripts/registries/backfill_shoot_location_catalog_assets.py
  python _scripts/registries/backfill_shoot_location_catalog_assets.py --dry-run
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SPECS: tuple[tuple[str, Path, str], ...] = (
    ("video", ROOT / "assets/video", "*.video.json"),
    ("audio", ROOT / "assets/audio", "*.audio.json"),
    ("still", ROOT / "assets/stills", "*.still.json"),
)


SHOOT_LOCATION_DEFAULT: dict[str, str | None] = {"place": None, "source": None}


def _opt_str(x: object) -> str | None:
    if x is None:
        return None
    if isinstance(x, str):
        return x
    return None


def normalize_shoot_location(val: object) -> dict[str, str | None]:
    if val is None:
        return dict(SHOOT_LOCATION_DEFAULT)
    if isinstance(val, str):
        return {"place": val, "source": None}
    if isinstance(val, dict):
        return {"place": _opt_str(val.get("place")), "source": _opt_str(val.get("source"))}
    return dict(SHOOT_LOCATION_DEFAULT)


def atomic_write_json(path: Path, obj: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def rebuild_with_shoot_location(obj: dict) -> dict:
    """Return a new dict with shoot_location placed per project convention."""
    val = normalize_shoot_location(obj.get("shoot_location"))
    body = {k: v for k, v in obj.items() if k != "shoot_location"}
    out: dict = {}
    placed = False
    for k, v in body.items():
        if k == "location":
            out["shoot_location"] = val
            placed = True
        out[k] = v
    if not placed:
        keys = list(out.keys())
        pivot = None
        for cand in ("linked_assets", "asset_classifications"):
            if cand in keys:
                pivot = cand
                break
        if pivot:
            new_out: dict = {}
            for k in keys:
                if k == pivot:
                    new_out["shoot_location"] = val
                new_out[k] = out[k]
            out = new_out
        else:
            out["shoot_location"] = val
    return out


def needs_rewrite(before: dict, after: dict) -> bool:
    if before.keys() != after.keys():
        return True
    for k in before:
        if before[k] != after[k]:
            return True
    return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    total = 0
    changed = 0

    for _kind, d, pat in SPECS:
        if not d.is_dir():
            continue
        for p in d.glob(pat):
            total += 1
            raw = json.loads(p.read_text(encoding="utf-8"))
            merged = rebuild_with_shoot_location(raw)
            if needs_rewrite(raw, merged):
                changed += 1
                if not args.dry_run:
                    atomic_write_json(p, merged)

    print(f"files_total={total}")
    print(f"files_changed={changed}")
    print(f"dry_run={args.dry_run}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
