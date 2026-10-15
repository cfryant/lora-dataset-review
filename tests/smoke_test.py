#!/usr/bin/env python3
"""
End-to-end smoke test for both apps (stdlib only; no browser needed).

    python tests/smoke_test.py

Copies sample_data/ to a temp dir, starts both servers on free ports with explicit
--stills / --out paths, exercises every page and API endpoint (including a POST round-trip,
the removed rule, HTTP Range for video, path-traversal rejection), checks the --demo and
--config start-up paths, then stops everything. Exit code 0 = all checks passed.
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable
results = []


def check(name, cond, extra=""):
    results.append(bool(cond))
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if extra else ""), flush=True)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def req(url, data=None, headers=None, method=None):
    r = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(r, timeout=10) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def getj(url):
    code, _, body = req(url)
    return code, json.loads(body)


def postj(url, obj):
    code, _, body = req(url, json.dumps(obj).encode(), {"Content-Type": "application/json"}, "POST")
    return code, json.loads(body)


def start(args, port):
    p = subprocess.Popen([PY, *args, "--port", str(port)], cwd=REPO,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    for _ in range(50):
        try:
            if req(f"http://127.0.0.1:{port}/")[0] == 200:
                return p
        except OSError:
            pass
        if p.poll() is not None:
            break
        time.sleep(0.1)
    p.kill()
    raise SystemExit(f"server failed to start: {args}\n{p.stdout.read()}")


def stop(p):
    p.terminate()
    try:
        p.wait(5)
    except subprocess.TimeoutExpired:
        p.kill()


def test_stills(stills):
    port = free_port()
    p = start(["stills_review/server.py", "--stills", stills], port)
    base = f"http://127.0.0.1:{port}"
    try:
        code, _, html = req(base + "/")
        check("stills: GET / serves the UI", code == 200 and b"Remove from set" in html)
        code, j = getj(base + "/api/clips")
        names = [c["clip"] for c in j["clips"]]
        check("stills: /api/clips lists 5 ready clips", code == 200 and len(names) == 5, names)
        check("stills: clip without candidates is 'waiting'", j["waiting"] == ["demo_006"], j["waiting"])
        check("stills: round_start_hidden exposed", j.get("round_start_hidden") == ["demo_001", "demo_005"])
        check("stills: removed list", j["removed"] == ["demo_005"], j["removed"])
        code, hdr, img = req(base + "/img/demo_002/demo_002_s01.png")
        check("stills: image served", code == 200 and img[:8] == b"\x89PNG\r\n\x1a\n" and hdr.get("Content-Type") == "image/png")
        check("stills: path traversal rejected", req(base + "/img/demo_002/..%2F..%2Fselects.json")[0] in (400, 404))
        code, q = getj(base + "/api/redo_queue")
        check("stills: queue before = [demo_001]", [x["clip"] for x in q["queue"]] == ["demo_001"], q["queue"])
        check("stills: removed clip skipped", q["removed_skipped"] == ["demo_005"])
        code, r = postj(base + "/api/selects", {"clip": "demo_002", "select": 3, "rerun": False})
        check("stills: POST pick saved", code == 200 and r["ok"] and r["selects"]["demo_002"]["select"] == 3)
        postj(base + "/api/selects", {"demo_003": {"select": None, "rerun": True}})
        postj(base + "/api/selects", {"clip": "demo_004", "select": 1, "rerun": False, "removed": True})
        code, q = getj(base + "/api/queue")
        check("stills: queue after (alias /api/queue)", [x["clip"] for x in q["queue"]] == ["demo_001", "demo_002"], q)
        check("stills: rerun bucket", q["rerun"] == ["demo_003"], q["rerun"])
        check("stills: removed-with-select never queued", "demo_004" in q["removed_skipped"])
        code, r = postj(base + "/api/selects", {"clip": "demo_002", "select": 500})
        check("stills: invalid select -> 400", code == 400)
        with open(os.path.join(stills, "selects.json")) as fh:
            disk = json.load(fh)
        check("stills: selects.json written atomically", disk["demo_002"]["select"] == 3 and disk["demo_004"].get("removed") is True)
        check("stills: removed key absent when not removed", "removed" not in disk["demo_002"])
    finally:
        stop(p)


def test_video(out, stills):
    port = free_port()
    p = start(["video_review/server.py", "--out", out, "--stills", stills], port)
    base = f"http://127.0.0.1:{port}"
    try:
        code, _, html = req(base + "/")
        check("video: GET / serves the UI", code == 200 and b"Approve" in html)
        code, j = getj(base + "/api/clips")
        names = [c["short"] for c in j["clips"]]
        check("video: 3 complete pairs", names == ["demo_001", "demo_002", "demo_004"], names)
        check("video: target without source is incomplete", [c["clip"] for c in j["incomplete"]] == ["demo_003"])
        c1 = j["clips"][0]
        check("video: MP4 header parsed (24 fps, 48 frames)",
              c1["source"]["info"] == {"fps": 24.0, "frames": 48, "duration": 2.0}, c1["source"]["info"])
        check("video: still from stills selects.json", c1["still"] == {"n": 2, "file": "demo_001_s02.png"}, c1["still"])
        check("video: still from chosen_stills.json override", j["clips"][1]["still"] == {"n": 3, "file": "demo_002_s03.png"})
        code, hdr, body = req(base + "/media/targets/demo_001.mp4", headers={"Range": "bytes=0-99"})
        check("video: Range request -> 206", code == 206 and len(body) == 100 and hdr.get("Content-Range", "").startswith("bytes 0-99/"))
        code, _, body = req(base + "/media/pairs/demo_001_src.mp4")
        check("video: full source served", code == 200 and body[4:8] == b"ftyp")
        code, _, body = req(base + "/still/demo_001/demo_001_s02.png")
        check("video: still thumbnail served", code == 200 and body[:4] == b"\x89PNG")
        check("video: bad media name -> 404", req(base + "/media/targets/..%2Fx.mp4")[0] == 404)
        code, rr = getj(base + "/api/review_round")
        check("video: review_round (nothing decided yet)", rr["hidden"] == [] and len(rr["shown"]) == 3, rr)
        code, r = postj(base + "/api/selects", {"clip": "demo_001", "status": "approved", "note": "ok"})
        check("video: POST approve", code == 200 and r["entry"]["status"] == "approved")
        postj(base + "/api/selects", {"clip": "demo_002", "status": "reject", "note": "flicker"})
        code, r = postj(base + "/api/selects", {"clip": "../etc", "status": "approved"})
        check("video: bad clip id -> 400", code == 400)
        code, r = postj(base + "/api/selects", {"clip": "demo_004", "status": "maybe"})
        check("video: bad status -> 400", code == 400)
        code, rr = getj(base + "/api/review_round")
        check("video: decided clips hidden next load", rr["hidden"] == ["demo_001", "demo_002"], rr)
        # Re-render demo_001: bump target mtime past the decision -> stale.
        tgt = os.path.join(out, "targets", "demo_001.mp4")
        future = time.time() + 120
        os.utime(tgt, (future, future))
        code, rr = getj(base + "/api/review_round")
        check("video: re-rendered clip's decision becomes stale", "demo_001" in rr["stale"] and "demo_001" in rr["shown"], rr)
        exp = subprocess.run([PY, "tools/export_approved.py", "videos", "--out", out], cwd=REPO,
                             capture_output=True, text=True)
        ej = json.loads(exp.stdout)
        check("export: stale approval not exported", ej["counts"]["approved"] == 0 and ej["stale"] == ["demo_001"], ej["counts"])
        os.utime(tgt, (time.time() - 3600, time.time() - 3600))
        ds = os.path.join(os.path.dirname(out), "dataset")
        exp = subprocess.run([PY, "tools/export_approved.py", "videos", "--out", out, "--copy-to", ds,
                              "--control-suffix", "_start", "--target-suffix", "_end"],
                             cwd=REPO, capture_output=True, text=True)
        ej = json.loads(exp.stdout)
        check("export: approved pair copied", ej["counts"]["approved"] == 1 and
              sorted(os.listdir(ds)) == ["demo_001_end.mp4", "demo_001_start.mp4", "manifest.json"], sorted(os.listdir(ds)))
        exp = subprocess.run([PY, "tools/export_approved.py", "stills", "--stills", stills], cwd=REPO,
                             capture_output=True, text=True)
        sj = json.loads(exp.stdout)
        check("export: stills queue matches API", [x["clip"] for x in sj["queue"]] == ["demo_001", "demo_002"], sj)
    finally:
        stop(p)


def test_startup_paths(tmp):
    # --demo (quick start) for both apps, then --config for both.
    for app, needle in (("stills_review/server.py", b"Remove from set"), ("video_review/server.py", b"Approve")):
        port = free_port()
        p = start([app, "--demo", "--demo-reset"], port)
        try:
            code, j = getj(f"http://127.0.0.1:{port}/api/clips")
            check(f"--demo: {app} serves sample clips", code == 200 and len(j["clips"]) >= 3)
        finally:
            stop(p)
    cfg = os.path.join(tmp, "config.json")
    with open(cfg, "w") as fh:
        json.dump({"stills_review": {"stills": os.path.join(tmp, "data", "stills")},
                   "video_review": {"out": os.path.join(tmp, "data", "video_out"),
                                    "stills": os.path.join(tmp, "data", "stills")}}, fh)
    for app in ("stills_review/server.py", "video_review/server.py"):
        port = free_port()
        p = start([app, "--config", cfg], port)
        try:
            code, j = getj(f"http://127.0.0.1:{port}/api/clips")
            check(f"--config: {app} starts", code == 200 and len(j["clips"]) >= 3)
        finally:
            stop(p)


def main():
    tmp = tempfile.mkdtemp(prefix="approval-sites-test-")
    try:
        data = os.path.join(tmp, "data")
        shutil.copytree(os.path.join(REPO, "sample_data"), data)
        test_stills(os.path.join(data, "stills"))
        test_video(os.path.join(data, "video_out"), os.path.join(data, "stills"))
        test_startup_paths(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n{sum(results)}/{len(results)} checks passed")
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()
