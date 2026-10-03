#!/usr/bin/env python3
"""
Before/after video review server  --  approve or reject (source clip, generated clip) pairs.
============================================================================================

WHAT IT IS
    A tiny, stdlib-only (no pip installs, no ffprobe) local web app at http://127.0.0.1:8766/.
    For every clip it plays the frame-aligned SOURCE video and the generated TARGET video
    (e.g. a Runway Aleph / ComfyUI video-to-video render) in sync: side-by-side, wipe, or
    toggle mode, with frame stepping. The reviewer clicks Approve or Reject (+ optional note).
    Approved pairs become (control -> target) training pairs for a LoRA / IC-LoRA.

WHEN TO USE IT
    Stage 2 of a LoRA dataset review, after the stills app (../stills_review/) chose the look
    frame for each clip and the video model rendered targets. Use it whenever a human must gate
    which generated clips enter a training set.

INPUTS  (folder layout)
    <out>/                            --out DIR (the render output root)
        targets/<stem>.mp4            generated clip            (override: --targets DIR)
        pairs/<stem>_src.mp4          frame-aligned source clip (override: --sources DIR)
        video_selects.json            written by this app       (override: --selects FILE)
        chosen_stills.json            OPTIONAL {"clip_001": 3}  (override: --chosen-stills FILE)
    <stills>/                         --stills DIR (optional; same folder as the stills app)
        <id>/<id>_sNN.png, selects.json
    <stem> is matched to the source by exact name; the clip ID ("short" id, used as the key in
    video_selects.json) is the first match of --clip-id-regex on the stem (default
    ^([A-Za-z][A-Za-z0-9]*_\\d{3}) so "clip_001_take2" -> "clip_001"). A clip with only one of
    the two videos is reported as "incomplete" and not shown. The still thumbnail shown under
    the player is chosen_stills.json[id] if present, else <stills>/selects.json[id].select.

OUTPUT  -- <out>/video_selects.json  (atomic writes)
    {
      "clip_001": {"status": "approved", "note": "",               "updated": "2026-10-03T13:05:00-04:00"},
      "clip_002": {"status": "reject",   "note": "flicker at f40", "updated": "..."},
      "clip_003": {"status": null,       "note": "",               "updated": "..."}   <- cleared
    }
    STALE RULE: a decision whose "updated" is OLDER than the current target file's mtime belongs
    to a previous render (the clip was re-rendered) and counts as undecided again.
    Approved pairs = status "approved" and not stale. tools/export_approved.py lists/copies them.

HTTP API  (JSON; bound to 127.0.0.1 by default, there is NO auth -- keep it local)
    GET  /                        -> index.html
    GET  /api/clips               -> {"clips":[{clip, short, source:{file,size,mtime,info},
                                      target:{...}, still}], "incomplete":[...], generated}
                                     info = {fps, frames, duration} parsed from the MP4 header
    GET  /media/targets/<file>    -> target mp4   (HTTP Range / 206 supported for seeking)
    GET  /media/pairs/<file>      -> source mp4
    GET  /still/<id>/<file>       -> chosen still thumbnail from <stills>/<id>/
    GET  /api/selects             -> video_selects.json ({} if missing)
    GET  /api/review_round        -> {hidden, shown, stale}: the page takes this snapshot ONCE at
                                     load; hidden = clips already decided for their current render
    POST /api/selects             -> {"clip": "<id>", "status": "approved"|"reject"|null, "note": ""}

HOW TO RUN
    python video_review/server.py --demo                          # sample data
    python video_review/server.py --out /path/to/out --stills /path/to/stills
    python video_review/server.py --config config.json            # "video_review" section
    Settings precedence: CLI flag > environment variable > config file > built-in default.
      env: VIDEO_REVIEW_OUT, VIDEO_REVIEW_STILLS, VIDEO_REVIEW_TARGETS, VIDEO_REVIEW_SOURCES,
           VIDEO_REVIEW_SELECTS, VIDEO_REVIEW_CHOSEN_STILLS, VIDEO_REVIEW_HOST, VIDEO_REVIEW_PORT,
           VIDEO_REVIEW_CLIP_ID_REGEX

STATIC DEMO (no server, GitHub Pages)
    docs/video/ = this app's index.html + docs/assets/demo-shim.js (DEMO-ONLY), rebuilt with
    python tools/build_pages.py. Any rule changed here must be mirrored in demo-shim.js.

HOW AN ASSISTANT SHOULD HAND THIS TO A USER
    1. Put/convert renders into targets/<stem>.mp4 and the matching source (same frame count,
       same fps, same start frame) into pairs/<stem>_src.mp4. The page warns on frame-count
       mismatch.
    2. Start the server on the machine holding the videos; give the user the URL and keys:
       Space play/pause, , . frame step, 1/2/3 mode, Left/Right flip (toggle mode),
       Up/Down next clip, N next undecided, P show all. Decisions save ONLY on button clicks.
    3. Afterwards read video_selects.json (or run tools/export_approved.py). Re-render the
       "reject" clips; once a new target is written its old decision becomes stale automatically.
"""
import argparse
import datetime
import json
import os
import re
import shutil
import struct
import sys
import tempfile
import threading
from email.utils import formatdate, parsedate_to_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlsplit

HERE = os.path.dirname(os.path.abspath(__file__))            # folder holding index.html
REPO = os.path.dirname(HERE)
SAMPLE = os.path.join(REPO, "sample_data")                    # committed placeholder data
DEMO_RUN = os.path.join(REPO, ".demo_run")                    # writable demo copy (gitignored)

VID_RE = re.compile(r"^[A-Za-z0-9_\-]{1,120}\.mp4$", re.I)
IMG_RE = re.compile(r"^[A-Za-z0-9_\-\.]{1,128}\.(png|jpg|jpeg|webp)$", re.I)
DEFAULT_CLIP_ID_REGEX = r"^([A-Za-z][A-Za-z0-9]*_\d{3})"
LOCK = threading.Lock()
VALID_STATUS = ("approved", "reject", None)

# Runtime settings (filled in by main()).
OUT = TARGETS = SOURCES = STILLS = SELECTS_FILE = CHOSEN_FILE = ""
CLIP_ID_RE = re.compile(DEFAULT_CLIP_ID_REGEX)


def now_iso():
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def clip_id(stem):
    """Short clip id (the video_selects.json key) for a file stem, or None."""
    m = CLIP_ID_RE.match(stem)
    return m.group(1) if m else None


def is_clip_id(s):
    return isinstance(s, str) and len(s) <= 64 and clip_id(s) == s


# ----------------------------------------------------------------------------- state files
def read_json(p, default):
    try:
        with open(p, "r", encoding="utf-8") as fh:
            d = json.load(fh)
        return d if isinstance(d, type(default)) else default
    except (OSError, ValueError):
        return default


def write_selects(data):
    """Atomic write: temp file next to the target, fsync, os.replace."""
    p = SELECTS_FILE
    fd, tmp = tempfile.mkstemp(prefix=".video_selects.", suffix=".tmp", dir=os.path.dirname(p))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, p)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ----------------------------------------------------------------------------- MP4 header parsing
# Minimal ISO-BMFF walk (moov/trak/mdia/{hdlr,mdhd,minf/stbl/stts}) to get fps and frame count
# of the first video track, so the page can frame-step accurately and flag frame-count
# mismatches between source and target without needing ffprobe on the machine.
def _boxes(f, start, end):
    pos = start
    while pos + 8 <= end:
        f.seek(pos)
        hdr = f.read(8)
        if len(hdr) < 8:
            return
        size, typ = struct.unpack(">I4s", hdr)
        hl = 8
        if size == 1:
            size = struct.unpack(">Q", f.read(8))[0]
            hl = 16
        elif size == 0:
            size = end - pos
        if size < hl:
            return
        yield typ.decode("latin-1"), pos + hl, pos + size
        pos += size


def _child(f, start, end, name):
    for t, s, e in _boxes(f, start, end):
        if t == name:
            return s, e
    return None


_INFO_CACHE = {}


def video_info(p):
    """{fps, frames, duration} of the first video track, or None. Cached by (path, mtime, size)."""
    try:
        st = os.stat(p)
    except OSError:
        return None
    key = (p, st.st_mtime, st.st_size)
    if key in _INFO_CACHE:
        return _INFO_CACHE[key]
    info = None
    try:
        with open(p, "rb") as f:
            moov = _child(f, 0, st.st_size, "moov")
            for t, s, e in (_boxes(f, *moov) if moov else []):
                if t != "trak":
                    continue
                mdia = _child(f, s, e, "mdia")
                if not mdia:
                    continue
                hdlr = _child(f, *mdia, "hdlr")
                f.seek(hdlr[0] + 8)
                if f.read(4) != b"vide":
                    continue
                mdhd = _child(f, *mdia, "mdhd")
                f.seek(mdhd[0])
                ver = f.read(1)[0]
                f.seek(mdhd[0] + (20 if ver == 1 else 12))
                ts = struct.unpack(">I", f.read(4))[0]
                minf = _child(f, *mdia, "minf")
                stbl = _child(f, *minf, "stbl")
                stts = _child(f, *stbl, "stts")
                f.seek(stts[0] + 4)
                n = struct.unpack(">I", f.read(4))[0]
                frames, dur, best = 0, 0, (0, 0)
                for _ in range(min(n, 100000)):
                    c, d = struct.unpack(">II", f.read(8))
                    frames += c
                    dur += c * d
                    if c > best[0]:
                        best = (c, d)
                fps = round(ts / best[1], 3) if best[1] else None
                info = {"fps": fps, "frames": frames, "duration": round(dur / ts, 3) if ts else None}
                break
    except (OSError, TypeError, IndexError, struct.error):
        info = None
    _INFO_CACHE[key] = info
    return info


# ----------------------------------------------------------------------------- clip discovery
def chosen_still(short):
    """Still shown under the player: chosen_stills.json wins, else the stills app's selects.json."""
    if not STILLS:
        return None
    m = read_json(CHOSEN_FILE, {}) if CHOSEN_FILE else {}
    n = m.get(short)
    if n is None:
        s = read_json(os.path.join(STILLS, "selects.json"), {}).get(short) or {}
        n = s.get("select") if isinstance(s, dict) else None
    try:
        n = int(n)
    except (TypeError, ValueError):
        return None
    f = f"{short}_s{n:02d}.png"
    return {"n": n, "file": f} if os.path.isfile(os.path.join(STILLS, short, f)) else None


def _ls(d):
    try:
        return sorted(os.listdir(d))
    except OSError:
        return []


def scan_clips():
    """Pair targets/<stem>.mp4 with pairs/<stem>_src.mp4; re-scanned on every call."""
    items = {}

    def add(kind, d, name, stem):
        p = os.path.join(d, name)
        try:
            st = os.stat(p)
        except OSError:
            return
        e = items.setdefault(stem, {"clip": stem})
        e[kind] = {"file": name, "size": st.st_size, "mtime": int(st.st_mtime), "info": video_info(p)}

    for name in _ls(TARGETS):
        if VID_RE.match(name):
            add("target", TARGETS, name, name[:-4])
    for name in _ls(SOURCES):
        if VID_RE.match(name) and name.lower().endswith("_src.mp4"):
            add("source", SOURCES, name, name[:-8])
    clips, waiting = [], []
    for stem in sorted(items):
        e = items[stem]
        sid = clip_id(stem)
        e["short"] = sid or stem
        e["still"] = chosen_still(sid) if sid else None
        (clips if ("target" in e and "source" in e) else waiting).append(e)
    return {"clips": clips, "incomplete": waiting, "generated": now_iso()}


def _decision_time(e):
    try:
        return datetime.datetime.fromisoformat(str(e.get("updated"))).timestamp()
    except (TypeError, ValueError):
        return None


def review_round():
    """Which clips the page hides this session (already decided for the CURRENT render).
    The page calls this once per load and never recomputes it, so the list stays stable."""
    data = scan_clips()
    sel = read_json(SELECTS_FILE, {})
    hidden, shown, stale = [], [], []
    for c in data["clips"]:
        e = sel.get(c["short"]) or {}
        if isinstance(e, dict) and e.get("status") in ("approved", "reject"):
            t = _decision_time(e)
            if t is None or t >= c["target"]["mtime"]:
                hidden.append(c["short"])
                continue
            stale.append(c["short"])  # decision belongs to an older render of this clip
        shown.append(c["short"])
    return {"generated": now_iso(), "hidden": hidden, "shown": shown, "stale": stale,
            "rule": "hide clips with an approved/reject decision saved at or after the current target render"}


# ----------------------------------------------------------------------------- HTTP handler
class H(BaseHTTPRequestHandler):
    server_version = "VideoReview/1.0"
    protocol_version = "HTTP/1.1"   # keep-alive + Range requests for smooth <video> seeking

    def log_message(self, fmt, *a):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        path = unquote(urlsplit(self.path).path)
        parts = [p for p in path.split("/") if p]
        try:
            if path in ("/", "/index.html"):
                with open(os.path.join(HERE, "index.html"), "rb") as fh:
                    return self._send(200, fh.read(), "text/html; charset=utf-8")
            if path == "/api/clips":
                return self._send(200, scan_clips())
            if path == "/api/selects":
                return self._send(200, read_json(SELECTS_FILE, {}))
            if path == "/api/review_round":
                return self._send(200, review_round())
            if len(parts) == 3 and parts[0] == "media" and parts[1] in ("targets", "pairs") and VID_RE.match(parts[2]):
                base = TARGETS if parts[1] == "targets" else SOURCES
                return self._file(os.path.join(base, parts[2]), "video/mp4")
            if len(parts) == 3 and parts[0] == "still" and STILLS and is_clip_id(parts[1]) and IMG_RE.match(parts[2]):
                ext = parts[2].rsplit(".", 1)[1].lower()
                ctype = "image/png" if ext == "png" else "image/" + ext.replace("jpg", "jpeg")
                return self._file(os.path.join(STILLS, parts[1], parts[2]), ctype)
            return self._send(404, {"error": "not found"})
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def _file(self, p, ctype):
        """Static file with ETag / If-Modified-Since and single-range (bytes=a-b) support."""
        try:
            st = os.stat(p)
        except OSError:
            return self._send(404, {"error": "not found"})
        size = st.st_size
        lm = formatdate(st.st_mtime, usegmt=True)
        etag = '"%x-%x"' % (int(st.st_mtime), size)
        ims = self.headers.get("If-Modified-Since")
        if self.headers.get("If-None-Match") == etag or (ims and not self.headers.get("Range") and _not_modified(ims, st.st_mtime)):
            self.send_response(304)
            self.send_header("ETag", etag)
            self.end_headers()
            return
        start, end, code = 0, size - 1, 200
        rng = self.headers.get("Range")
        if rng:
            m = re.match(r"^bytes=(\d*)-(\d*)$", rng.strip())
            if not m or (m.group(1) == "" and m.group(2) == ""):
                return self._send(416, b"", "text/plain", {"Content-Range": f"bytes */{size}"})
            if m.group(1) == "":
                n = int(m.group(2))
                start, end = max(0, size - n), size - 1
            else:
                start = int(m.group(1))
                end = min(int(m.group(2)), size - 1) if m.group(2) else size - 1
            if start >= size or start > end:
                return self._send(416, b"", "text/plain", {"Content-Range": f"bytes */{size}"})
            code = 206
        length = end - start + 1
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        self.send_header("Last-Modified", lm)
        self.send_header("ETag", etag)
        self.send_header("Cache-Control", "no-cache")
        if code == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if self.command == "HEAD":
            return
        with open(p, "rb") as fh:
            fh.seek(start)
            left = length
            while left > 0:
                chunk = fh.read(min(262144, left))
                if not chunk:
                    break
                self.wfile.write(chunk)
                left -= len(chunk)

    def do_POST(self):
        path = urlsplit(self.path).path
        if path != "/api/selects":
            return self._send(404, {"error": "not found"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0 or n > 65536:
                return self._send(400, {"error": "bad length"})
            req = json.loads(self.rfile.read(n).decode("utf-8"))
            if not isinstance(req, dict):
                return self._send(400, {"error": "body must be an object"})
            clip = req.get("clip")
            status = req.get("status")
            note = req.get("note", "")
            if not is_clip_id(clip):
                return self._send(400, {"error": "bad clip id (must fully match --clip-id-regex)"})
            if status not in VALID_STATUS:
                return self._send(400, {"error": "bad status (approved | reject | null)"})
            if not isinstance(note, str) or len(note) > 4000:
                return self._send(400, {"error": "bad note"})
            with LOCK:
                data = read_json(SELECTS_FILE, {})
                data[clip] = {"status": status, "note": note, "updated": now_iso()}
                write_selects(data)
            return self._send(200, {"ok": True, "clip": clip, "entry": data[clip]})
        except ValueError as e:
            return self._send(400, {"error": str(e)})
        except OSError as e:
            return self._send(500, {"error": str(e)})


def _not_modified(ims, mtime):
    try:
        return int(mtime) <= int(parsedate_to_datetime(ims).timestamp())
    except (TypeError, ValueError):
        return False


# ----------------------------------------------------------------------------- settings / demo
def load_config(path):
    """Read the "video_review" section of a JSON config file (or a flat object)."""
    if not path:
        return {}
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise SystemExit(f"config {path}: expected a JSON object")
    sect = data.get("video_review", data)
    return sect if isinstance(sect, dict) else {}


def prepare_demo(reset):
    """Copy sample_data/{video_out,stills} to .demo_run/ (writable, gitignored) and seed one
    decision so the 'hidden at load / P show all' behaviour is visible. Returns (out, stills)."""
    out, stills = os.path.join(DEMO_RUN, "video_out"), os.path.join(DEMO_RUN, "stills")
    if reset:
        for d in (out, stills):
            if os.path.isdir(d):
                shutil.rmtree(d)
    if not os.path.isdir(stills):
        shutil.copytree(os.path.join(SAMPLE, "stills"), stills)
    if not os.path.isdir(out):
        shutil.copytree(os.path.join(SAMPLE, "video_out"), out)
        seed = {"demo_002": {"status": "approved", "note": "seeded by --demo", "updated": now_iso()}}
        with open(os.path.join(out, "video_selects.json"), "w", encoding="utf-8") as fh:
            json.dump(seed, fh, indent=2)
            fh.write("\n")
    return out, stills


def main(argv=None):
    global OUT, TARGETS, SOURCES, STILLS, SELECTS_FILE, CHOSEN_FILE, CLIP_ID_RE
    ap = argparse.ArgumentParser(description="Before/after video review server (approve source vs generated pairs).")
    ap.add_argument("--config", help="JSON config file (uses its \"video_review\" section)")
    ap.add_argument("--out", help="output root containing targets/ and pairs/")
    ap.add_argument("--targets", help="generated clips folder (default <out>/targets)")
    ap.add_argument("--sources", help="source clips folder, files named <stem>_src.mp4 (default <out>/pairs)")
    ap.add_argument("--stills", help="stills folder from the stills app (optional, for the thumbnail)")
    ap.add_argument("--selects", help="decision file (default <out>/video_selects.json)")
    ap.add_argument("--chosen-stills", help="optional {id: n} override map (default <out>/chosen_stills.json)")
    ap.add_argument("--clip-id-regex", help=f"regex whose group 1 is the clip id (default {DEFAULT_CLIP_ID_REGEX})")
    ap.add_argument("--host", help="bind address (default 127.0.0.1; there is no auth)")
    ap.add_argument("--port", type=int, help="port (default 8766)")
    ap.add_argument("--demo", action="store_true", help="serve a writable copy of sample_data/")
    ap.add_argument("--demo-reset", action="store_true", help="with --demo: restore the pristine sample copy")
    a = ap.parse_args(argv)
    cfg = load_config(a.config)

    def pick(cli, env, key, default=None):
        if cli is not None:
            return cli
        if os.environ.get(env):
            return os.environ[env]
        return cfg[key] if cfg.get(key) is not None else default   # JSON null = use default

    def absp(p):
        return os.path.abspath(os.path.expanduser(p)) if p else ""

    if a.demo:
        OUT, STILLS = prepare_demo(a.demo_reset)
    else:
        OUT = absp(pick(a.out, "VIDEO_REVIEW_OUT", "out"))
        STILLS = absp(pick(a.stills, "VIDEO_REVIEW_STILLS", "stills"))
    if not OUT or not os.path.isdir(OUT):
        ap.error("output root missing: pass --out DIR (with targets/ and pairs/), set VIDEO_REVIEW_OUT, use --config, or --demo")
    TARGETS = absp(pick(a.targets, "VIDEO_REVIEW_TARGETS", "targets", os.path.join(OUT, "targets")))
    SOURCES = absp(pick(a.sources, "VIDEO_REVIEW_SOURCES", "sources", os.path.join(OUT, "pairs")))
    SELECTS_FILE = absp(pick(a.selects, "VIDEO_REVIEW_SELECTS", "selects", os.path.join(OUT, "video_selects.json")))
    CHOSEN_FILE = absp(pick(a.chosen_stills, "VIDEO_REVIEW_CHOSEN_STILLS", "chosen_stills",
                            os.path.join(OUT, "chosen_stills.json")))
    CLIP_ID_RE = re.compile(pick(a.clip_id_regex, "VIDEO_REVIEW_CLIP_ID_REGEX", "clip_id_regex", DEFAULT_CLIP_ID_REGEX))
    host = pick(a.host, "VIDEO_REVIEW_HOST", "host", "127.0.0.1")
    port = int(pick(a.port, "VIDEO_REVIEW_PORT", "port", 8766))
    if host not in ("127.0.0.1", "localhost", "::1"):
        print(f"WARNING: binding {host}; this app has no authentication.", file=sys.stderr)

    srv = ThreadingHTTPServer((host, port), H)
    srv.daemon_threads = True
    print(f"Video review on http://{host}:{srv.server_port}/  targets={TARGETS} sources={SOURCES} "
          f"stills={STILLS or '-'} selects={SELECTS_FILE}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
