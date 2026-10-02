// 线上诊断：抓网络失败 + 全部 console + 周期性截图。用于判断"前端为什么没挂载"。
// 用法: node tools/cdp_live_diagnose.mjs <url> [seconds] [outPrefix]
import { spawn } from "node:child_process";
import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const url = process.argv[2];
const secs = Number(process.argv[3] || 90);
const prefix = process.argv[4] || join(tmpdir(), "diag");
const port = 9487;
const EDGE = "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe";
const profile = mkdtempSync(join(tmpdir(), "cdp-diag-"));
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
let id = 0; const pending = new Map();
const consoleAll = []; const netFail = []; const wsEvents = [];
ws.onmessage = (ev) => {
  const m = JSON.parse(ev.data);
  if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); return; }
  if (m.method === "Runtime.consoleAPICalled")
    consoleAll.push(`[${m.params.type}] ` + (m.params.args || []).map(a => a.value ?? a.description ?? "?").join(" ").slice(0, 300));
  if (m.method === "Runtime.exceptionThrown")
    consoleAll.push("[exception] " + (m.params.exceptionDetails?.exception?.description || JSON.stringify(m.params.exceptionDetails)).slice(0, 400));
  if (m.method === "Network.loadingFailed")
    netFail.push(`${m.params.type} ${m.params.errorText} blocked=${m.params.blockedReason || "-"}`);
  if (m.method === "Network.webSocketFrameError")
    wsEvents.push("WS frame error: " + m.params.errorMessage);
  if (m.method === "Network.webSocketClosed")
    wsEvents.push("WS closed");
  if (m.method === "Network.webSocketCreated")
    wsEvents.push("WS created: " + m.params.url.slice(0, 120));
};
const send = (method, params = {}, s) => new Promise((res) => {
  const mid = ++id; pending.set(mid, res);
  ws.send(JSON.stringify({ id: mid, method, params, ...(s ? { sessionId: s } : {}) }));
});
const { result: t } = await send("Target.createTarget", { url: "about:blank" });
const { result: att } = await send("Target.attachToTarget", { targetId: t.targetId, flatten: true });
const sid = att.sessionId;
await send("Runtime.enable", {}, sid);
await send("Network.enable", {}, sid);
await send("Page.enable", {}, sid);
await send("Page.navigate", { url }, sid);
const ev = async (e) => {
  const r = await send("Runtime.evaluate", { expression: e, returnByValue: true }, sid);
  return r.result?.result?.value;
};

for (let i = 1; i <= Math.ceil(secs / 15); i++) {
  await sleep(15000);
  const st = await ev(`(()=>{
    const app=document.querySelector('[data-testid="stAppViewContainer"]')||document.querySelector('.stApp');
    return {mounted:!!app, len:((app?app.textContent:'')||'').length,
            bodyLen:(document.body.textContent||'').length,
            spinner:!!document.querySelector('.stSpinner,[data-testid="stSpinner"],[data-testid="stStatusWidget"]'),
            dashes:document.querySelectorAll('[data-testid="stAppSkeleton"],[class*=skeleton]').length};
  })()`);
  console.log(`[${i * 15}s] ${JSON.stringify(st)}`);
  const { result: shot } = await send("Page.captureScreenshot", { format: "png" }, sid);
  if (shot?.data) writeFileSync(`${prefix}_${i * 15}s.png`, Buffer.from(shot.data, "base64"));
  if (st?.mounted && st.len > 200) break;
}
console.log("\n=== 网络失败 ===");
console.log(netFail.length ? [...new Set(netFail)].slice(0, 20).join("\n") : "（无）");
console.log("\n=== WebSocket 事件 ===");
console.log(wsEvents.length ? [...new Set(wsEvents)].slice(0, 12).join("\n") : "（无）");
console.log("\n=== 控制台（全部）===");
console.log(consoleAll.length ? consoleAll.slice(0, 20).join("\n") : "（无）");
try { child.kill(); } catch {}
process.exit(0);
