# Graphics
*Approaches we're weighing for data-driven lower-thirds and overlays.*

Graphics aren't built yet; this is the design space. The useful point is that every lower-third's content (name, title, location, date) already lives in the catalog and sidecars, so the only thing left to build is a renderer. Two paths we're considering:

1. **HTML/CSS → PNG overlays → burn over a rendered MP4 with FFmpeg.** Template each graphic in HTML/CSS, fill it from catalog/sidecar fields, render to a transparent PNG (or a short sequence for animation), then composite it over the exported cut with an FFmpeg overlay filter. Fully scriptable, no NLE required, and it reuses the data the cut already carries. This is the broadcast-graphics pattern (CasparCG, NodeCG, and the OGraf standard all drive HTML templates from JSON).

2. **PNG inserts into the XML for direct NLE edits.** Generate the same PNGs, then write them into the xmeml as graphic clips on an overlay track so they import into Premiere (or another NLE) for hand-tweaking placement and timing.

A mix is most likely: HTML/CSS for the look, PNG as the interchange, then either an FFmpeg burn for a no-NLE output or the XML route when a human should adjust on the timeline. Whichever renderer wins, it's a small build because the data is already structured.
