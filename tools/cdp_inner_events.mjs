// 在**组件 iframe 内部**派发真实事件，验证交互逻辑本身（不是验证 CDP 能否跨 iframe 送事件）。
//
// 背景：CDP 的 Input.dispatchMouseEvent 在这台机器上送不进 iframe
//（右键与左键拖动都静默无效），但组件自身的处理函数是好的 —— 用 JS 在 iframe 内
// 直接构造 MouseEvent/PointerEvent 并派发，才能真正测到逻辑。
//
// 验三件事：
//   1. 左键按住 + 移动 => 视图 transform 应发生变化（平移生效）
//   2. 右键 => 应写入 pick 查询参数，且 defaultPrevented 为 true（菜单被拦）
//   3. 滚轮 => 应写入 pnav 且 zoom 变化
//
// 用法: node tools/cdp_inner_events.mjs <app_url>
import { spawn } from "node:child_process";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const url = process.argv[2] || "http://localhost:8501/";
const port = 9581;
const EDGE = "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe";
const profile = mkdtempSync(join(tmpdir(), "cdp-ie-"));
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
ws.onmessage = (ev) => { const m = JSON.parse(ev.data);
  if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); return; }
  if (m.method === "Runtime.exceptionThrown")
    errs.push((m.params.exceptionDetails?.exception?.description || "?").slice(0, 160));
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
  if (r.result?.exceptionDetails) return { __err: r.result.exceptionDetails.text };
  return r.result?.result?.value;
};

for (let i = 0; i < 30; i++) {
  await sleep(3000);
  if (await ev(`(() => { for (const f of document.querySelectorAll('iframe')) {
      try { const d=f.contentDocument; if (d && d.getElementById('frame')) return true; } catch(e){} }
    return false; })()`)) break;
}
await sleep(4000);

// 统一的"在组件内派发事件"辅助脚本（注入到页面上下文执行）
const RUN = (body) => `(() => {
  for (const f of document.querySelectorAll('iframe')) {
    try {
      const d = f.contentDocument, w = f.contentWindow;
      const fr = d && d.getElementById('frame');
      if (!fr) continue;
      const st = d.getElementById('stage');
      const r = fr.getBoundingClientRect();
      const cx = r.left + r.width / 2, cy = r.top + r.height / 2;
      ${body}
    } catch (e) { return { err: String(e) }; }
  }
  return { err: 'no frame' };
})()`;

// ---- 1) 左键拖动 ----
const drag = await ev(`(() => {
  for (const f of document.querySelectorAll('iframe')) {
    try {
      const d = f.contentDocument, w = f.contentWindow;
      const fr = d.getElementById('frame'), st = d.getElementById('stage');
      if (!fr) continue;
      const r = fr.getBoundingClientRect();
      const cx = r.left + r.width / 2, cy = r.top + r.height / 2;
      const before = st.style.transform;
      const mk = (type, x, y, extra) => new w.PointerEvent(type, Object.assign({
        bubbles: true, cancelable: true, clientX: x, clientY: y,
        button: 0, buttons: 1, pointerId: 1, pointerType: 'mouse', isPrimary: true }, extra || {}));
      fr.dispatchEvent(mk('pointerdown', cx, cy));
      for (let i = 1; i <= 5; i++) fr.dispatchEvent(mk('pointermove', cx + i * 20, cy + i * 12));
      fr.dispatchEvent(mk('pointerup', cx + 100, cy + 60, { buttons: 0 }));
      return { ran: true, before, after: st.style.transform };
    } catch (e) { return { err: String(e) }; }
  }
  return { err: 'no frame' };
})()`);
await sleep(6000);
const navAfterDrag = await ev(`new URLSearchParams(location.search).get('pnav')`);
console.log("① 左键拖动:", JSON.stringify(drag), " pnav =", navAfterDrag);

// ---- 2) 右键选点 ----
const rc = await ev(`(() => {
  for (const f of document.querySelectorAll('iframe')) {
    try {
      const d = f.contentDocument, w = f.contentWindow;
      const fr = d.getElementById('frame');
      if (!fr) continue;
      const r = fr.getBoundingClientRect();
      const evt = new w.MouseEvent('contextmenu', { bubbles: true, cancelable: true,
        clientX: r.left + r.width * 0.5, clientY: r.top + r.height * 0.5, button: 2, buttons: 2 });
      fr.dispatchEvent(evt);
      return { ran: true, defaultPrevented: evt.defaultPrevented };
    } catch (e) { return { err: String(e) }; }
  }
  return { err: 'no frame' };
})()`);
await sleep(8000);
const afterPick = await ev(`(() => { const q = new URLSearchParams(location.search);
  const t = document.body.innerText || '';
  return { pick: q.get('pick'), pnav: q.get('pnav'),
           stored: (t.match(/已选机位：[^\\n]*/) || [])[0] || null }; })()`);
console.log("② 右键选点:", JSON.stringify(rc));
console.log("   结果:", JSON.stringify(afterPick));

// ---- 3) 滚轮缩放 ----
const wh = await ev(`(() => {
  for (const f of document.querySelectorAll('iframe')) {
    try {
      const d = f.contentDocument, w = f.contentWindow;
      const fr = d.getElementById('frame'), st = d.getElementById('stage');
      if (!fr) continue;
      const r = fr.getBoundingClientRect();
      const before = st.style.transform;
      const evt = new w.WheelEvent('wheel', { bubbles: true, cancelable: true,
        clientX: r.left + r.width / 2, clientY: r.top + r.height / 2, deltaY: -120, deltaMode: 0 });
      fr.dispatchEvent(evt);
      return { ran: true, before, after: st.style.transform };
    } catch (e) { return { err: String(e) }; }
  }
  return { err: 'no frame' };
})()`);
await sleep(6000);
const navAfterWheel = await ev(`new URLSearchParams(location.search).get('pnav')`);
console.log("③ 滚轮缩放:", JSON.stringify(wh), " pnav =", navAfterWheel);

console.log("\n控制台异常:", errs.length ? errs.slice(0, 4) : "（无）");
try { child.kill(); } catch {}
process.exit(0);
