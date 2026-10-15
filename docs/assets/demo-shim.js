/*
 * DEMO-ONLY  --  static "fake server" for the GitHub Pages demo (docs/).
 * =====================================================================
 * The real apps (stills_review/, video_review/) talk to a small Python server through
 * relative URLs: api/clips, api/selects, api/redo_queue (alias api/aleph_queue), api/review_round.
 * On GitHub Pages there is no server, so this script is loaded BEFORE the app's own <script>
 * and replaces window.fetch for those api/* URLs only:
 *   - clip lists come from ./manifest.json (written by tools/build_pages.py),
 *   - decisions are kept in localStorage (key "demo.<app>.state") instead of a JSON file,
 *   - the same validation / REMOVED / STALE rules as the Python servers are re-implemented
 *     here (mirror any rule change in stills_review/server.py or video_review/server.py),
 *   - a small banner offers "Export ... .json" (download, same format the server writes)
 *     and "Reset demo".
 * Images and videos are plain static files next to the page (img/, media/, still/).
 * Nothing here is used when the apps run with their Python servers.
 */
(() => {
  "use strict";
  const APP = document.currentScript.dataset.app;            // "stills" | "video"
  const KEY = `demo.${APP}.state`;
  const realFetch = window.fetch.bind(window);
  const manifestP = realFetch("manifest.json", { cache: "no-store" }).then((r) => r.json());

  const nowIso = () => new Date().toISOString().replace(/\.\d{3}Z$/, "Z");
  const json = (body, status = 200) =>
    new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
  function loadState(man) {
    try { const s = JSON.parse(localStorage.getItem(KEY)); if (s && s.selects) return s; } catch {}
    const s = { selects: JSON.parse(JSON.stringify(man.seed_selects || {})) };
    localStorage.setItem(KEY, JSON.stringify(s));
    return s;
  }
  const saveState = (s) => localStorage.setItem(KEY, JSON.stringify(s));

  // ------------------------------------------------------------------ stills app (port of stills_review/server.py)
  const CLIP_RE = /^[A-Za-z0-9_\-]{1,64}$/;
  const isRemoved = (e) => !!e && typeof e === "object" && e.removed === true;
  function stillsClips(man, st) {
    const rnd = man.round && Array.isArray(man.round.clips) && man.round.clips.length ? man.round : null;
    const only = rnd ? new Set(rnd.clips) : null;
    const clips = man.clips.filter((c) => !only || only.has(c.clip))
      .map((c) => Object.assign({}, c, { removed: isRemoved(st.selects[c.clip]) }));
    const out = { clips, waiting: man.waiting.filter((c) => !only || only.has(c)), generated: nowIso(),
                  removed: clips.filter((c) => c.removed).map((c) => c.clip) };
    if (rnd) { out.round = rnd.round || ""; out.round_clips = rnd.clips;
               if (Array.isArray(rnd.round_start_hidden)) out.round_start_hidden = rnd.round_start_hidden; }
    return out;
  }
  function cleanEntry(v) {
    if (!v || typeof v !== "object" || Array.isArray(v)) throw new Error("entry must be an object");
    const sel = v.select ?? null;
    if (sel !== null && !(Number.isInteger(sel) && sel >= 1 && sel <= 99)) throw new Error("select must be an integer 1..99 or null");
    const rerun = v.rerun ?? false;
    if (typeof rerun !== "boolean") throw new Error("rerun must be boolean");
    const removed = v.removed ?? null;
    if (removed !== null && typeof removed !== "boolean") throw new Error("removed must be boolean");
    return { select: sel, rerun, updated: nowIso(), removed };
  }
  function stillsQueue(man, st) {
    const c = stillsClips(man, st), ready = Object.fromEntries(c.clips.map((x) => [x.clip, x]));
    const order = c.round_clips || Object.keys(ready).sort();
    const q = { round: c.round || "", queue: [], removed_skipped: [], rerun: [], unpicked: [], generated: nowIso(),
                rule: "removed clips are never sent to the next step" };
    for (const clip of order) {
      const s = st.selects[clip] || {};
      if (isRemoved(s)) { q.removed_skipped.push(clip); continue; }
      if (s.rerun) { q.rerun.push(clip); continue; }
      const opt = ((ready[clip] || {}).options || []).find((o) => o.n === s.select);
      if (!opt) { q.unpicked.push(clip); continue; }
      q.queue.push({ clip, select: s.select, still: opt.file });
    }
    return q;
  }
  function stillsApi(name, method, body, man, st) {
    if (name === "clips") return json(stillsClips(man, st));
    if (name === "redo_queue" || name === "queue" || name === "aleph_queue") return json(stillsQueue(man, st));
    if (name === "selects" && method === "GET") return json(st.selects);
    if (name === "selects" && method === "POST") {
      try {
        const payload = JSON.parse(body || "{}");
        const updates = "clip" in payload ? { [payload.clip]: payload } : payload;
        const cleaned = {};
        for (const [clip, v] of Object.entries(updates)) {
          if (!CLIP_RE.test(clip)) throw new Error(`bad clip name: ${clip}`);
          cleaned[clip] = cleanEntry(v);
        }
        for (const [clip, e] of Object.entries(cleaned)) {     // finalize_entry(): keep "removed" only while true
          let rm = e.removed; delete e.removed;
          if (rm === null) rm = isRemoved(st.selects[clip]);
          if (rm) e.removed = true;
          st.selects[clip] = e;
        }
        saveState(st);
        return json({ ok: true, saved: cleaned, selects: st.selects });
      } catch (err) { return json({ ok: false, error: String(err.message || err) }, 400); }
    }
    return json({ error: "not found" }, 404);
  }

  // ------------------------------------------------------------------ video app (port of video_review/server.py)
  function videoRound(man, st) {
    const hidden = [], shown = [], stale = [];
    for (const c of man.clips) {
      const e = st.selects[c.short] || {};
      if (e.status === "approved" || e.status === "reject") {
        const t = Date.parse(e.updated) / 1000;
        if (isNaN(t) || t >= c.target.mtime) { hidden.push(c.short); continue; }
        stale.push(c.short);                                   // decision older than the current render
      }
      shown.push(c.short);
    }
    return { generated: nowIso(), hidden, shown, stale,
             rule: "hide clips with an approved/reject decision saved at or after the current target render" };
  }
  function videoApi(name, method, body, man, st) {
    if (name === "clips") return json({ clips: man.clips, incomplete: man.incomplete, generated: nowIso() });
    if (name === "review_round") return json(videoRound(man, st));
    if (name === "selects" && method === "GET") return json(st.selects);
    if (name === "selects" && method === "POST") {
      let req; try { req = JSON.parse(body || ""); } catch { return json({ error: "bad json" }, 400); }
      const { clip, status = null, note = "" } = req || {};
      if (!man.clips.some((c) => c.short === clip) && !man.incomplete.some((c) => c.short === clip))
        return json({ error: "bad clip id" }, 400);
      if (!["approved", "reject", null].includes(status)) return json({ error: "bad status (approved | reject | null)" }, 400);
      if (typeof note !== "string" || note.length > 4000) return json({ error: "bad note" }, 400);
      st.selects[clip] = { status, note, updated: nowIso() };
      saveState(st);
      return json({ ok: true, clip, entry: st.selects[clip] });
    }
    return json({ error: "not found" }, 404);
  }

  // ------------------------------------------------------------------ fetch override (api/* only)
  window.fetch = async (input, init = {}) => {
    const url = typeof input === "string" ? input : input.url;
    const m = new URL(url, location.href).pathname.match(/\/api\/([a-z_]+)$/);
    if (!m) return realFetch(input, init);
    const man = await manifestP, st = loadState(man);
    const method = (init.method || "GET").toUpperCase();
    return (APP === "stills" ? stillsApi : videoApi)(m[1], method, init.body, man, st);
  };

  // ------------------------------------------------------------------ banner: export / reset / links
  function download(name, obj) {
    const a = document.createElement("a");
    a.href = URL.createObjectURL(new Blob([JSON.stringify(obj, null, 2) + "\n"], { type: "application/json" }));
    a.download = name; document.body.appendChild(a); a.click();
    setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 1000);
  }
  function banner() {
    const bar = document.createElement("div");
    bar.id = "demo-banner";
    bar.innerHTML = `<style>
      #demo-banner{position:fixed;left:10px;bottom:10px;z-index:100;display:flex;gap:6px;align-items:center;flex-wrap:wrap;
        max-width:calc(100vw - 20px);padding:6px 8px;border-radius:8px;background:rgba(20,22,26,.94);border:1px solid #3b4048;
        color:#cfd3da;font:12px/1.3 system-ui,sans-serif;box-shadow:0 2px 10px rgba(0,0,0,.5)}
      #demo-banner b{color:#ffcc66}#demo-banner button,#demo-banner a{font:inherit;color:#e6e6e6;background:#2a2d33;
        border:1px solid #444a53;border-radius:5px;padding:3px 8px;cursor:pointer;text-decoration:none}
      #demo-banner button:hover,#demo-banner a:hover{border-color:#6b7280}
      body{padding-bottom:56px}</style>
      <b>Static demo</b><span>placeholder data &middot; decisions stay in this browser</span>`;
    const btn = (label, fn) => { const b = document.createElement("button"); b.type = "button"; b.textContent = label;
      b.addEventListener("click", (e) => { fn(); e.currentTarget.blur(); }); bar.appendChild(b); };
    const link = (label, href) => { const a = document.createElement("a"); a.textContent = label; a.href = href; bar.appendChild(a); };
    manifestP.then((man) => {
      if (APP === "stills") {
        btn("Export selects.json", () => download("selects.json", loadState(man).selects));
        btn("Export queue.json", () => download("redo_queue.json", stillsQueue(man, loadState(man))));
      } else {
        btn("Export video_selects.json", () => download("video_selects.json", loadState(man).selects));
      }
      btn("Reset demo", () => {
        if (!confirm("Reset this demo to the sample state?")) return;
        for (const k of Object.keys(localStorage)) if (k.startsWith(`demo.${APP}.`) || k.startsWith(`${APP === "stills" ? "stills" : "video"}_review.`)) localStorage.removeItem(k);
        location.hash = ""; location.reload();
      });
      link(APP === "stills" ? "Video review demo \u2192" : "Stills review demo \u2192", APP === "stills" ? "../video/" : "../stills/");
      link("Home", "../");
    });
    document.body.appendChild(bar);
  }
  if (document.body) banner(); else document.addEventListener("DOMContentLoaded", banner);
})();
