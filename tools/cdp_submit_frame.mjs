// Streamlit 投稿页验证：进入 st_folium 的 iframe，确认外框与视野锁定都生效。
// 用法: node tools/cdp_submit_frame.mjs <app_url> [screenshot.png]
import { spawn } from "node:child_process";
import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const url = process.argv[2] || "http://127.0.0.1:8501";
const outPng = process.argv[3] || join(tmpdir(), "submit.png");
const port = 9379;
const EDGE = "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe";
const profile = mkdtempSync(join(tmpdir(), "cdp-s-"));
const child = spawn(EDGE, ["--headless=new", "--disable-gpu", "--no-first-run",
  `--remote-debugging-port=${port}`, `--user-data-dir=${profile}`,
  "--window-size=1400,1000", "--hide-scrollbars", "about:blank"], { stdio: "ignore" });
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function wsUrl() {
  for (let i = 0; i < 80; i++) {
    try {
      const j = await (await fetch(`http://127.0.0.1:${port}/json/version`)).json();
      if (j.webSocketDebuggerUrl) return j.webSocketDebuggerUrl;
    } catch {}
    await sleep(300);
  }
  throw new Error("no cdp");
}
const ws = new WebSocket(await wsUrl());
await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej; });
let id = 0; const pending = new Map(); const consoleMsgs = [];
ws.onmessage = (ev) => {
  const m = JSON.parse(ev.data);
  if (m.method === "Runtime.consoleAPICalled" && m.params?.type === "error") {
    consoleMsgs.push((m.params.args || []).map(a => a.value ?? a.description ?? "").join(" ").slice(0, 200));
  }
  if (m.method === "Runtime.exceptionThrown") {
    consoleMsgs.push("EXC: " + (m.params?.exceptionDetails?.text || "").slice(0, 200));
  }
  if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); }
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
await sleep(28000);   // Streamlit 首次编译 + 唤醒

const evl = async (expr) => {
  const r = await send("Runtime.evaluate", { expression: expr, returnByValue: true }, sid);
  if (r.result?.exceptionDetails) return { __err: r.result.exceptionDetails.text };
  return r.result?.result?.value ?? r.result;
};

console.log("=== 主页面 ===");
console.log(JSON.stringify(await evl(`(() => {
  const t = document.body.innerText || '';
  const err = [...document.querySelectorAll('[data-testid="stException"], .stException, [data-testid="stAlert"]')]
      .map(x => x.innerText.slice(0, 300));
  return {
    hasTitle: t.includes('天光云影'),
    hasSubmitHeading: t.includes('选择校区并在图上点出机位'),
    iframeCount: document.querySelectorAll('iframe').length,
    alerts: err,
  };
})()`), null, 2));

console.log("\n=== st_folium iframe 内部 ===");
console.log(JSON.stringify(await evl(`(() => {
  const f = document.querySelector('iframe');
  if (!f) return { err: '没有 iframe' };
  let w; try { w = f.contentWindow; } catch (e) { return { err: 'cross-origin' }; }
  const d = w.document;
  if (!d) return { err: 'no document' };
  const lm = d.querySelector('.leaflet-container');
  const tiles = d.querySelectorAll('.leaflet-tile-pane img');
  // 从 leaflet 容器上拿 map 实例（folium 把 map 变量留在全局）
  let info = {};
  try {
    const mapObj = w.map || (w.eval && w.eval('typeof map !== "undefined" ? map : null'));
    if (mapObj) {
      const b = mapObj.getBounds();
      info = {
        zoom: mapObj.getZoom(),
        minZoom: mapObj.getMinZoom(),
        maxBounds: mapObj.options.maxBounds ? mapObj.options.maxBounds.toBBoxString() : null,
        viscosity: mapObj.options.maxBoundsViscosity,
        view: [ +b.getSouth().toFixed(6), +b.getWest().toFixed(6), +b.getNorth().toFixed(6), +b.getEast().toFixed(6) ],
      };
    } else { info = { note: '拿不到 map 实例' }; }
  } catch (e) { info = { err: String(e).slice(0, 160) }; }
  return {
    leafletPresent: !!lm,
    mapW: lm ? lm.clientWidth : null,
    mapH: lm ? lm.clientHeight : null,
    tiles: tiles.length,
    tilesLoaded: [...tiles].filter(i => i.complete && i.naturalWidth > 0).length,
    rectangles: d.querySelectorAll('path.leaflet-interactive, path').length,
    ...info,
  };
})()`), null, 2));

console.log("\n=== 控制台错误 ===");
console.log(consoleMsgs.length ? consoleMsgs.join("\n") : "（无）");

const shot = await send("Page.captureScreenshot", { format: "png" }, sid);
if (shot?.result?.data) writeFileSync(outPng, Buffer.from(shot.result.data, "base64"));
console.log("\n截图 ->", outPng);
ws.close(); child.kill(); process.exit(0);
