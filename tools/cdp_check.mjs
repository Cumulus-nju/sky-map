// 用 CDP 打开页面，抓 console / 异常 / 失败请求，并截图。
// 用法: node cdp_check.mjs <file-or-url> [out.png] [waitMs]
import { spawn } from "node:child_process";
import { writeFileSync, mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const target = process.argv[2];
const outPng = process.argv[3] || join(tmpdir(), "cdp_shot.png");
const waitMs = Number(process.argv[4] || 12000);
const port = 9333;

const EDGE = "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe";
const profile = mkdtempSync(join(tmpdir(), "cdp-prof-"));

const child = spawn(EDGE, [
  "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
  `--remote-debugging-port=${port}`, `--user-data-dir=${profile}`,
  "--window-size=1600,1000", "--hide-scrollbars",
  "about:blank",
], { stdio: "ignore" });

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function getWsUrl() {
  for (let i = 0; i < 60; i++) {
    try {
      const r = await fetch(`http://127.0.0.1:${port}/json/version`);
      const j = await r.json();
      if (j.webSocketDebuggerUrl) return j.webSocketDebuggerUrl;
    } catch { /* not up yet */ }
    await sleep(300);
  }
  throw new Error("Edge CDP 未就绪");
}

const wsUrl = await getWsUrl();
const ws = new WebSocket(wsUrl);
await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej; });

let id = 0;
const pending = new Map();
const events = [];
ws.onmessage = (ev) => {
  const m = JSON.parse(ev.data);
  if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); }
  else if (m.method) events.push(m);
};

function send(method, params = {}, sessionId) {
  const mid = ++id;
  return new Promise((res) => {
    pending.set(mid, res);
    ws.send(JSON.stringify({ id: mid, method, params, ...(sessionId ? { sessionId } : {}) }));
  });
}

// 建一个 target（页面）并 attach
const { result: t } = await send("Target.createTarget", { url: "about:blank" });
const { result: att } = await send("Target.attachToTarget", { targetId: t.targetId, flatten: true });
const sid = att.sessionId;

await send("Runtime.enable", {}, sid);
await send("Log.enable", {}, sid);
await send("Network.enable", {}, sid);
await send("Page.enable", {}, sid);

const url = target.startsWith("http") ? target : "file:///" + target.replace(/\\/g, "/");
await send("Page.navigate", { url }, sid);
await sleep(waitMs);

const { result: shot } = await send("Page.captureScreenshot", { format: "png" }, sid);
if (shot?.data) writeFileSync(outPng, Buffer.from(shot.data, "base64"));

// 页面内省：瓦片与图层状态
const probe = await send("Runtime.evaluate", {
  returnByValue: true,
  expression: `(() => {
    const q = (s) => document.querySelector(s);
    const imgs = [...document.querySelectorAll('.leaflet-tile-pane img')];
    const loaded = imgs.filter(i => i.complete && i.naturalWidth > 0);
    let layers = null, online = null, onlineIn = null, ctr = null;
    try {
      if (typeof map !== 'undefined' && map) {
        layers = [];
        map.eachLayer(l => layers.push(l.constructor.name + (l._url ? ' url=' + String(l._url).slice(0,60) : '')));
        online = map._online ? 'exists' : 'none';
        onlineIn = (map._online && map.hasLayer(map._online)) ? 'on-map' : 'off-map';
        ctr = [map.getCenter().lat.toFixed(5), map.getCenter().lng.toFixed(5), map.getZoom()];
      }
    } catch (e) { layers = 'ERR ' + e.message; }
    return {
      leaflet: typeof L !== 'undefined' ? L.version : 'MISSING',
      mapElSize: q('#map') ? [q('#map').clientWidth, q('#map').clientHeight] : null,
      tileCount: imgs.length,
      tilesLoaded: loaded.length,
      allImgInMap: document.querySelectorAll('#map img').length,
      panes: [...document.querySelectorAll('.leaflet-pane')].map(p => p.className),
      layers, online, onlineIn, ctr,
      vectorInMap: (typeof vectorLayer !== 'undefined' && vectorLayer && map) ? (map.hasLayer(vectorLayer) ? 'on' : 'off') : 'n/a',
      vectorLayers: (typeof vectorLayer !== 'undefined' && vectorLayer) ? vectorLayer.getLayers().length : -1,
      /** 当前视口在矢量底图里是否有建筑 */
      firstBldName: (typeof DATA !== 'undefined' && DATA.buildings.gulou[0]) ? DATA.buildings.gulou[0].name : null,
      firstBldRings: (typeof DATA !== 'undefined' && DATA.buildings.gulou[0]) ? DATA.buildings.gulou[0].rings.length : null,
      markers: document.querySelectorAll('.leaflet-marker-icon').length,
      vectorPaths: document.querySelectorAll('path').length,
      pinHtml: q('.sp .pin') ? q('.sp .pin').outerHTML.slice(0,120) : null,
      /* 列表缩略图是否真的加载 */
      listImgs: [...document.querySelectorAll('#list .card img')].map(i => ({
        hasSrc: i.hasAttribute('src'), src: (i.getAttribute('src')||'').slice(0,60),
        complete: i.complete, nw: i.naturalWidth, rect: [Math.round(i.getBoundingClientRect().width), Math.round(i.getBoundingClientRect().height)]
      })).slice(0, 4),
      markerCover: (() => {
        const c = document.querySelector('.pinwrap .cover');
        return c ? { src: (c.getAttribute('src')||'').slice(0,60), complete: c.complete, nw: c.naturalWidth } : null;
      })(),
      firstShotSrcs: (typeof DATA !== 'undefined' && DATA.spots[0]) ? DATA.spots[0].shots.map(s=>s.src).slice(0,3) : null,
      spots: (typeof DATA !== 'undefined') ? DATA.spots.length : 'no DATA',
      bldCounts: (typeof DATA !== 'undefined') ? Object.fromEntries(Object.entries(DATA.buildings).map(([k,v])=>[k,v.length])) : null
    };
  })()`,
}, sid);
console.log("=== PROBE ===");
console.log(JSON.stringify(probe.result?.result?.value ?? probe.result, null, 2));

// 汇总
const lines = [];
for (const e of events) {
  if (e.method === "Runtime.consoleAPICalled") {
    const txt = (e.params.args || []).map((a) => a.value ?? a.description ?? a.type).join(" ");
    lines.push(`[console.${e.params.type}] ${txt}`);
  } else if (e.method === "Runtime.exceptionThrown") {
    const d = e.params.exceptionDetails;
    lines.push(`[EXCEPTION] ${d.text} ${d.exception?.description || ""}`);
  } else if (e.method === "Log.entryAdded") {
    lines.push(`[log.${e.params.entry.level}] ${e.params.entry.text}`);
  } else if (e.method === "Network.loadingFailed") {
    lines.push(`[netfail] ${e.params.errorText} ${e.params.type}`);
  } else if (e.method === "Network.responseReceived") {
    const r = e.params.response;
    if (r.status >= 400) lines.push(`[http ${r.status}] ${r.url.slice(0, 130)}`);
  }
}
writeFileSync(outPng.replace(/\.png$/, ".log.txt"), lines.join("\n"), "utf8");
console.log(lines.slice(0, 60).join("\n") || "(无 console 输出)");
console.log(`\n--- 截图: ${outPng}`);

ws.close();
child.kill();
process.exit(0);
