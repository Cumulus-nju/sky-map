// 切换底图后，检查每个 marker 的实际像素位置与可见性，定位"卫星模式下点位不显示"。
import { spawn } from "node:child_process";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const target = process.argv[2];
const btnId = process.argv[3] || "btnSat";
const port = 9339;
const EDGE = "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe";
const profile = mkdtempSync(join(tmpdir(), "cdp-mk-"));
const child = spawn(EDGE, ["--headless=new", "--disable-gpu", "--no-first-run",
  `--remote-debugging-port=${port}`, `--user-data-dir=${profile}`,
  "--window-size=1600,1000", "--hide-scrollbars", "about:blank"], { stdio: "ignore" });
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function wsUrl() {
  for (let i = 0; i < 60; i++) {
    try { const j = await (await fetch(`http://127.0.0.1:${port}/json/version`)).json();
      if (j.webSocketDebuggerUrl) return j.webSocketDebuggerUrl; } catch {}
    await sleep(300);
  }
  throw new Error("no cdp");
}
const ws = new WebSocket(await wsUrl());
await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej; });
let id = 0; const pending = new Map();
ws.onmessage = (ev) => { const m = JSON.parse(ev.data); if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); } };
const send = (method, params = {}, s) => new Promise((res) => {
  const mid = ++id; pending.set(mid, res);
  ws.send(JSON.stringify({ id: mid, method, params, ...(s ? { sessionId: s } : {}) }));
});
const { result: t } = await send("Target.createTarget", { url: "about:blank" });
const { result: att } = await send("Target.attachToTarget", { targetId: t.targetId, flatten: true });
const sid = att.sessionId;
await send("Runtime.enable", {}, sid);

const evl = async (expression) => {
  const r = await send("Runtime.evaluate", { expression, returnByValue: true, awaitPromise: false }, sid);
  return r.result?.result?.value ?? r.result;
};

await send("Page.navigate", { url: "file:///" + target.replace(/\\/g, "/") }, sid);
await sleep(12000);

for (const mode of ["btnStreet", "btnSat", "btnVector"]) {
  await evl(`document.getElementById('${mode}').click(); true;`);
  await sleep(6000);
  const info = await evl(`(() => {
    const ms = [...document.querySelectorAll('.leaflet-marker-icon.sp')];
    const mapRect = document.getElementById('map').getBoundingClientRect();
    return {
      mode: '${mode}',
      baseLayer: baseLayer,
      markerCount: ms.length,
      inView: ms.filter(m => { const r = m.getBoundingClientRect();
        return r.left > mapRect.left && r.right < mapRect.right && r.top > mapRect.top && r.bottom < mapRect.bottom; }).length,
      sample: ms.slice(0,4).map(m => { const r = m.getBoundingClientRect();
        return { x: Math.round(r.left), y: Math.round(r.top), w: Math.round(r.width), h: Math.round(r.height),
                 vis: getComputedStyle(m).visibility, disp: getComputedStyle(m).display, op: getComputedStyle(m).opacity,
                 cls: m.className }; }),
      firstSpot: DATA.spots[0] ? [DATA.spots[0].lat, DATA.spots[0].lon] : null,
      tpFirst: DATA.spots[0] ? tp(DATA.spots[0].lat, DATA.spots[0].lon) : null,
      center: [map.getCenter().lat, map.getCenter().lng],
      zoom: map.getZoom(),
    };
  })()`);
  console.log(JSON.stringify(info, null, 2));
}
ws.close(); child.kill(); process.exit(0);
