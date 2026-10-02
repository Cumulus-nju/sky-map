// 线上/本地站点验收探针。
//
// ⚠️⚠️ 重要警告（2026-10-02 血的教训）：**这个探针在 Streamlit Cloud 线上页面上会给出
// 假阴性**。实测同一次会话里，`Page.captureScreenshot` 明确显示应用已经完整渲染
// （标题/侧边栏/地图/横幅全在），而同一 session 的 `Runtime.evaluate` 读
// `document.body.textContent` 却始终只有 72 个字符的静态壳（"You need to enable
// JavaScript..."），stApp / data-testid 全为空。本地跑同一个探针却 10 秒内正常读到。
//
// ⇒ **在线上页面，"截图"比 "Runtime.evaluate" 可信**。看到本探针报
//   `mounted:false / textLen 很小` 时，**必须再看截图**，不要直接判定白屏 ——
//   否则会把"站点正常"误报成故障（本次就是这么绕了一大圈）。
//   判断线上到底跑没跑新代码，更可靠的办法是**看截图里侧边栏的「构建版本」**。
//
// 用法: node tools/cdp_accept.mjs <url> [outPng] [maxWaitSeconds]
//
// 用法: node tools/cdp_accept.mjs <url> [outPng] [maxWaitSeconds]
import { spawn } from "node:child_process";
import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const url = process.argv[2] || "https://azzjin9anwdbyykyd3dyh6.streamlit.app/";
const outPng = process.argv[3] || join(tmpdir(), "accept.png");
const maxWait = Number(process.argv[4] || 150) * 1000;
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
  if (m.method === "Runtime.consoleAPICalled" && m.params.type === "error")
    consoleErrors.push((m.params.args || []).map(a => a.value ?? a.description ?? "?").join(" "));
  if (m.method === "Runtime.exceptionThrown")
    consoleErrors.push("EXC " + (m.params.exceptionDetails?.exception?.description || "?"));
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

// 多来源取"页面上到底有没有字"，并识别"还在唤醒"的状态。
//
// 判据是**应用容器出现**（Streamlit 挂载完成的标志），而不是"文字够长"：
// 免费层冷启动时页面会先显示一个转圈，此时 body.textContent 只有页脚几十个字，
// 若按长度判定就会误报成"白屏"（2026-10-02 被这个坑过一次）。
const SNAPSHOT = `(() => {
  const pick = (el) => (el ? (el.innerText || el.textContent || '') : '');
  const app = document.querySelector('[data-testid="stAppViewContainer"]') || document.querySelector('.stApp');
  const txt = [pick(app), pick(document.body), pick(document.documentElement)]
                .sort((a,b) => b.length - a.length)[0] || '';
  const spinner = document.querySelector('.stSpinner, [data-testid="stSpinner"], [data-testid="stStatusWidget"]');
  return {
    len: txt.length,
    text: txt,
    mounted: !!app,                       // ★ Streamlit 前端挂载完成
    skeleton: !!document.querySelector('[data-testid="stSkeleton"]'),
    spinner: !!spinner,
    iframes: document.querySelectorAll('iframe').length,
  };
})()`;

let snap = { len: 0, text: "", mounted: false, spinner: false };
const deadline = Date.now() + maxWait;
while (Date.now() < deadline) {
  snap = (await evaluate(SNAPSHOT)) || snap;
  // 挂载完成 + 不再转圈 + 有实质内容 才算就绪
  if (snap.mounted && !snap.spinner && snap.len > 200) break;
  await sleep(3000);
}
// 再稳 5 秒，让 iframe/地图把瓦片也画上
await sleep(5000);

const final = (await evaluate(SNAPSHOT)) || snap;
const txt = final.text || "";
const info = await evaluate(`(() => {
  const q = (s) => document.querySelector(s);
  const txt = ${JSON.stringify(txt)};
  return {
    hasError: txt.includes('AttributeError') || txt.includes('ImportError') || txt.includes('Traceback'),
    hasDegradedWarn: txt.includes('内置兜底数据') || txt.includes('不是最新版'),
    build: (txt.match(/构建版本\\s*\`?([0-9a-zA-Z.\\-]+)/) || [])[1] || null,
    configVer: (txt.match(/模块版本：\`([^\`]+)\`/) || [])[1] || null,
    storageSupabase: txt.includes('Supabase 云端'),
    storageLocal: txt.includes('本地临时存储'),
    leafletTop: !!q('.leaflet-container'),
    iframes: document.querySelectorAll('iframe').length,
    buttons: [...document.querySelectorAll('button')].map(b=>(b.innerText||'').trim()).filter(Boolean).slice(0,12),
  };
})()`);

const { result: shot } = await send("Page.captureScreenshot", { format: "png" }, sid);
if (shot?.data) writeFileSync(outPng, Buffer.from(shot.data, "base64"));

console.log(JSON.stringify({
  url, textLen: final.len, mounted: final.mounted, spinner: final.spinner,
  ...info, consoleErrors: consoleErrors.slice(0, 10),
  bodyHead: txt.slice(0, 500), screenshot: outPng,
}, null, 2));
try { child.kill(); } catch {}
process.exit(0);
