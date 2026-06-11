"""
Primary timeline date provenance for catalog media (video / audio / still).

**Precedence for ``primary_timeline_date``**:

- **Default:** **source path wins where available** (folder / path layout via
  ``path_metadata.shoot_date``); **otherwise** embedded **camera** time
  (ffprobe ``creation_time`` or stills EXIF ``date_taken``).

- **Exception — camera over source path** for paths matching
  `source_path_prefers_camera_over_folder` (configured shoot matchers —
  placeholders in that function; set them to your corpus's known-bad-clock
  shoots). For those, embedded time wins when present; if missing,
  the same source-path-then-camera chain applies as everywhere else.

`date_source` on each asset JSON is one of:
  - source_path: from folder / path layout (`path_metadata.shoot_date` + `shoot_date_source` == folder)
  - camera_metadata: embedded tags (ffprobe `creation_time`, EXIF date_taken / path filled from ffprobe)
  - filesystem_metadata: primary calendar day matches file `mtime` and not a stronger signal
"""
from __future__ import annotations

import re
from typing import Literal

DATE_SOURCE_SOURCE_PATH = "source_path"
DATE_SOURCE_CAMERA_METADATA = "camera_metadata"
DATE_SOURCE_FILESYSTEM_METADATA = "filesystem_metadata"

RecordKind = Literal["video", "audio", "still"]


def calendar_day_is_usable_for_primary(day: str | None) -> bool:
    """
    Reject placeholders (e.g. 0000-00-00), malformed strings, and impossible Y-M-D.
    """
    if not day or not isinstance(day, str) or len(day) < 10:
        return False
    if day[4] != "-" or day[7] != "-":
        return False
    y, m, d = day[0:4], day[5:7], day[8:10]
    if y == "0000" or m == "00" or d == "00":
        return False
    try:
        yi, mi, di = int(y, 10), int(m, 10), int(d, 10)
    except ValueError:
        return False
    if yi < 1 or mi < 1 or mi > 12 or di < 1 or di > 31:
        return False
    return True


def day_from_still_exif(exif: dict | None) -> str | None:
    """Normalize still ``exif.date_taken`` to ``YYYY-MM-DD`` or return None."""
    exd = exif or {}
    if not isinstance(exd, dict) or "_error" in exd:
        return None
    dt = exd.get("date_taken")
    if not isinstance(dt, str) or len(dt) < 10:
        return None
    dday = _exif_date_prefix(dt)
    if len(dday) != 10 or not calendar_day_is_usable_for_primary(dday):
        return None
    return dday


def day_from_ffprobe_creation_time(creation_time: str) -> str | None:
    """Normalize ffprobe ``creation_time`` to ``YYYY-MM-DD`` or return None."""
    if not isinstance(creation_time, str) or len(creation_time) < 10:
        return None
    if creation_time[4] == "-" and creation_time[7] == "-":
        day = creation_time[:10]
    elif creation_time[4] == ":" and creation_time[7] == ":":
        day = f"{creation_time[0:4]}-{creation_time[5:7]}-{creation_time[8:10]}"
    else:
        day = creation_time[:10]
    if not calendar_day_is_usable_for_primary(day):
        return None
    return day


def source_path_prefers_camera_over_folder(source_path: str) -> bool:
    """
    Shoots where **camera** (embedded) wins over **source path** when both exist.

    Named-folder matchers below are placeholders — set them to your corpus's
    shoots with unreliable camera-folder dates. The regex lines show the
    dated-shoot-pattern variant.
    """
    pl = (source_path or "").replace("/", "\\").casefold()
    if "<undated-shoot-a>" in pl:
        return True
    if "<undated-shoot-b>" in pl:
        return True
    if "<undated-shoot-c>\\" in pl:
        return True
    if re.search(r"\\20\d{2}-\d{1,2}-\d{1,2}_<city>\\", pl):
        return True
    if re.search(r"\\[^\\]*_<name>\\", pl):
        return True
    if "<undated-phone-folder>" in pl:
        return True
    return False


def _exif_date_prefix(date_taken: str) -> str:
    """First 10 chars as YYYY-MM-DD, tolerating YYYY:MM:DD prefix."""
    if len(date_taken) >= 10 and date_taken[4] == ":" and date_taken[7] == ":":
        return f"{date_taken[0:4]}-{date_taken[5:7]}-{date_taken[8:10]}"
    return date_taken[:10]


def timeline_date_fields_for_new_asset(
    record_kind: RecordKind,
    path_metadata: dict | None,
    ffprobe: dict | None,
    exif: dict | None,
    source_path: str | None = None,
) -> tuple[str | None, str | None]:
    """
    Match `index_missing_assets_with_locations` primary_timeline_date precedence.
    Returns (primary_timeline_date as YYYY-MM-DD or None, date_source or None).

    Default: **source path** (path ``shoot_date``) when available, else **camera**.
    Exception paths try **camera** first (see `source_path_prefers_camera_over_folder`),
    then the same source-path / camera chain if embedded is missing.
    """
    if source_path and source_path_prefers_camera_over_folder(source_path):
        if record_kind in ("video", "audio"):
            ff = ffprobe or {}
            if isinstance(ff, dict) and "_error" not in ff:
                ct = ff.get("creation_time")
                if isinstance(ct, str):
                    day = day_from_ffprobe_creation_time(ct)
                    if day:
                        return day, DATE_SOURCE_CAMERA_METADATA
        elif record_kind == "still":
            dday = day_from_still_exif(exif)
            if dday:
                return dday, DATE_SOURCE_CAMERA_METADATA

    pm = path_metadata or {}
    sd = pm.get("shoot_date")
    sds = pm.get("shoot_date_source")

    if isinstance(sd, str) and len(sd) >= 10:
        day = sd[:10]
        if sd[4] == ":" and sd[7] == ":":
            day = f"{sd[0:4]}-{sd[5:7]}-{sd[8:10]}"
        if calendar_day_is_usable_for_primary(day):
            if sds == "folder":
                return day, DATE_SOURCE_SOURCE_PATH
            if sds == "ffprobe_creation_time":
                return day, DATE_SOURCE_CAMERA_METADATA
            return day, DATE_SOURCE_SOURCE_PATH

    if record_kind in ("video", "audio"):
        ff = ffprobe or {}
        if isinstance(ff, dict) and "_error" not in ff:
            ct = ff.get("creation_time")
            if isinstance(ct, str):
                day = day_from_ffprobe_creation_time(ct)
                if day:
                    return day, DATE_SOURCE_CAMERA_METADATA

    if record_kind == "still":
        dday = day_from_still_exif(exif)
        if dday:
            return dday, DATE_SOURCE_CAMERA_METADATA

    return None, None


def infer_date_source_from_asset_record(rec: dict) -> str | None:
    """
    Derive date_source from an on-disk asset row without changing primary_timeline_date.
    Interprets which signal matches ``primary_timeline_date``: path shoot_date vs
    embedded vs mtime. Does not re-run carve-out rules; assumes primary is already correct.
    """
    ptd = rec.get("primary_timeline_date")
    if not isinstance(ptd, str) or len(ptd) < 10:
        return None
    day = ptd[:10]
    if ptd[4] == ":" and ptd[7] == ":":
        day = f"{ptd[0:4]}-{ptd[5:7]}-{ptd[8:10]}"
    if not calendar_day_is_usable_for_primary(day):
        return None

    pm = rec.get("path_metadata") or {}
    sd = pm.get("shoot_date")
    sds = pm.get("shoot_date_source")
    if isinstance(sd, str) and len(sd) >= 10:
        sd_day = sd[:10]
        if sd[4] == ":" and sd[7] == ":":
            sd_day = f"{sd[0:4]}-{sd[5:7]}-{sd[8:10]}"
        if sd_day == day:
            if sds == "ffprobe_creation_time":
                return DATE_SOURCE_CAMERA_METADATA
            return DATE_SOURCE_SOURCE_PATH

    rk = rec.get("record_kind")
    if rk in ("video", "audio"):
        ff = rec.get("ffprobe") or {}
        ct = ff.get("creation_time")
        if isinstance(ct, str) and len(ct) >= 10 and ct[:10] == day:
            return DATE_SOURCE_CAMERA_METADATA
    if rk == "still":
        ex = rec.get("exif") or {}
        dt = ex.get("date_taken")
        if isinstance(dt, str) and len(dt) >= 10:
            if _exif_date_prefix(dt) == day:
                return DATE_SOURCE_CAMERA_METADATA

    mt = rec.get("mtime")
    if isinstance(mt, str) and len(mt) >= 10 and mt[:10] == day:
        return DATE_SOURCE_FILESYSTEM_METADATA

    return None
