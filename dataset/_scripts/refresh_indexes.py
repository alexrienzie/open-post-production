"""
Rebuild all derived catalog indexes.

This refreshes:
- MANIFEST.json (via build_indexes.py)
- `../indexes/editorial_catalog.sqlite` join-surface (via build_editor_db.py)

Usage:
  python _scripts/refresh_indexes.py
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def refresh_all_indexes(*, quiet: bool = False) -> None:
    # Import locally so callers can reuse this function without side effects
    # at module import time.
    sys.path.insert(0, str(ROOT / "_scripts"))
    import build_indexes  # type: ignore
    import build_editor_db  # type: ignore

    if not quiet:
        print("[indexes] rebuilding MANIFEST.json + reverse indexes")
    build_indexes.main()

    if not quiet:
        print("[indexes] rebuilding ../indexes/editorial_catalog.sqlite")
    build_editor_db.main()

    if not quiet:
        print("[indexes] refresh complete")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quiet", action="store_true", help="Suppress status prints.")
    args = ap.parse_args()
    refresh_all_indexes(quiet=args.quiet)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

