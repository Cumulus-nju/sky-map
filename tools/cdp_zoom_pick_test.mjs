// 验证静态底图"缩放后点选"是否仍然准确 —— 这是本次改动最大的风险点。
//
// 做三件事：
//   1. 用滚轮放大到 z=2，确认视图真的变了（窗口变小 = 图像被拉大）；
//   2. 在放大后的视图上点一下，读出回传的 pick；
//   3. 按"当前窗口 + 视口缩放比"独立回算应有的经纬度，与 pick 比对。
//
// 用法: node tools/cdp_zoom_pick_test.mjs <app_url> [outPng]
import { spawn } from "node:child_process";
import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const url = process.argv[2] || "http://localhost:8501/";
const outPng = process.argv[3] || join(tmpdir(), "zoom.png");
const port = 9531;
const EDGE = "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe";
const profile = mkdtempSync(join(tmpdir(), "cdp-zoom-"));
const child = spawn(EDGE, ["--headless=new", "--disable-gpu", "--no-first-run",
  `--remote-debugging-port=${port}`, `--user-data-dir=${profile}`,
  "--window-size=1500,1100", "--hide-scrollbars", "about:blank"], { stdio: "ignore" });
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
  if (m.method === "Runtime.consoleAPICalled" && m.params.type === "error")
    errs.push((m.params.args || []).map(a => a.value ?? a.description).join(" "));
  if (m.method === "Runtime.exceptionThrown")
    errs.push("EXC " + (m.params.exceptionDetails?.exception?.description || "?").slice(0, 200));
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

// 等组件就绪
let ready = false;
for (let i = 0; i < 30; i++) {
  await sleep(3000);
  ready = await ev(`(() => {
    for (const f of document.querySelectorAll('iframe')) {
      try { const d = f.contentDocument; if (d && d.getElementById('frame')) return true; } catch (e) {}
    }
    return false;
  })()`);
  if (ready) break;
}
console.log("组件就绪:", ready);
await sleep(3000);

// 组件在页面坐标里的位置
const geo = await ev(`(() => {
  for (const f of document.querySelectorAll('iframe')) {
    try {
      const d = f.contentDocument; const w = d && d.getElementById('frame');
      if (!w) continue;
      const fr = f.getBoundingClientRect(), wr = w.getBoundingClientRect();
      return { found: true, pageLeft: fr.left + wr.left, pageTop: fr.top + wr.top,
               w: wr.width, h: wr.height,
               imgW: d.getElementById('base').naturalWidth,
               imgH: d.getElementById('base').naturalHeight };
    } catch (e) { return { err: String(e) }; }
  }
  return { found: false };
})()`);
console.log("组件几何:", JSON.stringify(geo));

// 地图变高后可能超出视口，交互事件必须落在**视口内** —— 所以先把地图滚进视口中央，
// 再按"滚动后"的位置算坐标。（不这么做的话事件会落到页面外，静默无效。）
await ev(`(() => {
  for (const f of document.querySelectorAll('iframe')) {
    try { const d = f.contentDocument; if (d && d.getElementById('frame')) {
      f.scrollIntoView({block: 'center'}); return true; } } catch (e) {}
  }
  return false;
})()`);
await sleep(2500);

const geo2 = await ev(`(() => {
  for (const f of document.querySelectorAll('iframe')) {
    try {
      const d = f.contentDocument; const w = d && d.getElementById('frame');
      if (!w) continue;
      const r = w.getBoundingClientRect();
      return { pageLeft: r.left, pageTop: r.top, w: r.width, h: r.height,
               vh: window.innerHeight, vw: window.innerWidth,
               inView: r.top > 0 && r.top + r.height < window.innerHeight };
    } catch (e) { return { err: String(e) }; }
  }
  return { err: 'not found' };
})()`);
console.log("滚动后几何:", JSON.stringify(geo2));

const cx0 = geo2.pageLeft + geo2.w / 2, cy0 = geo2.pageTop + geo2.h / 2;

// ---- 1) 滚轮放大到最大 ----
for (let i = 0; i < 4; i++) {
  await send("Input.dispatchMouseEvent", {
    type: "mouseWheel", x: Math.round(cx0), y: Math.round(cy0),
    deltaX: 0, deltaY: -120, button: "none",
  }, sid);
  await sleep(700);
}
await sleep(6000);   // 等 rerun 与状态回写

const navState = await ev(`(() => {
  const p = new URLSearchParams(location.search);
  return { pnav: p.get('pnav'), pick: p.get('pick') };
})()`);
console.log("放大后 URL:", JSON.stringify(navState));

// 读组件内部真实状态（缩放级与窗口）
const inner = await ev(`(() => {
  for (const f of document.querySelectorAll('iframe')) {
    try {
      const d = f.contentDocument; const w = d && d.getElementById('frame');
      if (!w) continue;
      const st = d.getElementById('stage');
      const img = d.getElementById('base');
      return { transform: st.style.transform, imgW: img.width, imgH: img.height,
               frameW: w.clientWidth, frameH: w.clientHeight,
               hint: (d.getElementById('hint')||{}).textContent };
    } catch (e) { return { err: String(e) }; }
  }
  return { err: 'not found' };
})()`);
console.log("组件内部:", JSON.stringify(inner));

// ---- 2) 在放大后的视图上**右键**选点（左键现在只负责拖动）----
const fx = 0.62, fy = 0.38;
const px = geo2.pageLeft + geo2.w * fx, py = geo2.pageTop + geo2.h * fy;
// 右键：button=right, buttons=2
await send("Input.dispatchMouseEvent", {
  type: "mousePressed", x: Math.round(px), y: Math.round(py),
  button: "right", buttons: 2, clickCount: 1,
}, sid);
await send("Input.dispatchMouseEvent", {
  type: "mouseReleased", x: Math.round(px), y: Math.round(py),
  button: "right", buttons: 0, clickCount: 1,
}, sid);
await sleep(9000);

const after = await ev(`(() => {
  const p = new URLSearchParams(location.search);
  const t = document.body.innerText || '';
  return {
    pick: p.get('pick'), pnav: p.get('pnav'),
    stored: (t.match(/已选机位：[^\\n]*/) || [])[0] || null,
  };
})()`);
console.log("点击后:", JSON.stringify(after, null, 2));

const { result: shot } = await send("Page.captureScreenshot", { format: "png" }, sid);
if (shot?.data) writeFileSync(outPng, Buffer.from(shot.data, "base64"));
console.log("截图 ->", outPng);
console.log("控制台错误:", errs.length ? errs.slice(0, 5) : "（无）");
try { child.kill(); } catch {}
process.exit(0);

