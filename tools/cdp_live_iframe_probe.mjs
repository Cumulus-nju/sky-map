// 诊断：线上投稿页的选点组件 iframe 到底能不能访问 parent？
//
// 背景（2026-10-04）：CDP 从**父页面**访问 iframe 时报
//   SecurityError: Blocked a frame ... from accessing a cross-origin frame
// 而 `frame_picker` 的坐标回传**完全依赖** `window.parent.history.replaceState`
// （同源才行）。如果 iframe 真是跨域的，同学在线上就**选不了点**。
//
// 本脚本绕开同源限制：用 CDP 的 **executionContextId** 直接进入 iframe 的上下文执行 JS
// （CDP 是浏览器级的，不受同源策略约束），从而测出：
//   ① iframe 里有没有我们的 snapToFootprint；
//   ② 从 iframe 内部看 window.parent 能不能访问（= 回传是否可行）。
//
// 用法: node tools/cdp_live_iframe_probe.mjs [url]
import { spawn } from "node:child_process";
import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const url = process.argv[2] || "https://azzjin9anwdbyykyd3dyh6.streamlit.app/";
const port = 9551;
const EDGE = "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe";
const profile = mkdtempSync(join(tmpdir(), "cdp-probe-"));
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

const contexts = [];            // {id, origin, name, auxData}
let id = 0; const pending = new Map();
ws.onmessage = (ev) => {
  const m = JSON.parse(ev.data);
  if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); return; }
  if (m.method === "Runtime.executionContextCreated") {
    const c = m.params.context;
    contexts.push({ id: c.id, origin: c.origin, name: c.name,
                    isDefault: c.auxData ? c.auxData.isDefault : undefined,
                    frameId: c.auxData ? c.auxData.frameId : undefined });
  }
  if (m.method === "Runtime.executionContextDestroyed") {
    const i = contexts.findIndex(c => c.id === m.params.executionContextId);
    if (i >= 0) contexts.splice(i, 1);
  }
};
const send = (method, params = {}, s) => new Promise((res) => {
  const mid = ++id; pending.set(mid, res);
  ws.send(JSON.stringify({ id: mid, method, params, ...(s ? { sessionId: s } : {}) }));
});
const { result: t } = await send("Target.createTarget", { url: "about:blank" });
const { result: att } = await send("Target.attachToTarget", { targetId: t.targetId, flatten: true });
const sid = att.sessionId;
await send("Runtime.enable", {}, sid);          // 必须在 navigate 前开，才能收到 context 事件
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

// 等组件出现（线上冷启动可能要一两分钟）
let found = null;
for (let i = 0; i < 40; i++) {
  await sleep(3000);
  for (const c of contexts.slice()) {
    const t2 = await evIn("typeof snapToFootprint", c.id);
    if (t2 === "function") { found = c; break; }
  }
  if (found) break;
}

console.log("=== 所有执行上下文 ===");
for (const c of contexts) {
  const t2 = await evIn("typeof snapToFootprint", c.id);
  console.log(`  id=${c.id} origin=${c.origin} default=${c.isDefault} snap=${t2}`);
}
console.log("\n=== 组件上下文 ===", JSON.stringify(found));

if (!found) {
  console.log("\n✗ 没找到含 snapToFootprint 的上下文（页面可能还没渲染完）");
  try { child.kill(); } catch {}
  process.exit(1);
}

const probe = await evIn(`(() => {
  const out = {};
  out.href = location.href;
  out.origin = location.origin;
  out.parentIsSelf = (window.parent === window);
  try { out.parentHref = window.parent.location.href; out.parentOk = true; }
  catch (e) { out.parentOk = false; out.parentErr = String(e).slice(0, 120); }
  try { window.parent.history.replaceState({}, '', '?probe=1'); out.replaceStateOk = true; }
  catch (e) { out.replaceStateOk = false; out.replaceStateErr = String(e).slice(0, 120); }
  try { out.postMessageOk = (typeof window.parent.postMessage === 'function'); }
  catch (e) { out.postMessageOk = 'err:' + String(e).slice(0, 60); }
  return out;
})()`, found.id);

console.log("\n=== 从 iframe 内部看「能不能回传」===");
console.log(JSON.stringify(probe, null, 2));

const ok = probe && probe.replaceStateOk === true;
console.log(ok
  ? "\n✓ 回传可行（window.parent.history.replaceState 成功）"
  : "\n✗ **回传不可行** —— 同学在线上点选会把坐标丢掉（本地同源所以测不出来）");

// ---------------------------------------------------------------------------
// 既然能进 iframe 上下文，就顺手把**线上**的吸附端到端也验一遍：
// 派发 contextmenu → pin 应被吸附到楼基 → 页面应显示「已选机位」。
console.log("\n=== 线上端到端：派发 contextmenu 到屋顶 ===");
const pick = await evIn(`(() => {
  const R = D.roofs;
  if (!R || !R.b) return {err: 'no roofs'};
  for (let i = R.b.length - 1; i >= 0; i--) {
    const dz = R.b[i][0], flat = R.b[i][1], m = flat.length / 2;
    if (dz < 40) continue;
    let minx=1e9,maxx=-1e9,miny=1e9,maxy=-1e9;
    for (let k=0;k<m;k++){const x=flat[2*k],y=flat[2*k+1];
      if(x<minx)minx=x; if(x>maxx)maxx=x; if(y<miny)miny=y; if(y>maxy)maxy=y;}
    const px=(minx+maxx)/2, py=(miny+maxy)/2;
    if (!inPoly(px,py,flat)) continue;
    const g = snapToFootprint(px, py);
    const w = winW(), h = winH(), scale = vw()/w;
    const left = cx - w/2, top = cy - h/2;
    const sx = (px-left)*scale, sy = (py-top)*scale;
    if (sx < 5 || sy < 5 || sx > vw()-5 || sy > vh()-5) continue;
    const want = imgPxToLatLng(g[0], g[1]);
    const raw = imgPxToLatLng(px, py);
    frame.dispatchEvent(new MouseEvent('contextmenu',
      {clientX: sx, clientY: sy, bubbles: true, cancelable: true, button: 2}));
    return {dz: dz, imgPt: [px, py], snapPt: [g[0], g[1]],
            want: want, raw: raw, roofs: R.b.length,
            pin: [+pin.dataset.ix, +pin.dataset.iy],
            pinOn: pin.classList.contains('on')};
  }
  return {err: 'no suitable roof visible'};
})()`, found.id);
console.log(JSON.stringify(pick, null, 2));

if (pick && !pick.err) {
  const pinOk = pick.pinOn
    && Math.abs(pick.pin[0] - pick.snapPt[0]) < 1.0
    && Math.abs(pick.pin[1] - pick.snapPt[1]) < 1.0;
  const moved = Math.hypot(pick.snapPt[0] - pick.imgPt[0], pick.snapPt[1] - pick.imgPt[1]);
  console.log(`pin 吸附: ${pinOk ? 'OK' : '失败'}（偏移 ${moved.toFixed(1)} px，`
              + `期望 ${(pick.dz * Math.hypot(0.34, 1.0)).toFixed(1)} px）`);

  await sleep(14000);        // 等 Streamlit rerun
  const { result: shot } = await send("Page.captureScreenshot", { format: "png" }, sid);
  const outPng = "C:\\Users\\Administrator\\.dsh\\sky_map\\data\\tile_check\\live_snap_e2e.png";
  if (shot?.data) writeFileSync(outPng, Buffer.from(shot.data, "base64"));
  console.log("截图（看页面是否显示「已选机位」）:", outPng);
}

try { child.kill(); } catch {}
process.exit(ok ? 0 : 1);
