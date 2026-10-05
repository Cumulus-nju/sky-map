// 线上冷启动验收：**先唤醒休眠的应用**，再等渲染 → 截图 + 抓构建版本。
//
// 为什么单独写这个：Streamlit Cloud 免费层闲置后会休眠，页面只剩一个
// "Yes, get this app back up!" 按钮。`cdp_accept.mjs` 不会点它，于是永远拿到那个
// 193 字符的休眠壳 —— 非常容易被误判成"站点坏了"（2026-10-04 差点这么判）。
//
// ⚠ 仍然遵守 2026-10-02 的教训：**线上判断以截图为准**，DOM 文本会假阴性。
//
// 用法: node tools/cdp_wake_live.mjs <url> [outPng] [maxSeconds]
import { spawn } from "node:child_process";
import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const url = process.argv[2] || "https://azzjin9anwdbyykyd3dyh6.streamlit.app/";
const outPng = process.argv[3] || join(tmpdir(), "wake.png");
const maxSec = Number(process.argv[4] || 300);
const port = 9491;
const EDGE = "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe";
const profile = mkdtempSync(join(tmpdir(), "cdp-wake-"));
const child = spawn(EDGE, ["--headless=new", "--disable-gpu", "--no-first-run",
  // ⚠ 绕开本机系统代理：代理出口被 Streamlit 拉黑，线上会 HTTP 444
  //   "source IP address not allowed"（详见 cdp_dblclick_pick.mjs 里的注释）
  "--no-proxy-server",
  `--remote-debugging-port=${port}`, `--user-data-dir=${profile}`,
  // ⚠ 窗口要高：侧边栏底部的「构建版本」在 1100 高的视口外，截不到就没法确认部署。
  "--window-size=1500,2400", "--hide-scrollbars", "about:blank"], { stdio: "ignore" });
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
let id = 0; const pending = new Map(); const consoleErrors = [];
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

const SNAPSHOT = `(() => {
  const pick = (el) => (el ? (el.innerText || el.textContent || '') : '');
  const app = document.querySelector('[data-testid="stAppViewContainer"]') || document.querySelector('.stApp');
  const txt = [pick(app), pick(document.body), pick(document.documentElement)]
                .sort((a,b) => b.length - a.length)[0] || '';
  const spinner = document.querySelector('.stSpinner, [data-testid="stSpinner"], [data-testid="stStatusWidget"]');
  const wake = [...document.querySelectorAll('button')]
      .find(b => /get this app back up|wake (it|this)/i.test(b.innerText || ''));
  return { len: txt.length, text: txt, mounted: !!app, spinner: !!spinner,
           sleeping: !!wake, iframes: document.querySelectorAll('iframe').length };
})()`;

// ---- 第一步：发现休眠壳就点醒它 ----
let woke = false;
let snap = { len: 0, text: "", mounted: false, spinner: false, sleeping: false };
const wakeDeadline = Date.now() + 60000;
while (Date.now() < wakeDeadline) {
  snap = (await evaluate(SNAPSHOT)) || snap;
  if (snap.mounted && snap.len > 200) break;                 // 已经是醒着的
  if (snap.sleeping) {
    const clicked = await evaluate(`(() => {
      const b = [...document.querySelectorAll('button')]
        .find(x => /get this app back up|wake (it|this)/i.test(x.innerText || ''));
      if (!b) return false;
      b.click();
      return true;
    })()`);
    if (clicked) { woke = true; console.log("→ 已点击「唤醒」按钮，等冷启动…"); break; }
  }
  await sleep(2000);
}
if (!woke) console.log("→ 无需唤醒（应用已是醒着的）");

// ---- 第二步：等应用真正渲染完（冷启动可能要几分钟）----
const deadline = Date.now() + maxSec * 1000;
let tick = 0;
while (Date.now() < deadline) {
  await sleep(8000);
  tick++;
  snap = (await evaluate(SNAPSHOT)) || snap;
  console.log(`[${tick * 8}s] mounted=${snap.mounted} spinner=${snap.spinner} len=${snap.len} ` +
              `head="${(snap.text || '').replace(/\s+/g, ' ').slice(0, 80)}"`);
  if (snap.mounted && !snap.spinner && snap.len > 200) break;
}
await sleep(6000);        // 稳一会儿，让地图/iframe 把瓦片画上

const final = (await evaluate(SNAPSHOT)) || snap;
const txt = final.text || "";
const info = await evaluate(`(() => {
  const q = (s) => document.querySelector(s);
  const txt = ${JSON.stringify(txt)};
  return {
    hasError: txt.includes('AttributeError') || txt.includes('ImportError') || txt.includes('Traceback'),
    hasDegradedWarn: txt.includes('内置兜底数据') || txt.includes('不是最新版'),
    build: (txt.match(/构建版本\\s*\`?([0-9a-zA-Z.\\-]+)/) || [])[1] || null,
    configVer: (txt.match(/模块版本：\`?([^\`）)]+)/) || [])[1] || null,
    storageSupabase: txt.includes('Supabase 云端'),
    storageLocal: txt.includes('本地临时存储'),
    leafletTop: !!q('.leaflet-container'),
    iframes: document.querySelectorAll('iframe').length,
    buttons: [...document.querySelectorAll('button')].map(b=>(b.innerText||'').trim()).filter(Boolean).slice(0,12),
  };
})()`);

// ⚠ **不要用 `captureBeyondViewport`**：在 headless=new 下它会挂住不返回
//   （2026-10-04 实测连续两次卡死到超时）。改用**高窗口**
//   （启动参数 `--window-size=1500,2400`）就能拿到侧边栏底部的「构建版本」。
const { result: shot } = await send("Page.captureScreenshot", { format: "png" }, sid);
if (shot?.data) writeFileSync(outPng, Buffer.from(shot.data, "base64"));

console.log(JSON.stringify({
  url, woke, textLen: final.len, mounted: final.mounted, spinner: final.spinner,
  ...info, consoleErrors: consoleErrors.slice(0, 10),
  bodyHead: txt.replace(/\s+/g, ' ').slice(0, 600), screenshot: outPng,
}, null, 2));
try { child.kill(); } catch {}
process.exit(0);
