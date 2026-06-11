"""clean_audio_cuts.py — silencedetect-driven de-pause + filler detection.

Replicates the manual "keep the energy up" audio cleanup the editor does by hand:
remove dead-air pauses and (optionally) flag filler words, so a raw selects window
becomes a tight, momentum-y cut.

WHY silencedetect (not audio fingerprint): fingerprinting answers "is this the same
audio" (identity). For *pauses* the right signal is energy — ffmpeg `silencedetect`
gives precise silence intervals on the actual proxy, and it works on EVERY source
regardless of whether the transcript carries word text (some early WhisperKit runs
had word timing only — but most transcripts DO carry words[].text, so
filler/stutter detection works there too).
Validated against a hand cut: detected silences land on the exact points the
editor cut manually (within ~0.5s).

CAVEAT (filename collision): clip_asset_id() resolves words by `filename LIKE` +
LIMIT 1, which silently picks the WRONG asset when a camera filename repeats across
shoot days (the same camera filename can exist on two different shoot days). For
such sources, drive the logic from a builder that passes the correct asset_id to
load_words().

What it does, per audio clip in the input xmeml:
  1. decode the clip's proxy path + source [in,out]
  2. run silencedetect over that range
  3. compute "speech islands" = [in,out] minus silences >= --min-silence, keeping
     --pad seconds of breath at each edge (so cuts don't clip word onsets)
  4. emit the islands as abutted sub-clips (hard joins = de-paused), with short
     fade-in/out on the outer edges
  5. (podcasts) scan word-text for filler tokens + repeated-word stutters and
     REPORT them as candidates — NOT auto-removed (filler removal is editorial;
     "like"/"you know" are sometimes meaningful, and Whisper word stamps are ±100ms)

Pause removal is the safe, automatic layer. Filler removal is propose-then-confirm.

Usage:
  # de-pause only:
  py clean_audio_cuts.py --xml "<in.xml>" --out "<out.xml>" [--min-silence 0.7]
  # de-pause + safe disfluencies (stutters + um/uh):
  py clean_audio_cuts.py --xml "<in.xml>" --out "<out.xml>" --apply-stutters

Context-dependent fillers ("like"/"you know"/"i mean"/...) — TWO-PASS (inference in the loop):
  # PASS 1 — emit candidates with surrounding text for an LLM to judge:
  py clean_audio_cuts.py --xml "<in.xml>" --out _ --emit-filler-candidates cands.json
  # (inference sets each entry's verdict: "filler" / "meaningful" / "borderline";
  #  e.g. "sounds like"/"seems like"/"all right"/"like 300 yards" -> meaningful, kept.
  #  Do this with the LLM agent, or a Gemini batch like the catalog pipeline.)
  # PASS 2 — apply ONLY the confirmed fillers (+ stutters/um-uh):
  py clean_audio_cuts.py --xml "<in.xml>" --out "<out.xml>" --apply-stutters --filler-spans cands.json

NOTE: aggressive filler removal fragments a clip into many micro-cuts (±100ms word
stamps) — listen before trusting it; dial back via which verdicts you remove.

Flags: [--noise -32dB] [--min-silence 0.5] [--pad 0.1] [--report-fillers] [--edge-fade-frames 0]

Reads our-builder-style xmeml (selects reel / scene builds). Output is a cleaned
sequential reel + a per-source report (orig dur -> cleaned dur, time saved).
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import urllib.parse
from pathlib import Path

from lxml import etree

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
_REPO = _HERE.parent.parent.parent
import _pproticks as ticks  # noqa: E402
from insert_video_clips import _build_basic_motion_filter  # noqa: E402

FPS = 24000 / 1001
TX = _REPO / "dataset" / "assets" / "transcripts"

FILLERS = {"um", "uh", "uhh", "er", "ah", "like", "basically", "literally", "actually",
           "you know", "i mean", "kind of", "sort of", "i guess", "right"}


# ---------------- silence detection ----------------
def detect_silences(proxy: Path, in_sec: float, out_sec: float, noise: str, min_sil: float):
    """Return list of (abs_start, abs_end) silence intervals within [in_sec, out_sec]."""
    dur = out_sec - in_sec
    cmd = ["ffmpeg", "-nostats", "-ss", f"{in_sec:.3f}", "-t", f"{dur:.3f}",
           "-i", str(proxy), "-af", f"silencedetect=noise={noise}:d={min_sil}", "-f", "null", "-"]
    r = subprocess.run(cmd, capture_output=True, text=True)
    out = r.stderr + r.stdout
    sils = []
    cur = None
    for m in re.finditer(r"silence_(start|end):\s*([0-9.]+)", out):
        kind, val = m.group(1), float(m.group(2))
        if kind == "start":
            cur = val
        elif kind == "end" and cur is not None:
            sils.append((in_sec + cur, in_sec + val))
            cur = None
    return sils


def speech_islands(in_sec, out_sec, silences, pad, min_island=0.20):
    """[in,out] minus silences, keeping `pad` of breath at each silence edge."""
    islands = []
    cursor = in_sec
    for s0, s1 in silences:
        seg_end = min(s0 + pad, out_sec)
        if seg_end - cursor >= min_island:
            islands.append((cursor, seg_end))
        cursor = max(s1 - pad, cursor)
    if out_sec - cursor >= min_island:
        islands.append((cursor, out_sec))
    return islands


SAFE_FILLERS = {"um", "uh", "uhh", "er", "ah"}  # auto-removable; NOT "like"/"you know"


def subtract_spans(islands, spans, min_island=0.15):
    """Remove word spans (filler/stutter) from islands, splitting as needed."""
    res = list(islands)
    for s, e in spans:
        new = []
        for a, b in res:
            if e <= a or s >= b:
                new.append((a, b)); continue
            if s - a >= min_island:
                new.append((a, s))
            if b - e >= min_island:
                new.append((e, b))
        res = new
    return res


# ---------------- filler scan (report only) ----------------
def load_words(asset_id_short):
    import sqlite3
    con = sqlite3.connect(str(_REPO / "indexes" / "editorial_catalog.sqlite"))
    row = con.execute("SELECT asset_id FROM asset WHERE asset_id LIKE ?", (asset_id_short + "%",)).fetchone()
    con.close()
    if not row:
        return []
    p = TX / f"{row[0]}.transcript.json"
    if not p.exists():
        return []
    ws = []
    for s in json.loads(p.read_text(encoding="utf-8")).get("segments", []):
        for w in (s.get("words") or []):
            ws.append((w["start_sec"], w["end_sec"], (w.get("text", "") or "").strip().lower().strip(".,!?")))
    return ws


def scan_fillers(words, in_sec, out_sec):
    inwin = [(a, b, t) for a, b, t in words if a >= in_sec - 0.05 and b <= out_sec + 0.05]
    hits = []
    for i, (a, b, t) in enumerate(inwin):
        if t in FILLERS:
            hits.append((a, b, t))
        if i > 0 and t and t == inwin[i - 1][2]:  # repeated-word stutter
            hits.append((a, b, f"(stutter) {t}"))
    return hits


# Context-dependent fillers: CANNOT be removed by rule — "like"/"right"/"you know"
# are sometimes meaningful (verb "I like", comparative "like the wind", "that's right").
# These get EMITTED as candidates with surrounding text, classified by inference
# (filler / meaningful / borderline), and only verdict=="filler" is removed.
CONTEXT_FILLERS_1 = {"like", "right", "actually", "basically", "literally", "honestly"}
CONTEXT_FILLERS_2 = {("you", "know"), ("i", "mean"), ("kind", "of"), ("sort", "of"), ("i", "guess")}


def find_context_fillers(words, in_sec, out_sec, asset_id):
    """Return contextual-filler candidates (need inference to classify), with context."""
    inwin = [(a, b, t) for a, b, t in words if a >= in_sec - 0.05 and b <= out_sec + 0.05]
    out = []
    for i, (a, b, t) in enumerate(inwin):
        ctx = " ".join(x[2] for x in inwin[max(0, i - 7):i + 7])
        if t in CONTEXT_FILLERS_1:
            out.append({"asset": asset_id[:12], "t_start": round(a, 2), "t_end": round(b, 2),
                        "word": t, "context": ctx, "verdict": None})
        if i + 1 < len(inwin) and (t, inwin[i + 1][2]) in CONTEXT_FILLERS_2:
            out.append({"asset": asset_id[:12], "t_start": round(a, 2), "t_end": round(inwin[i + 1][1], 2),
                        "word": f"{t} {inwin[i + 1][2]}", "context": ctx, "verdict": None})
    return out


# ---------------- xmeml helpers (compact; mirror the scene builders) ----------------
def _tc(p):
    tc = etree.SubElement(p, "timecode"); r = etree.SubElement(tc, "rate")
    etree.SubElement(r, "timebase").text = "24"; etree.SubElement(r, "ntsc").text = "TRUE"
    etree.SubElement(tc, "string").text = "00:00:00:00"; etree.SubElement(tc, "frame").text = "0"
    etree.SubElement(tc, "displayformat").text = "NDF"


def _levels(in_f, out_f, fi, fo):
    flt = etree.Element("filter"); eff = etree.SubElement(flt, "effect")
    for tag, val in (("name", "Audio Levels"), ("effectid", "audiolevels"), ("effectcategory", "audiolevels"),
                     ("effecttype", "audiolevels"), ("mediatype", "audio"), ("pproBypass", "false")):
        etree.SubElement(eff, tag).text = val
    p = etree.SubElement(eff, "parameter", authoringApp="PremierePro")
    etree.SubElement(p, "parameterid").text = "level"; etree.SubElement(p, "name").text = "Level"
    etree.SubElement(p, "valuemin").text = "0"; etree.SubElement(p, "valuemax").text = "3.98109"; etree.SubElement(p, "value").text = "1"
    kfs = ([(in_f, 0.0), (in_f + fi, 1.0)] if fi > 0 else [(in_f, 1.0)]) + \
          ([(out_f - fo, 1.0), (out_f, 0.0)] if fo > 0 else [(out_f, 1.0)])
    seen = {}
    for w, v in kfs:
        seen[int(w)] = v
    for w in sorted(seen):
        kf = etree.SubElement(p, "keyframe"); etree.SubElement(kf, "when").text = str(w); etree.SubElement(kf, "value").text = f"{seen[w]:g}"
    return flt


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--xml", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--noise", default="-32dB")
    ap.add_argument("--min-silence", type=float, default=0.5)
    ap.add_argument("--pad", type=float, default=0.1)
    ap.add_argument("--report-fillers", action="store_true")
    ap.add_argument("--apply-stutters", action="store_true",
                    help="auto-remove SAFE disfluencies only: repeated-word stutters + um/uh/er/ah. "
                         "Leaves 'like'/'you know'/etc (context-dependent). Needs word-text (podcasts).")
    ap.add_argument("--edge-fade-frames", type=int, default=0,
                    help="frames of fade-in/out on each source's outer edges. Default 0 = NO level "
                         "automation (clips at natural unity volume). The de-pause/destutter joins are "
                         "always hard cuts regardless. Set e.g. 3-4 only if you want edge fades baked in.")
    ap.add_argument("--emit-filler-candidates", metavar="JSON",
                    help="PASS 1: write context-dependent filler candidates (like/you know/i mean/...) "
                         "with surrounding text to JSON for inference to classify (set each verdict to "
                         "'filler'/'meaningful'/'borderline'). No XML is written in this mode.")
    ap.add_argument("--filler-spans", metavar="JSON",
                    help="PASS 2: a classified candidates JSON; entries with verdict=='filler' are removed "
                         "alongside stutters/um-uh (requires --apply-stutters). 'meaningful'/'borderline' kept.")
    a = ap.parse_args()

    root = etree.parse(a.xml).getroot()
    fpath = {f.get("id"): f.findtext("pathurl") for f in root.iter("file") if f.get("id") and f.findtext("pathurl")}

    # gather audio clips (dedup video/audio of same source-range by (pathurl,in,out))
    clips = []
    seen = set()
    for ci in root.iter("clipitem"):
        fel = ci.find("file")
        if fel is None:
            continue
        pu = fpath.get(fel.get("id")) or (fel.findtext("pathurl") or "")
        if not pu:
            continue
        try:
            i = int(ci.findtext("in")); o = int(ci.findtext("out"))
        except (TypeError, ValueError):
            continue
        key = (pu, i, o)
        if key in seen:
            continue
        seen.add(key)
        clips.append({"pu": pu, "in": i, "out": o, "name": ci.findtext("name") or ""})

    def pu_to_path(pu):
        rest = pu.replace("file://localhost/", "")
        return Path(urllib.parse.unquote(rest).replace("&amp;", "&").replace("/", "\\"))

    print(f"Cleaning {len(clips)} source clip(s) from {Path(a.xml).name}")
    print(f"params: noise={a.noise} min_silence={a.min_silence}s pad={a.pad}s\n")

    # PASS 2 input: classified filler verdicts (asset_short -> [(t_start,t_end), ...])
    filler_by_asset = {}
    if a.filler_spans:
        for e in json.loads(Path(a.filler_spans).read_text(encoding="utf-8")):
            if e.get("verdict") == "filler":
                filler_by_asset.setdefault(e["asset"], []).append((e["t_start"], e["t_end"]))

    def clip_asset_id(proxy):
        if proxy.suffix.lower() != ".wav":
            return None
        import sqlite3
        con = sqlite3.connect(str(_REPO / "indexes" / "editorial_catalog.sqlite"))
        row = con.execute("SELECT asset_id FROM asset WHERE filename LIKE ? LIMIT 1", (proxy.stem + ".%",)).fetchone()
        con.close()
        return row[0] if row else None

    emit_candidates = []
    out_clips = []  # (name, proxy, pu, islands[list of (in_f,out_f)])
    tot_orig = tot_clean = 0.0
    for c in clips:
        proxy = pu_to_path(c["pu"])
        in_s, out_s = c["in"] / FPS, c["out"] / FPS
        aid = clip_asset_id(proxy)
        words = load_words(aid[:12]) if aid else []

        # PASS 1: collect context-dependent filler candidates for inference, skip the rest
        if a.emit_filler_candidates:
            if words:
                emit_candidates.extend(find_context_fillers(words, in_s, out_s, aid))
            continue

        sils = detect_silences(proxy, in_s, out_s, a.noise, a.min_silence)
        islands = speech_islands(in_s, out_s, sils, a.pad)
        orig = out_s - in_s

        fh = scan_fillers(words, in_s, out_s) if ((a.report_fillers or a.apply_stutters) and words) else []
        safe = []
        if a.apply_stutters and fh:  # stutters + um/uh/er/ah (rule-safe)
            safe += [(s, e) for s, e, t in fh if t.startswith("(stutter)") or t in SAFE_FILLERS]
        if aid and filler_by_asset.get(aid[:12]):  # inference-confirmed contextual fillers
            safe += [(s, e) for s, e in filler_by_asset[aid[:12]] if in_s - 0.05 <= s and e <= out_s + 0.05]
        note = ""
        if safe:
            before = sum(b - x for x, b in islands)
            islands = subtract_spans(islands, sorted(safe))
            note = f"  [trim: -{before - sum(b - x for x, b in islands):.1f}s, {len(safe)} disfluency/filler]"

        clean = sum(b - a2 for a2, b in islands)
        tot_orig += orig; tot_clean += clean
        print(f"  {c['name'][:34]:34s} {orig:5.1f}s -> {clean:5.1f}s  (-{orig-clean:4.1f}s, {len(sils)} sil, {len(islands)} islands){note}")
        if a.report_fillers and fh:
            print(f"      stutter/um-uh: " + ", ".join(f"{t}@{x:.1f}" for x, b, t in fh[:12]))
        out_clips.append((c["name"], proxy, c["pu"], [(int(round(x * FPS)), int(round(y * FPS))) for x, y in islands]))

    if a.emit_filler_candidates:
        Path(a.emit_filler_candidates).write_text(json.dumps(emit_candidates, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"PASS 1: wrote {len(emit_candidates)} contextual-filler candidates -> {a.emit_filler_candidates}")
        print("Classify each (verdict: filler / meaningful / borderline), then PASS 2:")
        print("  --apply-stutters --filler-spans <classified.json>")
        return

    print(f"\nTOTAL: {tot_orig:.1f}s -> {tot_clean:.1f}s  (removed {tot_orig-tot_clean:.1f}s, {100*(tot_orig-tot_clean)/tot_orig:.0f}%)")

    # ---- emit cleaned sequential reel ----
    GAP = 24
    FADE = a.edge_fade_frames  # default 0 = flat unity (no volume change); de-pause joins stay hard cuts
    root_o = etree.Element("xmeml", version="4")
    seq = etree.SubElement(root_o, "sequence", id="sequence-cleaned")
    etree.SubElement(seq, "uuid").text = "clean-audio-cuts"
    rate = etree.SubElement(seq, "rate"); etree.SubElement(rate, "timebase").text = "24"; etree.SubElement(rate, "ntsc").text = "TRUE"
    etree.SubElement(seq, "name").text = f"{Path(a.xml).stem} CLEANED (de-paused)"
    media = etree.SubElement(seq, "media")
    aud = etree.SubElement(media, "audio"); etree.SubElement(aud, "numOutputChannels").text = "2"
    fmt = etree.SubElement(aud, "format"); sc = etree.SubElement(fmt, "samplecharacteristics")
    etree.SubElement(sc, "depth").text = "16"; etree.SubElement(sc, "samplerate").text = "48000"
    tr = etree.SubElement(aud, "track")
    tr.set("currentExplodedTrackIndex", "0"); tr.set("totalExplodedTrackCount", "1"); tr.set("premiereTrackType", "Mono")

    cid = [0]; fid = [0]; emitted = {}
    def ncid():
        cid[0] += 1; return f"clipitem-{cid[0]}"
    cursor = 0
    total_frames = 0
    for name, proxy, pu, islands in out_clips:
        if pu not in emitted:
            fid[0] += 1; emitted[pu] = f"file-{fid[0]}"
        file_id = emitted[pu]
        first = True
        for k, (i_f, o_f) in enumerate(islands):
            dur = o_f - i_f
            ci = etree.SubElement(tr, "clipitem", id=ncid(), premiereChannelType="mono")
            etree.SubElement(ci, "masterclipid").text = file_id
            etree.SubElement(ci, "name").text = name[:40]
            etree.SubElement(ci, "enabled").text = "TRUE"
            etree.SubElement(ci, "duration").text = str(dur)
            r = etree.SubElement(ci, "rate"); etree.SubElement(r, "timebase").text = "24"; etree.SubElement(r, "ntsc").text = "TRUE"
            etree.SubElement(ci, "start").text = str(cursor); etree.SubElement(ci, "end").text = str(cursor + dur)
            etree.SubElement(ci, "in").text = str(i_f); etree.SubElement(ci, "out").text = str(o_f)
            etree.SubElement(ci, "pproTicksIn").text = str(ticks.ticks_for_frame(i_f))
            etree.SubElement(ci, "pproTicksOut").text = str(ticks.ticks_for_frame(o_f))
            # file def: full on very first use of this file across the whole doc
            if emitted.get(("_full", pu)) is None:
                f = etree.SubElement(ci, "file", id=file_id)
                etree.SubElement(f, "name").text = proxy.name
                etree.SubElement(f, "pathurl").text = ticks.windows_path_to_pathurl(str(proxy))
                fr = etree.SubElement(f, "rate"); etree.SubElement(fr, "timebase").text = "24"; etree.SubElement(fr, "ntsc").text = "TRUE"
                etree.SubElement(f, "duration").text = "999999"
                _tc(f)
                m = etree.SubElement(f, "media"); au = etree.SubElement(m, "audio"); ascx = etree.SubElement(au, "samplecharacteristics")
                etree.SubElement(ascx, "depth").text = "16"; etree.SubElement(ascx, "samplerate").text = "16000"
                etree.SubElement(au, "channelcount").text = "1"
                emitted[("_full", pu)] = True
            else:
                etree.SubElement(ci, "file", id=file_id)
            st = etree.SubElement(ci, "sourcetrack"); etree.SubElement(st, "mediatype").text = "audio"; etree.SubElement(st, "trackindex").text = "1"
            fi = FADE if first else 0
            fo = FADE if k == len(islands) - 1 else 0
            ci.append(_levels(i_f, o_f, fi, fo))
            lk = etree.SubElement(ci, "link"); etree.SubElement(lk, "linkclipref").text = ci.get("id")
            etree.SubElement(lk, "mediatype").text = "audio"; etree.SubElement(lk, "trackindex").text = "1"; etree.SubElement(lk, "clipindex").text = "1"
            cursor += dur
            first = False
        cursor += GAP  # gap between sources
        total_frames = cursor
    etree.SubElement(tr, "enabled").text = "TRUE"; etree.SubElement(tr, "locked").text = "FALSE"; etree.SubElement(tr, "outputchannelindex").text = "1"
    seq.insert(2, etree.Element("duration")); seq.find("duration").text = str(total_frames)

    for el in root_o.iter():
        if el.text is None and len(el) == 0 and "id" not in el.attrib:
            el.text = ""
    body = etree.tostring(root_o, pretty_print=True, encoding="UTF-8").decode("utf-8")
    Path(a.out).write_text('<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE xmeml>\n' + body, encoding="utf-8")
    print(f"\nWrote cleaned reel: {a.out}")


if __name__ == "__main__":
    main()
