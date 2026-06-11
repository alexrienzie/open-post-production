"""
Sidecar storage for heavy speaker provenance blocks so transcript JSON stays lean.

Transcript field (when using sidecars):
  "speaker_provenance": {
    "schema_version": 1,
    "sidecar": "_audit/transcript_provenance/<asset_id>.json"
  }

Sidecar file JSON:
  {
    "asset_id": "...",
    "speaker_resolution_audit": { ... },
    "speaker_confidence_audit": { ... },   // optional
    "linked_alignment": { ... },           // optional — donor/offset metadata (no transcript bloat)
    "applied_text_correction_batches": [] // optional
  }

Inline `speaker_resolution_audit` / `speaker_confidence_audit` on the transcript are still
supported (legacy); readers should use get_* helpers below.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
PROVENANCE_DIR = ROOT / "_audit" / "transcript_provenance"
SIDE_CAR_SCHEMA_VERSION = 1


def asset_id_from_transcript_path(path: Path) -> str:
    name = path.name
    if name.endswith(".transcript.json"):
        return name[: -len(".transcript.json")]
    return path.stem


def sidecar_relative_posix(asset_id: str) -> str:
    return f"_audit/transcript_provenance/{asset_id}.json"


def sidecar_path(asset_id: str) -> Path:
    return PROVENANCE_DIR / f"{asset_id}.json"


def atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _load_sidecar_file(asset_id: str) -> dict[str, Any]:
    p = sidecar_path(asset_id)
    if not p.exists():
        return {}
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return raw if isinstance(raw, dict) else {}


def get_speaker_resolution_audit(rec: dict[str, Any], *, asset_id: str) -> dict[str, Any] | None:
    inline = rec.get("speaker_resolution_audit")
    if isinstance(inline, dict) and inline:
        return inline
    prov = rec.get("speaker_provenance")
    if isinstance(prov, dict) and prov.get("sidecar"):
        data = _load_sidecar_file(asset_id)
        aud = data.get("speaker_resolution_audit")
        if isinstance(aud, dict) and aud:
            return aud
    return None


def get_speaker_confidence_audit(rec: dict[str, Any], *, asset_id: str) -> dict[str, Any] | None:
    inline = rec.get("speaker_confidence_audit")
    if isinstance(inline, dict) and inline:
        return inline
    prov = rec.get("speaker_provenance")
    if isinstance(prov, dict) and prov.get("sidecar"):
        data = _load_sidecar_file(asset_id)
        aud = data.get("speaker_confidence_audit")
        if isinstance(aud, dict) and aud:
            return aud
    return None


def attach_pointer(rec: dict[str, Any], asset_id: str) -> None:
    rec["speaker_provenance"] = {
        "schema_version": SIDE_CAR_SCHEMA_VERSION,
        "sidecar": sidecar_relative_posix(asset_id),
    }


def strip_inline_audits(rec: dict[str, Any]) -> None:
    rec.pop("speaker_resolution_audit", None)
    rec.pop("speaker_confidence_audit", None)


def merge_sidecar_fields(asset_id: str, fields: dict[str, Any]) -> None:
    """Merge top-level keys into the sidecar without dropping existing blocks."""
    cur = _load_sidecar_file(asset_id)
    if not isinstance(cur, dict):
        cur = {}
    cur["asset_id"] = asset_id
    for k, v in fields.items():
        if v is None:
            cur.pop(k, None)
        else:
            cur[k] = v
    atomic_write_json(sidecar_path(asset_id), cur)


def write_sidecar_merged(
    asset_id: str,
    *,
    speaker_resolution_audit: dict[str, Any] | None = None,
    speaker_confidence_audit: dict[str, Any] | None = None,
) -> None:
    """Merge into existing sidecar (preserves whichever audit block is not being replaced)."""
    cur = _load_sidecar_file(asset_id)
    out: dict[str, Any] = {k: v for k, v in cur.items() if k not in ("asset_id",)}
    out["asset_id"] = asset_id
    if speaker_resolution_audit is not None:
        out["speaker_resolution_audit"] = speaker_resolution_audit
    elif "speaker_resolution_audit" not in out:
        pass
    if speaker_confidence_audit is not None:
        out["speaker_confidence_audit"] = speaker_confidence_audit
    atomic_write_json(sidecar_path(asset_id), out)


def ensure_provenance_pointer_on_transcript(transcript_path: Path) -> bool:
    """If a sidecar file exists but the transcript lacks `speaker_provenance`, add the pointer."""
    aid = asset_id_from_transcript_path(transcript_path)
    if not sidecar_path(aid).exists():
        return False
    try:
        rec = json.loads(transcript_path.read_text(encoding="utf-8"))
    except Exception:
        return False
    if rec.get("speaker_provenance"):
        return False
    attach_pointer(rec, aid)
    atomic_write_json(transcript_path, rec)
    return True


def set_resolution_audit_on_transcript(
    rec: dict[str, Any],
    asset_id: str,
    audit: dict[str, Any],
    *,
    use_sidecar: bool = True,
) -> None:
    if use_sidecar:
        write_sidecar_merged(asset_id, speaker_resolution_audit=audit)
        attach_pointer(rec, asset_id)
        strip_inline_audits(rec)
    else:
        rec["speaker_resolution_audit"] = audit
        rec.pop("speaker_provenance", None)
