// 精确诊断：地图视野 vs 外框（用 leaflet 的像素空间，不用 getBoundingClientRect）。
// 用法: node tools/cdp_frame_precise.mjs <app_url>
import { spawn } from "node:child_process";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const url = process.argv[2] || "http://localhost:8501/";
const port = 9507;
const EDGE = "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe";
const profile = mkdtempSync(join(tmpdir(), "cdp-prec-"));
const child = spawn(EDGE, ["--headless=new", "--disable-gpu", "--no-first-run",
  `--remote-debugging-port=${port}`, `--user-data-dir=${profile}`,
  "--window-size=1500,1100", "--hide-scrollbars", "about:blank"], { stdio: "ignore" });
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
ws.onmessage = (ev) => { const m = JSON.parse(ev.data);
  if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); } };
const send = (method, params = {}, s) => new Promise((res) => {
  const mid = ++id; pending.set(mid, res);
  ws.send(JSON.stringify({ id: mid, method, params, ...(s ? { sessionId: s } : {}) }));
});
const { result: t } = await send("Target.createTarget", { url: "about:blank" });
const { result: att } = await send("Target.attachToTarget", { targetId: t.targetId, flatten: true });
const sid = att.sessionId;
await send("Page.enable", {}, sid);
await send("Page.navigate", { url }, sid);
const ev = async (e) => {
  const r = await send("Runtime.evaluate", { expression: e, returnByValue: true }, sid);
  if (r.result?.exceptionDetails) return { __err: r.result.exceptionDetails.text };
  return r.result?.result?.value;
};
for (let i = 0; i < 30; i++) {
  await sleep(3000);
  if (await ev(`(() => { const f=document.querySelector('iframe'); try { const d=f&&f.contentDocument; return !!(d&&d.querySelector('.leaflet-container')); } catch(e){ return false; } })()`)) break;
}
await sleep(5000);

console.log(JSON.stringify(await ev(`(() => {
  const f = document.querySelector('iframe');
  const w = f.contentWindow, d = f.contentDocument;
  const m = w.map;
  if (!m) return { err: 'no map var' };
  const sz = m.getSize();
  const b = m.getBounds();
  const cnt = m.getCenter();
  // 外框的两个角在像素空间的位置
  const rects = [...d.querySelectorAll('path')];
  const info = rects.map(p => {
    try {
      const bb = p.getBBox ? p.getBBox() : null;
      return bb ? { w: Math.round(bb.width), h: Math.round(bb.height), x: Math.round(bb.x), y: Math.round(bb.y) } : null;
    } catch (e) { return null; }
  }).filter(Boolean).filter(r => r.w > 3 && r.h > 3).sort((a,b)=>(b.w*b.h)-(a.w*a.h));
  return {
    mapSizePx: [sz.x, sz.y],
    zoom: m.getZoom(), minZoom: m.getMinZoom(),
    center: [+cnt.lat.toFixed(6), +cnt.lng.toFixed(6)],
    viewBounds: [+b.getSouth().toFixed(6), +b.getWest().toFixed(6), +b.getNorth().toFixed(6), +b.getEast().toFixed(6)],
    viewSpanDeg: [+(b.getNorth()-b.getSouth()).toFixed(6), +(b.getEast()-b.getWest()).toFixed(6)],
    maxBounds: m.options.maxBounds ? m.options.maxBounds.toBBoxString() : null,
    viscosity: m.options.maxBoundsViscosity,
    biggestRectPx: info[0] || null,
    rectFillW: info[0] ? +(info[0].w / sz.x).toFixed(3) : null,
    rectFillH: info[0] ? +(info[0].h / sz.y).toFixed(3) : null,
    allRectsTop5: info.slice(0, 5),
  };
})()`), null, 2));
try { child.kill(); } catch {}
process.exit(0);
