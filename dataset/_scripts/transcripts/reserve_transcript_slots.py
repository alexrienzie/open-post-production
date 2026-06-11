"""
Reserve `analysis`, `craft`, `embedding_anchor` blocks on every transcript
record. Bumps schema_version 3 → 4.

Idempotent. Atomic writes. Adds blocks only if missing — preserves any
already-populated fields.

Run after this lands and you regenerate the prompt context doc, the LLM batch
runs have target slots ready.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TRANSCRIPTS = ROOT / "assets/transcripts"


EMPTY_ANALYSIS = {
    "summary_one_line": None,
    "summary_paragraph": None,
    "topics": [],
    "themes": [],
    "tone": {"mood": None, "energy": None, "formality": None},
    "key_quotes": [],
    "key_moments": [],
    "storylines": [],
    "analyzed_at": None,
    "analyzer": None,
}

EMPTY_CRAFT = {
    "shot_kind": None,
    "audio_quality": None,
}


def atomic_write_json(path: Path, data: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def compute_text_anchor(rec: dict) -> str:
    """Hash of the canonical text-for-embedding so we can detect content changes later."""
    parts = [rec.get("full_text") or ""]
    an = rec.get("analysis") or {}
    if an.get("summary_one_line"):
        parts.append(an["summary_one_line"])
    parts.extend(sorted(an.get("topics") or []))
    h = hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()
    return h


def migrate_one(rec: dict) -> tuple[bool, list[str]]:
    """Returns (changed, list_of_added_blocks)."""
    if rec.get("schema_version") == 4:
        return False, []
    added = []

    if "analysis" not in rec:
        rec["analysis"] = dict(EMPTY_ANALYSIS)
        rec["analysis"]["tone"] = dict(EMPTY_ANALYSIS["tone"])
        added.append("analysis")
    else:
        # Already partial — fill missing keys without clobbering populated ones
        a = rec["analysis"]
        for k, v in EMPTY_ANALYSIS.items():
            if k not in a:
                a[k] = (dict(v) if isinstance(v, dict) else (list(v) if isinstance(v, list) else v))
                added.append(f"analysis.{k}")
        if "tone" in a and isinstance(a["tone"], dict):
            for k, v in EMPTY_ANALYSIS["tone"].items():
                if k not in a["tone"]:
                    a["tone"][k] = v

    if "craft" not in rec:
        rec["craft"] = dict(EMPTY_CRAFT)
        added.append("craft")

    if "embedding_anchor" not in rec:
        rec["embedding_anchor"] = {
            "text_sha256": compute_text_anchor(rec),
            "last_embedded_at": None,
        }
        added.append("embedding_anchor")

    if "place_ids" not in rec:
        rec["place_ids"] = []
        added.append("place_ids")

    rec["schema_version"] = 4
    return True, added


def main() -> int:
    if not TRANSCRIPTS.exists():
        print(f"ERROR: {TRANSCRIPTS} not found", file=sys.stderr)
        return 1

    examined = 0
    migrated = 0
    skipped = 0
    errors = 0
    blocks_added: dict[str, int] = {}

    for p in TRANSCRIPTS.glob("*.json"):
        examined += 1
        try:
            rec = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            errors += 1
            continue
        if rec.get("schema_version") == 4:
            skipped += 1
            continue
        changed, added = migrate_one(rec)
        if changed:
            atomic_write_json(p, rec)
            migrated += 1
            for b in added:
                blocks_added[b] = blocks_added.get(b, 0) + 1

    print(f"=== Transcript slot reservation (v3 → v4) ===")
    print(f"  examined: {examined}")
    print(f"  migrated: {migrated}")
    print(f"  skipped (already v4): {skipped}")
    print(f"  errors: {errors}")
    if blocks_added:
        print(f"  blocks added:")
        for k, c in sorted(blocks_added.items(), key=lambda x: -x[1]):
            print(f"    {c:>5}  {k}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
