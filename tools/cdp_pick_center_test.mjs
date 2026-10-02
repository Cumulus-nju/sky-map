// 严格验证：在视图**正中**右键选点，回传的 pick 必须等于当前视图中心（pnav）。
//
// 为什么这是个好判据：视图中心是组件自己算出来的已知量，而 pick 走的是
// "屏幕坐标 -> 原图像素 -> 经纬度"这条路径。若这条路径哪里差了一个偏移，
// 正中点击就会暴露出来（其它位置只能靠我手算对比，容易算错）。
//
// 用法: node tools/cdp_pick_center_test.mjs <app_url> [outPng]
import { spawn } from "node:child_process";
import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const url = process.argv[2] || "http://localhost:8501/";
const outPng = process.argv[3] || join(tmpdir(), "center.png");
const port = 9551;
const EDGE = "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe";
const profile = mkdtempSync(join(tmpdir(), "cdp-ctr-"));
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
  if (m.method === "Runtime.exceptionThrown")
    errs.push("EXC " + (m.params.exceptionDetails?.exception?.description || "?").slice(0, 160));
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

for (let i = 0; i < 30; i++) {
  await sleep(3000);
  if (await ev(`(() => { for (const f of document.querySelectorAll('iframe')) {
      try { const d=f.contentDocument; if (d && d.getElementById('frame')) return true; } catch(e){} }
    return false; })()`)) break;
}
await sleep(3000);

const info = await ev(`(() => {
  for (const f of document.querySelectorAll('iframe')) {
    try {
      const d = f.contentDocument, w = d && d.getElementById('frame');
      if (!w) continue;
      const r = w.getBoundingClientRect();
      return { left: r.left, top: r.top, w: r.width, h: r.height,
               vw: window.innerWidth, vh: window.innerHeight };
    } catch (e) { return { err: String(e) }; }
  }
  return { err: 'not found' };
})()`);
console.log("组件几何:", JSON.stringify(info));

// 点正中
const cx = Math.round(info.left + info.w / 2);
const cy = Math.round(info.top + info.h / 2);
await send("Input.dispatchMouseEvent", {
  type: "mousePressed", x: cx, y: cy, button: "right", buttons: 2, clickCount: 1 }, sid);
await send("Input.dispatchMouseEvent", {
  type: "mouseReleased", x: cx, y: cy, button: "right", buttons: 0, clickCount: 1 }, sid);
await sleep(9000);

const out = await ev(`(() => {
  const p = new URLSearchParams(location.search);
  const t = document.body.innerText || '';
  return { pick: p.get('pick'), pnav: p.get('pnav'),
           stored: (t.match(/已选机位：[^\\n]*/) || [])[0] || null };
})()`);

console.log("点击正中:", cx, cy);
console.log("结果:", JSON.stringify(out, null, 2));
if (out.pick && out.pnav) {
  const [plat, plon] = out.pick.split(",").map(Number);
  const [nlat, nlon] = out.pnav.split(",").map(Number);
  const mPerDegLat = 111132.0;
  const mPerDegLon = 111320.0 * Math.cos(nlat * Math.PI / 180);
  const dLat = Math.abs(plat - nlat) * mPerDegLat;
  const dLon = Math.abs(plon - nlon) * mPerDegLon;
  console.log(`\n正中点击 vs 视图中心：lat 差 ${dLat.toFixed(2)} m, lon 差 ${dLon.toFixed(2)} m`);
  console.log(dLat < 2 && dLon < 2 ? "✅ 完全吻合（应为 0，注意 4 位小数的量化约 11 m）"
                                  : "⚠ 偏差偏大，需查映射");
}
const { result: shot } = await send("Page.captureScreenshot", { format: "png" }, sid);
if (shot?.data) writeFileSync(outPng, Buffer.from(shot.data, "base64"));
console.log("控制台异常:", errs.length ? errs.slice(0, 4) : "（无）");
try { child.kill(); } catch {}
process.exit(0);
