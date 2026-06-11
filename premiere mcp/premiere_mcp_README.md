# Premiere MCP: notes, not code
*Pointers to the open-source MCP servers we drove Premiere with, and the Premiere 26.x patches we needed.*

This repo does **not** ship a Premiere MCP server. We drove Adobe Premiere Pro from an AI agent over the Model Context Protocol using an existing open-source server, lightly patched. If you want the same, install one of the upstreams below and apply the fixes that matter for your Premiere version.

## Upstream servers (both MIT)

- **What we used (base):** [leancoderkavy/premiere-pro-mcp](https://github.com/leancoderkavy/premiere-pro-mcp): ~269 tools over a CEP + ExtendScript + QE DOM bridge. Strong editorial surface: trim primitives, source-monitor 3-point flow, deep introspection. Full credit for that tool surface is theirs.
- **Alternative (audio depth):** [hetpatel-11/Adobe_Premiere_Pro_MCP](https://github.com/hetpatel-11/Adobe_Premiere_Pro_MCP): fewer trim primitives, but has audio ducking, bulk audio effects, caption reading, and transitions. Worth porting a couple of tools from.

## Patches we needed on Premiere 26.x (light trial and error)

The upstream tools are good, but several broke on Premiere 26.2.2 against a real multicam project. The fixes, in case you hit the same:

- **`undo`**: `app.project.undo()` doesn't exist on 26.x; use `qe.project.undo()` (the path the working `redo` tool already used).
- **Trim primitives** (`ripple_delete`, `roll_edit`, `split_clip`, `slide`/`slip`): the QE DOM calls (`Track.razor`, `TrackItem.roll/slide/slip`) are silently broken or reject every arg shape on 26.x. Rewrite them in ExtendScript: trim with `clip.end =` / `clip.start =` setters, split with `seq.overwriteClip()`.
- **QE index mismatch**: `qeTrack.getItemAt(n)` counts gaps as items, so it doesn't line up with ExtendScript's `track.clips[n]`. That index bug is what made the upstream `ripple_delete` close a leading gap instead of removing the clip.
- **Sympathetic ripple**: `seq.insertClip()` only ripples the targeted track and its linked legs, not all sync-locked tracks like the UI does, so other tracks silently desync. Snapshot every unlocked V+A clip at or after the edit point and shift them by the delta yourself.
- **Silent same-source merge**: `seq.overwriteClip()` merges with an adjacent same-source clip into one extended clip, with no warning. Detect the adjacency first, then refuse or delete-then-overwrite.
- **Linked V+A**: walk `getLinkedItems()` so ripple/roll/split act on the audio leg atomically, and auto-detect the linked-audio track instead of hardcoding index 0 (multicam sequences put a lock-coupled multicam track at 0).
- **Undo caveat**: the ExtendScript `.end` / `.start` setters don't create undo-stack entries on 26.x; save the project before destructive ops rather than relying on undo.

## Install

Install whichever upstream you pick per its own README. Apply the patches above only if you need matching timeline and ripple behavior on Premiere 26.x.
