#!/usr/bin/env python3
"""
build_transcripts.py — Read MacWhisper's SQLite, write per-asset transcripts to
the staging directory `derivative media/_transcript staging/` (the canonical v5 schema).
The downstream promotion step moves those into the canonical
`dataset/assets/transcripts/` and runs entity backfill; this script does
NOT flip `has_machine_transcript` on the video / audio record.

Idempotent: skips any asset whose transcript JSON already exists in (a) the
active out-dir, (b) the canonical catalog dir, or (c) the pre-port legacy
location at `/Volumes/WorkspaceSSD/Old Data/Whisper Transcripts/` (kept as a safety
check for runs that landed before the path port).

Usage:
    python3 dataset/_scripts/extraction/build_transcripts.py [--dry-run] [--purge-external-media] [--force]
"""
import os, sys, json, sqlite3, argparse, shutil
from pathlib import Path
from datetime import datetime, timezone
from collections import Counter

import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))  # shared modules live at _scripts root
from _paths import VIDEO_CATALOG, AUDIO_CATALOG, TRANSCRIPT_CATALOG, TRANSCRIPT_STAGING

TRANSCRIPT_OUT_DIR = TRANSCRIPT_STAGING
TRANSCRIPT_CANONICAL_DIR = TRANSCRIPT_CATALOG
# Pre-port staging on the old the workspace SSD layout; absent on freshly-set-up machines.
PRE_PORT_OUT_DIR = Path("/Volumes/WorkspaceSSD/Old Data/Whisper Transcripts")

MW_DB = Path(os.path.expanduser(
    "~/Library/Application Support/MacWhisper/Database/main.sqlite"))
MW_EXTERNAL_MEDIA = MW_DB.parent / "ExternalMedia"

TRANSCRIPT_SCHEMA_VERSION = 5


def now_utc_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def atomic_write_json(path, obj):
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=2)
    os.replace(tmp, path)


def ms_to_sec(ms):
    return round(ms / 1000.0, 3) if ms is not None else None


def find_record(asset_id):
    """Return (record_path, record, kind) or (None, None, None)."""
    vp = VIDEO_CATALOG / f"{asset_id}.video.json"
    if vp.exists():
        return vp, json.loads(vp.read_text()), "video"
    ap = AUDIO_CATALOG / f"{asset_id}.audio.json"
    if ap.exists():
        return ap, json.loads(ap.read_text()), "audio"
    return None, None, None


def main():
    global TRANSCRIPT_OUT_DIR

    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--purge-external-media", action="store_true",
                    help="After build, delete WAV duplicates in MacWhisper's ExternalMedia/")
    ap.add_argument("--force", action="store_true",
                    help="Rewrite even if existing transcript JSON has matching session_id")
    ap.add_argument("--out-dir", default=str(TRANSCRIPT_OUT_DIR),
                    help=f"Override the staging directory (default: {TRANSCRIPT_OUT_DIR})")
    args = ap.parse_args()

    TRANSCRIPT_OUT_DIR = Path(args.out_dir)

    print(f"=== build_transcripts.py | {now_utc_iso()} ===")
    print(f"Transcripts out: {TRANSCRIPT_OUT_DIR}")
    if not MW_DB.exists():
        print(f"ABORT: {MW_DB} missing"); sys.exit(1)
    if not TRANSCRIPT_OUT_DIR.parent.exists():
        print(f"ABORT: {TRANSCRIPT_OUT_DIR.parent} not accessible — is the workspace SSD mounted?")
        sys.exit(1)
    TRANSCRIPT_OUT_DIR.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(MW_DB.as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    sessions = conn.execute("""
        SELECT hex(id) AS sid_hex, originalFilename, modelEngine, modelIdentifer,
               detectedLanguage, playbackDuration, timeTakenToTranscribe,
               fullText, hasBeenDiarized, dateCreated
        FROM session
        WHERE transcriptionDidSucceed = 1
          AND (dateDeleted IS NULL OR dateDeleted = 0)
          AND length(originalFilename) = 64
        ORDER BY dateCreated ASC
    """).fetchall()
    print(f"Successful sessions in DB: {len(sessions)}")

    # Latest-per-asset
    by_asset = {}
    for row in sessions:
        by_asset[row["originalFilename"]] = row
    print(f"Unique asset_ids: {len(by_asset)}")

    counters = Counter()
    speaker_cache = {}
    record_updates = {}  # rec_path -> (rec, has_machine_transcript)

    for asset_id, sess in by_asset.items():
        rec_path, rec, kind = find_record(asset_id)
        if rec is None:
            counters["no_catalog_match"] += 1
            continue

        transcript_path = TRANSCRIPT_OUT_DIR / f"{asset_id}.transcript.json"
        pre_port_path = PRE_PORT_OUT_DIR / f"{asset_id}.transcript.json"
        canonical_path = TRANSCRIPT_CANONICAL_DIR / f"{asset_id}.transcript.json"

        if not args.force:
            if transcript_path.exists():
                counters["already_in_active_out"] += 1
                continue
            if pre_port_path != transcript_path and pre_port_path.exists():
                counters["already_in_pre_port_staging"] += 1
                continue
            if canonical_path.exists():
                counters["already_in_alex_canonical"] += 1
                continue

        # Pull lines
        lines = conn.execute(f"""
            SELECT start, end, text, hex(speakerID) AS spk_hex, wordsJson
            FROM transcriptline
            WHERE sessionId = X'{sess["sid_hex"]}'
            ORDER BY start ASC
        """).fetchall()

        segments = []
        speaker_ids_in_session = set()
        for ln in lines:
            words = []
            if ln["wordsJson"]:
                try:
                    for w in json.loads(ln["wordsJson"]):
                        words.append({
                            "text": w.get("text", ""),
                            "start_sec": ms_to_sec(w.get("startTime")),
                            "end_sec": ms_to_sec(w.get("endTime")),
                        })
                except json.JSONDecodeError:
                    pass
            spk = ln["spk_hex"]
            if spk:
                speaker_ids_in_session.add(spk)
            segments.append({
                "start_sec": ms_to_sec(ln["start"]),
                "end_sec": ms_to_sec(ln["end"]),
                "text": ln["text"],
                "speaker_raw": spk,
                "speaker": None,        # canonical (filled downstream)
                "words": words,
            })

        # Speakers
        speakers_raw = {}
        for spk in speaker_ids_in_session:
            if spk not in speaker_cache:
                row = conn.execute("SELECT name, isStub FROM speaker WHERE hex(id) = ?",
                                   (spk,)).fetchone()
                speaker_cache[spk] = (
                    {"name": row["name"], "is_stub": bool(row["isStub"])}
                    if row else {"name": "Unknown", "is_stub": True}
                )
            speakers_raw[spk] = speaker_cache[spk]

        # Build v5 transcript record
        # Normalize MW dateCreated which arrives as "YYYY-MM-DD HH:MM:SS.fff"
        raw_dc = sess["dateCreated"] or ""
        if " " in raw_dc and "T" not in raw_dc:
            transcribed_at = raw_dc.replace(" ", "T") + "Z"
        else:
            transcribed_at = raw_dc

        transcript = {
            "schema_version": TRANSCRIPT_SCHEMA_VERSION,
            "asset_id": asset_id,
            "manifest": {
                "current_version": 1,
                "model": {
                    "engine": sess["modelEngine"],
                    "id": sess["modelIdentifer"],
                },
                "language": sess["detectedLanguage"],
                "transcribed_at": transcribed_at,
                "updated_at": now_utc_iso(),
                "_macwhisper_session_id": sess["sid_hex"],
            },
            "playback_duration_sec": sess["playbackDuration"],
            "has_been_diarized": bool(sess["hasBeenDiarized"]),
            "speakers_raw": speakers_raw,
            "people_ids": [],
            "org_ids": [],
            "place_ids": [],
            "beat_ids": [],
            "embeddings": {"text_uri": None, "model": None},
            "record_kind": "transcript",
            "primary_timeline_date": rec.get("primary_timeline_date"),
            "full_text": sess["fullText"] or "",
            "segments": segments,
        }

        if args.dry_run:
            counters["would_write"] += 1
            continue

        atomic_write_json(transcript_path, transcript)
        counters["written"] += 1
        # Do NOT flip has_machine_transcript on the video record here.
        # The promotion step sets that when re-integrating the transcripts.

    conn.close()

    print()
    print("=== Summary ===")
    for k, v in counters.most_common():
        print(f"  {k:<22}: {v}")

    if args.purge_external_media and not args.dry_run:
        if MW_EXTERNAL_MEDIA.exists():
            files = list(MW_EXTERNAL_MEDIA.iterdir())
            total_gb = sum(f.stat().st_size for f in files if f.is_file()) / 1e9
            print(f"\nPurging {len(files)} files from {MW_EXTERNAL_MEDIA} ({total_gb:.2f} GB)...")
            for f in files:
                try:
                    if f.is_file():
                        f.unlink()
                except OSError as e:
                    print(f"  could not delete {f.name}: {e}")
            print("Purge complete.")


if __name__ == "__main__":
    main()
