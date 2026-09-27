// 在页面里直接调用 showOnline() 并捕获异常，定位瓦片层为何没挂上。
import { spawn } from "node:child_process";
import { writeFileSync, mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const target = process.argv[2];
const port = 9334;
const EDGE = "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe";
const profile = mkdtempSync(join(tmpdir(), "cdp-prof2-"));
const child = spawn(EDGE, ["--headless=new", "--disable-gpu", "--no-first-run",
  `--remote-debugging-port=${port}`, `--user-data-dir=${profile}`,
  "--window-size=1600,1000", "about:blank"], { stdio: "ignore" });
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function wsUrl() {
  for (let i = 0; i < 60; i++) {
    try { const r = await fetch(`http://127.0.0.1:${port}/json/version`); const j = await r.json();
      if (j.webSocketDebuggerUrl) return j.webSocketDebuggerUrl; } catch {}
    await sleep(300);
  }
  throw new Error("no cdp");
}
const ws = new WebSocket(await wsUrl());
await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej; });
let id = 0; const pending = new Map();
ws.onmessage = (ev) => { const m = JSON.parse(ev.data); if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); } };
const send = (method, params = {}, sessionId) => new Promise((res) => {
  const mid = ++id; pending.set(mid, res);
  ws.send(JSON.stringify({ id: mid, method, params, ...(sessionId ? { sessionId } : {}) }));
});

const { result: t } = await send("Target.createTarget", { url: "about:blank" });
const { result: att } = await send("Target.attachToTarget", { targetId: t.targetId, flatten: true });
const sid = att.sessionId;
await send("Runtime.enable", {}, sid);
const url = "file:///" + target.replace(/\\/g, "/");
await send("Page.navigate", { url }, sid);
await sleep(9000);

const expr = `(() => {
  const out = {};
  try {
    out.beforeOnline = map._online ? 'exists' : 'none';
    out.tileLayerFn = typeof L.tileLayer;
    // 手动造一个瓦片层，看能不能挂上
    const probe = L.tileLayer('https://a.basemaps.cartocdn.com/light_all/{z}/{x}/{y}.png', {maxZoom:19});
    out.probeCreated = !!probe;
    out.probeUrl = probe._url;
    probe.addTo(map);
    out.probeAdded = map.hasLayer(probe);
    out.probeContainer = probe._container ? probe._container.className : 'no-container';
    out.probeTiles = probe._container ? probe._container.querySelectorAll('img').length : -1;
    // 真正的 showOnline
    try { showOnline(); out.showOnline = 'ok'; } catch (e) { out.showOnline = 'THREW: ' + e.message; }
    out.afterOnline = map._online ? 'exists' : 'none';
    out.onlineInMap = map._online ? map.hasLayer(map._online) : null;
    out.onlineContainer = (map._online && map._online._container) ? map._online._container.className : 'no-container';
    out.onlineTiles = (map._online && map._online._container) ? map._online._container.querySelectorAll('img').length : -1;
    out.onlineUrl = map._online ? map._online._url : null;
    out.currentCampus = currentCampus;
    out.cfgStreets = DATA.campuses[currentCampus === 'all' ? 'gulou' : currentCampus].streets;
    // 瓦片 pane 内容
    const tp = document.querySelector('.leaflet-tile-pane');
    out.tilePaneChildren = tp ? tp.childElementCount : -1;
    out.tilePaneHtml = tp ? tp.innerHTML.slice(0, 300) : null;
  } catch (e) { out.FATAL = e.message + ' | ' + e.stack.split('\\n')[1]; }
  return out;
})()`;

const r = await send("Runtime.evaluate", { expression: expr, returnByValue: true, awaitPromise: false }, sid);
console.log(JSON.stringify(r.result?.result?.value ?? r.result, null, 2));

ws.close(); child.kill(); process.exit(0);
