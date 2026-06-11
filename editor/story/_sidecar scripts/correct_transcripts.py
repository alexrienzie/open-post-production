#!/usr/bin/env python3
"""Review the sidecar's in-cut transcript snippets via Sonnet and propose
glossary additions for ASR errors not yet covered.

Operates ONLY on the act sidecar. The flow:
  1. Load actII.sidecar.json
  2. Collect unique in-cut transcript snippets (deduped by content key)
  3. Batch per beat -> one `claude -p` call per beat with snippets +
     glossary + asset context
  4. Aggregate proposed corrections into _glossary_suggestions.json
  5. With --auto-merge, suggestions meeting --confidence-threshold are
     merged into _project_glossary.json directly
  6. Re-run refresh_act_sidecar.py -- denormalize applies updated glossary

The glossary is the durable record. Source transcripts stay untouched.

Usage:
  py correct_transcripts.py [--dry-run] [--model claude-sonnet-4-6]
                            [--beats b_06,b_07] [--sidecar PATH] [--glossary PATH]
                            [--out _glossary_suggestions.json]
                            [--auto-merge] [--confidence-threshold medium]
                            [--claude-bin C:\path\to\claude.cmd]
"""
from __future__ import annotations
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional


FILM_CONTEXT = "<one sentence on your film: subject, domain, and the proper nouns ASR is likely to mangle>"

PROMPT_TMPL = """You are reviewing ASR transcript errors for a documentary.
Film context: """ + FILM_CONTEXT + """

Project glossary (canonical names + known mishearings already corrected):
{glossary}

Beat context: **{beat_id} -- {beat_label}** ({n_scenes} scenes)

Transcript snippets in this beat (one per cut clip, with asset classification, subject, and scene):

{snippets}

Task: identify proper-noun ASR errors (people names, organization names, place names, technical terms) that are NOT already in the glossary. These are mishearings that should be added to the glossary so future refreshes correct them automatically.

Focus on:
- Misspellings of proper nouns (variant spellings of names already in glossary, or new names mentioned)
- Org / abbreviation errors (e.g., 'fire' lowercase when 'FIRE' the org is intended)
- Place name variants

Do NOT include:
- Stylistic rewrites or paraphrases
- Common English words misheard as other common English words
- Punctuation / capitalization-only differences
- Anything already in the glossary

Confidence calibration:
- high  = unambiguous mishearing of a name already in the glossary, OR a clearly-named new person/org grounded by context (subject, chunk_subject, scene label)
- medium = plausible mishearing with reasonable context support
- low   = a guess; the corrected form is uncertain

Return ONLY a JSON object on a single line, no markdown:
{{"glossary_additions": [{{"category": "people|orgs|places|terms", "canonical": "...", "variants": ["..."], "p_id": "p_..."|null, "evidence_snippet": "...", "confidence": "high|medium|low", "rationale": "..."}}]}}

If you find no new errors return: {{"glossary_additions": []}}
"""

CONF_RANK = {"high": 3, "medium": 2, "low": 1}


def _find_claude_bin(override: Optional[str] = None) -> Optional[str]:
    """Find the claude CLI on Windows. Checks: --claude-bin, $CLAUDE_BIN env,
    PATH, then common Windows install locations."""
    if override:
        if Path(override).exists():
            return override
        found = shutil.which(override)
        if found:
            return found
    env_bin = os.environ.get("CLAUDE_BIN")
    if env_bin and Path(env_bin).exists():
        return env_bin
    for name in ("claude.cmd", "claude.exe", "claude"):
        found = shutil.which(name)
        if found:
            return found
    candidates = [
        Path(os.environ.get("APPDATA", "")) / "npm" / "claude.cmd",
        Path(os.environ.get("APPDATA", "")) / "npm" / "claude.ps1",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "claude" / "claude.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "anthropic" / "claude-code" / "claude.exe",
        Path("C:/Program Files/Anthropic/Claude/claude.exe"),
    ]
    for c in candidates:
        if c.exists():
            return str(c)
    return None


def _load_glossary_compact(p: Path) -> dict:
    g = json.loads(p.read_text(encoding="utf-8"))
    out = {}
    for cat in ("people", "orgs", "places", "terms"):
        out[cat] = []
        for e in (g.get(cat) or []):
            entry = {"canonical": e.get("canonical")}
            if e.get("p_id"):
                entry["p_id"] = e["p_id"]
            if e.get("abbreviation"):
                entry["abbreviation"] = e["abbreviation"]
            if e.get("expansion"):
                entry["expansion"] = e["expansion"]
            if e.get("variants"):
                entry["variants"] = e["variants"]
            out[cat].append(entry)
    return out


def _build_snippet_block(beat: dict, sidecar: dict):
    bid = beat["id"]
    anns = [a for a in sidecar.get("annotations", []) if a.get("beat") == bid]
    seen = set()
    snippets = []
    for a in anns:
        k = a.get("key") or {}
        key = (k.get("asset_id"), k.get("source_in_frames"), k.get("source_out_frames"), k.get("track"))
        if key in seen:
            continue
        seen.add(key)
        text = (a.get("transcript_text") or "").strip()
        if not text:
            continue
        asset = a.get("asset") or {}
        cls = asset.get("classifications") or {}
        subj = (a.get("subject") or {}).get("name")
        gem_sub = (a.get("chunk_subject") or "").strip()
        meta_parts = [a.get("clip_id") or "?", asset.get("filename") or "?", cls.get("type") or "--"]
        if subj:
            meta_parts.append("subject=" + subj)
        if gem_sub:
            meta_parts.append("about=\"" + gem_sub[:80] + "\"")
        meta = " | ".join(meta_parts)
        snippets.append("[" + (a.get("clip_id") or "?") + "] scene=" + str(a.get("scene_label") or a.get("scene")) + " * " + meta + "\n  text: " + text)
    return ("\n\n".join(snippets), len(snippets))


def _extract_json_block(text: str):
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


def review_beat(sidecar: dict, beat: dict, glossary_compact: dict, model: str,
                 dry_run: bool, claude_bin: Optional[str] = None) -> dict:
    bid = beat["id"]
    blabel = beat.get("label", "")
    n_scenes = len(beat.get("scenes", []))
    block, n_snippets = _build_snippet_block(beat, sidecar)
    if n_snippets == 0:
        return {"beat": bid, "skipped": True, "reason": "no transcript content"}

    prompt = PROMPT_TMPL.format(
        glossary=json.dumps(glossary_compact, ensure_ascii=False),
        beat_id=bid, beat_label=blabel, n_scenes=n_scenes, snippets=block,
    )

    if dry_run:
        return {"beat": bid, "dry_run": True, "n_snippets": n_snippets, "prompt_len": len(prompt)}

    # Match the canonical dataset/_scripts pattern: prompt via STDIN (not argv).
    # argv with 30K+ char prompts blows past Windows command-line limits silently.
    bin_path = claude_bin or "claude"
    cmd = [bin_path, "--print", "--model", model]
    try:
        use_shell = bin_path.lower().endswith((".cmd", ".bat", ".ps1"))
        proc = subprocess.run(
            cmd, input=prompt, capture_output=True, text=True,
            encoding="utf-8", timeout=300, check=False, shell=use_shell,
        )
    except FileNotFoundError:
        return {"beat": bid, "error": "claude command not found (pass --claude-bin <path>)"}
    except subprocess.TimeoutExpired:
        return {"beat": bid, "error": "claude --print timed out (>300s)"}

    if proc.returncode != 0:
        return {"beat": bid, "error": "claude --print exited " + str(proc.returncode),
                "stderr": (proc.stderr or "")[:500],
                "stdout_head": (proc.stdout or "")[:500]}

    parsed = _extract_json_block(proc.stdout)
    if not parsed:
        return {"beat": bid, "error": "no JSON in response", "raw_head": proc.stdout[:500]}

    return {
        "beat": bid,
        "n_snippets": n_snippets,
        "additions": parsed.get("glossary_additions", []),
        "raw_response_len": len(proc.stdout),
    }


def _load_people_pid_index(people_json_path: Optional[Path]) -> dict:
    """Build {canonical_name_lower: p_id} from dataset/people/people.json so that
    new glossary additions in the 'people' category can auto-backfill their p_id."""
    out = {}
    if not people_json_path or not people_json_path.exists():
        return out
    try:
        data = json.loads(people_json_path.read_text(encoding="utf-8"))
    except Exception:
        return out
    # The file may be a list, or a dict with people under various keys
    people = []
    if isinstance(data, list):
        people = data
    elif isinstance(data, dict):
        if isinstance(data.get("people"), list):
            people = data["people"]
        elif isinstance(data.get("entries"), list):
            people = data["entries"]
        elif all(isinstance(v, dict) for v in data.values()):
            people = list(data.values())
    for p in people:
        if not isinstance(p, dict):
            continue
        pid = p.get("p_id") or p.get("id")
        name = p.get("canonical_name") or p.get("name") or p.get("canonical")
        if pid and name:
            out[name.lower()] = pid
            # Also index aliases if present
            for alias in (p.get("aliases") or []):
                if isinstance(alias, str):
                    out.setdefault(alias.lower(), pid)
    return out


def merge_into_glossary(glossary_path: Path, all_additions: list, threshold: str,
                        people_json_path: Optional[Path] = None) -> dict:
    """Merge LLM-proposed additions into the glossary.

    Improvements over the first pass:
      - Cross-category dedup: if 'Grand Teton National Park' already exists as
        an org, don't create a duplicate in places — merge variants into the
        existing entry regardless of which category Sonnet suggested.
      - p_id backfill: new people entries get their p_id auto-populated from
        dataset/people/people.json when the canonical name matches.
    """
    min_rank = CONF_RANK.get(threshold, 3)
    glossary = json.loads(glossary_path.read_text(encoding="utf-8"))

    bak = glossary_path.with_suffix(glossary_path.suffix + ".bak_" + time.strftime("%Y%m%d_%H%M%S"))
    shutil.copy2(glossary_path, bak)

    # Cross-category index: canonical_lower -> (cat, entry)
    canon_index: dict = {}
    for cat in ("people", "orgs", "places", "terms"):
        for entry in glossary.get(cat, []):
            canonical = entry.get("canonical")
            if canonical:
                canon_index.setdefault(canonical.lower(), (cat, entry))

    # People p_id lookup
    pid_index = _load_people_pid_index(people_json_path)

    report = {
        "merged_into_existing": [],
        "merged_cross_category": [],
        "new_entries": [],
        "skipped_below_threshold": [],
        "skipped_no_variants": [],
        "p_ids_backfilled": [],
        "p_ids_unmatched": [],
        "backup": str(bak),
        "people_json_loaded": bool(pid_index),
    }

    for add in all_additions:
        cat = add.get("category")
        canonical = add.get("canonical")
        variants = add.get("variants") or []
        conf = add.get("confidence", "low")
        if cat not in ("people", "orgs", "places", "terms"):
            continue
        if not canonical:
            continue
        if CONF_RANK.get(conf, 0) < min_rank:
            report["skipped_below_threshold"].append({"canonical": canonical, "confidence": conf})
            continue
        if not variants:
            report["skipped_no_variants"].append({"canonical": canonical})
            continue

        # Cross-category lookup
        hit = canon_index.get(canonical.lower())
        if hit:
            existing_cat, existing = hit
            existing_lower = {v.lower() for v in (existing.get("variants") or [])}
            added = []
            for v in variants:
                if v.lower() not in existing_lower:
                    existing.setdefault("variants", []).append(v)
                    existing_lower.add(v.lower())
                    added.append(v)
            if added:
                if existing_cat == cat:
                    report["merged_into_existing"].append({"canonical": canonical, "variants_added": added})
                else:
                    report["merged_cross_category"].append({
                        "canonical": canonical, "proposed_cat": cat, "existing_cat": existing_cat,
                        "variants_added": added,
                    })
        else:
            new_entry = {"canonical": canonical, "variants": variants}
            # p_id: prefer Sonnet's suggestion, else lookup from people.json (people only)
            pid = add.get("p_id")
            if not pid and cat == "people":
                pid = pid_index.get(canonical.lower())
                if pid:
                    report["p_ids_backfilled"].append({"canonical": canonical, "p_id": pid})
                else:
                    report["p_ids_unmatched"].append({"canonical": canonical})
            if pid:
                new_entry["p_id"] = pid
            glossary.setdefault(cat, []).append(new_entry)
            canon_index[canonical.lower()] = (cat, new_entry)
            report["new_entries"].append({"canonical": canonical, "category": cat, "variants": variants, "p_id": pid})

    glossary_path.write_text(json.dumps(glossary, indent=2, ensure_ascii=False), encoding="utf-8")
    return report


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--editor-root", default=None)
    ap.add_argument("--sidecar", default=None)
    ap.add_argument("--glossary", default=None)
    ap.add_argument("--out", default=None)
    ap.add_argument("--beats", default=None, help="comma-separated beat ids to limit to")
    ap.add_argument("--model", default="claude-sonnet-4-6")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--auto-merge", action="store_true",
                    help="merge approved suggestions into the glossary in-place")
    ap.add_argument("--confidence-threshold", default="medium", choices=["high", "medium", "low"],
                    help="minimum confidence for auto-merge (default: medium)")
    ap.add_argument("--claude-bin", default=None,
                    help="explicit path to claude CLI (auto-detected from PATH + common Windows install locations if not set)")
    args = ap.parse_args()

    claude_bin = _find_claude_bin(args.claude_bin)
    if not claude_bin and not args.dry_run:
        print("ERROR: could not find 'claude' CLI. Try one of:", file=sys.stderr)
        print("  1. Add it to PATH (PowerShell: where.exe claude)", file=sys.stderr)
        print("  2. Pass --claude-bin C:\\full\\path\\to\\claude.cmd", file=sys.stderr)
        print("  3. Set $env:CLAUDE_BIN before running", file=sys.stderr)
        print("Locate it via PowerShell:", file=sys.stderr)
        print("  Get-ChildItem -Path $env:APPDATA -Recurse -Filter 'claude*' -ErrorAction SilentlyContinue", file=sys.stderr)
        print("  Get-ChildItem -Path $env:LOCALAPPDATA -Recurse -Filter 'claude*' -ErrorAction SilentlyContinue", file=sys.stderr)
        return 2

    editor_root = (Path(args.editor_root).resolve() if args.editor_root
                   else Path(__file__).resolve().parent.parent.parent)
    scripts = editor_root / "story" / "_sidecar scripts"
    glossary_path = Path(args.glossary) if args.glossary else (scripts / "_project_glossary.json")
    sidecar_path = Path(args.sidecar) if args.sidecar else (editor_root / "story" / "sidecars" / "actII.sidecar.json")
    out_path = Path(args.out) if args.out else (scripts / "_glossary_suggestions.json")

    print("sidecar:        " + str(sidecar_path))
    print("glossary:       " + str(glossary_path))
    print("out:            " + str(out_path))
    print("model:          " + args.model + "   dry-run: " + str(args.dry_run))
    print("auto-merge:     " + str(args.auto_merge) + "   threshold: " + args.confidence_threshold)
    print("claude:         " + (claude_bin or "(dry-run, not resolved)"))
    print()

    sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
    glossary_compact = _load_glossary_compact(glossary_path)

    beats = sidecar.get("beats", [])
    if args.beats:
        wanted = set(args.beats.split(","))
        beats = [b for b in beats if b.get("id") in wanted]

    started = time.time()
    results = []
    all_additions = []
    for beat in beats:
        print("reviewing " + beat["id"] + " (" + beat.get("label", "") + ")...")
        r = review_beat(sidecar, beat, glossary_compact, args.model, args.dry_run, claude_bin)
        results.append(r)
        if r.get("error"):
            print("  ERROR: " + r["error"])
        elif r.get("skipped"):
            print("  skipped: " + r["reason"])
        elif r.get("dry_run"):
            print("  dry-run: " + str(r["n_snippets"]) + " snippets, prompt=" + str(r["prompt_len"]) + " chars")
        else:
            adds = r.get("additions", [])
            all_additions.extend(adds)
            by_conf = {"high": 0, "medium": 0, "low": 0}
            for a in adds:
                by_conf[a.get("confidence", "low")] = by_conf.get(a.get("confidence", "low"), 0) + 1
            print("  " + str(r["n_snippets"]) + " snippets, " + str(len(adds)) +
                  " proposed (high=" + str(by_conf["high"]) +
                  " medium=" + str(by_conf["medium"]) + " low=" + str(by_conf["low"]) + ")")

    out_payload = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "model": args.model,
        "sidecar": str(sidecar_path),
        "results": results,
    }
    if args.auto_merge and not args.dry_run and all_additions:
        people_json_path = editor_root.parent / "dataset" / "people" / "people.json"
        report = merge_into_glossary(glossary_path, all_additions, args.confidence_threshold,
                                     people_json_path=people_json_path)
        out_payload["merge_report"] = report
        print()
        print("MERGE into glossary (threshold=" + args.confidence_threshold + "):")
        print("  backup written:           " + report["backup"])
        print("  people.json loaded:       " + str(report.get("people_json_loaded")))
        print("  merged into existing:     " + str(len(report["merged_into_existing"])) + " entries")
        for m in report["merged_into_existing"][:10]:
            print("    + " + m["canonical"] + ": " + ", ".join(m["variants_added"]))
        print("  merged cross-category:    " + str(len(report.get("merged_cross_category", []))) + " entries")
        for m in report.get("merged_cross_category", [])[:10]:
            print("    + " + m["canonical"] + " (proposed=" + m["proposed_cat"] + ", existing=" + m["existing_cat"] + "): " + ", ".join(m["variants_added"]))
        print("  new entries appended:     " + str(len(report["new_entries"])))
        for n in report["new_entries"][:10]:
            pid_note = (" [p_id=" + n["p_id"] + "]") if n.get("p_id") else ""
            print("    + [" + n["category"] + "] " + n["canonical"] + pid_note + ": " + ", ".join(n["variants"]))
        print("  p_ids backfilled:         " + str(len(report.get("p_ids_backfilled", []))))
        for pp in report.get("p_ids_backfilled", [])[:10]:
            print("    + " + pp["canonical"] + " -> " + pp["p_id"])
        print("  p_ids unmatched (people): " + str(len(report.get("p_ids_unmatched", []))))
        for pp in report.get("p_ids_unmatched", [])[:10]:
            print("    ? " + pp["canonical"] + "  (not in dataset/people/people.json)")
        print("  skipped below threshold:  " + str(len(report["skipped_below_threshold"])))
        print("  skipped no variants:      " + str(len(report["skipped_no_variants"])))
    elif args.auto_merge:
        print("\n(auto-merge requested but no additions or dry-run -- glossary untouched)")

    out_path.write_text(json.dumps(out_payload, indent=2, ensure_ascii=False), encoding="utf-8")
    print()
    print("Suggestions written to: " + str(out_path))
    print("Elapsed: " + str(round(time.time() - started, 1)) + "s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
