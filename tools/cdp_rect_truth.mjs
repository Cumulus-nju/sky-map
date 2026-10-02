// 直接读矩形图层自身的经纬度边界 + 容器/iframe 真实尺寸 + 是否有 CSS 缩放。
// 用法: node tools/cdp_rect_truth.mjs <app_url>
import { spawn } from "node:child_process";
import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const url = process.argv[2] || "http://localhost:8501/";
const outPng = process.argv[3] || join(tmpdir(), "rect.png");
const port = 9515;
const EDGE = "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe";
const profile = mkdtempSync(join(tmpdir(), "cdp-rect-"));
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

const res = await ev(`(() => {
  const f = document.querySelector('iframe');
  const w = f.contentWindow, d = f.contentDocument;
  const m = w.map;
  const c = d.querySelector('.leaflet-container');
  // iframe 自身的显示尺寸 vs CSS 尺寸（判断有没有被 transform 缩放）
  const fr = f.getBoundingClientRect();
  const out = {
    iframe: {
      cssW: f.clientWidth, cssH: f.clientHeight,
      displayW: Math.round(fr.width), displayH: Math.round(fr.height),
      scaleX: +(fr.width / (f.clientWidth || 1)).toFixed(3),
      scaleY: +(fr.height / (f.clientHeight || 1)).toFixed(3),
      styleAttr: (f.getAttribute('style') || '').slice(0, 200),
    },
    container: { clientW: c.clientWidth, clientH: c.clientHeight, rectW: Math.round(c.getBoundingClientRect().width) },
    mapVarSize: m ? [m.getSize().x, m.getSize().y] : null,
    mapInternalSize: m && m._size ? [m._size.x, m._size.y] : null,
    dpr: w.devicePixelRatio,
    rects: [],
    viewBounds: m ? [+m.getBounds().getSouth().toFixed(6), +m.getBounds().getWest().toFixed(6), +m.getBounds().getNorth().toFixed(6), +m.getBounds().getEast().toFixed(6)] : null,
    zoom: m ? m.getZoom() : null,
  };
  if (m) {
    // 找到那个蓝框矩形图层：读它自己的 latLngBounds
    m.eachLayer(function (ly) {
      if (ly.getBounds && ly.getLatLngBounds && !ly.getLatLng) {
        const b = ly.getBounds();
        out.rects.push({
          type: ly.constructor && ly.constructor.name,
          south: +b.getSouth().toFixed(6), west: +b.getWest().toFixed(6),
          north: +b.getNorth().toFixed(6), east: +b.getEast().toFixed(6),
          stroke: ly.options && ly.options.color,
        });
      }
    });
  }
  return out;
})()`);
console.log(JSON.stringify(res, null, 2));
const { result: shot } = await send("Page.captureScreenshot", { format: "png" }, sid);
if (shot?.data) writeFileSync(outPng, Buffer.from(shot.data, "base64"));
console.log("截图 ->", outPng);
try { child.kill(); } catch {}
process.exit(0);
