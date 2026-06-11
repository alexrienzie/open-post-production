# `_scripts/`: workspace-wide utilities

Scripts that span multiple project layers (editor + dataset + indexes + premiere mcp) and don't belong inside any one of them.

## Files

| File | Purpose |
|---|---|
| `mirror_workspace.py` | Mirror the primary workspace <-> a second working SSD (e.g. `E:\\open-post-stack` <-> `D:\\open-post-stack`) with a manual-review gate. Useful when two editors (or two machines) each carry a copy. Two-phase: plan first (default), apply on `--apply` after y/n confirmation. |

## `mirror_workspace.py` quick reference

```powershell
# See what would change (safe, read-only):
py _scripts\mirror_workspace.py

# Save plan to a markdown file for offline review:
py _scripts\mirror_workspace.py --report mirror_plan.md

# Apply with interactive confirmation:
py _scripts\mirror_workspace.py --apply

# Just one subtree:
py _scripts\mirror_workspace.py --apply --path "editor"

# Reverse direction (mirror D: back to E:):
py _scripts\mirror_workspace.py --from D:\open-post-stack --to E:\open-post-stack

# Force-copy conflicts (overrides dest-newer protection):
py _scripts\mirror_workspace.py --apply --include-conflicts
```

### Per-drive files (NOT mirrored)

- **`.claude/`, `.vscode/`, `.idea/`**: per-user/per-machine IDE + tooling state.
- Anything matching the skip patterns below.

### What's automatically skipped

- `derivative media/`: RAID-mirrored proxy/audio/stills layer; has its own sync pipeline; mirroring would be terabytes of binary churn
- `node_modules/`, `__pycache__/`, `_cache/`, `.git/`, `.pytest_cache/`
- `editor/premiere projects/Adobe Premiere Pro Auto-Save/`: Premiere's local rolling autosaves
- `*.prlock`, `.DS_Store`, `Thumbs.db`

### Categories the plan groups changes by

- **COPY**: file is new on source, or source is newer + content differs (overwrites dest)
- **DELETE**: file is on dest but not on source (renames / cleanups that should propagate)
- **CONFLICT**: both differ but **dest mtime is newer**; default behavior is to **skip and flag for human decision** (assumes you edited the dest copy directly and the mirror shouldn't clobber)
- **SKIP-IDENTICAL**: files that match by SHA-256 (or size+mtime for files >200MB)

### When to use each phase

| Situation | Command |
|---|---|
| End of a session, want to push edits to the mirror SSD | Plan first → review → `--apply` |
| Quick sanity check what's drifted | Default (no flags) |
| The mirror SSD has local edits you want to pull back | `--from D:\open-post-stack --to E:\open-post-stack` |
| Premiere project files keep getting reported as conflict | They probably shouldn't: `editor/premiere projects/*.prproj` is excluded from the autosave subdirectory but the active `.prproj` files themselves DO mirror. Review the conflict list before applying. |

## Adding a new utility here

Match the conventions of the existing scripts:
- ES3-compatible Python (3.10+): `from __future__ import annotations` at top
- Dry-run by default; require explicit `--apply` or `--commit` flag for destructive ops
- Argparse with descriptive `--help` text
- Print a plan before executing; exit non-zero on errors
- Don't write outside the workspace root
