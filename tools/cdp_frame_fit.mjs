// 量投稿页地图：外框在视图里的**实际像素占比**，以及 minZoom 是否锁住。
// 这是判断"一进来是否只显示蓝框内容"的硬指标（不靠肉眼看图）。
// 用法: node tools/cdp_frame_fit.mjs <app_url> [outPng]
import { spawn } from "node:child_process";
import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const url = process.argv[2] || "http://localhost:8501/";
const outPng = process.argv[3] || join(tmpdir(), "fit.png");
const port = 9503;
const EDGE = "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe";
const profile = mkdtempSync(join(tmpdir(), "cdp-fit-"));
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
let id = 0; const pending = new Map(); const errs = [];
ws.onmessage = (ev) => {
  const m = JSON.parse(ev.data);
  if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); return; }
  if (m.method === "Runtime.consoleAPICalled" && m.params.type === "error")
    errs.push((m.params.args || []).map(a => a.value ?? a.description ?? "?").join(" "));
};
const send = (method, params = {}, s) => new Promise((res) => {
  const mid = ++id; pending.set(mid, res);
  ws.send(JSON.stringify({ id: mid, method, params, ...(s ? { sessionId: s } : {}) }));
});
const { result: t } = await send("Target.createTarget", { url: "about:blank" });
const { result: att } = await send("Target.attachToTarget", { targetId: t.targetId, flatten: true });
const sid = att.sessionId;
await send("Runtime.enable", {}, sid);
await send("Page.enable", {}, sid);
await send("Page.navigate", { url }, sid);
const ev = async (e) => {
  const r = await send("Runtime.evaluate", { expression: e, returnByValue: true }, sid);
  return r.result?.result?.value;
};

// 等 iframe 里的 leaflet 地图就绪
let ready = false;
for (let i = 0; i < 30; i++) {
  await sleep(3000);
  ready = await ev(`(() => {
    const f = document.querySelector('iframe');
    try { const d = f && f.contentDocument; return !!(d && d.querySelector('.leaflet-container')); }
    catch (e) { return false; }
  })()`);
  if (ready) break;
}
await sleep(4000);   // 让 fitBounds 与瓦片稳定

const MEASURE = `(() => {
  const f = document.querySelector('iframe');
  let d; try { d = f.contentDocument; } catch (e) { return { err: 'cross-origin' }; }
  if (!d) return { err: 'no document' };
  const c = d.querySelector('.leaflet-container');
  const W = c.clientWidth, H = c.clientHeight;

  // 视图地理范围（像素 -> 经纬度，用 leaflet 的比例尺函数）
  const rects = [...d.querySelectorAll('path')].map(p => {
    const r = p.getBoundingClientRect ? p.getBoundingClientRect() : null;
    return r ? { x: Math.round(r.left), y: Math.round(r.top), w: Math.round(r.width), h: Math.round(r.height) } : null;
  }).filter(Boolean).filter(r => r.w > 5 && r.h > 5);

  // 蓝框（最大那个矩形）的像素占比。宽扁框会被投影成"细线"，所以取面积最大者
  const biggest = rects.sort((a, b) => b.w * b.h - a.w * a.h)[0] || null;

  return {
    mapW: W, mapH: H,
    rectCount: rects.length,
    biggest,
    fillW: biggest ? +(biggest.w / W).toFixed(3) : null,
    fillH: biggest ? +(biggest.h / H).toFixed(3) : null,
    biggestIsThin: biggest ? (biggest.w < 12 || biggest.h < 12) : null,
    tiles: d.querySelectorAll('.leaflet-tile-pane img').length,
    tilesLoaded: [...d.querySelectorAll('.leaflet-tile-pane img')].filter(i => i.complete && i.naturalWidth > 0).length,
    hasMapVar: (() => { try { return typeof f.contentWindow.map !== 'undefined' && !!f.contentWindow.map; } catch (e) { return false; } })(),
  };
})()`;

const before = await ev(MEASURE);

// 试着缩出去，验证"只能放大不能缩小"
const zoomTest = await ev(`(() => {
  const f = document.querySelector('iframe');
  const w = f.contentWindow, d = f.contentDocument;
  const m = w.map;
  if (!m) return { note: '拿不到 map 实例' };
  const z0 = m.getZoom();
  m.zoomOut(); const z1 = m.getZoom();
  m.zoomIn(); m.zoomIn(); const z2 = m.getZoom();
  return { startZoom: z0, minZoom: m.getMinZoom(), afterZoomOut: z1, afterZoomIn2: z2,
           lockedAtMin: z1 === m.getMinZoom() && z0 === z1 };
})()`);

const { result: shot } = await send("Page.captureScreenshot", { format: "png" }, sid);
if (shot?.data) writeFileSync(outPng, Buffer.from(shot.data, "base64"));

console.log(JSON.stringify({ url, leafletReady: ready, map: before, zoomTest, consoleErrors: errs.slice(0, 6), screenshot: outPng }, null, 2));
try { child.kill(); } catch {}
process.exit(0);
