# AGENTS.md: instructions for AI assistants

This repo holds two human-approval web apps for LoRA / IC-LoRA dataset building. Read `README.md` for the full
details. Each source file starts with a header covering what it does, its inputs, its outputs and how to hand it to a user.

## Apps

| App | Start | URL | Writes |
|---|---|---|---|
| Stills review (pick 1 candidate still per clip) | `python stills_review/server.py --stills DIR` | `http://127.0.0.1:8765/` | `DIR/selects.json` |
| Video review (approve source→generated pairs) | `python video_review/server.py --out DIR [--stills DIR]` | `http://127.0.0.1:8766/` | `DIR/video_selects.json` |

Add `--demo` to either command for placeholder data. Run `python tests/smoke_test.py` to verify both apps (stdlib only).

## Workflow

1. Lay out the files (servers read local files only, on the machine that holds the media):
   `stills/<clip>/<clip>_original.png` + `<clip>_sNN.png`; `video_out/targets/<stem>.mp4` + `video_out/pairs/<stem>_src.mp4`
   (same frame count, fps and start frame). Video clip ids come from `--clip-id-regex` (default `^([A-Za-z][A-Za-z0-9]*_\d{3})`).
2. Start the server. Check that `GET /api/clips` lists the clips, then give the user the URL and shortcuts:
   * stills: `1-9/0` pick, `R` rerun, `X` remove from set, `↑↓` clip, `N` next undecided, `P` show picked, `M` mode
   * video: `Space` play, `,` `.` frame step, `1/2/3` mode, `←→` flip, `↑↓` clip, `N` next undecided, `P` show all
3. Do not edit the JSON files while the user is reviewing. Wait until they say they are done.
4. Read the results:
   * stills: `GET /api/aleph_queue` or `python tools/export_approved.py stills --stills DIR`.
     Only `queue` goes to the video model. Regenerate `rerun` clips, then start a new round with `round.json`.
   * video: `python tools/export_approved.py videos --out DIR [--copy-to DATASET --control-suffix _start --target-suffix _end]`.
     Train on `approved` only. Re-render `reject` clips (see `note`).
5. Report counts and paths. Do not paste whole JSON files unless asked.

## Rules you must keep

* **REMOVED rule:** a stills entry with `"removed": true` is never sent onward, even if it has a `select`.
* **STALE rule:** a video decision older than the target file's mtime (re-rendered clip) counts as undecided.
* The UIs save only on explicit user actions. Keep it that way if you edit them (see the comments in each `index.html`).
* No auth: bind `127.0.0.1` (the default). No personal paths or secrets in code. Use `config.json` (gitignored) or env vars.
* If you change a rule in a `server.py`, mirror it in `docs/assets/demo-shim.js` (static demo), then run
  `python tools/build_pages.py` and `python tests/smoke_test.py`.
