"""Sample scene builder — the shipped end-to-end example.

Builds a 15.5s three-clip scene from the repo's sample assets:

    V1/A1:  C8962.MP4        runner warm-up at a campsite (verite)     0.5-4.5s
    V1/A1:  C0215.MP4        bull-moose b-roll (transition)            3.0-7.0s
    V1/A1:  A003C0028_...MOV the "Nobel Prize for running" banter      8.0-15.5s

This demonstrates the scene-sandbox pattern from scene_workspace_README.md:
one build script per scene, source in/outs as editable constants, abutted V1
cuts with linked stereo audio. Re-emit after every editorial note (~3s).

Downstream, the same scene feeds the sidecar pipeline example (see
editor/story/sidecars/sidecars_README.md): build_resolver -> make_act_sidecar ->
denormalize -> render. Output: scene_sample.v1.xml
"""
from __future__ import annotations
import sys
from pathlib import Path
from lxml import etree

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent / "_scripts"))
import _pproticks as ticks  # noqa: E402
from insert_video_clips import _build_basic_motion_filter  # noqa: E402

OUTPUT_XML = _HERE / "scene_sample.v1.xml"
SAMPLE = _HERE.parent.parent.parent / "derivative media" / "sample"

# (label, file, src_in_sec, src_out_sec, total_sec) — edit in/outs and re-emit
CUTS = [
    ("runner warm-up (C8962)",        "C8962.MP4",                   0.5,  4.5,  6.01),
    ("bull moose b-roll (C0215)",     "C0215.MP4",                   3.0,  7.0, 10.51),
    ("Nobel banter (A003C0028)",      "A003C0028_240826_K29Y06.MOV", 8.0, 15.5, 19.05),
]


def _f(s):
    return ticks.sec_to_frame(s)


def _tc(p):
    tc = etree.SubElement(p, "timecode"); r = etree.SubElement(tc, "rate")
    etree.SubElement(r, "timebase").text = "24"; etree.SubElement(r, "ntsc").text = "TRUE"
    etree.SubElement(tc, "string").text = "00:00:00:00"; etree.SubElement(tc, "frame").text = "0"
    etree.SubElement(tc, "displayformat").text = "NDF"


def _file_def(fid, name, proxy, total_f):
    f = etree.Element("file", id=fid)
    etree.SubElement(f, "name").text = name
    etree.SubElement(f, "pathurl").text = ticks.windows_path_to_pathurl(str(proxy))
    r = etree.SubElement(f, "rate"); etree.SubElement(r, "timebase").text = "24"; etree.SubElement(r, "ntsc").text = "TRUE"
    etree.SubElement(f, "duration").text = str(total_f)
    _tc(f)
    media = etree.SubElement(f, "media")
    v = etree.SubElement(media, "video"); vsc = etree.SubElement(v, "samplecharacteristics")
    vr = etree.SubElement(vsc, "rate"); etree.SubElement(vr, "timebase").text = "24"; etree.SubElement(vr, "ntsc").text = "TRUE"
    etree.SubElement(vsc, "width").text = "1280"; etree.SubElement(vsc, "height").text = "720"
    etree.SubElement(vsc, "anamorphic").text = "FALSE"; etree.SubElement(vsc, "pixelaspectratio").text = "square"
    etree.SubElement(vsc, "fielddominance").text = "none"
    au = etree.SubElement(media, "audio"); asc = etree.SubElement(au, "samplecharacteristics")
    etree.SubElement(asc, "depth").text = "16"; etree.SubElement(asc, "samplerate").text = "48000"
    etree.SubElement(au, "channelcount").text = "2"
    return f


def _levels():
    flt = etree.Element("filter")
    eff = etree.SubElement(flt, "effect")
    for t, v in (("name", "Audio Levels"), ("effectid", "audiolevels"), ("effectcategory", "audiolevels"),
                 ("effecttype", "audiolevels"), ("mediatype", "audio"), ("pproBypass", "false")):
        etree.SubElement(eff, t).text = v
    par = etree.SubElement(eff, "parameter", authoringApp="PremierePro")
    etree.SubElement(par, "parameterid").text = "level"
    etree.SubElement(par, "name").text = "Level"
    etree.SubElement(par, "valuemin").text = "0"; etree.SubElement(par, "valuemax").text = "3.98109"
    etree.SubElement(par, "value").text = "1"
    return flt


def _video_clip(cid, mcid, name, start, end, in_f, out_f, total_f, file_el, aid):
    ci = etree.Element("clipitem", id=cid)
    etree.SubElement(ci, "masterclipid").text = mcid
    etree.SubElement(ci, "name").text = name
    etree.SubElement(ci, "enabled").text = "TRUE"; etree.SubElement(ci, "duration").text = str(total_f)
    r = etree.SubElement(ci, "rate"); etree.SubElement(r, "timebase").text = "24"; etree.SubElement(r, "ntsc").text = "TRUE"
    etree.SubElement(ci, "start").text = str(start); etree.SubElement(ci, "end").text = str(end)
    etree.SubElement(ci, "in").text = str(in_f); etree.SubElement(ci, "out").text = str(out_f)
    etree.SubElement(ci, "pproTicksIn").text = str(ticks.ticks_for_frame(in_f))
    etree.SubElement(ci, "pproTicksOut").text = str(ticks.ticks_for_frame(out_f))
    etree.SubElement(ci, "alphatype").text = "none"; etree.SubElement(ci, "pixelaspectratio").text = "square"; etree.SubElement(ci, "anamorphic").text = "FALSE"
    ci.append(file_el); ci.append(_build_basic_motion_filter())
    l1 = etree.SubElement(ci, "link"); etree.SubElement(l1, "linkclipref").text = cid
    etree.SubElement(l1, "mediatype").text = "video"; etree.SubElement(l1, "trackindex").text = "1"; etree.SubElement(l1, "clipindex").text = "1"
    l2 = etree.SubElement(ci, "link"); etree.SubElement(l2, "linkclipref").text = aid
    etree.SubElement(l2, "mediatype").text = "audio"; etree.SubElement(l2, "trackindex").text = "1"; etree.SubElement(l2, "clipindex").text = "1"
    return ci


def _audio_clip(cid, mcid, name, start, end, in_f, out_f, total_f, fid, vid):
    ci = etree.Element("clipitem", id=cid, premiereChannelType="stereo")
    etree.SubElement(ci, "masterclipid").text = mcid
    etree.SubElement(ci, "name").text = name
    etree.SubElement(ci, "enabled").text = "TRUE"; etree.SubElement(ci, "duration").text = str(total_f)
    r = etree.SubElement(ci, "rate"); etree.SubElement(r, "timebase").text = "24"; etree.SubElement(r, "ntsc").text = "TRUE"
    etree.SubElement(ci, "start").text = str(start); etree.SubElement(ci, "end").text = str(end)
    etree.SubElement(ci, "in").text = str(in_f); etree.SubElement(ci, "out").text = str(out_f)
    etree.SubElement(ci, "pproTicksIn").text = str(ticks.ticks_for_frame(in_f))
    etree.SubElement(ci, "pproTicksOut").text = str(ticks.ticks_for_frame(out_f))
    etree.SubElement(ci, "file", id=fid)
    st = etree.SubElement(ci, "sourcetrack"); etree.SubElement(st, "mediatype").text = "audio"; etree.SubElement(st, "trackindex").text = "1"
    ci.append(_levels())
    l1 = etree.SubElement(ci, "link"); etree.SubElement(l1, "linkclipref").text = vid
    etree.SubElement(l1, "mediatype").text = "video"; etree.SubElement(l1, "trackindex").text = "1"; etree.SubElement(l1, "clipindex").text = "1"
    l2 = etree.SubElement(ci, "link"); etree.SubElement(l2, "linkclipref").text = cid
    etree.SubElement(l2, "mediatype").text = "audio"; etree.SubElement(l2, "trackindex").text = "1"; etree.SubElement(l2, "clipindex").text = "1"
    return ci


def main():
    cursor = 0
    v_clips, a_clips, placements = [], [], []
    for i, (label, fn, in_s, out_s, total_s) in enumerate(CUTS):
        fid, mcid = f"file-{101+i}", f"masterclip-{1+i}"
        vid, aid = f"clipitem-{2*i+1}", f"clipitem-{2*i+2}"
        total_f, in_f, out_f = _f(total_s), _f(in_s), _f(out_s)
        dur = out_f - in_f
        fdef = _file_def(fid, fn, SAMPLE / fn, total_f)
        v_clips.append(_video_clip(vid, mcid, label, cursor, cursor + dur, in_f, out_f, total_f, fdef, aid))
        a_clips.append(_audio_clip(aid, mcid, label, cursor, cursor + dur, in_f, out_f, total_f, fid, vid))
        placements.append((cursor, dur, label))
        cursor += dur
    total_frames = cursor

    root = etree.Element("xmeml", version="4")
    seq = etree.SubElement(root, "sequence", id="sequence-sample-scene")
    etree.SubElement(seq, "uuid").text = "sample-scene-v1"
    etree.SubElement(seq, "duration").text = str(total_frames)
    rate = etree.SubElement(seq, "rate"); etree.SubElement(rate, "timebase").text = "24"; etree.SubElement(rate, "ntsc").text = "TRUE"
    etree.SubElement(seq, "name").text = "sample scene v1"
    media = etree.SubElement(seq, "media")
    video_el = etree.SubElement(media, "video")
    fmt = etree.SubElement(video_el, "format"); fsc = etree.SubElement(fmt, "samplecharacteristics")
    fr = etree.SubElement(fsc, "rate"); etree.SubElement(fr, "timebase").text = "24"; etree.SubElement(fr, "ntsc").text = "TRUE"
    etree.SubElement(fsc, "width").text = "1280"; etree.SubElement(fsc, "height").text = "720"
    etree.SubElement(fsc, "anamorphic").text = "FALSE"; etree.SubElement(fsc, "pixelaspectratio").text = "square"
    etree.SubElement(fsc, "fielddominance").text = "none"; etree.SubElement(fsc, "colordepth").text = "24"
    vt = etree.SubElement(video_el, "track")
    for c in v_clips:
        vt.append(c)
    etree.SubElement(vt, "enabled").text = "TRUE"; etree.SubElement(vt, "locked").text = "FALSE"
    audio_el = etree.SubElement(media, "audio"); etree.SubElement(audio_el, "numOutputChannels").text = "2"
    af = etree.SubElement(audio_el, "format"); asc = etree.SubElement(af, "samplecharacteristics")
    etree.SubElement(asc, "depth").text = "16"; etree.SubElement(asc, "samplerate").text = "48000"
    a1 = etree.SubElement(audio_el, "track")
    a1.set("currentExplodedTrackIndex", "0"); a1.set("totalExplodedTrackCount", "1"); a1.set("premiereTrackType", "Stereo")
    for c in a_clips:
        a1.append(c)
    etree.SubElement(a1, "enabled").text = "TRUE"; etree.SubElement(a1, "locked").text = "FALSE"; etree.SubElement(a1, "outputchannelindex").text = "1"

    for el in root.iter():
        if el.text is None and len(el) == 0 and "id" not in el.attrib:
            el.text = ""
    body = etree.tostring(root, pretty_print=True, encoding="UTF-8").decode("utf-8")
    OUTPUT_XML.write_text('<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE xmeml>\n' + body, encoding="utf-8")

    def tc(fr2):
        s = fr2 * 1001 / 24000
        return f"{int(s//60)}:{s%60:05.2f}"
    print(f"Wrote: {OUTPUT_XML.name} ({OUTPUT_XML.stat().st_size/1024:.1f} KB)")
    print(f"Total: {total_frames}f = {total_frames*1001/24000:.1f}s | {len(v_clips)} V clips + {len(a_clips)} A clips")
    for st, dur, label in placements:
        print(f"  {tc(st)}  {label}  ({dur*1001/24000:.1f}s)")


if __name__ == "__main__":
    main()
