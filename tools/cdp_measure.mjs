// 量一量嵌入地图的 iframe 与容器真实宽度，定位"地图只有半宽"的原因。
import { spawn } from "node:child_process";
import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const url = process.argv[2];
const outPng = process.argv[3] || join(tmpdir(), "probe.png");
const port = 9345;
const EDGE = "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe";
const profile = mkdtempSync(join(tmpdir(), "cdp-w-"));
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
await send("Page.enable", {}, sid);
await send("Page.navigate", { url }, sid);
await sleep(24000);

const evl = async (expr, target) => {
  const r = await send("Runtime.evaluate", { expression: expr, returnByValue: true },
    target || sid);
  return r.result?.result?.value ?? r.result;
};

// 主页面：量 Streamlit 的容器与 iframe
console.log("=== 主页面 ===");
console.log(JSON.stringify(await evl(`(() => {
  const f = [...document.querySelectorAll('iframe')].map(x => ({
    w: Math.round(x.getBoundingClientRect().width), h: Math.round(x.getBoundingClientRect().height),
    src: (x.getAttribute('srcdoc') ? 'srcdoc' : x.src || '').slice(0, 50)
  }));
  const st = document.querySelector('section.main, [data-testid="stMain"]');
  return {
    winW: window.innerWidth,
    mainW: st ? Math.round(st.getBoundingClientRect().width) : null,
    iframes: f
  };
})()`), null, 2));

// 进入 iframe 内部量
const frames = await evl(`(() => {
  const f = document.querySelector('iframe');
  return f ? true : false;
})()`);
if (frames) {
  // srcdoc 的 iframe 在 frame tree 里 url 是 about:srcdoc，直接进 contentWindow 更可靠
  const inner = await evl(`(() => {
    const f = document.querySelector('iframe');
    if (!f) return { err: 'no iframe' };
    let w;
    try { w = f.contentWindow; } catch (e) { return { err: 'cross-origin' }; }
    if (!w || !w.document) return { err: 'no contentWindow' };
    const doc = w.document;
    const b = doc.body, d = doc.documentElement;
    const map = doc.getElementById('map');
    const lc = doc.querySelector('.leaflet-container');
    const side = doc.getElementById('side');
    const main = doc.getElementById('main');
    const tiles = doc.querySelectorAll('.leaflet-tile-pane img');
    return {
      bodyClass: b ? b.className : null,
      htmlW: d ? d.clientWidth : null,
      bodyW: b ? b.clientWidth : null,
      appW: (doc.getElementById('app')||{}).clientWidth,
      mainW: main ? main.clientWidth : null,
      mapW: map ? map.clientWidth : null,
      leafletW: lc ? lc.clientWidth : null,
      sideW: side ? side.clientWidth : null,
      sideDisplay: side ? w.getComputedStyle(side).display : null,
      tiles: tiles.length,
      tilesLoaded: [...tiles].filter(i => i.complete && i.naturalWidth > 0).length,
      markers: doc.querySelectorAll('.leaflet-marker-icon').length,
    };
  })()`);
  console.log("=== iframe 内部 ===");
  console.log(JSON.stringify(inner, null, 2));
}

const shot = await send("Page.captureScreenshot", { format: "png" }, sid);
if (shot?.result?.data) writeFileSync(outPng, Buffer.from(shot.result.data, "base64"));
console.log("截图 ->", outPng);
ws.close(); child.kill(); process.exit(0);
