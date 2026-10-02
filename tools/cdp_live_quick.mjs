// 快速诊断线上页面状态：只加载 25 秒，打印正文与截图，不做长时间轮询。
// 用法: node tools/cdp_live_quick.mjs [url] [out.png]
import { spawn } from "node:child_process";
import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const url = process.argv[2] || "https://azzjin9anwdbyykyd3dyh6.streamlit.app/";
const outPng = process.argv[3] || join(tmpdir(), "quick.png");
const port = 9383;
const EDGE = "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe";
const profile = mkdtempSync(join(tmpdir(), "cdp-q-"));
const child = spawn(EDGE, ["--headless=new", "--disable-gpu", "--no-first-run",
  `--remote-debugging-port=${port}`, `--user-data-dir=${profile}`,
  "--window-size=1400,1000", "--hide-scrollbars", "about:blank"], { stdio: "ignore" });
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
ws.onmessage = (ev) => {
  const m = JSON.parse(ev.data);
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

const evl = async (expr) => {
  const r = await send("Runtime.evaluate", { expression: expr, returnByValue: true }, sid);
  if (r.result?.exceptionDetails) return { __err: r.result.exceptionDetails.text };
  return r.result?.result?.value ?? r.result;
};

for (const wait of [20000, 20000, 30000, 30000]) {
  await sleep(wait);
  const s = await evl(`(() => {
    const txt = (document.body.innerText || '').trim();
    const f = document.querySelector('iframe');
    let leaflet = false;
    if (f) { try { leaflet = !!(f.contentWindow.document.querySelector('.leaflet-container')); } catch(e){} }
    return { txt: txt.slice(0, 260), len: txt.length, iframe: !!f, leaflet };
  })()`);
  console.log(`--- 累计 ${(wait/1000)}s ---`);
  console.log(JSON.stringify(s, null, 2).slice(0, 900));
  if (s?.leaflet || (s?.txt || "").includes("ImportError")) break;
}

const shot = await send("Page.captureScreenshot", { format: "png" }, sid);
if (shot?.result?.data) writeFileSync(outPng, Buffer.from(shot.result.data, "base64"));
console.log("截图 ->", outPng);
ws.close(); child.kill(); process.exit(0);
