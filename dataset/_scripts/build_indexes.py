"""
Build the root MANIFEST.json for retrieval.

Outputs:
  MANIFEST.json   (root) — machine-readable index of all catalogs + registries
"""
from __future__ import annotations

import json
import os
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from workspace_paths import (
    clip_and_still_embeddings_sqlite_path,
    editorial_catalog_sqlite_path,
    transcript_rolling_embeddings_sqlite_path,
)

ROOT = Path(__file__).resolve().parent.parent


def _manifest_relpath(absolute: Path) -> str:
    """POSIX path for MANIFEST `join_views` entries, relative to `dataset/` (ROOT)."""
    return Path(os.path.relpath(absolute.resolve(), ROOT.resolve())).as_posix()



# Align with README.md + STATS.json between schema-version bumps.
WORKSPACE_VERSION = "2026-05-04"


def _load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def read_records(
    d: Path,
    *,
    skip_stats: dict[str, int] | None = None,
    decode_paths: dict[str, list[str]] | None = None,
    root: Path | None = None,
):
    """Yield (path, record) pairs. Handles both per-file JSONs in a directory
    and a single JSONL file.

    If skip_stats is provided, counts JSON/JSONL decode failures (non-fatal).
    If decode_paths is provided (with root), append relative paths / jsonl#Ln for failures.
    """
    def bump(key: str, n: int = 1) -> None:
        if skip_stats is not None:
            skip_stats[key] = skip_stats.get(key, 0) + n

    def note_path(key: str, ref: str) -> None:
        if decode_paths is None or root is None:
            return
        decode_paths.setdefault(key, []).append(ref)

    def rel_file(p: Path) -> str:
        return str(p.relative_to(root)).replace("\\", "/")

    if not d.exists():
        return
    if d.is_file() and d.suffix == ".jsonl":
        try:
            for lineno, line in enumerate(d.read_text(encoding="utf-8").splitlines(), start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    yield d, json.loads(line)
                except Exception:
                    bump("jsonl_line_decode_errors")
                    note_path("jsonl_line_decode_errors", f"{rel_file(d)}#L{lineno}")
        except Exception:
            bump("jsonl_file_read_errors")
            if root is not None:
                note_path("jsonl_file_read_errors", rel_file(d))
            return
        return
    for p in d.glob("*.json"):
        try:
            yield p, json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            bump("json_file_decode_errors")
            if root is not None:
                note_path("json_file_decode_errors", rel_file(p))
            continue


def main() -> None:

    read_warnings: dict[str, int] = {}
    decode_paths: dict[str, list[str]] = {}

    catalog_specs = [
        ("video",       ROOT / "assets/video"),
        ("audio",       ROOT / "assets/audio"),
        ("still",       ROOT / "assets/stills"),
        ("transcript",  ROOT / "assets/transcripts"),
        ("article",     ROOT / "documents/press/articles"),
        ("comment",     ROOT / "documents/press/comments"),
        ("social_post", ROOT / "documents/press/social_posts"),
    ]

    record_counts: dict[str, int] = {}

    for kind, d in catalog_specs:
        n = 0
        for p, rec in read_records(d, skip_stats=read_warnings, decode_paths=decode_paths, root=ROOT):
            n += 1
        record_counts[kind] = n

    people_doc = _load_json(ROOT / "people/people.json")
    orgs_doc = _load_json(ROOT / "organizations/orgs.json")
    places_doc = _load_json(ROOT / "places/places.json")
    _moments_path = ROOT / "story/moments.json"
    moments_doc = json.loads(_moments_path.read_text(encoding="utf-8")) if _moments_path.exists() else None

    people_count = len(people_doc.get("people") or [])
    orgs_count = len(orgs_doc.get("organizations") or [])
    places_count = len(places_doc.get("places") or [])

    people_registry_version = (people_doc.get("_meta") or {}).get("registry_version")
    orgs_registry_version = (orgs_doc.get("_meta") or {}).get("registry_version")
    people_registry_schema = people_doc.get("schema_version", 2)
    orgs_registry_schema = orgs_doc.get("schema_version", 1)
    places_registry_schema = places_doc.get("schema_version", 1)
    places_registry_version = (places_doc.get("_meta") or {}).get("registry_version")


    # Build MANIFEST.json
    manifest = {
        "workspace_version": WORKSPACE_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "schema_versions": {
            "video": 7,
            "audio": 5,
            "still": 4,
            "transcript": 5,
            "article": 3,
            "comment": 3,
            "social_post": 2,
            "people_registry": people_registry_schema,
            "orgs_registry": orgs_registry_schema,
            "places_registry": places_registry_schema,
        },
        "catalogs": [
            {
                "id": "video", "path": "assets/video/", "schema_version": 7,
                "primary_key": "asset_id", "record_count": record_counts.get("video", 0),
                "foreign_keys": {
                    "linked_assets": "typed edges: video/audio/stills buckets → target_asset_id + link_kind + established_by",
                    "transcript": "assets/transcripts/{asset_id}.transcript.json (people/org/moment tags live here; presence ⇒ machine transcript)",
                },
            },
            {
                "id": "audio", "path": "assets/audio/", "schema_version": 5,
                "primary_key": "asset_id", "record_count": record_counts.get("audio", 0),
                "foreign_keys": {
                    "linked_assets": "typed edges (e.g. audio_video_transcript → video; same_kind_audio → audio)",
                    "transcript": "assets/transcripts/{asset_id}.transcript.json (entity tags)",
                },
            },
            {
                "id": "still", "path": "assets/stills/", "schema_version": 4,
                "primary_key": "asset_id", "record_count": record_counts.get("still", 0),
                "foreign_keys": {
                    "linked_assets": "typed edges (e.g. still_to_video → video)",
                },
            },
            {
                "id": "transcript", "path": "assets/transcripts/", "schema_version": 5,
                "primary_key": "asset_id (matches video/audio asset_id)", "record_count": record_counts.get("transcript", 0),
                "foreign_keys": {
                    "people_ids[]": "people/people.json#people[].id",
                    "org_ids[]": "organizations/orgs.json#organizations[].id",
                    "speakers[].p_id": "people/people.json#people[].id",
                    "asset_id": "assets/{video,audio}/{asset_id}.{kind}.json",
                    "moment_ids[]": "story/moments.json#moments_outline[].moment_id",
                },
            },
            {
                "id": "article", "path": "documents/press/articles/", "schema_version": 3,
                "primary_key": "article_id", "record_count": record_counts.get("article", 0),
                "foreign_keys": {
                    "people_ids[]": "people/people.json#people[].id",
                    "org_ids[]": "organizations/orgs.json#organizations[].id",
                    "moment_ids[]": "story/moments.json#moments_outline[].moment_id",
                },
            },
            {
                "id": "comment", "path": "documents/press/comments/", "schema_version": 3,
                "primary_key": "comment_id", "record_count": record_counts.get("comment", 0),
                "foreign_keys": {
                    "parent.id (kind=article)": "documents/press/articles/{article_id}.json",
                    "parent.id (kind=social_post)": "documents/press/social_posts/{post_id}.json",
                    "people_ids[]": "people/people.json#people[].id",
                    "org_ids[]": "organizations/orgs.json#organizations[].id",
                    "moment_ids[]": "story/moments.json#moments_outline[].moment_id",
                },
            },
            {
                "id": "social_post", "path": "documents/press/social_posts/", "schema_version": 2,
                "primary_key": "post_id", "record_count": record_counts.get("social_post", 0),
                "foreign_keys": {
                    "people_ids[]": "people/people.json#people[].id",
                    "org_ids[]": "organizations/orgs.json#organizations[].id",
                    "moment_ids[]": "story/moments.json#moments_outline[].moment_id",
                    "comments[]": "documents/press/comments/{comment_id}.json",
                },
            },
        ],
        "registries": [
            {"id": "people", "path": "people/people.json",
             "schema_version": people_registry_schema,
             "registry_version": people_registry_version,
             "record_count": people_count, "primary_key": "id"},
            {"id": "orgs", "path": "organizations/orgs.json",
             "schema_version": orgs_registry_schema,
             "registry_version": orgs_registry_version,
             "record_count": orgs_count, "primary_key": "id"},
            {"id": "moments", "path": "story/moments.json",
             "schema_version": moments_doc.get("schema_version", 1) if moments_doc else 1,
             "record_count": len((moments_doc or {}).get("moments_outline") or []), "primary_key": "moment_id"},
            {"id": "places", "path": "places/places.json",
             "schema_version": places_registry_schema,
             "registry_version": places_registry_version,
             "record_count": places_count, "primary_key": "id"},
        ],
        "join_views": {
            "editorial_catalog": {
                "path": _manifest_relpath(editorial_catalog_sqlite_path()),
                "note": "stdlib sqlite3; DuckDB can ATTACH this file later",
                "built_by": "_scripts/build_editor_db.py",
                "views": ["v_asset", "v_segment", "v_event", "v_press_mention", "v_person_appearance"],
            },
            "clip_and_still_embeddings": {
                "path": _manifest_relpath(clip_and_still_embeddings_sqlite_path()),
                "note": "Optional sidecar (not produced by rebuild_all). SigLIP vectors + chunk registry (semantic_chunks/semantic_stills rows without JSON after slim). Editorial text lives in catalog asset_semantic_summary. Tables: semantic_chunks, clip_embeddings, semantic_stills, still_embeddings.",
                "tables": [
                    "semantic_chunks",
                    "clip_embeddings",
                    "semantic_stills",
                    "still_embeddings",
                ],
            },
            "transcript_rolling_embeddings": {
                "path": _manifest_relpath(transcript_rolling_embeddings_sqlite_path()),
                "note": "Optional. Rolling-window transcript text embeddings (float32 BLOB). Built by `_scripts/transcripts/embed_transcript_rolling_windows.py`.",
                "tables": ["embedding_run", "transcript_window_embedding"],
            },
        },
    }

    (ROOT / "MANIFEST.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    print("MANIFEST.json built.")
    print(f"  totals:    {sum(record_counts.values())} records indexed across {len(record_counts)} catalogs")
    if read_warnings:
        print(f"  read_warnings: {read_warnings}")
    if decode_paths:
        print("  read_warning paths:")
        for k, paths in sorted(decode_paths.items()):
            for ref in paths[:20]:
                print(f"    [{k}] {ref}")
            if len(paths) > 20:
                print(f"    ... and {len(paths) - 20} more for [{k}]")


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(
        description="Build dataset/MANIFEST.json from on-disk catalog JSON."
    )
    ap.add_argument(
        "command",
        nargs="?",
        default="build",
        choices=["build"],
        help="Subcommand (default: build MANIFEST.json)",
    )
    args = ap.parse_args()
    if args.command == "build":
        main()
