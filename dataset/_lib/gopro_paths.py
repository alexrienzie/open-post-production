"""Heuristics for GoPro-style source paths (filename and folder segments)."""

from __future__ import annotations

import re
from pathlib import Path


def is_gopro_source_path(source_path: str) -> bool:
    """GOPR* / GH###### / GX###### clips, or any path segment containing 'gopro'."""
    pl = source_path.replace("/", "\\")
    for seg in pl.split("\\"):
        if "gopro" in seg.casefold():
            return True
    stem = Path(source_path).stem.casefold()
    if re.match(r"^gopr\d", stem):
        return True
    if re.match(r"^gh\d{6}$", stem):
        return True
    if re.match(r"^gx\d{6}$", stem):
        return True
    return False
