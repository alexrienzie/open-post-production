"""
WS2-B — Catalog freshness audit (read-only).

Checks (best-effort):
- MANIFEST.json record counts vs on-disk counts (JSON dirs + JSONL files)
- Orphan transcript records (no corresponding video/audio catalog record)
- machine_transcript boolean drift vs transcript file presence
- Audio extract "orphans" (extract exists but source_path missing) + missing extracts
- Proxy ↔ master drift using assets/catalog_prep/raid_media_inventory.csv:
  - proxy without a master (by stem match)
  - master newer than proxy (proxy stale)

Output:
  _runs/freshness_<utc_ts>/report.md
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

ROOT = Path(__file__).resolve().parents[2]

MANIFEST_PATH = ROOT / "MANIFEST.json"

CATALOG_VIDEO = ROOT / "assets/video"
CATALOG_AUDIO = ROOT / "assets/audio"
CATALOG_STILLS = ROOT / "assets/stills"
CATALOG_TRANSCRIPTS = ROOT / "assets/transcripts"

CATALOG_HUMAN_ROSTER = ROOT / "assets/_human transcripts/index.jsonl"
CATALOG_HUMAN_DOCX = ROOT / "assets/_human transcripts/docx_manifest.jsonl"

CATALOG_ARTICLES = ROOT / "documents/press/articles"
CATALOG_COMMENTS = ROOT / "documents/press/comments"
CATALOG_SOCIAL = ROOT / "documents/press/social_posts"

CATALOG_US_EVENTS = ROOT / "timeline/us_events.jsonl"

RAID_MEDIA_INVENTORY = ROOT / "assets/catalog_prep/raid_media_inventory.csv"

RUNS_DIR = ROOT / "_runs"


def _utc_ts_slug() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d_%H%M%S")


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _count_json_dir(d: Path, suffix: str) -> int:
    if not d.exists():
        return 0
    return sum(1 for _ in d.glob(f"*{suffix}"))


def _count_jsonl(path: Path) -> int:
    if not path.exists():
        return 0
    n = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            n += 1
    return n


def _parse_iso_ts(s: str) -> Optional[dt.datetime]:
    if not isinstance(s, str) or not s:
        return None
    try:
        # Inventory mtime_utc uses 7-digit fractional seconds + Z, e.g. 2024-08-13T12:14:26.0000000Z
        if s.endswith("Z"):
            s2 = s[:-1] + "+00:00"
        else:
            s2 = s
        return dt.datetime.fromisoformat(s2)
    except Exception:
        return None


def _short_list(items: list[str], limit: int) -> list[str]:
    return items[:limit]


@dataclass
class Finding:
    title: str
    count: int
    examples: list[str]
    note: str = ""


def audit_manifest_counts() -> list[Finding]:
    if not MANIFEST_PATH.exists():
        return [Finding(title="MANIFEST.json missing", count=1, examples=[str(MANIFEST_PATH)])]
    m = _load_json(MANIFEST_PATH)
    by_id: dict[str, int] = {}
    for c in (m.get("catalogs") or []):
        if isinstance(c, dict) and c.get("id"):
            by_id[str(c["id"])] = int(c.get("record_count") or 0)

    # Local on-disk counts (catalog truth)
    actual = {
        "video": _count_json_dir(CATALOG_VIDEO, ".video.json"),
        "audio": _count_json_dir(CATALOG_AUDIO, ".audio.json"),
        "still": _count_json_dir(CATALOG_STILLS, ".still.json"),
        "transcript": _count_json_dir(CATALOG_TRANSCRIPTS, ".transcript.json"),
        "article": _count_json_dir(CATALOG_ARTICLES, ".json"),
        "comment": _count_json_dir(CATALOG_COMMENTS, ".json"),
        "social_post": _count_json_dir(CATALOG_SOCIAL, ".json"),
        "us_event": _count_jsonl(CATALOG_US_EVENTS),
        "human_transcript_roster": _count_jsonl(CATALOG_HUMAN_ROSTER),
        "human_transcript_docx": _count_jsonl(CATALOG_HUMAN_DOCX),
    }

    drift: list[str] = []
    for k, a in actual.items():
        man = by_id.get(k)
        if man is None:
            drift.append(f"{k}: MANIFEST missing entry (disk={a})")
        elif man != a:
            drift.append(f"{k}: MANIFEST={man} vs disk={a}")

    return [Finding(title="MANIFEST ↔ disk count drift", count=len(drift), examples=_short_list(drift, 50),
                    note="Counts are per directory (JSON) or non-empty lines (JSONL).")]


def _catalog_asset_id_from_filename(p: Path) -> str:
    # <asset_id>.video.json / <asset_id>.audio.json / <asset_id>.still.json / <asset_id>.transcript.json
    name = p.name
    for suf in (".video.json", ".audio.json", ".still.json", ".transcript.json"):
        if name.endswith(suf):
            return name[: -len(suf)]
    return p.stem


def audit_transcript_orphans() -> list[Finding]:
    # Orphan transcript = transcript exists but neither video nor audio record exists.
    vid_ids = {p.name.replace(".video.json", "") for p in CATALOG_VIDEO.glob("*.video.json")}
    aud_ids = {p.name.replace(".audio.json", "") for p in CATALOG_AUDIO.glob("*.audio.json")}

    orphan: list[str] = []
    for p in CATALOG_TRANSCRIPTS.glob("*.transcript.json"):
        aid = _catalog_asset_id_from_filename(p)
        if aid not in vid_ids and aid not in aud_ids:
            orphan.append(f"{aid}: transcript exists but no video/audio record ({p.as_posix()})")

    return [Finding(title="Orphan transcript records", count=len(orphan), examples=_short_list(orphan, 50),
                    note="Orphan = transcript has no corresponding video/audio catalog JSON.")]


def audit_has_machine_transcript_drift() -> list[Finding]:
    # Best-effort: compare machine_transcript bool to transcript file presence.
    drift: list[str] = []

    transcript_ids = {p.name.replace(".transcript.json", "") for p in CATALOG_TRANSCRIPTS.glob("*.transcript.json")}

    for p in list(CATALOG_VIDEO.glob("*.video.json")) + list(CATALOG_AUDIO.glob("*.audio.json")):
        try:
            rec = _load_json(p)
        except Exception:
            continue
        aid = rec.get("asset_id") or _catalog_asset_id_from_filename(p)
        raw = rec.get("machine_transcript", rec.get("has_machine_transcript"))
        has = bool(raw)
        present = aid in transcript_ids
        if has != present:
            drift.append(f"{aid}: {p.name} machine_transcript={has} but transcript_present={present}")

    return [Finding(title="machine_transcript drift", count=len(drift), examples=_short_list(drift, 50),
                    note="Expected: machine_transcript matches transcript JSON presence.")]


def _maybe_existing_path(path_str: str) -> Optional[Path]:
    if not isinstance(path_str, str) or not path_str.strip():
        return None
    # Absolute Windows path or POSIX-ish; Path handles both reasonably.
    p = Path(path_str)
    if p.is_absolute() and p.exists():
        return p
    # If relative, treat as workspace-relative.
    rel = (ROOT / path_str)
    if rel.exists():
        return rel
    return None


def _raid_seems_mounted() -> bool:
    # Heuristic: treat the canonical root as "mounted" if the drive + directory exist.
    # This prevents low-signal "missing source media" spam when running on a machine
    # without the RAID connected.
    return Path(r"D:\Project").exists()


def audit_audio_extracts(*, check_media_existence: bool) -> list[Finding]:
    missing_extract: list[str] = []
    orphan_extract: list[str] = []
    missing_source: list[str] = []

    for p in list(CATALOG_VIDEO.glob("*.video.json")) + list(CATALOG_AUDIO.glob("*.audio.json")):
        try:
            rec = _load_json(p)
        except Exception:
            continue
        aid = rec.get("asset_id") or _catalog_asset_id_from_filename(p)

        source_path = rec.get("source_path") or ""
        source_exists = True
        if check_media_existence:
            source_exists = _maybe_existing_path(source_path) is not None
            if not source_exists:
                missing_source.append(f"{aid}: source_path missing on disk: {source_path!r} ({p.name})")

        ae = rec.get("audio_extract") or {}
        if not isinstance(ae, dict) or not ae.get("path"):
            continue
        extract_path = str(ae.get("path"))
        extract_exists = True
        if check_media_existence:
            extract_exists = _maybe_existing_path(extract_path) is not None

        if extract_exists and not source_exists:
            orphan_extract.append(f"{aid}: extract exists ({extract_path}) but source missing ({source_path!r})")
        if (not extract_exists) and source_exists:
            missing_extract.append(f"{aid}: extract missing ({extract_path}) but source exists ({source_path})")

    return [
        Finding(title="Missing source media (catalog ↔ disk)", count=len(missing_source),
                examples=_short_list(missing_source, 50),
                note=(
                    "Disabled when RAID is not mounted (or when --check-media-existence is off). "
                    "Run this audit on a machine with the RAID connected for high-signal results."
                )),
        Finding(title="Missing audio extracts", count=len(missing_extract), examples=_short_list(missing_extract, 50),
                note="extract missing but source exists (best-effort existence check)."),
        Finding(title="Orphan audio extracts", count=len(orphan_extract), examples=_short_list(orphan_extract, 50),
                note="extract exists but source_path missing (potential orphan/corruption)."),
    ]


def audit_proxy_master_drift(*, enabled: bool) -> list[Finding]:
    if not enabled:
        return [
            Finding(
                title="Proxy ↔ master drift (legacy RAID proxy inventory)",
                count=0,
                examples=[],
                note=(
                    "Skipped. This check uses assets/catalog_prep/raid_media_inventory.csv and only "
                    "reflects proxies present on the RAID inventory snapshot. Your current proxy set "
                    "lives on another machine (e.g. director’s Mac) and isn’t audited here."
                ),
            )
        ]
    if not RAID_MEDIA_INVENTORY.exists():
        return [Finding(title="Proxy ↔ master drift (raid_media_inventory.csv missing)", count=0, examples=[],
                        note=f"Expected at {RAID_MEDIA_INVENTORY.as_posix()}")]

    proxies: list[dict] = []
    masters_by_stem: dict[str, list[dict]] = {}
    with RAID_MEDIA_INVENTORY.open(encoding="utf-8", newline="") as f:
        r = csv.DictReader(f)
        for row in r:
            if (row.get("media_class") or "").strip() != "video":
                continue
            stem = (row.get("stem") or "").strip()
            if not stem:
                continue
            is_proxy = (row.get("is_proxy_path") or "").strip() == "1"
            if is_proxy:
                proxies.append(row)
            else:
                masters_by_stem.setdefault(stem, []).append(row)

    proxy_orphan: list[str] = []
    proxy_stale: list[str] = []

    for pr in proxies:
        stem = (pr.get("stem") or "").strip()
        p_path = pr.get("full_path") or ""
        p_mtime = _parse_iso_ts(pr.get("mtime_utc") or "")
        masters = masters_by_stem.get(stem) or []
        if not masters:
            proxy_orphan.append(f"{stem}: proxy has no master match by stem ({p_path})")
            continue
        # Only auto-compare when there's exactly one master candidate — otherwise ambiguous.
        if len(masters) != 1:
            continue
        mr = masters[0]
        m_path = mr.get("full_path") or ""
        m_mtime = _parse_iso_ts(mr.get("mtime_utc") or "")
        if p_mtime and m_mtime and m_mtime > p_mtime:
            proxy_stale.append(f"{stem}: master newer than proxy (master={m_mtime.isoformat()} {m_path}; "
                               f"proxy={p_mtime.isoformat()} {p_path})")

    return [
        Finding(title="Proxy files without a master (by stem match)", count=len(proxy_orphan),
                examples=_short_list(proxy_orphan, 50),
                note="Heuristic: proxy row is_proxy_path=1; master matched by identical stem."),
        Finding(title="Stale proxies (master newer than proxy)", count=len(proxy_stale),
                examples=_short_list(proxy_stale, 50),
                note="Only computed for stems with exactly one master candidate (avoids ambiguous matches)."),
    ]


def write_report(out_dir: Path, findings: list[Finding]) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    report_path = out_dir / "report.md"

    now = dt.datetime.now(dt.timezone.utc).isoformat()
    lines: list[str] = []
    lines.append(f"# Freshness audit report\n")
    lines.append(f"- Generated at (UTC): `{now}`")
    lines.append(f"- Workspace: `{ROOT}`")
    lines.append("")

    abnormal = [f for f in findings if f.count]
    lines.append("## Summary")
    lines.append(f"- Findings with non-zero count: **{len(abnormal)}** / {len(findings)}")
    for f in abnormal:
        lines.append(f"- **{f.title}**: {f.count}")
    if not abnormal:
        lines.append("- No abnormalities detected by this audit.")
    lines.append("")

    lines.append("## Details")
    for f in findings:
        lines.append(f"### {f.title}")
        lines.append(f"- Count: **{f.count}**")
        if f.note:
            lines.append(f"- Note: {f.note}")
        if f.examples:
            lines.append("")
            lines.append("Examples:")
            for ex in f.examples:
                lines.append(f"- {ex}")
        lines.append("")

    report_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return report_path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-dir", type=Path, default=None,
                    help="Output directory. Default: _runs/freshness_<ts>/")
    ap.add_argument(
        "--check-media-existence",
        action=argparse.BooleanOptionalAction,
        default=None,
        help=(
            "Whether to check that source_path/audio_extract.path exist on disk. "
            "Default: auto (enabled only if the RAID appears mounted)."
        ),
    )
    ap.add_argument(
        "--audit-raid-proxies",
        action="store_true",
        help=(
            "Enable legacy proxy↔master drift checks using assets/catalog_prep/raid_media_inventory.csv. "
            "This does NOT cover the current proxy set on other machines."
        ),
    )
    ap.add_argument("--max-examples", type=int, default=50,
                    help="Max examples per finding section (currently fixed at 50).")
    args = ap.parse_args()

    ts = _utc_ts_slug()
    out_dir = args.out_dir.resolve() if args.out_dir else (RUNS_DIR / f"freshness_{ts}")

    findings: list[Finding] = []
    findings.extend(audit_manifest_counts())
    findings.extend(audit_transcript_orphans())
    findings.extend(audit_has_machine_transcript_drift())
    check_media = bool(args.check_media_existence) if args.check_media_existence is not None else _raid_seems_mounted()
    findings.extend(audit_audio_extracts(check_media_existence=check_media))
    findings.extend(audit_proxy_master_drift(enabled=bool(args.audit_raid_proxies)))

    report_path = write_report(out_dir, findings)
    print(f"[freshness] wrote {report_path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

