#!/usr/bin/env python3
"""
Read the review results WITHOUT the servers running and print / copy what was approved.
=====================================================================================

Two subcommands, both stdlib-only and read-only unless --copy-to is given:

  stills  -> the hand-off list from the stills app (same rules as GET /api/redo_queue):
             python tools/export_approved.py stills --stills DIR [--round FILE]
             prints {"queue":[{clip, select, still, path}], "removed_skipped", "rerun", "unpicked"}
             REMOVED RULE: clips with "removed": true are never in "queue".

  videos  -> approved (source, generated) pairs from the video app:
             python tools/export_approved.py videos --out DIR [--targets DIR] [--sources DIR]
                    [--selects FILE] [--clip-id-regex RE]
                    [--copy-to DATASET_DIR --control-suffix _control --target-suffix _target]
             prints {"approved":[{clip, stem, source, target}], "rejected", "stale", "undecided"}
             STALE RULE: a decision older than the target file's mtime (clip re-rendered since) is
             treated as undecided, exactly like the review page does.
             --copy-to copies each approved pair as <id><control-suffix>.mp4 / <id><target-suffix>.mp4
             (e.g. --control-suffix _start --target-suffix _end for fal's LTX IC-LoRA trainer layout)
             and writes manifest.json there. Existing files are not overwritten unless --force.

Typical assistant use: run `videos --out ...` after the user says they're done reviewing, show
the counts, then build the training dataset from "approved" (captions are up to your pipeline).
"""
import argparse
import datetime
import json
import os
import re
import shutil
import sys

DEFAULT_CLIP_ID_REGEX = r"^([A-Za-z][A-Za-z0-9]*_\d{3})"


def read_json(p, default):
    try:
        with open(p, "r", encoding="utf-8") as fh:
            d = json.load(fh)
        return d if isinstance(d, type(default)) else default
    except (OSError, ValueError):
        return default


def ls(d):
    try:
        return sorted(os.listdir(d))
    except OSError:
        return []


def cmd_stills(a):
    stills = os.path.abspath(a.stills)
    rnd = read_json(a.round or os.path.join(stills, "round.json"), {})
    sel = read_json(os.path.join(stills, "selects.json"), {})
    order = rnd.get("clips") if isinstance(rnd.get("clips"), list) and rnd.get("clips") else \
        [n for n in ls(stills) if os.path.isdir(os.path.join(stills, n))]
    out = {"round": str(rnd.get("round") or ""), "queue": [], "removed_skipped": [], "rerun": [], "unpicked": []}
    for c in order:
        s = sel.get(c) if isinstance(sel.get(c), dict) else {}
        if s.get("removed") is True:
            out["removed_skipped"].append(c)
            continue
        if s.get("rerun"):
            out["rerun"].append(c)
            continue
        n = s.get("select")
        f = f"{c}_s{n:02d}.png" if isinstance(n, int) and not isinstance(n, bool) else None
        if not f or not os.path.isfile(os.path.join(stills, c, f)):
            out["unpicked"].append(c)
            continue
        out["queue"].append({"clip": c, "select": n, "still": f, "path": os.path.join(stills, c, f)})
    return out


def cmd_videos(a):
    out_dir = os.path.abspath(a.out)
    targets = os.path.abspath(a.targets or os.path.join(out_dir, "targets"))
    sources = os.path.abspath(a.sources or os.path.join(out_dir, "pairs"))
    sel = read_json(a.selects or os.path.join(out_dir, "video_selects.json"), {})
    rx = re.compile(a.clip_id_regex)
    res = {"approved": [], "rejected": [], "stale": [], "undecided": []}
    for name in ls(targets):
        if not name.lower().endswith(".mp4"):
            continue
        stem = name[:-4]
        src = os.path.join(sources, stem + "_src.mp4")
        if not os.path.isfile(src):
            continue  # incomplete pair, never reviewable
        m = rx.match(stem)
        cid = m.group(1) if m else stem
        tgt = os.path.join(targets, name)
        e = sel.get(cid) if isinstance(sel.get(cid), dict) else {}
        status = e.get("status")
        if status in ("approved", "reject"):
            try:
                t = datetime.datetime.fromisoformat(str(e.get("updated"))).timestamp()
            except (TypeError, ValueError):
                t = None
            if t is not None and t < int(os.stat(tgt).st_mtime):
                res["stale"].append(cid)
                continue
        item = {"clip": cid, "stem": stem, "source": src, "target": tgt, "note": e.get("note", "")}
        if status == "approved":
            res["approved"].append(item)
        elif status == "reject":
            res["rejected"].append(cid)
        else:
            res["undecided"].append(cid)
    if a.copy_to:
        dst = os.path.abspath(a.copy_to)
        os.makedirs(dst, exist_ok=True)
        manifest = []
        for it in res["approved"]:
            pair = {"clip": it["clip"],
                    "control": os.path.join(dst, it["clip"] + a.control_suffix + ".mp4"),
                    "target": os.path.join(dst, it["clip"] + a.target_suffix + ".mp4")}
            for k_src, k_dst in (("source", "control"), ("target", "target")):
                if os.path.exists(pair[k_dst]) and not a.force:
                    sys.exit(f"refusing to overwrite {pair[k_dst]} (use --force)")
                shutil.copy2(it[k_src], pair[k_dst])
            manifest.append(pair)
        with open(os.path.join(dst, "manifest.json"), "w", encoding="utf-8") as fh:
            json.dump({"pairs": manifest, "count": len(manifest)}, fh, indent=2)
            fh.write("\n")
        res["copied_to"] = dst
    res["counts"] = {k: len(v) for k, v in res.items() if isinstance(v, list)}
    return res


def main():
    ap = argparse.ArgumentParser(description="Export review results (read-only unless --copy-to).")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("stills", help="hand-off list from stills selects.json")
    s.add_argument("--stills", required=True)
    s.add_argument("--round")
    v = sub.add_parser("videos", help="approved pairs from video_selects.json")
    v.add_argument("--out", required=True)
    v.add_argument("--targets")
    v.add_argument("--sources")
    v.add_argument("--selects")
    v.add_argument("--clip-id-regex", default=DEFAULT_CLIP_ID_REGEX)
    v.add_argument("--copy-to")
    v.add_argument("--control-suffix", default="_control")
    v.add_argument("--target-suffix", default="_target")
    v.add_argument("--force", action="store_true")
    a = ap.parse_args()
    print(json.dumps(cmd_stills(a) if a.cmd == "stills" else cmd_videos(a), indent=2))


if __name__ == "__main__":
    main()
