// 用 leaflet 自己的投影 API 量外框在视图里的占比，并报告 fitBounds 会选哪一级。
// 用法: node tools/cdp_leaflet_truth.mjs <app_url> <S> <W> <N> <E>
import { spawn } from "node:child_process";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const url = process.argv[2];
const [S, W, N, E] = process.argv.slice(3, 7).map(Number);
const port = 9511;
const EDGE = "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe";
const profile = mkdtempSync(join(tmpdir(), "cdp-lt-"));
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
  const w = document.querySelector('iframe').contentWindow;
  const m = w.map, L = w.L;
  if (!m || !L) return { err: 'no map/L' };
  const sz = m.getSize();
  const b = L.latLngBounds([[${S}, ${W}], [${N}, ${E}]]);
  const zFit = m.getBoundsZoom(b);              // fitBounds 会选的级别（含 padding 默认 0）
  const zFitPad = m.getBoundsZoom(b, false, L.point(6, 6));
  // 外框在"当前缩放"下占多少像素（leaflet 自己的投影）
  const p1 = m.project(b.getNorthWest(), m.getZoom());
  const p2 = m.project(b.getSouthEast(), m.getZoom());
  const px = { w: Math.abs(p2.x - p1.x), h: Math.abs(p2.y - p1.y) };
  // 外框在 zFit 下占多少像素
  const q1 = m.project(b.getNorthWest(), zFit);
  const q2 = m.project(b.getSouthEast(), zFit);
  const pxAtFit = { w: Math.abs(q2.x - q1.x), h: Math.abs(q2.y - q1.y) };
  return {
    mapSizePx: [sz.x, sz.y],
    currentZoom: m.getZoom(), getMinZoom: m.getMinZoom(), getMaxZoom: m.getMaxZoom(),
    fitBoundsZoom: zFit, fitBoundsZoomWithPad6: zFitPad,
    framePxAtCurrentZoom: { w: Math.round(px.w), h: Math.round(px.h) },
    fillAtCurrentZoom: { w: +(px.w / sz.x).toFixed(3), h: +(px.h / sz.y).toFixed(3) },
    framePxAtFitZoom: { w: Math.round(pxAtFit.w), h: Math.round(pxAtFit.h) },
    fillAtFitZoom: { w: +(pxAtFit.w / sz.x).toFixed(3), h: +(pxAtFit.h / sz.y).toFixed(3) },
    layerPointOfNW_atCurrent: m.latLngToLayerPoint(b.getNorthWest()),
    layerPointOfSE_atCurrent: m.latLngToLayerPoint(b.getSouthEast()),
    mapPanePos: m._mapPane ? m._mapPane._leaflet_pos : null,
  };
})()`), null, 2));
try { child.kill(); } catch {}
process.exit(0);
