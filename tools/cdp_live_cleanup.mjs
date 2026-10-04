// 管理后台：登录 + （可选）清理测试投稿。
//
// 用法:
//   node tools/cdp_live_cleanup.mjs <口令>            # 只登录 + 列出页面元素（不改任何数据）
//   node tools/cdp_live_cleanup.mjs <口令> --delete   # 登录后删除标题含「链路自测」的投稿
//
// ⚠ 口令**从命令行传**，不要写进这个文件（会被提交进 git）。
// ⚠ 只删标题含 MARK 的记录，避免误删真实投稿。
import { spawn } from "node:child_process";
import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const PASSWORD = process.argv[2];
const DO_DELETE = process.argv.includes("--delete");
const MARK = "链路自测";        // 我们那条测试投稿的标题标记
const BASE = "https://azzjin9anwdbyykyd3dyh6.streamlit.app/";
const url = BASE + encodeURIComponent("管理员");

if (!PASSWORD) {
  console.log("用法: node tools/cdp_live_cleanup.mjs <口令> [--delete]");
  process.exit(2);
}

const port = 9601;
const EDGE = "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe";
const profile = mkdtempSync(join(tmpdir(), "cdp-clean-"));
const child = spawn(EDGE, ["--headless=new", "--disable-gpu", "--no-first-run",
  `--remote-debugging-port=${port}`, `--user-data-dir=${profile}`,
  "--window-size=1500,1800", "--hide-scrollbars", "about:blank"], { stdio: "ignore" });
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
  if (m.method === "Runtime.executionContextCreated")
    contexts.push({ id: m.params.context.id, origin: m.params.context.origin });
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
  if (r.result?.exceptionDetails)
    return { __err: r.result.exceptionDetails.exception?.description };
  return r.result?.result?.value;
};

// ---- 找应用上下文 + 等口令输入框 ----
let appCtx = null, hasPwd = false;
for (let i = 0; i < 60; i++) {
  await sleep(3000);
  const woke = await evIn(`(() => {
    const b = [...document.querySelectorAll('button')]
      .find(x => /get this app back up|wake (it|this)/i.test(x.innerText || ''));
    if (b) { b.click(); return true; }
    return false;
  })()`);
  if (woke) console.log("→ 应用在休眠，已点醒…");
  for (const c of contexts.slice()) {
    const p = await evIn("(!!document.querySelector('input[type=password]'))", c.id);
    if (p === true) { appCtx = c; hasPwd = true; break; }
  }
  if (hasPwd) break;
}
if (!hasPwd) { console.log("✗ 没等到口令输入框"); try { child.kill(); } catch {} process.exit(1); }
console.log(`应用上下文 ctx=${appCtx.id}，口令框已就绪`);

// ---- 登录 ----
const login = await evIn(`(() => {
  const el = document.querySelector('input[type=password]');
  if (!el) return 'no-input';
  const setter = Object.getOwnPropertyDescriptor(
    window.HTMLInputElement.prototype, 'value').set;
  setter.call(el, ${JSON.stringify(PASSWORD)});
  el.dispatchEvent(new Event('input', { bubbles: true }));
  el.dispatchEvent(new Event('change', { bubbles: true }));
  el.dispatchEvent(new KeyboardEvent('keydown',
    {key:'Enter', code:'Enter', keyCode:13, which:13, bubbles:true}));
  el.blur();
  return 'filled';
})()`, appCtx.id);
console.log("填口令:", login);
await sleep(6000);

const clickLogin = await evIn(`(() => {
  const b = [...document.querySelectorAll('button')]
    .find(x => (x.innerText || '').trim() === '登录');
  if (!b) return 'no-btn';
  b.click();
  return 'clicked';
})()`, appCtx.id);
console.log("点登录:", clickLogin);
await sleep(14000);

const after = await evIn(`(() => {
  const t = document.body.innerText || '';
  return {
    loggedIn: !document.querySelector('input[type=password]'),
    hasPwdBox: !!document.querySelector('input[type=password]'),
    text: t.replace(/\\s+/g, ' ').slice(0, 400),
  };
})()`, appCtx.id);
console.log("登录后:", JSON.stringify(after, null, 2).slice(0, 900));

// ---- 列出元素（供确认删除按钮长什么样）----
const els = await evIn(`(() => {
  const out = [];
  for (const el of document.querySelectorAll('button')) {
    const r = el.getBoundingClientRect();
    out.push({text: (el.innerText || '').trim().slice(0, 26),
              r: [Math.round(r.left), Math.round(r.top)]});
  }
  return out;
})()`, appCtx.id);
console.log("\n按钮列表:", JSON.stringify(els));

if (!DO_DELETE) {
  const { result: shot } = await send("Page.captureScreenshot", { format: "png" }, sid);
  const outPng = "C:\\Users\\Administrator\\.dsh\\sky_map\\data\\tile_check\\admin_explore.png";
  if (shot?.data) writeFileSync(outPng, Buffer.from(shot.data, "base64"));
  console.log("\n（未加 --delete，不做任何修改）截图:", outPng);
  try { child.kill(); } catch {}
  process.exit(0);
}

// ---- 删除标记记录 ----
console.log(`\n=== 开始删除含「${MARK}」的投稿 ===`);
let deleted = 0;
for (let round = 0; round < 6; round++) {
  const res = await evIn(`(() => {
    const t = document.body.innerText || '';
    if (!t.includes(${JSON.stringify(MARK)})) return {found: false};
    // 找到含标记的那张卡片，点它附近的删除按钮
    const nodes = [...document.querySelectorAll('[data-testid="stExpander"], [data-testid="stVerticalBlock"]')];
    for (const n of nodes) {
      if (!(n.innerText || '').includes(${JSON.stringify(MARK)})) continue;
      const btn = [...n.querySelectorAll('button')]
        .find(b => /删除|移除|Delete/.test(b.innerText || ''));
      if (btn) { btn.click(); return {found: true, clicked: btn.innerText.trim()}; }
    }
    // 退而求其次：整页找带"删除"的按钮
    const any = [...document.querySelectorAll('button')]
      .find(b => /删除|移除/.test(b.innerText || ''));
    if (any) { any.click(); return {found: true, clicked: any.innerText.trim()}; }
    return {found: true, clicked: null};
  })()`, appCtx.id);
  console.log(`  第 ${round + 1} 轮:`, JSON.stringify(res));
  if (!res || res.found !== true) break;
  if (res.clicked) deleted++;
  await sleep(9000);
  // 可能的二次确认
  await evIn(`(() => {
    const b = [...document.querySelectorAll('button')]
      .find(x => /确认|确定|是的|删除/.test(x.innerText || ''));
    if (b) b.click();
    return true;
  })()`, appCtx.id);
  await sleep(9000);
  const still = await evIn(`(document.body.innerText || '').includes(${JSON.stringify(MARK)})`,
                           appCtx.id);
  if (still !== true) break;
}

const final = await evIn(`(() => {
  const t = document.body.innerText || '';
  return {stillHasMark: t.includes(${JSON.stringify(MARK)}),
          text: t.replace(/\\s+/g, ' ').slice(0, 300)};
})()`, appCtx.id);
console.log("\n删除后:", JSON.stringify(final, null, 2));

const { result: shot2 } = await send("Page.captureScreenshot", { format: "png" }, sid);
const outPng2 = "C:\\Users\\Administrator\\.dsh\\sky_map\\data\\tile_check\\admin_after_delete.png";
if (shot2?.data) writeFileSync(outPng2, Buffer.from(shot2.data, "base64"));
console.log("截图:", outPng2);
try { child.kill(); } catch {}
process.exit(final && final.stillHasMark === false ? 0 : 1);
