// 列出线上投稿页的**表单元素**（在"应用所在的那个执行上下文"里查）。
//
// 关键前提（2026-10-04 查明）：线上页面有多个执行上下文，
// **默认上下文只是外层空壳**（bodyLen=0、stApp=0），
// 真正的应用在一个子上下文里（href 形如 `.../~/+/`，stApp=1、225 个 testid）。
// 之前几次"线上 DOM 假阴性"的结论都是因为读错了上下文 —— 不是 DOM 不可访问。
//
// 本脚本自动定位该上下文（以 `[data-testid="stAppViewContainer"]` 存在为准），
// 然后列出所有 input / textarea / button / radio / checkbox 及其包围盒，
// 供后续用 CDP 的真实鼠标键盘事件填写（而不是直接改 value —— React 不认）。
//
// 用法: node tools/cdp_live_form.mjs [url]
import { spawn } from "node:child_process";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const BASE = "https://azzjin9anwdbyykyd3dyh6.streamlit.app/";
const _arg = process.argv[2] || "";
// 传路径（如 "管理员"）就自动拼 + 编码；传完整 URL 就直接用
const url = _arg.startsWith("http") ? _arg : BASE + encodeURIComponent(_arg);
const port = 9571;
const EDGE = "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe";
const profile = mkdtempSync(join(tmpdir(), "cdp-form-"));
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
    contexts.push({ id: c.id, origin: c.origin, frameId: c.auxData ? c.auxData.frameId : undefined });
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
    { expression: expr, returnByValue: true, contextId: ctxId }, sid);
  if (r.result?.exceptionDetails) {
    return { __err: r.result.exceptionDetails.exception?.description
                    || r.result.exceptionDetails.text };
  }
  return r.result?.result?.value;
};

// ---- 定位"应用所在上下文"：以 stAppViewContainer 存在为准 ----
let appCtx = null;
for (let i = 0; i < 30; i++) {
  await sleep(3000);
  for (const c of contexts.slice()) {
    const has = await evIn(
      "(!!document.querySelector('[data-testid=\"stAppViewContainer\"]'))", c.id);
    if (has === true) { appCtx = c; break; }
  }
  if (appCtx) break;
}
if (!appCtx) {
  console.log("✗ 没找到应用上下文（页面可能还没渲染完）");
  try { child.kill(); } catch {}
  process.exit(1);
}
console.log(`应用上下文: id=${appCtx.id} origin=${appCtx.origin}`);

// ⚠ Streamlit 是**渐进渲染**：`stAppViewContainer` 出现时只有上半页
//   （标题 + 校区选择），**地图和后半张表单（上传/提交）还没出来**。
//   第一版就是在这里踩的坑 —— 列出来的元素只有 3 个校区 radio，
//   差点误判成"线上表单不可自动化"。
//   判据改成等**上传控件**出现（它是表单后半段的标志）。
let full = false;
for (let i = 0; i < 30; i++) {
  for (const c of contexts.slice()) {
    const has = await evIn("(!!document.querySelector('input[type=file]'))", c.id);
    if (has === true) { appCtx = c; full = true; break; }
  }
  if (full) break;
  await sleep(4000);
}
console.log(full ? "整页就绪 ✓（上传控件已出现）" : "⚠ 等了 120s 仍未见上传控件\n");

const meta = await evIn(`(() => ({
  topIsSelf: (window.top === window),
  href: location.href,
  innerW: window.innerWidth, innerH: window.innerHeight,
  scrollY: window.scrollY, docH: document.documentElement.scrollHeight,
}))()`, appCtx.id);
console.log("页面:", JSON.stringify(meta), "\n");

const LIST = `(() => {
  const out = [];
  const rect = (el) => { const r = el.getBoundingClientRect();
    return [Math.round(r.left), Math.round(r.top), Math.round(r.width), Math.round(r.height)]; };
  const lab = (el) => {
    let p = el.closest('[data-testid^="st"]');
    let s = p ? (p.innerText || '') : '';
    return s.replace(/\\s+/g, ' ').slice(0, 46);
  };
  for (const el of document.querySelectorAll('input, textarea')) {
    out.push({ k: 'input', type: el.type || 'textarea', label: lab(el),
               ph: el.getAttribute('placeholder') || '',
               val: String(el.value || '').slice(0, 30),
               checked: el.checked === true, r: rect(el) });
  }
  for (const el of document.querySelectorAll('[data-testid="stCheckbox"]')) {
    out.push({ k: 'checkbox', label: (el.innerText || '').replace(/\\s+/g, ' ').slice(0, 46),
               r: rect(el) });
  }
  for (const el of document.querySelectorAll('[data-testid="stRadio"] label')) {
    out.push({ k: 'radio-label', label: (el.innerText || '').trim().slice(0, 20), r: rect(el) });
  }
  for (const el of document.querySelectorAll('button')) {
    out.push({ k: 'button', label: (el.innerText || '').trim().slice(0, 24), r: rect(el) });
  }
  return out;
})()`;

const items = await evIn(LIST, appCtx.id);
console.log("=== 表单元素 ===");
for (const it of (items || [])) {
  console.log(`  ${it.k.padEnd(12)} ${JSON.stringify(it.r).padEnd(24)} `
    + `type=${String(it.type || '-').padEnd(9)} ph=${JSON.stringify(it.ph || '')} `
    + `val=${JSON.stringify(it.val || '')} | ${it.label}`);
}

const { result: shot } = await send("Page.captureScreenshot", { format: "png" }, sid);
const outPng = "C:\\Users\\Administrator\\.dsh\\sky_map\\data\\tile_check\\live_form.png";
if (shot?.data) {
  const { writeFileSync } = await import("node:fs");
  writeFileSync(outPng, Buffer.from(shot.data, "base64"));
}
console.log("\n截图:", outPng);
try { child.kill(); } catch {}
process.exit(0);
