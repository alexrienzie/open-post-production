#!/usr/bin/env python3
"""build_face_index.py — Face index pipeline for INGEST.md candidate Phase K


Subcommands:
  stills    P0 — detect + embed faces on cataloged stills (smoke + first pass)
  video     P1 — same on video keyframes (timestamps from clip_embeddings)
  seed      P2 — pull seed exemplars from single-person-tagged assets, auto-tag matches
  cluster   P3a — HDBSCAN/DBSCAN over unmatched faces, populate face_cluster
  label     P3b — CLI labeling pass over top clusters
  status    Print coverage stats: detections by record_kind, identified vs unknown

Idempotent at the asset level — skips any asset_id that already has face_detection
rows unless --force.

Usage:
    .../python build_face_index.py stills [--limit N] [--force]
    .../python build_face_index.py video [--limit N] [--asset-ids file]
    .../python build_face_index.py status
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Make _faces importable; it also injects _paths
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _faces import (  # noqa: E402
    open_db, get_face_app, load_image_bgr, extract_frame_at,
    faces_to_rows, INSERT_FACE_SQL, INSERT_PROCESSED_SQL,
    EMBEDDING_DIM, FACE_EMBEDDINGS_DB, pack_embedding, unpack_embedding,
)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from _paths import (  # noqa: E402
    DERIVATIVE_MEDIA, VIDEO_CATALOG, STILLS_CATALOG, INDEXES_DIR, RUNS_DIR,
    FACE_EXEMPLARS_DIR, PEOPLE_REGISTRY,
    derivative_relative, resolve_proxy_via_asset_map,
)


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def already_indexed(con: sqlite3.Connection, asset_id: str) -> bool:
    cur = con.execute(
        "SELECT 1 FROM face_detection WHERE asset_id=? LIMIT 1", (asset_id,)
    )
    return cur.fetchone() is not None


def record_run_start(con: sqlite3.Connection, phase: str, args: argparse.Namespace) -> int:
    cur = con.execute(
        "INSERT INTO face_run (phase, started_at, args_json) VALUES (?, ?, ?)",
        (phase, now_iso(), json.dumps(vars(args), default=str)),
    )
    con.commit()
    return cur.lastrowid


def record_run_end(con: sqlite3.Connection, run_pk: int, summary: dict) -> None:
    con.execute(
        "UPDATE face_run SET finished_at=?, summary_json=? WHERE run_pk=?",
        (now_iso(), json.dumps(summary, default=str), run_pk),
    )
    con.commit()


# ---------------- stills (P0) ----------------

def cmd_stills(args: argparse.Namespace) -> None:
    con = open_db()
    run_pk = record_run_start(con, "stills", args)

    files = sorted(p for p in STILLS_CATALOG.glob("*.still.json") if not p.name.startswith("._"))
    if args.limit:
        files = files[: args.limit]
    print(f"=== build_face_index stills | {now_iso()} ===")
    print(f"Catalog stills: {len(files)}")

    # Lazy-load model (heavy import + 280 MB on first run)
    print("loading insightface buffalo_l...")
    app = get_face_app()
    print("ready.")

    counters = {"processed": 0, "already_done": 0, "no_disk_path": 0,
                "raw_or_unreadable": 0, "no_face": 0, "with_faces": 0,
                "total_faces": 0, "errors": 0}
    t_start = time.time()
    last_print = [0]

    for i, f in enumerate(files, 1):
        try:
            d = json.loads(f.read_text())
        except Exception:
            counters["errors"] += 1
            continue

        aid = d.get("asset_id")
        if not aid:
            counters["errors"] += 1
            continue

        if not args.force and already_indexed(con, aid):
            counters["already_done"] += 1
            continue

        sp = d.get("source_path") or ""
        try:
            rel = derivative_relative(sp)
        except ValueError:
            counters["no_disk_path"] += 1
            continue
        img_path = DERIVATIVE_MEDIA / rel
        if not img_path.exists():
            counters["no_disk_path"] += 1
            continue

        img = load_image_bgr(img_path)
        if img is None:
            counters["raw_or_unreadable"] += 1
            continue

        try:
            faces = app.get(img)
        except Exception as e:
            counters["errors"] += 1
            print(f"  ERR {aid[:12]} {img_path.name}: {e}")
            continue

        counters["processed"] += 1
        still_chunk_id = f"still:{aid}"
        ts = now_iso()
        con.execute(INSERT_PROCESSED_SQL,
                    (still_chunk_id, 0, aid, "still", len(faces), ts))
        if not faces:
            counters["no_face"] += 1
            continue
        counters["with_faces"] += 1
        counters["total_faces"] += len(faces)

        rows = list(faces_to_rows(aid, "still", still_chunk_id, 0, 0.0, faces, ts))
        con.executemany(INSERT_FACE_SQL, rows)
        if counters["processed"] % 50 == 0:
            con.commit()

        if i - last_print[0] >= 25 or i == len(files):
            elapsed = time.time() - t_start
            done = counters["processed"] + counters["already_done"]
            rate = done / elapsed if elapsed else 0
            eta = (len(files) - i) / rate if rate else 0
            print(f"[{i:>5}/{len(files)}] processed={counters['processed']:5d} "
                  f"faces={counters['total_faces']:5d}  "
                  f"elapsed={elapsed/60:5.1f}m  ETA={eta/60:5.1f}m", flush=True)
            last_print[0] = i

    con.commit()
    elapsed = time.time() - t_start
    summary = {**counters, "elapsed_sec": round(elapsed, 1)}
    record_run_end(con, run_pk, summary)

    print(f"\n=== Summary ===")
    print(f"Elapsed: {elapsed/60:.1f} min")
    for k, v in counters.items():
        print(f"  {k:<20s}: {v}")
    print(f"\nDB: {FACE_EMBEDDINGS_DB}")


# ---------------- video (P1) ----------------

# Asset types we expect to be largely faceless and skip by default.
FACELESS_TYPES = ("b_roll", "timelapse")


def _build_video_worklist(con: sqlite3.Connection, asset_ids_filter: set[str] | None):
    """Return list of (chunk_id, parent_asset_id, frame_idx, abs_time_sec, proxy_path).
    Skips assets in FACELESS_TYPES, frames already processed, and assets whose
    proxy can't be resolved via asset_map.json.
    """
    con.execute(f"ATTACH DATABASE '{INDEXES_DIR / 'editorial_catalog.sqlite'}' AS ec")
    con.execute(f"ATTACH DATABASE '{INDEXES_DIR / 'clip_and_still_embeddings.sqlite'}' AS clip")

    eligible_assets = set()
    for (aid,) in con.execute(
        "SELECT asset_id FROM ec.asset WHERE record_kind='video' "
        f"AND (asset_type IS NULL OR asset_type NOT IN {FACELESS_TYPES})"
    ):
        eligible_assets.add(aid)
    print(f"  face-eligible video assets in catalog: {len(eligible_assets)}")

    processed = set()
    for (cid, fidx) in con.execute(
        "SELECT chunk_id, frame_idx FROM face_processed_frame WHERE record_kind='video'"
    ):
        processed.add((cid, fidx))
    print(f"  frames already processed: {len(processed)}")

    raw_rows = con.execute("""
        SELECT ce.chunk_id, gc.parent_asset_id, ce.frame_idx,
               COALESCE(gc.chunk_start_sec, 0.0) + ce.timestamp_sec AS abs_time
        FROM clip.clip_embeddings ce
        JOIN clip.semantic_chunks gc ON ce.chunk_id = gc.chunk_id
    """).fetchall()
    print(f"  total clip_embeddings rows: {len(raw_rows)}")

    work = []
    proxy_cache: dict[str, Path | None] = {}
    skip_filter = skip_processed = skip_no_proxy = 0
    for cid, aid, fidx, abs_t in raw_rows:
        if aid not in eligible_assets:
            skip_filter += 1
            continue
        if asset_ids_filter and aid not in asset_ids_filter:
            skip_filter += 1
            continue
        if (cid, fidx) in processed:
            skip_processed += 1
            continue
        if aid not in proxy_cache:
            proxy_cache[aid] = resolve_proxy_via_asset_map(aid)
        proxy = proxy_cache[aid]
        if proxy is None or not proxy.exists():
            skip_no_proxy += 1
            continue
        work.append((cid, aid, fidx, abs_t, proxy))

    print(f"  filtered out (asset_type / --asset-ids): {skip_filter}")
    print(f"  skipped (already processed):             {skip_processed}")
    print(f"  skipped (no proxy on disk):              {skip_no_proxy}")
    print(f"  remaining to process:                    {len(work)}")
    return work


def cmd_video(args: argparse.Namespace) -> None:
    from concurrent.futures import ThreadPoolExecutor

    con = open_db()
    run_pk = record_run_start(con, "video", args)
    print(f"=== build_face_index video | {now_iso()} ===")

    asset_filter = None
    if args.asset_ids:
        asset_filter = {ln.strip() for ln in open(args.asset_ids) if ln.strip()}
        print(f"--asset-ids filter: {len(asset_filter)} ids")

    work = _build_video_worklist(con, asset_filter)
    if args.limit:
        work = work[: args.limit]
        print(f"  limited to first {len(work)}")
    if not work:
        print("nothing to do.")
        record_run_end(con, run_pk, {"processed": 0, "note": "no_work"})
        return

    print("loading insightface buffalo_l...")
    app = get_face_app()
    print("ready.")

    # ffmpeg extraction is the slow path — parallelize it. Detection serial
    # (single ONNX session, already multi-threaded internally).
    extract_pool = ThreadPoolExecutor(
        max_workers=args.workers, thread_name_prefix="ff")
    in_flight: dict = {}  # future -> (cid, aid, fidx, abs_t, proxy)
    PREFETCH = max(args.workers * 4, 16)

    counters = {"processed": 0, "extract_fail": 0, "no_face": 0,
                "with_faces": 0, "total_faces": 0, "errors": 0}
    t_start = time.time()
    last_print = [time.time()]
    iter_work = iter(work)

    def prefetch():
        while len(in_flight) < PREFETCH:
            try:
                cid, aid, fidx, abs_t, proxy = next(iter_work)
            except StopIteration:
                return
            fut = extract_pool.submit(extract_frame_at, proxy, abs_t)
            in_flight[fut] = (cid, aid, fidx, abs_t, proxy)

    try:
        prefetch()
        while in_flight:
            # Pull the first completed future (FIFO-ish)
            done_fut = next(iter(in_flight))
            for f in list(in_flight.keys()):
                if f.done():
                    done_fut = f
                    break
            # Block on done_fut
            img = done_fut.result()
            cid, aid, fidx, abs_t, proxy = in_flight.pop(done_fut)
            prefetch()

            ts = now_iso()
            if img is None:
                counters["extract_fail"] += 1
                con.execute(INSERT_PROCESSED_SQL, (cid, fidx, aid, "video", 0, ts))
            else:
                try:
                    faces = app.get(img)
                except Exception as e:
                    counters["errors"] += 1
                    print(f"  ERR {aid[:12]} chunk={cid} fidx={fidx}: {e}")
                    continue
                counters["processed"] += 1
                con.execute(INSERT_PROCESSED_SQL,
                            (cid, fidx, aid, "video", len(faces), ts))
                if not faces:
                    counters["no_face"] += 1
                else:
                    counters["with_faces"] += 1
                    counters["total_faces"] += len(faces)
                    rows = list(faces_to_rows(
                        aid, "video", cid, fidx, abs_t, faces, ts))
                    con.executemany(INSERT_FACE_SQL, rows)

            done = counters["processed"] + counters["extract_fail"]
            if done % 200 == 0:
                con.commit()
            now = time.time()
            if now - last_print[0] >= 15:
                elapsed = now - t_start
                rate = done / elapsed if elapsed else 0
                remaining = len(work) - done
                eta_min = (remaining / rate / 60) if rate else 0
                print(
                    f"[{done:>6}/{len(work)}] {100*done/len(work):5.1f}%  "
                    f"rate={rate:5.1f}/s  faces={counters['total_faces']:6d}  "
                    f"with_face={counters['with_faces']:5d}  "
                    f"no_face={counters['no_face']:5d}  err={counters['errors']}  "
                    f"elapsed={elapsed/60:5.1f}m  ETA={eta_min:5.1f}m",
                    flush=True)
                last_print[0] = now
    finally:
        extract_pool.shutdown(wait=True)
        con.commit()

    elapsed = time.time() - t_start
    summary = {**counters, "elapsed_sec": round(elapsed, 1),
               "total_frames": len(work)}
    record_run_end(con, run_pk, summary)
    print(f"\n=== Summary ===")
    print(f"Elapsed: {elapsed/60:.1f} min")
    for k, v in counters.items():
        print(f"  {k:<16s}: {v}")


# ---------------- seed (P2a) ----------------

def _is_human(person: dict) -> bool:
    """Filter dogs / pets / non-human entities from the people registry."""
    roles = " ".join(person.get("roles") or []).lower()
    if any(tag in roles for tag in ("(dog)", "(cat)", "(animal)", "pet", "(horse)")):
        return False
    notes = (person.get("notes") or "").lower()
    if "non-human" in notes:
        return False
    return True


def _load_humans() -> dict[str, str]:
    """p_id -> canonical_name for human persons only."""
    data = json.loads(PEOPLE_REGISTRY.read_text())
    out = {}
    for p in (data.get("people") or []):
        pid = p.get("id") or p.get("p_id")
        if not pid or not _is_human(p):
            continue
        out[pid] = p.get("canonical_name") or pid
    return out


def _load_humans_full() -> list[dict]:
    """Full person records for humans only, for callers that need aliases too."""
    data = json.loads(PEOPLE_REGISTRY.read_text())
    return [
        p for p in (data.get("people") or [])
        if (p.get("id") or p.get("p_id")) and _is_human(p)
    ]


def _build_alias_map(humans_full: list[dict]) -> dict[str, set[str]]:
    """Lowercase alias / name token -> set of candidate p_ids.

    Includes canonical_name, every entry in `aliases[]`, and each whitespace
    token of both. Returning a set (not a single id) makes the caller handle
    ambiguity explicitly instead of arbitrary first-iteration shadowing."""
    from collections import defaultdict
    PUNCT = ".,'\";:!?"
    m: dict[str, set[str]] = defaultdict(set)
    for p in humans_full:
        pid = p.get("id") or p.get("p_id")
        names = [p.get("canonical_name")] + list(p.get("aliases") or [])
        for n in names:
            if not n:
                continue
            low = n.lower().strip()
            m[low].add(pid)
            for tok in low.split():
                tok = tok.strip(PUNCT)
                if len(tok) >= 2:
                    m[tok].add(pid)
    return dict(m)


def _resolve_name(query: str, alias_map: dict[str, set[str]]) -> set[str]:
    """Resolve a user-typed string to a set of candidate p_ids.

    Empty result = unknown; single = unambiguous; multiple = needs disambiguation.
    For multi-word input not present as a direct alias, intersect the per-token
    matches (so 'mike sunseri' picks Michelino out of all the Mikes + Sunseris)."""
    if not query:
        return set()
    q = query.lower().strip()
    if q.startswith("p_"):
        return {q}
    if q in alias_map:
        return set(alias_map[q])
    tokens = [t.strip(".,'\";:!?") for t in q.split() if t.strip(".,'\";:!?")]
    if len(tokens) < 2:
        return set()
    matched: set[str] | None = None
    for tok in tokens:
        if tok not in alias_map:
            return set()
        matched = set(alias_map[tok]) if matched is None else (matched & alias_map[tok])
        if not matched:
            return set()
    return matched or set()


def _crop_face(img, bbox, margin: float = 0.15):
    """Crop face from BGR image with margin around bbox [x1,y1,x2,y2]."""
    import cv2  # noqa: F401
    h, w = img.shape[:2]
    x1, y1, x2, y2 = bbox
    bw, bh = x2 - x1, y2 - y1
    pad_x = int(bw * margin)
    pad_y = int(bh * margin)
    x1 = max(0, x1 - pad_x)
    y1 = max(0, y1 - pad_y)
    x2 = min(w, x2 + pad_x)
    y2 = min(h, y2 + pad_y)
    return img[y1:y2, x1:x2]


def _refetch_image(con: sqlite3.Connection, face_row: dict):
    """Re-fetch the source image for a face row so we can save an exemplar crop.
    Returns BGR numpy array or None."""
    aid = face_row["asset_id"]
    kind = face_row["record_kind"]
    if kind == "still":
        # Pull source_path from editorial_catalog
        r = con.execute(
            "SELECT source_path FROM ec.asset WHERE asset_id=?", (aid,)
        ).fetchone()
        if not r:
            return None
        try:
            disk = DERIVATIVE_MEDIA / derivative_relative(r[0])
        except ValueError:
            return None
        if not disk.exists():
            return None
        return load_image_bgr(disk)
    # video — seek into proxy at frame_time_sec
    proxy = resolve_proxy_via_asset_map(aid)
    if proxy is None or not proxy.exists():
        return None
    return extract_frame_at(proxy, face_row["frame_time_sec"])


def cmd_seed(args: argparse.Namespace) -> None:
    """Build the seed exemplar bank by mining single-person-tagged assets.

    For each human in the people registry: find assets where they're the only
    tagged person, collect high-quality face detections from those assets,
    cluster intra-person to pin down the dominant face (rules out interviewers
    / B-roll faces that snuck in), and save the top-N as exemplars."""
    import numpy as np

    con = open_db()
    run_pk = record_run_start(con, "seed", args)
    print(f"=== build_face_index seed | {now_iso()} ===")

    con.execute(f"ATTACH DATABASE '{INDEXES_DIR / 'editorial_catalog.sqlite'}' AS ec")

    humans = _load_humans()
    print(f"  humans in registry: {len(humans)}")

    # Single-person-tagged assets, grouped by p_id, ordered by signal
    single_tagged = list(con.execute("""
        SELECT p_id, asset_id FROM ec.asset_people
        WHERE asset_id IN (
            SELECT asset_id FROM ec.asset_people GROUP BY asset_id HAVING COUNT(*)=1
        )
    """))
    from collections import defaultdict
    by_person = defaultdict(list)
    for pid, aid in single_tagged:
        if pid in humans:
            by_person[pid].append(aid)
    print(f"  humans with ≥1 single-tag asset: {len(by_person)}")

    # Cap to top-N most-tagged people for the seed pass
    ranked = sorted(by_person.items(), key=lambda kv: -len(kv[1]))[:args.top_people]
    print(f"  seeding top {len(ranked)} people (--top-people={args.top_people})\n")

    counters = {"seeded": 0, "skipped_too_few": 0, "skipped_no_dominant": 0,
                "exemplars_written": 0, "crops_saved": 0, "crops_failed": 0}

    if args.save_crops:
        FACE_EXEMPLARS_DIR.mkdir(parents=True, exist_ok=True)

    for pid, asset_ids in ranked:
        if len(asset_ids) < args.min_assets:
            counters["skipped_too_few"] += 1
            continue
        name = humans[pid]
        # Pull candidate face detections from those assets
        placeholder = ",".join("?" * len(asset_ids))
        cands = list(con.execute(f"""
            SELECT face_pk, asset_id, record_kind, frame_idx, frame_time_sec,
                   bbox_json, det_score, embedding
            FROM face_detection
            WHERE asset_id IN ({placeholder}) AND det_score >= ?
        """, (*asset_ids, args.min_det_score)))

        # Quality gate: bbox area
        qualified = []
        for face_pk, aid, kind, fidx, ft, bbox_json, score, emb_blob in cands:
            bbox = json.loads(bbox_json)
            area = max(0, bbox[2] - bbox[0]) * max(0, bbox[3] - bbox[1])
            if area < args.min_area:
                continue
            qualified.append({
                "face_pk": face_pk, "asset_id": aid, "record_kind": kind,
                "frame_idx": fidx, "frame_time_sec": ft,
                "bbox": bbox, "det_score": score, "area": area,
                "embedding": unpack_embedding(emb_blob),
            })

        if len(qualified) < args.min_candidates:
            counters["skipped_too_few"] += 1
            print(f"  SKIP {name:30s} ({pid})  candidates={len(qualified)} < {args.min_candidates}")
            continue

        # Dominant-cluster pick: for each candidate, count how many others have
        # cosine similarity > agreement_threshold. The highest-agreement
        # candidate sits at the densest point of the dominant cluster — for
        # single-person-tagged assets, this is overwhelmingly the tagged person
        # (other faces are interviewers / B-roll passers-by who appear sparsely
        # by comparison). Top-K by agreement = the cleanest exemplar set.
        embs = np.stack([c["embedding"] for c in qualified]).astype(np.float32)
        with np.errstate(over="ignore", invalid="ignore"):
            sim = embs @ embs.T  # cosine (L2-normalized)
        agreement = (sim > args.agreement_threshold).sum(axis=1)
        dominant_size = int(agreement.max())
        if dominant_size < args.min_candidates:
            counters["skipped_no_dominant"] += 1
            print(f"  SKIP {name:30s} ({pid})  max_agree={dominant_size} "
                  f"< min_candidates={args.min_candidates}")
            continue

        # Pick top exemplars by agreement, tie-break by det_score * sqrt(area)
        scored = [(agreement[i], qualified[i]["det_score"] * (qualified[i]["area"] ** 0.5), i)
                  for i in range(len(qualified))]
        scored.sort(reverse=True)
        top_idx = [i for _, _, i in scored[:args.exemplars]]

        # Wipe existing exemplars for this p_id (idempotent re-runs)
        con.execute("DELETE FROM face_exemplar WHERE p_id=?", (pid,))
        ts = now_iso()
        for ex_idx, ci in enumerate(top_idx):
            c = qualified[ci]
            source = f"face_pk:{c['face_pk']}:asset:{c['asset_id']}:frame:{c['frame_idx']}"
            con.execute("""
                INSERT INTO face_exemplar (p_id, exemplar_idx, embedding, source, added_at)
                VALUES (?, ?, ?, ?, ?)
            """, (pid, ex_idx, pack_embedding(c["embedding"]), source, ts))
            counters["exemplars_written"] += 1

            # Save visual crop for review
            if args.save_crops:
                person_dir = FACE_EXEMPLARS_DIR / pid
                person_dir.mkdir(parents=True, exist_ok=True)
                img = _refetch_image(con, c)
                if img is not None:
                    import cv2
                    crop = _crop_face(img, c["bbox"])
                    out = person_dir / f"exemplar_{ex_idx:02d}_score{c['det_score']:.2f}.jpg"
                    cv2.imwrite(str(out), crop)
                    counters["crops_saved"] += 1
                else:
                    counters["crops_failed"] += 1

        counters["seeded"] += 1
        print(f"  ✓ {name:30s} ({pid})  candidates={len(qualified)} "
              f"dominant={dominant_size} exemplars={len(top_idx)}")

    con.commit()
    record_run_end(con, run_pk, counters)
    print(f"\n=== Summary ===")
    for k, v in counters.items():
        print(f"  {k:<22s}: {v}")
    if args.save_crops:
        print(f"\nExemplar crops: {FACE_EXEMPLARS_DIR}")
        print(f"  Browse: open \"{FACE_EXEMPLARS_DIR}\"")


# ---------------- tag (P2b) ----------------

def cmd_tag(args: argparse.Namespace) -> None:
    """Auto-tag face detections by computing cosine similarity against each
    seed-exemplar centroid. Assigns p_id when max similarity exceeds threshold
    AND clearly beats the runner-up (margin)."""
    import numpy as np

    con = open_db()
    run_pk = record_run_start(con, "tag", args)
    print(f"=== build_face_index tag | {now_iso()} ===")

    humans = _load_humans()

    # Build per-person centroid from exemplars
    rows = list(con.execute("SELECT p_id, embedding FROM face_exemplar"))
    by_pid: dict[str, list[np.ndarray]] = {}
    for pid, blob in rows:
        by_pid.setdefault(pid, []).append(unpack_embedding(blob))
    if not by_pid:
        print("No exemplars — run `seed` first.")
        return
    pids = sorted(by_pid)
    centroids = []
    for pid in pids:
        c = np.mean(by_pid[pid], axis=0)
        c = c / (np.linalg.norm(c) + 1e-9)
        centroids.append(c)
    C = np.stack(centroids).astype(np.float32)  # (P, 512)
    print(f"  people with exemplars: {len(pids)}")
    print(f"  threshold={args.threshold} margin={args.margin}")

    # Pull all unassigned face_detection in batches
    where = "p_id IS NULL" if not args.force else "1=1"
    n_total = con.execute(f"SELECT COUNT(*) FROM face_detection WHERE {where}").fetchone()[0]
    print(f"  faces to consider: {n_total}")

    BATCH = 4096
    counters = {"considered": 0, "tagged": 0, "below_threshold": 0,
                "tied_no_margin": 0}
    per_person = {pid: 0 for pid in pids}
    t_start = time.time()
    last_print = [time.time()]

    cur = con.execute(f"SELECT face_pk, embedding FROM face_detection WHERE {where}")
    while True:
        rows = cur.fetchmany(BATCH)
        if not rows:
            break
        pks = [r[0] for r in rows]
        embs = np.stack([unpack_embedding(r[1]) for r in rows])
        sims = embs @ C.T  # (B, P)
        argmax = sims.argmax(axis=1)
        best = sims[np.arange(len(rows)), argmax]
        if C.shape[0] >= 2:
            sims_sorted = np.partition(sims, -2, axis=1)
            second = sims_sorted[:, -2]
        else:
            second = np.zeros_like(best)
        margins = best - second
        ok = (best >= args.threshold) & (margins >= args.margin)
        counters["considered"] += len(rows)
        counters["below_threshold"] += int((best < args.threshold).sum())
        counters["tied_no_margin"] += int(((best >= args.threshold) & (margins < args.margin)).sum())
        # Apply assignments
        update_batch = []
        for i, do in enumerate(ok):
            if not do:
                continue
            pid = pids[int(argmax[i])]
            update_batch.append((pid, "seed_match", float(best[i]), pks[i]))
            per_person[pid] += 1
        if update_batch:
            con.executemany("""
                UPDATE face_detection
                SET p_id=?, identified_via=?
                WHERE face_pk=?
            """, [(u[0], u[1], u[3]) for u in update_batch])
            counters["tagged"] += len(update_batch)
        con.commit()

        now = time.time()
        if now - last_print[0] >= 10:
            elapsed = now - t_start
            rate = counters["considered"] / elapsed if elapsed else 0
            eta = (n_total - counters["considered"]) / rate if rate else 0
            print(f"  [{counters['considered']:>6}/{n_total}] "
                  f"tagged={counters['tagged']:5d}  rate={rate:.0f}/s  "
                  f"elapsed={elapsed:5.1f}s  ETA={eta:5.1f}s", flush=True)
            last_print[0] = now

    elapsed = time.time() - t_start
    summary = {**counters, "elapsed_sec": round(elapsed, 2),
               "per_person": per_person}
    record_run_end(con, run_pk, summary)
    print(f"\n=== Summary ===")
    print(f"Elapsed: {elapsed:.1f}s")
    for k, v in counters.items():
        print(f"  {k:<18s}: {v}")
    print(f"\nTop tagged people:")
    for pid, n in sorted(per_person.items(), key=lambda kv: -kv[1])[:15]:
        if n == 0: break
        print(f"  {n:6d}  {humans.get(pid, pid)}  ({pid})")


# ---------------- cluster (P3a) ----------------

def cmd_cluster(args: argparse.Namespace) -> None:
    """Cluster all face embeddings using HDBSCAN. Writes cluster_id back to
    face_detection and populates face_cluster with centroids + sizes."""
    import numpy as np
    import hdbscan

    con = open_db()
    run_pk = record_run_start(con, "cluster", args)
    print(f"=== build_face_index cluster | {now_iso()} ===")

    # Pull embeddings with quality filter
    rows = list(con.execute("""
        SELECT face_pk, embedding FROM face_detection
        WHERE det_score >= ?
    """, (args.min_det_score,)))
    print(f"  faces in (det_score >= {args.min_det_score}): {len(rows)}")
    if not rows:
        print("nothing to cluster.")
        return
    embs = np.stack([unpack_embedding(r[1]) for r in rows]).astype(np.float32)
    pks = [r[0] for r in rows]

    t0 = time.time()
    print(f"  running HDBSCAN (min_cluster_size={args.min_cluster_size}, "
          f"min_samples={args.min_samples}, metric=euclidean)...")
    clusterer = hdbscan.HDBSCAN(
        min_cluster_size=args.min_cluster_size,
        min_samples=args.min_samples,
        metric="euclidean",                  # L2-normalized → euclidean ≈ angular
        cluster_selection_method="eom",
        core_dist_n_jobs=-1,
    )
    labels = clusterer.fit_predict(embs)
    cluster_time = time.time() - t0
    print(f"  clustered in {cluster_time:.1f}s")

    n_noise = int((labels == -1).sum())
    cluster_ids = sorted(set(int(l) for l in labels) - {-1})
    n_clusters = len(cluster_ids)
    print(f"  clusters: {n_clusters}, noise (unassigned): {n_noise}")
    if n_clusters == 0:
        print("no clusters found — try lowering --min-cluster-size")
        return

    # Wipe prior cluster assignments and the cluster table
    con.execute("DELETE FROM face_cluster")
    con.execute("UPDATE face_detection SET cluster_id=NULL")
    con.commit()

    # Persist cluster centroids + sizes; record top size for reporting
    sizes = []
    for cid in cluster_ids:
        mask = labels == cid
        size = int(mask.sum())
        centroid = embs[mask].mean(axis=0)
        centroid /= (np.linalg.norm(centroid) + 1e-9)
        con.execute("""
            INSERT INTO face_cluster (cluster_id, centroid, n_faces)
            VALUES (?, ?, ?)
        """, (int(cid), pack_embedding(centroid), size))
        sizes.append(size)
    con.commit()

    # Update face_detection.cluster_id (batch)
    BATCH = 4096
    for i in range(0, len(pks), BATCH):
        chunk = []
        for j in range(i, min(i + BATCH, len(pks))):
            l = int(labels[j])
            chunk.append((l if l != -1 else None, pks[j]))
        con.executemany(
            "UPDATE face_detection SET cluster_id=? WHERE face_pk=?", chunk)
    con.commit()

    sizes.sort(reverse=True)
    summary = {
        "considered": len(rows), "noise": n_noise,
        "clusters": n_clusters,
        "top_cluster_sizes": sizes[:20],
        "cluster_time_sec": round(cluster_time, 1),
    }
    record_run_end(con, run_pk, summary)
    print(f"\n=== Summary ===")
    print(f"  considered:     {len(rows)}")
    print(f"  clusters:       {n_clusters}")
    print(f"  noise:          {n_noise}  ({100*n_noise/len(rows):.1f}%)")
    print(f"  top 15 sizes:   {sizes[:15]}")
    print(f"  smallest size:  {sizes[-1] if sizes else 0}")
    print(f"  median size:    {sizes[len(sizes)//2] if sizes else 0}")


# ---------------- label (P3b) ----------------

def _grid_preview(crops: list, cols: int = 4) -> "np.ndarray":
    """Stitch face crops into a single image grid for quick visual review."""
    import cv2, numpy as np
    if not crops:
        return None
    h_target = 200
    resized = []
    for c in crops:
        if c is None or c.size == 0:
            continue
        h, w = c.shape[:2]
        nw = int(w * h_target / max(h, 1))
        resized.append(cv2.resize(c, (nw, h_target)))
    if not resized:
        return None
    rows = []
    for i in range(0, len(resized), cols):
        row_imgs = resized[i:i+cols]
        # Pad widths to a common width
        max_w = max(im.shape[1] for im in row_imgs)
        padded = []
        for im in row_imgs:
            pad = np.zeros((h_target, max_w - im.shape[1], 3), dtype=im.dtype) if im.shape[1] < max_w else None
            padded.append(np.hstack([im, pad]) if pad is not None else im)
        # Pad last row to full cols
        while len(padded) < cols:
            padded.append(np.zeros((h_target, max_w, 3), dtype=resized[0].dtype))
        rows.append(np.hstack(padded))
    # Pad rows to same width
    max_row_w = max(r.shape[1] for r in rows)
    aligned = []
    for r in rows:
        if r.shape[1] < max_row_w:
            pad = np.zeros((h_target, max_row_w - r.shape[1], 3), dtype=r.dtype)
            r = np.hstack([r, pad])
        aligned.append(r)
    return np.vstack(aligned)


def cmd_label(args: argparse.Namespace) -> None:
    """Interactive cluster labeling. For each top cluster, generate a preview
    grid of representative face crops, open it in Preview, prompt for a p_id
    (or 'skip' / 'split'). Propagate the label to face_detection.cluster_id."""
    import numpy as np
    import cv2

    con = open_db()
    con.execute(f"ATTACH DATABASE '{INDEXES_DIR / 'editorial_catalog.sqlite'}' AS ec")
    humans_full = _load_humans_full()
    humans = {(p.get("id") or p.get("p_id")): (p.get("canonical_name") or "")
              for p in humans_full}
    alias_map = _build_alias_map(humans_full)

    run_pk = record_run_start(con, "label", args)
    print(f"=== build_face_index label | {now_iso()} ===")
    print(f"  alias map: {len(alias_map)} terms across {len(humans)} humans")

    # Pull clusters, ordered by size
    clusters = list(con.execute("""
        SELECT cluster_id, n_faces, p_id, label_source FROM face_cluster
        WHERE n_faces >= ?
        ORDER BY n_faces DESC LIMIT ?
    """, (args.min_size, args.top_n)))
    print(f"  clusters to review: {len(clusters)} (size >= {args.min_size}, top {args.top_n})")

    preview_dir = FACE_EXEMPLARS_DIR / "_cluster_previews"
    preview_dir.mkdir(parents=True, exist_ok=True)

    counters = {"labeled": 0, "skipped": 0, "kept_prior": 0}
    for cluster_id, n_faces, prior_pid, prior_src in clusters:
        if prior_pid and not args.force:
            counters["kept_prior"] += 1
            continue
        # Sample representative faces by det_score
        sample_rows = list(con.execute("""
            SELECT face_pk, asset_id, record_kind, frame_idx, frame_time_sec,
                   bbox_json, det_score
            FROM face_detection
            WHERE cluster_id=?
            ORDER BY det_score DESC LIMIT ?
        """, (cluster_id, args.sample_n)))

        # Source breakdown for editorial context
        ctx_rows = list(con.execute("""
            SELECT COALESCE(a.shoot_label, a.category_name, '?') src, COUNT(*) c
            FROM face_detection fd JOIN ec.asset a ON fd.asset_id=a.asset_id
            WHERE fd.cluster_id=?
            GROUP BY src ORDER BY c DESC LIMIT 5
        """, (cluster_id,)))
        ctx_summary = ", ".join(f"{src}({c})" for src, c in ctx_rows)

        # Build crops for preview
        crops = []
        for r in sample_rows:
            face_row = {
                "asset_id": r[1], "record_kind": r[2],
                "frame_idx": r[3], "frame_time_sec": r[4],
            }
            img = _refetch_image(con, face_row)
            if img is None:
                continue
            crops.append(_crop_face(img, json.loads(r[5])))

        preview = _grid_preview(crops, cols=args.cols)
        if preview is None:
            print(f"\n  [cluster {cluster_id}] no crops available — skipping")
            continue
        preview_path = preview_dir / f"cluster_{cluster_id:04d}_n{n_faces}.jpg"
        cv2.imwrite(str(preview_path), preview)

        # Open in Preview.app (Mac)
        import subprocess
        subprocess.run(["open", str(preview_path)], capture_output=True)

        print(f"\n  [cluster {cluster_id}]  faces={n_faces}  sources: {ctx_summary}")
        print(f"  preview: {preview_path}")
        print(f"  Enter p_id (or canonical name), 's' to skip, 'q' to quit, 'd' to drop cluster:")
        try:
            ans = input("    > ").strip()
        except EOFError:
            print("(non-interactive; aborting)")
            break

        if ans.lower() in ("q", "quit", "exit"):
            print("  quitting label session")
            break
        if not ans or ans.lower() in ("s", "skip"):
            counters["skipped"] += 1
            continue
        if ans.lower() in ("d", "drop"):
            # Mark cluster as definitely-not-a-person (e.g., false positive)
            con.execute("""
                UPDATE face_cluster SET p_id=NULL, label_source='dropped', label_at=?, notes='manual drop'
                WHERE cluster_id=?
            """, (now_iso(), cluster_id))
            con.execute("""
                UPDATE face_detection SET p_id=NULL, identified_via='cluster_dropped'
                WHERE cluster_id=?
            """, (cluster_id,))
            con.commit()
            counters["skipped"] += 1
            continue

        # Resolve via the alias map; handle ambiguity explicitly
        candidates = _resolve_name(ans, alias_map)
        if not candidates:
            print(f"    ✗ unknown '{ans}' — try a full p_id (p_…), a known name, "
                  f"alias, or 's' to skip.")
            continue
        if len(candidates) > 1:
            sorted_c = sorted(candidates)
            print(f"    '{ans}' matches {len(sorted_c)} people:")
            for i, c in enumerate(sorted_c, 1):
                print(f"      {i:2d}. {humans.get(c, c):30s}  ({c})")
            pick = input("    Pick a number, or 's' to skip: ").strip()
            if pick.lower() in ("s", "skip", ""):
                counters["skipped"] += 1
                continue
            try:
                pid = sorted_c[int(pick) - 1]
            except (ValueError, IndexError):
                print(f"    invalid pick '{pick}' — skipping cluster")
                counters["skipped"] += 1
                continue
        else:
            pid = next(iter(candidates))

        canonical = humans.get(pid, "(unknown)")
        confirm = input(f"    → {pid} ({canonical})? [Y/n] > ").strip().lower()
        if confirm and confirm not in ("y", "yes", ""):
            counters["skipped"] += 1
            continue

        # Persist
        ts = now_iso()
        con.execute("""
            UPDATE face_cluster SET p_id=?, label_source='manual', label_at=?
            WHERE cluster_id=?
        """, (pid, ts, cluster_id))
        n_propagated = con.execute("""
            UPDATE face_detection SET p_id=?, identified_via='cluster_label'
            WHERE cluster_id=? AND (p_id IS NULL OR identified_via='cluster_label')
        """, (pid, cluster_id)).rowcount
        con.commit()
        counters["labeled"] += 1
        print(f"    ✓ labeled cluster {cluster_id} as {canonical}, "
              f"propagated to {n_propagated} face detections")

    summary = {**counters,
               "people_labeled": con.execute(
                   "SELECT COUNT(DISTINCT p_id) FROM face_cluster WHERE p_id IS NOT NULL"
               ).fetchone()[0]}
    record_run_end(con, run_pk, summary)
    print(f"\n=== Summary ===")
    for k, v in counters.items():
        print(f"  {k:<14s}: {v}")


# ---------------- suggest (post-P3 mop-up) ----------------

def cmd_suggest(args: argparse.Namespace) -> None:
    """Score each unlabeled cluster against three signals derived from the
    existing labels: face similarity to manually-labeled people, asset_people
    tags in the cluster's assets, and co-occurrence with other labeled clusters
    in the same assets. Use the resulting report to identify quick wins.

    With --apply, auto-label clusters where face similarity to a single known
    person exceeds --threshold AND beats the runner-up by --margin AND the
    suggested person isn't already explained by another cluster in those assets.
    """
    import numpy as np

    con = open_db()
    con.execute(f"ATTACH DATABASE '{INDEXES_DIR / 'editorial_catalog.sqlite'}' AS ec")
    humans = _load_humans()

    # 1. Per-CLUSTER centroids from MANUALLY-labeled clusters.
    # Each cluster represents one person in one shoot+context; matching to the
    # nearest CLUSTER preserves per-shoot lighting/angle context that gets
    # averaged out by per-person blob centroids.
    # Quality filter (det_score >= 0.7) drops noisy faces from each centroid.
    cluster_pids = []
    cluster_ids = []
    cluster_centroids = []
    for cid, pid in con.execute("""
        SELECT cluster_id, p_id FROM face_cluster
        WHERE p_id IS NOT NULL AND label_source = 'manual'
    """):
        embs = [unpack_embedding(b) for (b,) in con.execute(
            "SELECT embedding FROM face_detection WHERE cluster_id=? AND det_score>=0.7", (cid,)
        )]
        if not embs:
            continue
        c = np.mean(embs, axis=0)
        c = c / (np.linalg.norm(c) + 1e-9)
        cluster_centroids.append(c)
        cluster_pids.append(pid)
        cluster_ids.append(cid)
    if not cluster_centroids:
        print("No manually-labeled clusters — run `label` first.")
        return
    C = np.stack(cluster_centroids).astype(np.float32)  # (K, 512), one row per labeled cluster
    print(f"  built {len(cluster_centroids)} per-cluster centroids "
          f"across {len(set(cluster_pids))} manually-labeled people\n")

    # 2. Unlabeled clusters >= min_size
    unlabeled = list(con.execute("""
        SELECT cluster_id, n_faces FROM face_cluster
        WHERE p_id IS NULL AND (label_source IS NULL OR label_source != 'dropped')
          AND n_faces >= ?
        ORDER BY n_faces DESC
    """, (args.min_size,)))
    print(f"  unlabeled clusters >= {args.min_size} faces: {len(unlabeled)}\n")

    if args.apply:
        run_pk = record_run_start(con, "suggest_apply", args)
    applied = 0
    high_conf = 0

    for cid, n in unlabeled:
        # Cluster centroid (quality-filtered)
        embs = [unpack_embedding(b) for (b,) in con.execute(
            "SELECT embedding FROM face_detection WHERE cluster_id=? AND det_score>=0.7", (cid,)
        )]
        if not embs:
            print(f"[{cid:4d}] {n:5d}  (no det_score≥0.7 faces — skipping)\n")
            continue
        cc = np.mean(embs, axis=0)
        cc = cc / (np.linalg.norm(cc) + 1e-9)
        sims = C @ cc.astype(np.float32)  # (K,) — one similarity per labeled cluster

        # Aggregate per-person: take the BEST cluster match per p_id
        per_person_best: dict[str, float] = {}
        per_person_via: dict[str, int] = {}
        for i, pid in enumerate(cluster_pids):
            if sims[i] > per_person_best.get(pid, -1.0):
                per_person_best[pid] = float(sims[i])
                per_person_via[pid] = cluster_ids[i]
        ranked = sorted(per_person_best.items(), key=lambda kv: -kv[1])
        top_face = [(pid, sim) for pid, sim in ranked[:args.top_k]]

        # asset_people in cluster's assets (people text-tagged in those assets)
        ap = list(con.execute("""
            SELECT ap.p_id, COUNT(DISTINCT fd.asset_id) c
            FROM face_detection fd
            JOIN ec.asset_people ap ON fd.asset_id = ap.asset_id
            WHERE fd.cluster_id=?
            GROUP BY ap.p_id ORDER BY c DESC LIMIT 5
        """, (cid,)))

        # Already-labeled clusters that share the same assets (so we don't
        # mis-label a co-star as the protagonist)
        co_occur = list(con.execute("""
            SELECT fc2.p_id, COUNT(DISTINCT fd2.asset_id) c
            FROM face_detection fd1
            JOIN face_detection fd2 ON fd1.asset_id = fd2.asset_id
            JOIN face_cluster fc2 ON fd2.cluster_id = fc2.cluster_id
            WHERE fd1.cluster_id = ?
              AND fc2.p_id IS NOT NULL AND fd2.cluster_id != ?
            GROUP BY fc2.p_id ORDER BY c DESC LIMIT 5
        """, (cid, cid)))
        co_occur_set = {pid for pid, _ in co_occur}

        # Shoot context
        shoot_ctx = con.execute("""
            SELECT GROUP_CONCAT(src||'('||c||')', ' | ') FROM (
                SELECT COALESCE(a.shoot_label, a.category_name, '?') src, COUNT(*) c
                FROM face_detection fd JOIN ec.asset a ON fd.asset_id=a.asset_id
                WHERE fd.cluster_id=? GROUP BY src ORDER BY c DESC LIMIT 2)
        """, (cid,)).fetchone()[0]

        # Pretty-print
        best_pid, best_sim = top_face[0]
        runner_sim = top_face[1][1] if len(top_face) > 1 else 0.0
        margin = best_sim - runner_sim
        conf_flag = ""
        if best_sim >= args.threshold and margin >= args.margin:
            conf_flag = " ✓ HIGH"
            high_conf += 1

        print(f"[{cid:4d}] {n:5d}  {(shoot_ctx or '?')[:80]}{conf_flag}")
        print(f"      face-sim (best labeled cluster per person):")
        for pid, s in top_face:
            star = "*" if pid == best_pid else " "
            co = " (already in cluster)" if pid in co_occur_set else ""
            via = per_person_via.get(pid)
            print(f"        {star} {s:5.2f}  {humans.get(pid, pid):28s}  ({pid})  via cluster {via}{co}")
        if ap:
            print(f"      asset_people in cluster's assets:")
            for apid, ac in ap:
                marker = " ← already covered elsewhere here" if apid in co_occur_set else " ← UNCOVERED candidate"
                print(f"          {ac:3d}  {humans.get(apid, apid):28s}  ({apid}){marker}")

        # Auto-apply if requested + confident + not already explained by a sibling cluster
        if args.apply and best_sim >= args.threshold and margin >= args.margin:
            if best_pid in co_occur_set:
                print(f"      ⤬ skip: {best_pid} already covers this asset via another cluster")
            else:
                ts = now_iso()
                con.execute(
                    "UPDATE face_cluster SET p_id=?, label_source='suggest_auto', label_at=?, notes=? WHERE cluster_id=?",
                    (best_pid, ts, f"face_sim={best_sim:.3f} margin={margin:.3f}", cid))
                con.execute(
                    "UPDATE face_detection SET p_id=?, identified_via='cluster_label' WHERE cluster_id=? AND (p_id IS NULL OR identified_via='cluster_label')",
                    (best_pid, cid))
                con.commit()
                applied += 1
                print(f"      → APPLIED: cluster {cid} → {best_pid}")
        print()

    print(f"\n=== Summary ===")
    print(f"  unlabeled clusters reviewed: {len(unlabeled)}")
    print(f"  high-confidence candidates (sim≥{args.threshold}, margin≥{args.margin}): {high_conf}")
    if args.apply:
        print(f"  auto-applied: {applied}")
        record_run_end(con, run_pk, {"reviewed": len(unlabeled), "high_conf": high_conf, "applied": applied})


# ---------------- status ----------------

def cmd_status(args: argparse.Namespace) -> None:
    if not FACE_EMBEDDINGS_DB.exists():
        print(f"No face DB at {FACE_EMBEDDINGS_DB} — run `stills` first.")
        return
    con = open_db()
    print(f"DB: {FACE_EMBEDDINGS_DB}")
    print()
    for kind in ("still", "video"):
        n_assets = con.execute(
            "SELECT COUNT(DISTINCT asset_id) FROM face_detection WHERE record_kind=?",
            (kind,),
        ).fetchone()[0]
        n_faces = con.execute(
            "SELECT COUNT(*) FROM face_detection WHERE record_kind=?", (kind,),
        ).fetchone()[0]
        n_identified = con.execute(
            "SELECT COUNT(*) FROM face_detection WHERE record_kind=? AND p_id IS NOT NULL",
            (kind,),
        ).fetchone()[0]
        print(f"  {kind:<8s}  assets={n_assets:6d}  faces={n_faces:7d}  identified={n_identified:6d}")
    print()
    n_clusters = con.execute("SELECT COUNT(*) FROM face_cluster").fetchone()[0]
    n_labeled = con.execute(
        "SELECT COUNT(*) FROM face_cluster WHERE p_id IS NOT NULL"
    ).fetchone()[0]
    n_exemplars = con.execute(
        "SELECT COUNT(DISTINCT p_id) FROM face_exemplar"
    ).fetchone()[0]
    print(f"  clusters={n_clusters}  labeled={n_labeled}  people_with_exemplars={n_exemplars}")
    print()
    print("recent runs:")
    for r in con.execute(
        "SELECT phase, started_at, finished_at, summary_json FROM face_run "
        "ORDER BY run_pk DESC LIMIT 5"
    ):
        phase, started, finished, summary = r
        print(f"  {started}  {phase:<8s}  finished={finished or '(running)'}")
        if summary:
            s = json.loads(summary)
            key_stats = {k: v for k, v in s.items() if k in (
                "processed", "with_faces", "total_faces", "elapsed_sec", "errors"
            )}
            print(f"    {key_stats}")


# ---------------- entry ----------------

def main():
    ap = argparse.ArgumentParser(
        description="Face index pipeline.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_stills = sub.add_parser("stills", help="P0 — detect+embed faces on cataloged stills")
    p_stills.add_argument("--limit", type=int, default=None)
    p_stills.add_argument("--force", action="store_true",
                          help="Re-process even if asset already has face_detection rows")
    p_stills.set_defaults(func=cmd_stills)

    p_video = sub.add_parser("video", help="P1 — detect+embed on video keyframes")
    p_video.add_argument("--limit", type=int, default=None)
    p_video.add_argument("--asset-ids", type=str, default=None,
                         help="File with one asset_id per line; only process these")
    p_video.add_argument("--workers", type=int, default=4,
                         help="Parallel ffmpeg extract workers (default 4)")
    p_video.set_defaults(func=cmd_video)

    p_seed = sub.add_parser("seed", help="P2a — build exemplar bank from single-tagged assets")
    p_seed.add_argument("--top-people", type=int, default=40,
                        help="Cap to top-N people by single-tag asset count")
    p_seed.add_argument("--min-assets", type=int, default=2,
                        help="Skip people with fewer single-tag assets")
    p_seed.add_argument("--min-candidates", type=int, default=4,
                        help="Skip people with fewer qualified face candidates")
    p_seed.add_argument("--min-det-score", type=float, default=0.7,
                        help="Quality gate on individual face detection confidence")
    p_seed.add_argument("--min-area", type=int, default=60*60,
                        help="Minimum face bbox area in pixels")
    p_seed.add_argument("--agreement-threshold", type=float, default=0.4,
                        help="Cosine sim threshold for the 'same-person' agreement count")
    p_seed.add_argument("--exemplars", type=int, default=8,
                        help="Top-N exemplars to retain per person")
    p_seed.add_argument("--save-crops", action="store_true", default=True,
                        help="Save exemplar JPEGs to derivative media/_face exemplars/<p_id>/")
    p_seed.add_argument("--no-save-crops", dest="save_crops", action="store_false")
    p_seed.set_defaults(func=cmd_seed)

    p_tag = sub.add_parser("tag", help="P2b — auto-tag detections against the exemplar bank")
    p_tag.add_argument("--threshold", type=float, default=0.55,
                       help="Min cosine similarity to assign (ArcFace standard ~0.55)")
    p_tag.add_argument("--margin", type=float, default=0.10,
                       help="Min margin over runner-up to avoid ambiguous assignments")
    p_tag.add_argument("--force", action="store_true",
                       help="Re-tag faces that already have p_id")
    p_tag.set_defaults(func=cmd_tag)

    p_cluster = sub.add_parser("cluster", help="P3a — HDBSCAN cluster all face embeddings")
    p_cluster.add_argument("--min-det-score", type=float, default=0.6,
                           help="Quality filter on face detections before clustering")
    p_cluster.add_argument("--min-cluster-size", type=int, default=10,
                           help="HDBSCAN: minimum members per cluster")
    p_cluster.add_argument("--min-samples", type=int, default=5,
                           help="HDBSCAN: minimum density (higher = stricter clusters)")
    p_cluster.set_defaults(func=cmd_cluster)

    p_label = sub.add_parser("label", help="P3b — interactive cluster labeling CLI")
    p_label.add_argument("--top-n", type=int, default=50,
                         help="Label top-N clusters by face count")
    p_label.add_argument("--min-size", type=int, default=20,
                         help="Skip clusters smaller than this")
    p_label.add_argument("--sample-n", type=int, default=8,
                         help="Face crops to show per cluster preview")
    p_label.add_argument("--cols", type=int, default=4,
                         help="Grid columns in the preview image")
    p_label.add_argument("--force", action="store_true",
                         help="Re-label clusters that already have a p_id")
    p_label.set_defaults(func=cmd_label)

    p_suggest = sub.add_parser("suggest", help="Suggest p_id for unlabeled clusters using existing labels as ground truth")
    p_suggest.add_argument("--min-size", type=int, default=50,
                           help="Only score unlabeled clusters with at least N faces")
    p_suggest.add_argument("--top-k", type=int, default=3,
                           help="Show top-K face-similarity candidates per cluster")
    p_suggest.add_argument("--threshold", type=float, default=0.55,
                           help="Cosine similarity floor for high-confidence flag / --apply")
    p_suggest.add_argument("--margin", type=float, default=0.10,
                           help="Required gap between best and runner-up for high-confidence flag / --apply")
    p_suggest.add_argument("--apply", action="store_true",
                           help="Auto-apply labels when best ≥ threshold AND margin met AND person isn't already covering same assets via another cluster")
    p_suggest.set_defaults(func=cmd_suggest)

    p_status = sub.add_parser("status", help="Coverage stats")
    p_status.set_defaults(func=cmd_status)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
