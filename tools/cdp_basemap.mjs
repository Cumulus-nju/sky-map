// 打开地图并点击指定底图按钮（OSM街道/高德卫星/矢量），截图对比。
// 用法: node cdp_basemap.mjs <html文件> <按钮id> <输出png>
import { spawn } from "node:child_process";
import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const [target, btnId, outPng] = process.argv.slice(2);
const port = 9338;
const EDGE = "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe";
const profile = mkdtempSync(join(tmpdir(), "cdp-bm-"));
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
const send = (method, params = {}, sessionId) => new Promise((res) => {
  const mid = ++id; pending.set(mid, res);
  ws.send(JSON.stringify({ id: mid, method, params, ...(sessionId ? { sessionId } : {}) }));
});
const { result: t } = await send("Target.createTarget", { url: "about:blank" });
const { result: att } = await send("Target.attachToTarget", { targetId: t.targetId, flatten: true });
const sid = att.sessionId;
await send("Runtime.enable", {}, sid);

await send("Page.navigate", { url: "file:///" + target.replace(/\\/g, "/") }, sid);
await sleep(12000);

// 点按钮并等瓦片
await send("Runtime.evaluate", {
  expression: `document.getElementById(${JSON.stringify(btnId)}).click(); true;`,
  returnByValue: true,
}, sid);
await sleep(11000);

const probe = await send("Runtime.evaluate", {
  returnByValue: true,
  expression: `({
    baseLayer: typeof baseLayer !== 'undefined' ? baseLayer : 'n/a',
    tiles: document.querySelectorAll('.leaflet-tile-pane img').length,
    loaded: [...document.querySelectorAll('.leaflet-tile-pane img')].filter(i=>i.complete&&i.naturalWidth>0).length,
    sampleUrl: (document.querySelector('.leaflet-tile-pane img')||{}).src || null,
    note: (document.getElementById('note')||{}).innerText || '',
    markers: document.querySelectorAll('.leaflet-marker-icon').length,
  })`,
}, sid);
console.log(JSON.stringify(probe.result?.result?.value, null, 2));

const { result: shot } = await send("Page.captureScreenshot", { format: "png" }, sid);
if (shot?.data) writeFileSync(outPng, Buffer.from(shot.data, "base64"));
console.log("截图 ->", outPng);
ws.close(); child.kill(); process.exit(0);
