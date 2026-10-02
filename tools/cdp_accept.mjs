// 本地验收探针：打开页面，等渲染，量关键 DOM + 抓 console 错误。
// 用法: node tools/cdp_accept.mjs <url> [outPng]
import { spawn } from "node:child_process";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const url = process.argv[2] || "http://localhost:8501/";
const outPng = process.argv[3] || join(tmpdir(), "accept.png");
const port = 9411;
const EDGE = "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe";
const profile = mkdtempSync(join(tmpdir(), "cdp-acc-"));
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
const consoleErrors = [];
ws.onmessage = (ev) => {
  const m = JSON.parse(ev.data);
  if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); return; }
  if (m.method === "Runtime.consoleAPICalled" && m.params.type === "error") {
    consoleErrors.push((m.params.args || []).map(a => a.value ?? a.description ?? "?").join(" "));
  }
  if (m.method === "Runtime.exceptionThrown") {
    consoleErrors.push("EXC " + (m.params.exceptionDetails?.exception?.description || JSON.stringify(m.params.exceptionDetails)));
  }
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

const evalJs = async (expr) => {
  const r = await send("Runtime.evaluate", { expression: expr, returnByValue: true, awaitPromise: true }, sid);
  return r.result?.result?.value;
};

// 等页面真正渲染出来：等待正文出现中文标题，最多 90s（免费层冷启动很慢）
let sawTitle = false;
for (let i = 0; i < 45; i++) {
  await sleep(2000);
  const txt = await evalJs("document.body.innerText || ''");
  if (txt && txt.includes("天光云影")) { sawTitle = true; break; }
}
const body = await evalJs("document.body.innerText || ''");
const info = await evalJs(`(() => {
  const q = (s) => document.querySelector(s);
  const txt = document.body.innerText || '';
  return {
    len: txt.length,
    hasError: txt.includes('AttributeError') || txt.includes('ImportError') || txt.includes('Traceback'),
    hasDegradedWarn: txt.includes('内置兜底数据') || txt.includes('不是最新版'),
    build: (txt.match(/构建版本\\s*\`?([0-9a-zA-Z.\\-]+)/) || [])[1] || null,
    configVer: (txt.match(/模块版本：\`([^\`]+)\`/) || [])[1] || null,
    storage: (txt.match(/存储：\\s*(.+)/) || [])[1] || null,
    leaflet: !!q('.leaflet-container'),
    tiles: document.querySelectorAll('img.leaflet-tile').length,
    iframes: document.querySelectorAll('iframe').length,
    radios: document.querySelectorAll('[role=radiogroup]').length,
    fileInput: document.querySelectorAll('input[type=file]').length,
    buttons: [...document.querySelectorAll('button')].map(b => (b.innerText || '').trim()).filter(Boolean).slice(0, 14),
  };
})()`);

// 截图
const { result: shot } = await send("Page.captureScreenshot", { format: "png" }, sid);
if (shot?.data) {
  const { writeFileSync } = await import("node:fs");
  writeFileSync(outPng, Buffer.from(shot.data, "base64"));
}

console.log(JSON.stringify({
  url, sawTitle, ...info,
  consoleErrors: consoleErrors.slice(0, 12),
  bodyHead: (body || "").slice(0, 700),
  screenshot: outPng,
}, null, 2));

try { child.kill(); } catch {}
process.exit(0);
