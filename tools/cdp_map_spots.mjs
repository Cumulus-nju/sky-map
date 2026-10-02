// 进打卡点地图页的 iframe，数真实渲染出来的点位/图片（不靠肉眼）。
// 用法: node tools/cdp_map_spots.mjs <app_url> [outPng]
import { spawn } from "node:child_process";
import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const url = process.argv[2] || "http://localhost:8501/%E6%89%93%E5%8D%A1%E7%82%B9%E5%9C%B0%E5%9B%BE";
const outPng = process.argv[3] || join(tmpdir(), "mapspots.png");
const port = 9415;
const EDGE = "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe";
const profile = mkdtempSync(join(tmpdir(), "cdp-ms-"));
const child = spawn(EDGE, ["--headless=new", "--disable-gpu", "--no-first-run",
  `--remote-debugging-port=${port}`, `--user-data-dir=${profile}`,
  "--window-size=1500,1100", "--hide-scrollbars", "about:blank"], { stdio: "ignore" });
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function wsUrl() {
  for (let i = 0; i < 60; i++) {
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
let id = 0; const pending = new Map();
const errs = [];
ws.onmessage = (ev) => {
  const m = JSON.parse(ev.data);
  if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); return; }
  if (m.method === "Runtime.consoleAPICalled" && m.params.type === "error")
    errs.push((m.params.args || []).map(a => a.value ?? a.description).join(" "));
  if (m.method === "Runtime.exceptionThrown")
    errs.push("EXC " + (m.params.exceptionDetails?.exception?.description || "?"));
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

const evaluate = async (expr) => {
  const r = await send("Runtime.evaluate", { expression: expr, returnByValue: true }, sid);
  return r.result?.result?.value;
};

await sleep(30000);
// 主上下文里通过 contentDocument 读 iframe（同源，可直接访问）
const info = await evaluate(`(() => {
  const f = document.querySelector('iframe');
  const d = f && (f.contentDocument || (f.contentWindow && f.contentWindow.document));
  if (!d) return { ok:false, why:'no contentDocument' };
  const txt = d.body ? d.body.innerText : '';
  const imgs = [...d.querySelectorAll('img')];
  const loaded = imgs.filter(i => i.complete && i.naturalWidth > 0);
  return {
    ok: true,
    leaflet: !!d.querySelector('.leaflet-container'),
    markers: d.querySelectorAll('.leaflet-marker-icon').length,
    tiles: d.querySelectorAll('img.leaflet-tile').length,
    tilesLoaded: [...d.querySelectorAll('img.leaflet-tile')].filter(i=>i.complete&&i.naturalWidth>0).length,
    imgs: imgs.length,
    imgsLoaded: loaded.length,
    textLen: txt.length,
    hasSpotText: /GULOU|XIANLIN|SUZHOU|打卡点|幅/.test(txt),
    textHead: txt.slice(0, 400),
  };
})()`);

const { result: shot } = await send("Page.captureScreenshot", { format: "png" }, sid);
if (shot?.data) writeFileSync(outPng, Buffer.from(shot.data, "base64"));

console.log(JSON.stringify({ url, ...info, consoleErrors: errs.slice(0, 10), screenshot: outPng }, null, 2));
try { child.kill(); } catch {}
process.exit(0);
