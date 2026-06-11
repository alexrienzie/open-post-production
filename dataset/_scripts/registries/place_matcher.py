"""Match free-text locations to canonical `pl_*` place IDs from places/places.json."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path


def normalize_text(text: str) -> str:
    t = (text or "").lower()
    t = re.sub(r"[^a-z0-9]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


@dataclass(frozen=True)
class PlaceMatch:
    pl_id: str
    matched_phrase: str
    confidence: str  # high | medium | low


class PlaceRegistry:
    def __init__(self, places: list[dict]):
        self.by_id: dict[str, dict] = {
            p["id"]: p for p in places if isinstance(p, dict) and p.get("id")
        }
        # (normalized phrase, pl_id, raw phrase) — longest phrases first at match time
        self._phrases: list[tuple[str, str, str]] = []
        for pl_id, p in self.by_id.items():
            names = [p.get("canonical_name") or ""]
            names.extend(p.get("aliases") or [])
            seen: set[str] = set()
            for raw in names:
                if not isinstance(raw, str) or not raw.strip():
                    continue
                norm = normalize_text(raw)
                if len(norm) < 4 or norm in seen:
                    continue
                seen.add(norm)
                self._phrases.append((norm, pl_id, raw.strip()))
        self._phrases.sort(key=lambda x: len(x[0]), reverse=True)

    @classmethod
    def from_dataset(cls, dataset_root: Path) -> PlaceRegistry:
        path = dataset_root / "places" / "places.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(data.get("places") or [])

    def match_location(self, location: str | None, *, min_phrase_len: int = 5) -> list[PlaceMatch]:
        if not location or not isinstance(location, str):
            return []
        hay = normalize_text(location)
        if not hay:
            return []
        hits: dict[str, PlaceMatch] = {}
        for norm_phrase, pl_id, raw in self._phrases:
            if len(norm_phrase) < min_phrase_len:
                continue
            if norm_phrase not in hay:
                continue
            if pl_id in hits:
                continue
            conf = "high" if len(norm_phrase) >= 12 else "medium"
            hits[pl_id] = PlaceMatch(pl_id=pl_id, matched_phrase=raw, confidence=conf)
        return list(hits.values())

    def expand_with_parents(self, pl_ids: list[str], *, max_depth: int = 3) -> list[str]:
        out: list[str] = []
        seen: set[str] = set()
        for pl_id in pl_ids:
            cur = pl_id
            for _ in range(max_depth):
                if not cur or cur in seen:
                    break
                seen.add(cur)
                out.append(cur)
                parent = (self.by_id.get(cur) or {}).get("parent_id")
                if not parent or parent not in self.by_id:
                    break
                cur = parent
        return out
