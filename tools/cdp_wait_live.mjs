// 线上验收：等 Streamlit 挂载完成后提取应用文本（分步打印，避免长时间无声）。
// 用法: node tools/cdp_wait_live.mjs <url> [maxSeconds]
import { spawn } from "node:child_process";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const url = process.argv[2];
const maxSec = Number(process.argv[3] || 240);
const port = 9481;
const EDGE = "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe";
const profile = mkdtempSync(join(tmpdir(), "cdp-wait-"));
const child = spawn(EDGE, ["--headless=new", "--disable-gpu", "--no-first-run",
  `--remote-debugging-port=${port}`, `--user-data-dir=${profile}`,
  "--window-size=1500,1100", "about:blank"], { stdio: "ignore" });
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

const SNAP = `(() => {
  const app = document.querySelector('[data-testid="stAppViewContainer"]') || document.querySelector('.stApp');
  const txt = (app ? app.textContent : '') || '';
  return {
    mounted: !!app,
    len: txt.length,
    spinner: !!document.querySelector('.stSpinner, [data-testid="stSpinner"], [data-testid="stStatusWidget"]'),
    text: txt.replace(/\\s+/g, ' ')
  };
})()`;

let last = null;
const deadline = Date.now() + maxSec * 1000;
let tick = 0;
while (Date.now() < deadline) {
  await sleep(10000);
  tick++;
  last = (await ev(SNAP)) || { mounted: false, len: 0, text: "" };
  console.log(`[${tick * 10}s] mounted=${last.mounted} spinner=${last.spinner} len=${last.len} head="${last.text.slice(0, 90)}"`);
  if (last.mounted && last.len > 200 && !last.spinner) break;
}
console.log("\n=== 最终 ===");
console.log(JSON.stringify({
  mounted: last?.mounted, len: last?.len,
  hasError: /AttributeError|ImportError|Traceback/.test(last?.text || ""),
  degradedWarn: /内置兜底数据|不是最新版/.test(last?.text || ""),
  build: (last?.text || "").match(/构建版本\s*([0-9a-zA-Z.\-]+)/)?.[1] || null,
  configVer: (last?.text || "").match(/模块版本：([^）)]+)/)?.[1] || null,
  storageCloud: /Supabase 云端/.test(last?.text || ""),
  consoleErrors: errs.slice(0, 8),
  text: (last?.text || "").slice(0, 600),
}, null, 2));
try { child.kill(); } catch {}
process.exit(0);
