#!/usr/bin/env python3
"""
Stills review / approval server  --  pick one generated still per source clip.
==============================================================================

WHAT IT IS
    A tiny, stdlib-only (no pip installs) local web app. For every source clip it shows the
    clip's original first frame next to N AI-generated candidate stills (e.g. Magnific /
    ComfyUI img2img outputs) in a before/after slider. The reviewer picks the best candidate,
    marks the clip for a Rerun (regenerate candidates), or removes the clip from the set.
    Decisions are written to selects.json, which the NEXT pipeline step (e.g. a video-to-video
    model such as LTX 2.3 ref video-to-video via fal that uses the chosen still as its style/look reference) reads.

WHEN TO USE IT
    LoRA / IC-LoRA dataset building where every training sample needs a human-approved
    "look" frame before an expensive video render is spent on it. Stage 1 of 2; the companion
    app ../video_review/ is stage 2 (approve the rendered video pairs).

INPUTS  (folder layout; nothing else is read)
    <stills>/                         --stills DIR
        <clip>/                       one folder per source clip, name = [A-Za-z0-9_-]{1,64}
            <clip>_original.png       the source frame ("Before")
            <clip>_s01.png            candidate 1   (two-digit index, 01..99)
            <clip>_s02.png ...        candidate 2 ...
        selects.json                  written by this app (created if missing)
        round.json                    OPTIONAL review-round filter (see below)
    A clip folder appears in the UI only once it has the _original.png AND at least one
    _sNN.png; otherwise it is listed as "waiting". The list is re-scanned every 30 s, so you
    can start the server while generations are still landing.

    round.json (optional, default <stills>/round.json, override --round FILE):
        {"round": "round-2",                       free-form label
         "clips": ["clip_002", "clip_007"],        only these clips are shown / queued
         "round_start_hidden": ["clip_002"]}       FIXED snapshot of clips that were already
                                                   decided before this round began; the page hides
                                                   ONLY these (picks made during the round stay
                                                   listed so the list never jumps around).
    Without round_start_hidden the page freezes the "already decided" set at page load.

OUTPUT  -- <stills>/selects.json  (atomic writes, one entry per clip that was ever touched)
    {
      "clip_001": {"select": 2,    "rerun": false, "updated": "2026-10-03T13:00:00-04:00"},
      "clip_002": {"select": null, "rerun": true,  "updated": "..."},          <- regenerate
      "clip_003": {"select": 4,    "rerun": false, "updated": "...", "removed": true}
    }
    select  : int 1..99 = chosen candidate <clip>_sNN.png, or null
    rerun   : true = candidates are not good enough, regenerate them (keeps old select)
    removed : present ONLY while true. REMOVED RULE: a removed clip must NEVER be sent to the
              next step, even if it still has a select. GET /api/redo_queue applies this.

HTTP API  (all JSON; bound to 127.0.0.1 by default, there is NO auth -- keep it local)
    GET  /                     -> index.html (the UI)
    GET  /api/clips            -> {"clips":[{clip, original, options:[{n,file}], removed}],
                                   "waiting":[...], "removed":[...], round?, round_clips?,
                                   round_start_hidden?, generated}
    GET  /img/<clip>/<file>    -> one image from a clip folder (path-safe, If-Modified-Since)
    GET  /api/selects          -> selects.json ({} if missing)
    POST /api/selects          -> {"clip":"clip_001","select":3,"rerun":false,"removed":false}
                                  or a map {"clip_001": {...}, "clip_002": {...}}; merged per clip
    GET  /api/redo_queue       -> canonical hand-off list for the next step (alias: /api/queue):
                                  {"queue":[{clip, select, still}], "removed_skipped":[...],
                                   "rerun":[...], "unpicked":[...], round, generated, rule}
                                  (/api/queue is an alias)

HOW TO RUN
    python stills_review/server.py --demo                      # sample data, http://127.0.0.1:8765/
    python stills_review/server.py --stills /path/to/stills    # real data
    python stills_review/server.py --config config.json        # see config.example.json
    Settings precedence: CLI flag > environment variable > config file > built-in default.
      env: STILLS_REVIEW_DIR, STILLS_REVIEW_ROUND, STILLS_REVIEW_HOST, STILLS_REVIEW_PORT

STATIC DEMO (no server, GitHub Pages)
    docs/stills/ = this app's index.html + docs/assets/demo-shim.js (DEMO-ONLY), rebuilt with
    python tools/build_pages.py. Any rule changed here must be mirrored in demo-shim.js.

HOW AN ASSISTANT SHOULD HAND THIS TO A USER
    1. Make sure the stills folder follows the layout above (rename generated files to
       <clip>_sNN.png, copy the source frame to <clip>_original.png).
    2. Start the server on the machine that holds the images (it must read them locally) and
       give the user the URL plus the shortcuts: 1-9/0 pick, R rerun, X remove from set,
       Up/Down next clip, N next undecided, P show picked, M slider/toggle.
    3. Never edit selects.json while the user is reviewing; read it (or /api/redo_queue) after.
    4. Only clips in "queue" go to the next step. Clips in "rerun" need new candidates
       (regenerate, then start a new round with round.json). Never send "removed_skipped".
"""
import argparse
import datetime
import json
import mimetypes
import os
import re
import shutil
import sys
import tempfile
import threading
import time
from email.utils import formatdate, parsedate_to_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlsplit

HERE = os.path.dirname(os.path.abspath(__file__))           # folder holding index.html
REPO = os.path.dirname(HERE)
SAMPLE_STILLS = os.path.join(REPO, "sample_data", "stills")  # committed placeholder data
DEMO_RUN = os.path.join(REPO, ".demo_run")                   # writable demo copy (gitignored)

# Clip folder and image file names allowed in URLs / folders (also blocks path traversal).
CLIP_RE = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")
FILE_RE = re.compile(r"^[A-Za-z0-9_\-\.]{1,128}\.(png|jpg|jpeg|webp)$", re.I)

# Runtime settings (filled in by main()).
STILLS = ""          # folder with one sub-folder per clip + selects.json
ROUND_FILE = ""      # optional round.json
SELECTS_LOCK = threading.Lock()


# ----------------------------------------------------------------------------- state files
def selects_path():
    return os.path.join(STILLS, "selects.json")


def now_iso():
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def read_round():
    """Optional review-round filter (see header). Returns None when absent/invalid/empty.
    It is a DISPLAY/queue filter only: selects.json is never modified by it."""
    try:
        with open(ROUND_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    lst = data.get("clips")
    if not isinstance(lst, list) or not lst:
        return None
    hid = data.get("round_start_hidden")
    return {"round": str(data.get("round") or ""), "clips": [c for c in lst if isinstance(c, str)],
            "round_start_hidden": [c for c in hid if isinstance(c, str)] if isinstance(hid, list) else None}


def read_selects():
    try:
        with open(selects_path(), "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_selects(data):
    """Atomic write: temp file in the same folder, fsync, then os.replace."""
    p = selects_path()
    fd, tmp = tempfile.mkstemp(prefix=".selects.", suffix=".tmp", dir=os.path.dirname(p))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        # os.replace can transiently fail on Windows while another process reads the file
        for attempt in range(20):
            try:
                os.replace(tmp, p)
                return
            except PermissionError:
                time.sleep(0.05 * (attempt + 1))
        os.replace(tmp, p)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


# ----------------------------------------------------------------------------- clip discovery
def scan_clips():
    """Re-scan the stills folder on every call (cheap; lets new generations show up live)."""
    clips, waiting = [], []
    rnd = read_round()
    sel = read_selects()
    only = set(rnd["clips"]) if rnd else None
    try:
        names = sorted(os.listdir(STILLS))
    except OSError:
        names = []
    for name in names:
        d = os.path.join(STILLS, name)
        if not CLIP_RE.match(name) or not os.path.isdir(d):
            continue
        if only is not None and name not in only:
            continue
        try:
            files = os.listdir(d)
        except OSError:
            continue
        orig = f"{name}_original.png"
        opt_re = re.compile(r"^" + re.escape(name) + r"_s(\d{2})\.png$")
        opts = []
        for f in files:
            m = opt_re.match(f)
            if m and os.path.isfile(os.path.join(d, f)):
                opts.append({"n": int(m.group(1)), "file": f})
        opts.sort(key=lambda o: o["n"])
        if orig in files and opts:
            clips.append({"clip": name, "original": orig, "options": opts,
                          "removed": is_removed(sel.get(name))})
        else:
            waiting.append(name)   # folder exists but original or candidates not there yet
    out = {"clips": clips, "waiting": waiting, "generated": now_iso(),
           "removed": [c["clip"] for c in clips if c["removed"]]}
    if rnd:
        out["round"] = rnd["round"]
        out["round_clips"] = rnd["clips"]
        if rnd.get("round_start_hidden") is not None:
            out["round_start_hidden"] = rnd["round_start_hidden"]
    return out


# ----------------------------------------------------------------------------- validation
def clean_entry(v):
    """Validate one posted entry. "removed" is None when the client did not send it
    (older page); the POST handler then keeps whatever flag is already saved."""
    if not isinstance(v, dict):
        raise ValueError("entry must be an object")
    sel = v.get("select")
    if sel is not None:
        if isinstance(sel, bool) or not isinstance(sel, int) or not (1 <= sel <= 99):
            raise ValueError("select must be an integer 1..99 or null")
    rerun = v.get("rerun", False)
    if not isinstance(rerun, bool):
        raise ValueError("rerun must be boolean")
    removed = v.get("removed", None)
    if removed is not None and not isinstance(removed, bool):
        raise ValueError("removed must be boolean")
    return {"select": sel, "rerun": rerun, "updated": now_iso(), "removed": removed}


def finalize_entry(e, prev):
    """Resolve the removed flag against the saved entry; store the key only while true."""
    removed = e.pop("removed", None)
    if removed is None:
        removed = bool(isinstance(prev, dict) and prev.get("removed") is True)
    if removed:
        e["removed"] = True
    return e


def is_removed(entry):
    return isinstance(entry, dict) and entry.get("removed") is True


# ----------------------------------------------------------------------------- hand-off list
VIDEO_BACKEND = {"backend": "ltx23_quality_ref_v2v", "endpoint": "fal-ai/ltx-2.3-quality/reference-video-to-video", "name": "LTX 2.3 ref video-to-video via fal"}


def redo_queue():
    """Canonical list of picks for the next step. Removed clips are ALWAYS skipped.
    queue = (round) clips that have a valid select, are not rerun and are not removed."""
    rnd = read_round()
    with SELECTS_LOCK:
        sel = read_selects()
    ready = {c["clip"]: c for c in scan_clips()["clips"]}
    order = rnd["clips"] if rnd else sorted(ready)
    queue, removed, rerun, unpicked = [], [], [], []
    for c in order:
        s = sel.get(c) if isinstance(sel.get(c), dict) else {}
        if is_removed(s):
            removed.append(c)
            continue
        if s.get("rerun"):
            rerun.append(c)
            continue
        n = s.get("select")
        opts = {o["n"]: o["file"] for o in ready.get(c, {}).get("options", [])}
        if not isinstance(n, int) or n not in opts:
            unpicked.append(c)
            continue
        queue.append({"clip": c, "select": n, "still": opts[n]})
    return {"round": rnd["round"] if rnd else "", "queue": queue, "removed_skipped": removed,
            "rerun": rerun, "unpicked": unpicked, "generated": now_iso(),
            "rule": "removed clips are never sent to the next step",
            "video_backend": VIDEO_BACKEND}




# ----------------------------------------------------------------------------- HTTP handler
class Handler(BaseHTTPRequestHandler):
    server_version = "StillsReview/1.0"

    def log_message(self, fmt, *args):  # quiet; errors still surface as HTTP codes
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        path = unquote(urlsplit(self.path).path)
        try:
            if path in ("/", "/index.html"):
                with open(os.path.join(HERE, "index.html"), "rb") as fh:
                    return self._send(200, fh.read(), "text/html; charset=utf-8",
                                      {"Cache-Control": "no-cache"})
            if path == "/api/clips":
                return self._send(200, scan_clips(), extra={"Cache-Control": "no-store"})
            if path == "/api/selects":
                with SELECTS_LOCK:
                    data = read_selects()
                return self._send(200, data, extra={"Cache-Control": "no-store"})
            if path in ("/api/redo_queue", "/api/queue"):
                return self._send(200, redo_queue(), extra={"Cache-Control": "no-store"})
            if path.startswith("/img/"):
                return self._img(path[5:])
            return self._send(404, {"error": "not found"})
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _img(self, rest):
        """Serve <stills>/<clip>/<file>; both parts regex-checked and realpath-confined."""
        parts = rest.split("/")
        if len(parts) != 2:
            return self._send(400, {"error": "bad path"})
        clip, fname = parts
        if not CLIP_RE.match(clip) or not FILE_RE.match(fname) or ".." in fname:
            return self._send(400, {"error": "bad path"})
        base = os.path.realpath(os.path.join(STILLS, clip))
        full = os.path.realpath(os.path.join(base, fname))
        if os.path.dirname(full) != base or not os.path.isfile(full):
            return self._send(404, {"error": "not found"})
        st = os.stat(full)
        lm = formatdate(st.st_mtime, usegmt=True)
        ims = self.headers.get("If-Modified-Since")
        if ims:
            try:
                if int(parsedate_to_datetime(ims).timestamp()) >= int(st.st_mtime):
                    self.send_response(304)
                    self.send_header("Last-Modified", lm)
                    self.end_headers()
                    return
            except (TypeError, ValueError):
                pass
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        with open(full, "rb") as fh:
            data = fh.read()
        return self._send(200, data, ctype, {"Last-Modified": lm, "Cache-Control": "no-cache"})

    def do_POST(self):
        path = urlsplit(self.path).path
        if path != "/api/selects":
            return self._send(404, {"error": "not found"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n > 1_000_000:
                return self._send(413, {"error": "too large"})
            payload = json.loads(self.rfile.read(n).decode("utf-8") or "{}")
            if not isinstance(payload, dict):
                raise ValueError("body must be an object")
            # Accept {"clip": "clip_001", "select": 4, "rerun": false, "removed": false}
            # or a map {"clip_001": {...}, "clip_002": {...}} (merged per clip).
            updates = {payload["clip"]: payload} if "clip" in payload else payload
            cleaned = {}
            for clip, v in updates.items():
                if not isinstance(clip, str) or not CLIP_RE.match(clip):
                    raise ValueError(f"bad clip name: {clip!r}")
                cleaned[clip] = clean_entry(v)
            with SELECTS_LOCK:
                data = read_selects()
                for clip, e in cleaned.items():
                    finalize_entry(e, data.get(clip))
                data.update(cleaned)
                write_selects(data)
            return self._send(200, {"ok": True, "saved": cleaned, "selects": data})
        except (ValueError, KeyError) as e:
            return self._send(400, {"ok": False, "error": str(e)})
        except OSError as e:
            return self._send(500, {"ok": False, "error": str(e)})


# ----------------------------------------------------------------------------- settings / demo
def load_config(path):
    """Read the "stills_review" section of a JSON config file (or a flat object)."""
    if not path:
        return {}
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise SystemExit(f"config {path}: expected a JSON object")
    sect = data.get("stills_review", data)
    return sect if isinstance(sect, dict) else {}


def prepare_demo(reset):
    """Copy the committed placeholder stills to .demo_run/stills (writable, gitignored), so a
    demo session never modifies sample_data/. --demo-reset restores the pristine copy."""
    dst = os.path.join(DEMO_RUN, "stills")
    if reset and os.path.isdir(dst):
        shutil.rmtree(dst)
    if not os.path.isdir(dst):
        shutil.copytree(SAMPLE_STILLS, dst)
    return dst


def main(argv=None):
    global STILLS, ROUND_FILE
    ap = argparse.ArgumentParser(description="Stills review / approval server (pick one still per clip).")
    ap.add_argument("--config", help="JSON config file (uses its \"stills_review\" section)")
    ap.add_argument("--stills", help="stills folder: <clip>/<clip>_original.png + <clip>_sNN.png")
    ap.add_argument("--round", dest="round_file", help="optional round.json (default <stills>/round.json)")
    ap.add_argument("--host", help="bind address (default 127.0.0.1; there is no auth)")
    ap.add_argument("--port", type=int, help="port (default 8765)")
    ap.add_argument("--demo", action="store_true", help="serve a writable copy of sample_data/stills")
    ap.add_argument("--demo-reset", action="store_true", help="with --demo: restore the pristine sample copy")
    a = ap.parse_args(argv)
    cfg = load_config(a.config)

    def pick(cli, env, key, default=None):
        if cli is not None:
            return cli
        if os.environ.get(env):
            return os.environ[env]
        return cfg[key] if cfg.get(key) is not None else default   # JSON null = use default

    STILLS = prepare_demo(a.demo_reset) if a.demo else pick(a.stills, "STILLS_REVIEW_DIR", "stills")
    if not STILLS:
        ap.error("no stills folder: pass --stills DIR, set STILLS_REVIEW_DIR, use --config, or --demo")
    STILLS = os.path.abspath(os.path.expanduser(STILLS))
    if not os.path.isdir(STILLS):
        ap.error(f"stills folder does not exist: {STILLS}")
    ROUND_FILE = os.path.abspath(os.path.expanduser(
        pick(a.round_file, "STILLS_REVIEW_ROUND", "round", os.path.join(STILLS, "round.json"))))
    host = pick(a.host, "STILLS_REVIEW_HOST", "host", "127.0.0.1")
    port = int(pick(a.port, "STILLS_REVIEW_PORT", "port", 8765))
    if host not in ("127.0.0.1", "localhost", "::1"):
        print(f"WARNING: binding {host}; this app has no authentication.", file=sys.stderr)

    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    print(f"Stills review on http://{host}:{httpd.server_port}/  stills={STILLS}  round={ROUND_FILE}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
