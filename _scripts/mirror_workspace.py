"""Mirror the primary workspace <-> a second working SSD (two-editor / two-drive workflows) with a manual-review gate.

Two-phase execution by design:
  1. PLAN — scan both trees, classify every diff, print a review report. Default.
  2. APPLY — execute the plan. Requires explicit `--apply` AND y/n confirmation
     (unless `--yes` is also passed, e.g. for scripted use).

Default direction is E: -> D: (the workspace is the working drive, the workspace SSD is the mirror
target). Pass `--from D --to E` to reverse.

Quick start
-----------
    # See what would change (safe, read-only):
    py _scripts/mirror_workspace.py

    # Apply with interactive confirmation:
    py _scripts/mirror_workspace.py --apply

    # Save the plan to a markdown file before applying:
    py _scripts/mirror_workspace.py --report mirror_plan.md

    # Apply just one subtree:
    py _scripts/mirror_workspace.py --apply --path "editor"

What gets compared
------------------
Top-level project tree (`README.md`, `CLAUDE.md`, `editor/`, `premiere mcp/`,
`dataset/`, `indexes/`, etc.). The following are SKIPPED entirely (never read,
never copied):

- `derivative media/` — RAID-mirrored proxy/audio/stills layer with its own
  sync workflow; mirroring here would be terabytes of binary churn
- `node_modules/`, `__pycache__/`, `.git/`, `_cache/` — regenerable / dev cruft
- `editor/premiere projects/Adobe Premiere Pro Auto-Save/` — Premiere's local
  rolling autosaves; not stable across machines
- `editor/xml exports/_otio_eval/` — local-only eval scratch directory

Categories the plan groups changes by
-------------------------------------
- **COPY** — file is new on source, or source is newer + content differs.
  Will overwrite dest.
- **DELETE** — file is on dest but not on source (probably a rename/cleanup
  that should propagate). Will rm on dest.
- **CONFLICT** — file exists on both, content differs, BUT dest mtime is
  newer than source mtime. Default: skip and flag for human decision (you
  probably edited the dest copy directly and the mirror shouldn't clobber).
  Override per-file by editing the plan JSON, or by re-running with the
  source touched newer.
- **SKIP-IDENTICAL** — file is the same on both. No action.

Limits
------
- This does not handle symlinks (skips them).
- This is not transactional — if it fails midway, the dest may be in a
  half-mirrored state. Re-running picks up where it left off.
- Single-process only. Don't run two mirrors at once.

Safety notes
------------
- Plan phase NEVER writes. You can run it as many times as you like.
- Apply phase prompts y/n unless `--yes` is passed.
- The apply phase prints each operation as it's performed so you can spot
  surprises in real time.
"""

from __future__ import annotations

import argparse
import datetime
import fnmatch
import hashlib
import json
import os
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# === Configuration =========================================================

DEFAULT_SOURCE = Path("E:/open-post-stack")
DEFAULT_DEST = Path("D:/open-post-stack")

# Skip patterns are matched against the path RELATIVE to the workspace root,
# using fnmatch (Unix-style glob with `*`, `?`, `**`).
SKIP_PATTERNS: list[str] = [
    "derivative media/*",
    "derivative media/**",
    "**/node_modules/*",
    "**/node_modules/**",
    "**/__pycache__/*",
    "**/__pycache__/**",
    "**/.git/*",
    "**/.git/**",
    "**/_cache/*",
    "**/_cache/**",
    "**/.pytest_cache/*",
    "**/.pytest_cache/**",
    "**/*.pyc",
    # Per-user state — should never cross machines
    ".claude/*",
    ".claude/**",
    ".vscode/*",
    ".vscode/**",
    ".idea/*",
    ".idea/**",
    "editor/premiere projects/Adobe Premiere Pro Auto-Save/*",
    "editor/premiere projects/Adobe Premiere Pro Auto-Save/**",
    "editor/xml exports/_otio_eval/*",
    "editor/xml exports/_otio_eval/**",
    # Premiere lock + temp
    "**/*.prlock",
    "**/.DS_Store",
    "**/Thumbs.db",
    # Per-drive logs — each drive maintains its own session changelog.
    # Subdir CHANGELOG.md files (e.g. premiere mcp/premiere_mcp_CHANGELOG.md)
    # ARE meant to mirror; only the root-level one is per-drive.
    "CHANGELOG.md",
]

# Don't read content for files this big — they're too expensive to hash.
# We rely on mtime+size for these. Threshold: 200 MB.
LARGE_FILE_BYTES = 200 * 1024 * 1024

# === Helpers ===============================================================


def _matches_skip(rel_path: str, patterns: list[str]) -> bool:
    """Return True if rel_path matches any skip pattern (Unix-style globs)."""
    # Normalize to forward slashes for pattern matching
    p = rel_path.replace("\\", "/")
    for pat in patterns:
        if fnmatch.fnmatchcase(p, pat):
            return True
    return False


def _sha256(path: Path, max_bytes: int = LARGE_FILE_BYTES) -> Optional[str]:
    """Compute SHA-256 of a file, but bail out and return None for files
    larger than max_bytes (avoid spending forever hashing 4GB media)."""
    try:
        size = path.stat().st_size
    except OSError:
        return None
    if size > max_bytes:
        return None
    h = hashlib.sha256()
    try:
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


@dataclass
class FileEntry:
    rel: str          # Path relative to workspace root
    size: int
    mtime: float
    sha256: Optional[str] = None  # None for large files (size+mtime only)


def _scan(root: Path, patterns: list[str], filter_subtree: Optional[str] = None) -> dict[str, FileEntry]:
    """Walk root, return {rel_path: FileEntry} for every non-skipped file.

    Stats only — no hashing. Hashing happens lazily during diff comparison,
    only for files where size+mtime suggests they might have changed.

    If filter_subtree is set (e.g. "editor"), only walks that subdirectory.
    """
    base = root / filter_subtree if filter_subtree else root
    if not base.exists():
        return {}
    out: dict[str, FileEntry] = {}
    for dirpath, dirnames, filenames in os.walk(base):
        d_path = Path(dirpath)
        rel_dir = d_path.relative_to(root).as_posix() if d_path != root else ""
        # Prune skipped directories from descent (perf — don't walk into node_modules)
        new_dirs = []
        for dn in dirnames:
            sub = (rel_dir + "/" + dn) if rel_dir else dn
            if _matches_skip(sub + "/", patterns) or _matches_skip(sub, patterns):
                continue
            new_dirs.append(dn)
        dirnames[:] = new_dirs

        for fn in filenames:
            f_path = d_path / fn
            try:
                st = f_path.stat()
            except OSError:
                continue
            if not (st.st_mode & 0o400):
                continue
            rel = f_path.relative_to(root).as_posix()
            if _matches_skip(rel, patterns):
                continue
            # NOTE: sha256 deferred — _build_plan() will hash only the files
            # where size or mtime mismatches between source and dest.
            out[rel] = FileEntry(rel=rel, size=st.st_size, mtime=st.st_mtime, sha256=None)
    return out


@dataclass
class Plan:
    copies: list[tuple[str, str]] = field(default_factory=list)         # (rel, reason)
    deletes: list[tuple[str, str]] = field(default_factory=list)        # (rel, reason)
    conflicts: list[tuple[str, str]] = field(default_factory=list)      # (rel, reason)
    identical: list[str] = field(default_factory=list)
    source_root: Path = DEFAULT_SOURCE
    dest_root: Path = DEFAULT_DEST

    def total_ops(self) -> int:
        return len(self.copies) + len(self.deletes)

    def total_bytes_to_copy(self, src_index: dict[str, FileEntry]) -> int:
        return sum(src_index[r].size for r, _ in self.copies if r in src_index)


def _build_plan(src_idx: dict[str, FileEntry], dst_idx: dict[str, FileEntry],
                source_root: Path, dest_root: Path) -> Plan:
    """Classify every file. Hash on-demand only when size+mtime suggest a real
    difference — most files match by stat and don't need content comparison."""
    plan = Plan(source_root=source_root, dest_root=dest_root)
    all_paths = set(src_idx) | set(dst_idx)
    hashed = 0
    for rel in sorted(all_paths):
        in_src = rel in src_idx
        in_dst = rel in dst_idx
        if in_src and not in_dst:
            plan.copies.append((rel, f"new on source ({src_idx[rel].size:,} bytes)"))
            continue
        if in_dst and not in_src:
            plan.deletes.append((rel, "only on dest"))
            continue
        s = src_idx[rel]; d = dst_idx[rel]

        # Fast path: matching size + mtime (within 2s tolerance to handle FAT/NTFS
        # 2-second mtime granularity differences). Almost-certainly identical.
        same_stat = (s.size == d.size) and (abs(s.mtime - d.mtime) < 2.0)
        if same_stat:
            plan.identical.append(rel)
            continue

        # Slow path: stat differs. Hash both to know if content actually differs.
        # (Most diffs caught by stat — this should run on ~tens of files, not all.)
        if s.sha256 is None:
            s.sha256 = _sha256(source_root / rel)
            if s.sha256: hashed += 1
        if d.sha256 is None:
            d.sha256 = _sha256(dest_root / rel)
            if d.sha256: hashed += 1

        if s.sha256 is not None and d.sha256 is not None:
            identical = s.sha256 == d.sha256
        else:
            # One side is a large file (>200MB) — skip hashing, treat as different.
            identical = False

        if identical:
            # Same content, different mtime/size? Just touch mtime.
            plan.identical.append(rel)
            continue

        # Content differs. Mtime decides who's authoritative.
        if s.mtime > d.mtime + 2.0:
            plan.copies.append((rel, f"source newer by {s.mtime - d.mtime:.0f}s"))
        elif d.mtime > s.mtime + 2.0:
            plan.conflicts.append((rel, f"dest newer by {d.mtime - s.mtime:.0f}s -- skip to preserve dest's version"))
        else:
            # Mtimes equal-ish, content differs (unusual). Treat as copy with a note.
            plan.copies.append((rel, "same mtime, content differs (defensive copy)"))
    if hashed:
        print(f"  (hashed {hashed} files where size+mtime suggested a change)")
    return plan


def _format_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024.0
    return f"{n:.1f} TB"


def _print_plan(plan: Plan, src_idx: dict[str, FileEntry], verbose: bool) -> None:
    print(f"\n=== Mirror plan: {plan.source_root} -> {plan.dest_root} ===\n")
    print(f"  Files to COPY:     {len(plan.copies)}")
    print(f"  Files to DELETE:   {len(plan.deletes)}")
    print(f"  CONFLICTS (skip):  {len(plan.conflicts)}")
    print(f"  Already in sync:   {len(plan.identical)}")
    bytes_to_copy = plan.total_bytes_to_copy(src_idx)
    print(f"  Bytes to transfer: {_format_size(bytes_to_copy)}")

    if plan.conflicts:
        print(f"\n--- CONFLICTS -- dest is newer; skipping (review and resolve manually) ---")
        for rel, reason in plan.conflicts:
            print(f"  ! {rel}    ({reason})")

    if plan.copies:
        print(f"\n--- COPY (source -> dest) ---")
        for rel, reason in plan.copies if verbose else plan.copies[:50]:
            print(f"  + {rel}    ({reason})")
        if not verbose and len(plan.copies) > 50:
            print(f"  ... and {len(plan.copies) - 50} more (use --verbose to see all)")

    if plan.deletes:
        print(f"\n--- DELETE (only on dest) ---")
        for rel, reason in plan.deletes if verbose else plan.deletes[:50]:
            print(f"  - {rel}    ({reason})")
        if not verbose and len(plan.deletes) > 50:
            print(f"  ... and {len(plan.deletes) - 50} more")

    if not (plan.copies or plan.deletes or plan.conflicts):
        print("\n  Nothing to do. Both trees are in sync.\n")


def _write_report(plan: Plan, src_idx: dict[str, FileEntry], path: Path) -> None:
    """Write the plan as a markdown report for offline review."""
    lines: list[str] = []
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines.append(f"# Mirror plan report — {ts}\n")
    lines.append(f"- Source: `{plan.source_root}`")
    lines.append(f"- Dest:   `{plan.dest_root}`")
    lines.append(f"- Files to COPY: {len(plan.copies)}")
    lines.append(f"- Files to DELETE: {len(plan.deletes)}")
    lines.append(f"- CONFLICTS (skip): {len(plan.conflicts)}")
    lines.append(f"- Already in sync: {len(plan.identical)}")
    lines.append(f"- Bytes to transfer: {_format_size(plan.total_bytes_to_copy(src_idx))}\n")

    if plan.conflicts:
        lines.append("## CONFLICTS — dest is newer; skipped\n")
        lines.append("These need a human decision. Either edit the source copy to be newer, or accept the dest version as canonical.\n")
        for rel, reason in plan.conflicts:
            lines.append(f"- `{rel}` — {reason}")
        lines.append("")

    if plan.copies:
        lines.append("## COPY (source -> dest)\n")
        for rel, reason in plan.copies:
            lines.append(f"- `{rel}` — {reason}")
        lines.append("")

    if plan.deletes:
        lines.append("## DELETE (only on dest)\n")
        for rel, reason in plan.deletes:
            lines.append(f"- `{rel}` — {reason}")
        lines.append("")

    path.write_text("\n".join(lines), encoding="utf-8")
    print(f"\nReport written to: {path}")


def _apply(plan: Plan, src_root: Path, dst_root: Path) -> tuple[int, int, list[str]]:
    """Execute COPY + DELETE operations. Returns (copied, deleted, errors)."""
    copied = 0
    deleted = 0
    errors: list[str] = []

    for rel, _reason in plan.copies:
        src = src_root / rel
        dst = dst_root / rel
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(src), str(dst))  # preserves mtime + mode
            print(f"  + {rel}")
            copied += 1
        except Exception as e:
            errors.append(f"COPY {rel}: {e}")
            print(f"  ! COPY {rel} FAILED: {e}")

    for rel, _reason in plan.deletes:
        dst = dst_root / rel
        try:
            dst.unlink()
            print(f"  - {rel}")
            deleted += 1
        except FileNotFoundError:
            pass  # already gone, fine
        except Exception as e:
            errors.append(f"DELETE {rel}: {e}")
            print(f"  ! DELETE {rel} FAILED: {e}")

    return copied, deleted, errors


# === Main ==================================================================


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--from", dest="source", default=str(DEFAULT_SOURCE),
                        help=f"Source workspace root (default: {DEFAULT_SOURCE})")
    parser.add_argument("--to", dest="dest", default=str(DEFAULT_DEST),
                        help=f"Dest workspace root (default: {DEFAULT_DEST})")
    parser.add_argument("--path", default=None,
                        help="Restrict mirror to a subtree (e.g. 'editor'). Default: whole workspace.")
    parser.add_argument("--apply", action="store_true",
                        help="Actually execute the plan. Without this flag, the script only reports.")
    parser.add_argument("--yes", action="store_true",
                        help="Skip the y/n confirmation when applying (for scripted use).")
    parser.add_argument("--report", type=Path, default=None,
                        help="Write a markdown report of the plan to this path before applying.")
    parser.add_argument("--verbose", action="store_true",
                        help="List every COPY/DELETE item instead of truncating at 50.")
    parser.add_argument("--include-conflicts", action="store_true",
                        help="Also copy CONFLICT-flagged files (overrides dest-newer protection — be careful).")
    args = parser.parse_args()

    source_root = Path(args.source).resolve()
    dest_root = Path(args.dest).resolve()

    if not source_root.exists():
        sys.exit(f"Source path does not exist: {source_root}")
    if not dest_root.exists():
        sys.exit(f"Dest path does not exist: {dest_root} — create it before mirroring.")

    print(f"Scanning source: {source_root}{(' / ' + args.path) if args.path else ''}")
    src_idx = _scan(source_root, SKIP_PATTERNS, filter_subtree=args.path)
    print(f"  {len(src_idx):,} files indexed (skipped patterns: {len(SKIP_PATTERNS)})")

    print(f"Scanning dest:   {dest_root}{(' / ' + args.path) if args.path else ''}")
    dst_idx = _scan(dest_root, SKIP_PATTERNS, filter_subtree=args.path)
    print(f"  {len(dst_idx):,} files indexed")

    plan = _build_plan(src_idx, dst_idx, source_root, dest_root)

    if args.include_conflicts:
        # Move conflicts into copies
        plan.copies.extend([(rel, reason + " [conflict overridden by --include-conflicts]")
                            for rel, reason in plan.conflicts])
        plan.conflicts = []

    _print_plan(plan, src_idx, verbose=args.verbose)

    if args.report:
        _write_report(plan, src_idx, args.report)

    if not args.apply:
        print("\n(Dry-run only. Re-run with --apply to commit the changes.)\n")
        return

    if plan.total_ops() == 0:
        print("\n  No operations needed. Exiting.\n")
        return

    if not args.yes:
        print()
        try:
            resp = input(f"Apply {plan.total_ops()} operations to {dest_root}? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\nAborted.")
            return
        if resp not in ("y", "yes"):
            print("Aborted (no confirmation).")
            return

    print("\n=== Applying ===\n")
    copied, deleted, errors = _apply(plan, source_root, dest_root)

    print(f"\n=== Done ===\n")
    print(f"  Copied:  {copied}")
    print(f"  Deleted: {deleted}")
    if errors:
        print(f"  Errors:  {len(errors)}")
        for e in errors:
            print(f"    ! {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
