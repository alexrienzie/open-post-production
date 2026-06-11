# xml exports / _scripts

Direct-edit helpers for the Premiere FCP7 xmeml round-trip. Generate a new
xmeml file from a small JSON plan, layer new clipitems onto a target video
track, never overwrite the source export.

## Files

| File | Purpose |
|---|---|
| `insert_video_clips.py` | Main script. Reads a plan JSON, parses an existing xmeml, inserts new `<file>` defs + `<clipitem>` elements on the target track, writes a new xmeml alongside the original. |
| `_pproticks.py` | Helper: Premiere ticks math (`254,016,000,000` ticks/sec; for 23.976 NTSC, exactly `10,594,584,000` ticks/frame) + Windows path → `file://localhost/...` pathurl encoding to match Premiere's existing export style. |
| `_scripts_README.md` | This file. |

Plans go in `../_plans/`. Outputs land in `../` (alongside the source xmeml) with a timestamped filename.

## Quick start

```powershell
# Dry-run: resolve assets, compute frame math, check for overlaps, print plan, don't write.
py "editor\xml exports\_scripts\insert_video_clips.py" `
    --plan "editor\xml exports\_plans\<my_plan>.json" `
    --dry-run

# Real run: outputs to editor/xml exports/project_act II_<timestamp>_<suffix>.xml
py "editor\xml exports\_scripts\insert_video_clips.py" `
    --plan "editor\xml exports\_plans\<my_plan>.json"

# Validate structurally (uses the existing sidecar-scripts validator)
py "editor\story\_sidecar scripts\validate_xml_structure.py" `
    "editor\xml exports\project_act II_<timestamp>_<suffix>.xml" `
    --baseline "editor\xml exports\<source export>.xml"
```

Then in Premiere: `File → Import → select the new XML`. It imports as a fresh sequence (never overwrites the working `.prproj`).

## Plan JSON

```json
{
  "_description":         "optional free-text purpose tag",
  "source_xml":           "project_act II_premiere export_<ts>.xml",
  "target_video_track":   "V4",
  "fps":                  23.976023976,
  "output_name_suffix":   "broll_test",
  "sequence_name":        "Act II (b-roll test)",
  "insertions": [
    {
      "asset_id":           "<sha256 from editorial_catalog>",
      "label":              "human-readable note (logging only)",
      "timeline_start_sec": 307.39,
      "timeline_end_sec":   317.39,
      "source_in_sec":      0.0,
      "source_out_sec":     10.0
    }
  ]
}
```

Fields:

- `source_xml` is relative to `editor/xml exports/`. Absolute paths also accepted.
- `target_video_track` must be one of the existing video tracks (`V1`..`V<N>`). The script will not add new tracks; if you need V4 and the xmeml only has V1..V3, export from Premiere with more tracks first.
- `output_name_suffix` is appended to the auto-generated output filename (`project_act II_<ts>_<suffix>.xml`). Override entirely with `--output <path>`.
- `sequence_name` (optional) replaces `<sequence><name>` so Premiere's project panel can distinguish this test from other Act II exports.
- Per-insertion: timeline span and source span should match in frames for clean cuts. If they differ Premiere will apply a speed change to that clip (script logs a NOTE).

## What the script does, in order

1. Parse the plan JSON; validate required fields and non-empty insertions.
2. Resolve `source_xml` relative to the plan file's parent dir.
3. Parse the xmeml with lxml. Index:
   - existing `<file>` defs by filename → reuse the same `file-NNN` when re-cutting an already-used source
   - max `clipitem-NNNN`, `file-NNN`, `masterclip-NNN` ids to allocate fresh ones
4. Load `derivative media/_index/asset_map.json`. For each insertion's `asset_id`, look up `entries[asset_id].video_video_proxy.relative_path` and build the Windows-style proxy path `E:\open-post-stack\derivative media\<relative_path>`. **This is hardcoded** (`DERIVATIVE_MEDIA_PATHURL_ROOT`) so the output xmeml stays portable across machines regardless of where the script runs.
5. Build new clipitems. Resolution is hardcoded to 1280×720 in the new `<file>` defs because the actual proxy files are 1280×720; the catalog's `width`/`height` are source dimensions (often 3840×2160) and would mismatch the proxy media on import.
6. Check for overlaps on the target track. Fail if any.
7. Insert clipitems ordered by `<start>`. Update `<sequence><duration>` if any insertion extends past it.
8. Serialize and write atomically via /tmp + dd + sha256 verify (bindfs-safe pattern).

## Limitations / gotchas

- **Video-only.** New clipitems carry no `<link>` to audio. If you want the b-roll's source audio on an A-track too, drag it manually in Premiere after import.
- **Proxy assumption.** Hardcoded `1280×720` proxy resolution and `E:\open-post-stack\derivative media\` root. Change `DERIVATIVE_MEDIA_PATHURL_ROOT` in `insert_video_clips.py` if your derivative-media tree moves.
- **Track must exist.** Won't create new V-tracks. Export from Premiere with the target track already present (even if empty).
- **One-frame rounding.** Timeline and source spans may differ by 1 frame due to fps rounding; the script logs a NOTE per insertion. Premiere will interpret this as a sub-0.5% speed change, imperceptible but technically wrong. Adjust `source_out_sec` if it matters.
- **Sidecar drift.** This script does NOT update the act's sidecar. After importing in Premiere, re-export the xmeml and run `editor/story/_sidecar scripts/refresh_act_sidecar.py --xml <new export>` to bring the sidecar in sync.

## Where the asset_ids come from

Use `editor/queries/retrieval.py` to find candidate assets:

```powershell
# Place-based
py editor\queries\retrieval.py broll --location-like "jenny lake" --limit 20

# Text-described (requires SigLIP weights download on first call)
py editor\queries\retrieval.py similar-text --text "ranger station building, exterior" --asset-type b_roll --top-k 20

# Visually similar to an asset you already like
py editor\queries\retrieval.py similar-chunk --asset-id <known_good_asset_id> --asset-type b_roll --top-k 10
```

The `asset_id` column from those results goes directly into the plan JSON.
