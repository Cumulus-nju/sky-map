// 探测：线上页面里，**主页面执行上下文**能不能读到 Streamlit 的 DOM？
//
// 背景：之前用默认 `Runtime.evaluate` 在线上始终只读到 2989 字符（GTM 脚本），
// `[data-testid="stAppViewContainer"]` 也查不到，一度以为是"线上 DOM 不可访问"。
// 但截图明明显示应用渲染完整。怀疑是**执行上下文选错了**（页面里有多个 context，
// 默认那个可能不是主文档）。
//
// 本脚本列出所有上下文，并在**每个**上下文里试查 Streamlit 的关键元素，
// 从而确定：要自动化填表，应该往哪个 context 里发指令。
//
// 用法: node tools/cdp_live_dom.mjs [url]
import { spawn } from "node:child_process";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const BASE = "https://azzjin9anwdbyykyd3dyh6.streamlit.app/";
const _arg = process.argv[2] || "";
// 传路径（如 "打卡点地图"）就自动拼 + 编码；传完整 URL 就直接用
const url = _arg.startsWith("http") ? _arg : BASE + encodeURIComponent(_arg);
const port = 9561;
const EDGE = "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe";
const profile = mkdtempSync(join(tmpdir(), "cdp-dom-"));
const child = spawn(EDGE, ["--headless=new", "--disable-gpu", "--no-first-run",
  `--remote-debugging-port=${port}`, `--user-data-dir=${profile}`,
  "--window-size=1500,1700", "--hide-scrollbars", "about:blank"], { stdio: "ignore" });
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

const contexts = [];
let id = 0; const pending = new Map();
ws.onmessage = (ev) => {
  const m = JSON.parse(ev.data);
  if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); return; }
  if (m.method === "Runtime.executionContextCreated") {
    const c = m.params.context;
    contexts.push({ id: c.id, origin: c.origin, name: c.name,
                    frameId: c.auxData ? c.auxData.frameId : undefined });
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

const evIn = async (expr, ctxId) => {
  const r = await send("Runtime.evaluate",
    { expression: expr, returnByValue: true, ...(ctxId ? { contextId: ctxId } : {}) }, sid);
  if (r.result?.exceptionDetails) {
    return { __err: r.result.exceptionDetails.exception?.description
                    || r.result.exceptionDetails.text };
  }
  return r.result?.result?.value;
};

await sleep(45000);          // 给线上冷启动足够时间

const SURVEY = `(() => {
  const q = (s) => document.querySelectorAll(s).length;
  const fileInputs = document.querySelectorAll('input[type=file]').length;
  const textInputs = document.querySelectorAll('input[type=text], input[type=email], textarea').length;
  return {
    href: location.href.slice(0, 70),
    bodyLen: (document.body ? (document.body.innerText || '').length : -1),
    anyTestId: q('[data-testid]'),
    stApp: q('[data-testid="stAppViewContainer"]'),
    stApp2: q('.stApp'),
    buttons: q('button'),
    fileInputs: fileInputs,
    textInputs: textInputs,
    radio: q('[role=radiogroup], input[type=radio]'),
    checkbox: q('input[type=checkbox]'),
  };
})()`;

console.log("=== 上下文清单与各自的 DOM 情况 ===");
for (const c of contexts.slice()) {
  const r = await evIn(SURVEY, c.id);
  console.log(`\n-- ctx id=${c.id} origin=${c.origin} frame=${String(c.frameId).slice(0, 8)}`);
  console.log("   " + JSON.stringify(r));
}

console.log("\n=== 默认上下文（不带 contextId）===");
console.log("   " + JSON.stringify(await evIn(SURVEY)));

// ---- 顺带读出应用状态（投稿计数等）。用途：验证"投稿是否真的入库"。----
let appCtx = null;
for (const c of contexts.slice()) {
  const has = await evIn(
    "(!!document.querySelector('[data-testid=\"stAppViewContainer\"]'))", c.id);
  if (has === true) { appCtx = c; break; }
}
if (appCtx) {
  const st = await evIn(`(() => {
    const t = document.body.innerText || '';
    const g = (re) => (t.match(re) || [])[0] || null;
    return {
      url: location.href,
      received: g(/已收稿[\\s\\S]{0,14}?([0-9]+)\\s*幅/),
      located: g(/已自动定点[\\s\\S]{0,14}?([0-9]+)\\s*幅/),
      build: g(/构建版本\\s*[0-9A-Za-z.\\-]+/),
      storage: g(/存储：[^\\n]{0,24}/),
      hasSubmitWord: t.includes('提交投稿'),
    };
  })()`, appCtx.id);
  console.log("\n=== 应用状态（ctx " + appCtx.id + "）===");
  console.log(JSON.stringify(st, null, 2));

  const { result: shot } = await send("Page.captureScreenshot", { format: "png" }, sid);
  const outPng = "C:\\Users\\Administrator\\.dsh\\sky_map\\data\\tile_check\\live_state.png";
  if (shot?.data) {
    const { writeFileSync } = await import("node:fs");
    writeFileSync(outPng, Buffer.from(shot.data, "base64"));
    console.log("截图:", outPng);
  }
}

try { child.kill(); } catch {}
process.exit(0);
