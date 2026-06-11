"""Premiere xmeml tick + pathurl helpers.

ticks/frame derivation:
  Premiere uses 254,016,000,000 ticks/sec. At NTSC 23.976 (= 24000/1001 fps),
  one frame = 1001/24000 s -> 1001 * 254_016_000_000 / 24_000 ticks
            = 10,594,584,000 ticks/frame exactly.

  Confirmed empirically against 816 of 889 clipitems in the May 20 xmeml
  (the other 73 are non-23.976 source rates: 48fps, 60fps, etc.).

Pathurl encoding matches the existing xmeml export style:
  Windows drive E: -> "E%3a" (lowercase hex)
  Spaces           -> "%20"
  Backslash        -> "/"
  Prefix           -> "file://localhost/"
"""

from __future__ import annotations

import urllib.parse
from pathlib import Path

# Exact integer constant for 23.976 NTSC clips.
TICKS_PER_FRAME_23_976 = 254_016_000_000 * 1001 // 24_000  # = 10_594_584_000

TICKS_PER_SECOND = 254_016_000_000

NTSC_23_976 = 24_000 / 1001


def ticks_for_frame(frame: int, *, fps: float = NTSC_23_976) -> int:
    """Return Premiere ticks for a given frame at the given fps.

    For NTSC 23.976 specifically, we use the exact integer formula so the
    output matches what Premiere produces (no float drift).
    """
    if abs(fps - NTSC_23_976) < 1e-6:
        return int(frame) * TICKS_PER_FRAME_23_976
    # General case: ticks = frame / fps * TICKS_PER_SECOND
    return int(round(int(frame) / float(fps) * TICKS_PER_SECOND))


def sec_to_frame(sec: float, *, fps: float = NTSC_23_976) -> int:
    """Round seconds to nearest frame at the given fps."""
    return int(round(float(sec) * float(fps)))


def windows_path_to_pathurl(path: str) -> str:
    """Convert a Windows path (E:\\... or E:/...) to the Premiere file:// pathurl.

    Output style matches the existing xmeml exports:
        file://localhost/E%3a/open-post-stack/derivative%20media/foo.MP4

    PREMIERE-NATIVE ENCODING (verified 2026-05-27 against the known-good
    `project_act I_proxies_20260521.xml`, a cleanly-importing cut): Premiere
    percent-encodes ONLY spaces (`%20`) and the drive colon (`%3a`). Every
    other character is left LITERAL:
        ,  !  #  (  )  +  _  -  .   -> literal
        &                            -> literal `&`, which the XML serializer
                                        writes as the entity `&amp;`
    Do NOT use `urllib.parse.quote`: it percent-encodes `, & ! ( ) +` to
    `%2C %26 %21 %28 %29 %2B`, and Premiere's pathurl decoder treats those
    percent-codes LITERALLY -> the clip imports as MISSING MEDIA. This bit us
    on the b_06_s01 podcast build (Parkography/Rock Fight `.wav` paths contain
    `&` and `,`). See xml_README "pathurl encoding" invariant.

    Non-ASCII characters ARE percent-encoded as UTF-8 (e.g. the narrow
    no-break space ` ` macOS puts before "PM" in screenshot names ->
    `%e2%80%af`). Verified against Act I/III known-good exports, which carry
    exactly these sequences and import cleanly. So the rule is precise:
    percent-encode spaces, the drive colon, and any non-ASCII byte; leave all
    ASCII punctuation literal.
    """
    p = str(path).replace("\\", "/")

    def _enc(s: str) -> str:
        out = []
        for ch in s:
            if ch == "%":
                out.append("%25")  # literal percent -> escaped first
            elif ch == " ":
                out.append("%20")
            elif ord(ch) > 127:
                # UTF-8 percent-encode non-ASCII (lowercase hex, matches macOS/Premiere)
                out.append("".join(f"%{b:02x}" for b in ch.encode("utf-8")))
            else:
                out.append(ch)  # ASCII literal (incl. , ! # ( ) + & -- & is XML-escaped on write)
        return "".join(out)

    if len(p) >= 2 and p[1] == ":":
        drive = p[0]
        rest = p[2:].lstrip("/")
        return f"file://localhost/{drive}%3a/{_enc(rest)}"
    # Not a Windows-style absolute; encode as-is
    return "file://localhost/" + _enc(p.lstrip("/"))


def pathurl_to_windows(pathurl: str) -> str:
    """Inverse of windows_path_to_pathurl: extract original path for diffing/logs."""
    if pathurl.startswith("file://localhost/"):
        s = pathurl[len("file://localhost/") :]
    elif pathurl.startswith("file:///"):
        s = pathurl[len("file:///") :]
    else:
        s = pathurl
    s = urllib.parse.unquote(s)
    # Restore Windows drive form: "E:/..." -> "E:\..."
    if len(s) >= 2 and s[1] == ":":
        return s[0] + ":\\" + s[2:].lstrip("/").replace("/", "\\")
    return s
