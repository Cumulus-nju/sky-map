// **双击选点**的端到端验证（2026-10-05 新增手势：桌面双击 / 手机双击轻点）。
//
// 为什么要单独一个脚本：把选点从"右键"改成"双击"以后，**入口事件换了**。
// 函数级测试（snapToFootprint / inPoly 那些）全绿也证明不了"同学双击一下真能选中"——
// 计数窗口、位移阈值、"拖动后作废连击"任何一处写错，症状都是**双击毫无反应**（静默）。
//
// 验六件事：
//   ① 单击一下 **不能** 选中（否则拖动收尾就会误选）
//   ② 快速双击 **能** 选中，且坐标 = 吸附后的楼基（不是屋顶）
//   ③ 点一下 → 拖动 → 再点一下 **不能** 选中（拖动作废连击）
//   ④ 真·触屏双击轻点（CDP `Input.dispatchTouchEvent` + 移动端机型仿真）**能** 选中
//      —— 这一条才是"手机用户到底交不交得了稿"的答案
//   ⑤ 真·触屏双指捏合：缩放级 +1 且两指中点下的地物不跑，并回传给 Streamlit
//   ⑤c 合成捏合（坐标完全可控）：锚点漂移必须 ≈0 —— 把"算法准不准"与
//      "浏览器触摸坐标微调带来的噪声"分开，否则量到的漂移说不清是谁的
//   ①b/④b 提示文案按设备给：桌面说"滚轮"、手机说"双指"，且与实现一致
//
// 用法: node tools/cdp_dblclick_pick.mjs [app_url]
import { spawn } from "node:child_process";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const url = process.argv[2] || "http://localhost:8501/";
const port = 9547;
const EDGE = "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe";
const profile = mkdtempSync(join(tmpdir(), "cdp-dblclick-"));
const child = spawn(EDGE, ["--headless=new", "--disable-gpu", "--no-first-run",
  `--remote-debugging-port=${port}`, `--user-data-dir=${profile}`,
  // ⚠ 窗口必须够高：选点组件在页面中部，视口太矮时算出的点击坐标会落到视口外，
  //   事件派发不进去 → pick 恒为 null（会被误判成"双击坏了"）。
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
let id = 0; const pending = new Map(); let errs = [];
ws.onmessage = (ev) => {
  const m = JSON.parse(ev.data);
  if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); return; }
  if (m.method === "Runtime.consoleAPICalled" && m.params.type === "error")
    errs.push((m.params.args || []).map(a => a.value ?? a.description).join(" "));
  if (m.method === "Runtime.exceptionThrown")
    errs.push("EXC " + (m.params.exceptionDetails?.exception?.description || "?"));
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

const ev = async (expr) => {
  const r = await send("Runtime.evaluate", { expression: expr, returnByValue: true }, sid);
  if (r.result?.exceptionDetails) return { __err: r.result.exceptionDetails.text };
  return r.result?.result?.value;
};

// ---------------- 在 iframe（组件）里求值 ----------------
// 组件是 `components.html` 造的 iframe，脚本跑在它自己的 realm 里，所以要在里面 eval。
// 用 `typeof doPick` 认门：比"取第 0 个 iframe"稳（页面可能有别的 iframe）。
const IN_FRAME = (inner) =>
  "(function(){ for (const f of document.querySelectorAll('iframe')) {"
  + " try { const w = f.contentWindow; if (!w) continue;"
  + "  if (w.eval(\"typeof doPick\") !== 'function') continue;"
  + "  const v = w.eval(" + JSON.stringify(inner) + ");"
  + "  const r = f.getBoundingClientRect();"
  + "  return {v: v, iframeRect: [r.left, r.top], found: true};"
  + " } catch (e) {} } return {found: false}; })()";

const frameEval = async (inner) => {
  const r = await ev(IN_FRAME(inner));
  if (!r || !r.found) return { __noframe: true };
  return r.v;
};

// 等组件就绪（Streamlit 首屏 + 组件 iframe 都要时间）
const waitReady = async (label) => {
  for (let i = 0; i < 30; i++) {
    await sleep(1500);
    const r = await ev(IN_FRAME("'ok'"));
    if (r && r.found) return true;
  }
  console.log(`✗ ${label}: 组件始终没就绪`);
  return false;
};

// 挑一栋"够高"的楼（位移太小的话 吸附前/后 分不开），给出：
// 屋顶内部点在 #frame 里的坐标、吸附后的原图像素、以及吸附前/后的经纬度。
const PICK_PROBE = [
  "(function(){",
  "  if (typeof D === 'undefined' || !D.roofs || !D.roofs.b) return {err:'no roofs'};",
  "  var R = D.roofs;",
  "  for (var i = R.b.length - 1; i >= 0; i--) {",
  "    var dz = R.b[i][0], flat = R.b[i][1], m = flat.length / 2;",
  "    if (dz < 40) continue;",
  "    var minx=1e9,maxx=-1e9,miny=1e9,maxy=-1e9;",
  "    for (var k=0;k<m;k++){var x=flat[2*k],y=flat[2*k+1];",
  "      if(x<minx)minx=x; if(x>maxx)maxx=x; if(y<miny)miny=y; if(y>maxy)maxy=y;}",
  "    var px=(minx+maxx)/2, py=(miny+maxy)/2;",
  "    if (!inPoly(px, py, flat)) continue;",
  "    var g = snapToFootprint(px, py);",
  "    var w = winW(), h = winH(), scale = vw() / w;",
  "    var left = cx - w / 2, top = cy - h / 2;",
  "    var sx = (px - left) * scale, sy = (py - top) * scale;",
  "    if (sx < 5 || sy < 5 || sx > vw() - 5 || sy > vh() - 5) continue;",
  "    var fr = frame.getBoundingClientRect();",
  "    return {dz: dz, imgPt: [px, py], inFrame: [sx, sy],",
  "            frameRect: [fr.left, fr.top], vp: [vw(), vh()],",
  "            snapPt: [g[0], g[1]],",
  "            want: imgPxToLatLng(g[0], g[1]),",
  "            raw: imgPxToLatLng(px, py)};",
  "  }",
  "  return {err:'no suitable roof visible'};",
  "})()",
].join("\n");

// ---------------- 合成 pointer 事件（验"逻辑"）----------------
// 用 dispatchEvent 而不是 CDP 合成鼠标事件的**理由不同**于右键那套：
// 这里要验的是我们自己写的"数两次轻点"这段逻辑，事件源是谁不重要；
// 而真·触屏那一环（④）会另外用 CDP 的真实输入管线单独验。
const seq = (parts) => "(function(){"
  + "tap.t = 0;"                                  // 每个用例前清掉连击残留，免得用例相互污染
  + "function pd(type,x,y,buttons){"
  + "  frame.dispatchEvent(new PointerEvent(type,{clientX:x,clientY:y,"
  + "    button:0,buttons:buttons,bubbles:true,cancelable:true,"
  + "    pointerId:1,pointerType:'mouse',isPrimary:true}));}"
  + parts
  + "function urlPick(){try{return new URL(window.parent.location.href)"
  + "  .searchParams.get('pick');}catch(e){return 'ERR:'+e;}}"
  + "return {on: pin.classList.contains('on'), pick: urlPick(),"
  + "        pin: (pin.dataset.ix===undefined?null:[+pin.dataset.ix,+pin.dataset.iy]),"
  + "        tapSet: tap.t !== 0};"
  + "})()";

const TAP = (x, y) => `pd('pointerdown',${x},${y},1);pd('pointerup',${x},${y},0);`;

// ---------------- 逐用例 ----------------
const results = [];
const record = (name, ok, detail) => {
  results.push({ name, ok });
  console.log(`${ok ? "✓" : "✗"} ${name}${detail ? "  " + detail : ""}`);
};

const navigate = async () => {
  await send("Page.navigate", { url }, sid);
};

// ============ ① 单击不能选中 / ② 双击能选中（同一次页面加载）============
await navigate();                                   // ⚠ 别忘了先导航（target 出生在 about:blank）
if (!await waitReady("① 双击")) { try { child.kill(); } catch {} process.exit(1); }
const info = await frameEval(PICK_PROBE);
console.log("挑中的屋顶:", JSON.stringify(info));
if (!info || info.err) { try { child.kill(); } catch {} process.exit(1); }
const [fx, fy] = info.inFrame;
console.log(`组件内坐标 (${fx.toFixed(1)}, ${fy.toFixed(1)})，吸附位移 ${info.dz.toFixed(0)} px`);

const single = await frameEval(seq(TAP(fx, fy)));
record("① 单击一下**不**选中", !!(single && !single.on && !single.pick),
  JSON.stringify(single));

// 桌面提示文案必须还是"滚轮/左键"那套（按设备给文案别把桌面也改了）
const tipDesk = await frameEval("hint.textContent");
record("①b 桌面提示 = 滚轮/左键那套", typeof tipDesk === "string" && tipDesk.indexOf("滚轮") >= 0
  && tipDesk.indexOf("双指") < 0, `"${tipDesk}"`);

// 双击：两次轻点必须落在 DBL_MS(350ms) 窗口内 —— 同一个 eval 里同步派发，间隔≈0
const dbl = await frameEval(seq(TAP(fx, fy) + TAP(fx, fy)));
const got = dbl && dbl.pick ? dbl.pick.split(",").map(Number) : null;
const want = info.want;
const dWant = got ? Math.max(Math.abs(got[0] - want[0]), Math.abs(got[1] - want[1])) : NaN;
const pinOk = !!(dbl && dbl.on && dbl.pin)
  && Math.abs(dbl.pin[0] - info.snapPt[0]) < 1.0
  && Math.abs(dbl.pin[1] - info.snapPt[1]) < 1.0;
record("② 双击选中，坐标 = 吸附后的楼基",
  !!got && dWant <= 1.5e-4 && pinOk,
  `pick=${dbl && dbl.pick} 期望≈${want.map(v => v.toFixed(4))} 差 ${dWant.toExponential(2)}° pinOk=${pinOk}`);

// ============ ③ 点一下→拖动→再点一下 不能选中 ============
// 拖动会 report(false) 触发 Streamlit rerun（组件会被重建），所以单独重载一次页面。
await navigate();
if (!await waitReady("③ 拖动作废连击")) { try { child.kill(); } catch {} process.exit(1); }
const info3 = await frameEval(PICK_PROBE);
if (!info3 || info3.err) { console.log("✗ ③ 挑不到屋顶:", JSON.stringify(info3)); try { child.kill(); } catch {} process.exit(1); }
const [gx, gy] = info3.inFrame;
const dragSeq = TAP(gx, gy)
  + `pd('pointerdown',${gx},${gy},1);`
  + `pd('pointermove',${gx + 4},${gy + 4},1);`   // > 3px 阈值 ⇒ 认定成拖动
  + `pd('pointerup',${gx + 4},${gy + 4},0);`
  + TAP(gx, gy);                                  // 紧接着再点一下：因为中间拖动过，不能算双击
const afterDrag = await frameEval(seq(dragSeq));
record("③ 拖动作废连击（拖完立刻点一下不算双击）",
  !!(afterDrag && !afterDrag.on && !afterDrag.pick), JSON.stringify(afterDrag));

// ============ ⑥ 缩放按钮（手机上的兜底缩放方式）============
// 为什么要单独验：本次把滚轮/双指/按钮的缩放**合并成了一个 zoomAtPoint**，
// 三处调用里按钮这条最容易漏（函数测不会覆盖"按钮的 click 处理器写没写对"）。
const btn = await frameEval(`(function(){
  const z0 = zoom;
  zin.dispatchEvent(new MouseEvent('click', {bubbles:true}));
  const z1 = zoom;
  zout.dispatchEvent(new MouseEvent('click', {bubbles:true}));
  const z2 = zoom;
  const rb = zin.getBoundingClientRect(), rb2 = zout.getBoundingClientRect();
  return {z0:z0, z1:z1, z2:z2,
          width:[Math.round(rb.width), Math.round(rb2.width)],
          opacity:[getComputedStyle(zin).opacity, getComputedStyle(zout).opacity]};
})()`);
record("⑥ 缩放按钮仍有效（+1 再 −1 回到原级）",
  !!(btn && btn.z1 === btn.z0 + 1 && btn.z2 === btn.z0), JSON.stringify(btn));

// ============ ④ 真·触屏双击轻点（移动端机型仿真）============
console.log("\n--- ④ 真触屏：390×844 机型 + Input.dispatchTouchEvent ---");
errs = [];
await send("Emulation.setDeviceMetricsOverride",
  { width: 390, height: 844, deviceScaleFactor: 3, mobile: true }, sid);
await send("Emulation.setTouchEmulationEnabled", { enabled: true, maxTouchPoints: 5 }, sid);
await navigate();
if (!await waitReady("④ 触屏双击")) { try { child.kill(); } catch {} process.exit(1); }

// 窄屏下地图很可能在首屏之外 —— 先把组件滚进视口，坐标才有意义
// （getBoundingClientRect 是**视口**坐标，滚动后会变，所以必须滚完再量）
const scrolled = await ev(
  "(function(){ for (const f of document.querySelectorAll('iframe')) {"
  + " try { if (f.contentWindow.eval(\"typeof doPick\") !== 'function') continue;"
  + "  f.scrollIntoView({block:'center'}); return true; } catch (e) {} } return false; })()");
await sleep(600);
const info4 = await frameEval(PICK_PROBE);
if (!info4 || info4.err) { console.log("✗ ④ 窄屏挑不到屋顶:", JSON.stringify(info4)); try { child.kill(); } catch {} process.exit(1); }

const rect4 = await ev(
  "(function(){ for (const f of document.querySelectorAll('iframe')) {"
  + " try { if (f.contentWindow.eval(\"typeof doPick\") !== 'function') continue;"
  + "  const r = f.getBoundingClientRect();"
  + "  return {left:r.left, top:r.top, innerW:window.innerWidth, innerH:window.innerHeight};"
  + " } catch (e) {} } return null; })()");
if (!rect4) {
  record("④ 真触屏双击轻点能选中", false, "窄屏下没找到组件 iframe");
  console.log("\n" + JSON.stringify(results, null, 1));
  try { child.kill(); } catch {}
  process.exit(1);
}
const px4 = Math.round(rect4.left + info4.frameRect[0] + info4.inFrame[0]);
const py4 = Math.round(rect4.top + info4.frameRect[1] + info4.inFrame[1]);
console.log(`scrolled=${scrolled} iframe=${JSON.stringify(rect4)}`);
console.log(`组件框 ${info4.vp.map(v => v.toFixed(0))}px，触点在页面 (${px4}, ${py4})`);

if (py4 < 4 || py4 > rect4.innerH - 4 || px4 < 4 || px4 > rect4.innerW - 4) {
  record("④ 真触屏双击轻点能选中", false,
    `触点 (${px4},${py4}) 落在 ${rect4.innerW}×${rect4.innerH} 视口外，滚动没把它带进来 —— 这是脚本的问题，不是组件的问题`);
} else {
  const touch = async (type, pts) =>
    send("Input.dispatchTouchEvent", { type, touchPoints: pts }, sid);
  const P = [{ x: px4, y: py4, radiusX: 8, radiusY: 8, force: 1 }];
  await touch("touchStart", P); await sleep(40); await touch("touchEnd", []);
  await sleep(90);                                  // 第二下要落在 350ms 窗口内
  await touch("touchStart", P); await sleep(40); await touch("touchEnd", []);
  const immediate = await frameEval(
    "(function(){try{return {on: pin.classList.contains('on'),"
    + " pick: new URL(window.parent.location.href).searchParams.get('pick')};}"
    + "catch(e){return {err:String(e)};}})()");
  console.log("触屏双击后（rerun 前）:", JSON.stringify(immediate));
  await sleep(10000);                               // 等 Streamlit rerun + 回显

  // pick 参数是**读走即删**（site_common.take_pick 里 clear_qp），所以 rerun 之后
  // URL 里已经没有了 —— 必须看页面渲染出来的「已选机位：lat, lon」。
  const shown = await ev(
    "(function(){ const t = document.body.innerText || '';"
    + " const m = t.match(/已选机位：([0-9.\\-]+),\\s*([0-9.\\-]+)/);"
    + " return {shown: m ? [parseFloat(m[1]), parseFloat(m[2])] : null, raw: m ? m[0] : null}; })()");
  const g4 = shown.shown;
  const d4 = g4 ? Math.max(Math.abs(g4[0] - info4.want[0]), Math.abs(g4[1] - info4.want[1])) : NaN;
  record("④ 真触屏双击轻点能选中", Number.isFinite(d4) && d4 <= 1.5e-4,
    `页面回显 ${shown.raw}，期望≈${info4.want.map(v => v.toFixed(4))}，差 ${d4}`);
  console.log("   consoleErrors:", JSON.stringify(errs.slice(0, 4)));

  // 手机上提示文案不能再写"滚轮"（手机没有滚轮），而且要提示"双指"——并且真的能用
  const tipPhone = await frameEval("hint.textContent");
  record("④b 手机提示 = 双指/单指那套",
    typeof tipPhone === "string" && tipPhone.indexOf("双指") >= 0
    && tipPhone.indexOf("滚轮") < 0, `"${tipPhone}"`);
}

// ============ ⑤ 真触屏双指捏合（手机上主要的缩放方式）============
// 判据两条：① 缩放级真的变了；② **两指中点下的地物没跑**（锚点算法对）。
// 第二条是关键 —— 缩放级变了但位置被带跑，用户会以为"地图自己跳了"。
console.log("\n--- ⑤ 真触屏双指捏合 ---");
await navigate();
if (!await waitReady("⑤ 双指捏合")) { try { child.kill(); } catch {} process.exit(1); }
await ev("(function(){ for (const f of document.querySelectorAll('iframe')) { try {"
  + " if (f.contentWindow.eval(\"typeof doPick\") !== 'function') continue;"
  + " f.scrollIntoView({block:'center'}); return true; } catch(e){} } return false; })()");
await sleep(700);

const rect5 = await ev(
  "(function(){ for (const f of document.querySelectorAll('iframe')) {"
  + " try { if (f.contentWindow.eval(\"typeof doPick\") !== 'function') continue;"
  + "  const r = f.getBoundingClientRect(); return {left:r.left, top:r.top}; } catch (e) {} }"
  + " return null; })()");
const geo5 = await frameEval("({vp:[vw(),vh()], zoom:zoom, iw:D.iw})");
if (rect5 && geo5 && geo5.vp) {
  const midLocal = [geo5.vp[0] / 2, geo5.vp[1] / 2];        // 组件内坐标（中点取视图正中）
  const bx = Math.round(rect5.left + midLocal[0]), by = Math.round(rect5.top + midLocal[1]);
  const before5 = await frameEval(
    `(function(){ var p = screenToImg(${midLocal[0]},${midLocal[1]});`
    + " return {zoom: zoom, img: [p[0], p[1]]}; })()");
  const mk = (x, y, i) => ({ x: Math.round(x), y: Math.round(y), id: i,
                             radiusX: 8, radiusY: 8, force: 1 });
  // 两指从 ±40px 张开到 ±80px＝间距翻倍 ⇒ 正好进一级
  await send("Input.dispatchTouchEvent", { type: "touchStart",
    touchPoints: [mk(bx - 40, by, 1), mk(bx + 40, by, 2)] }, sid);
  await sleep(60);
  await send("Input.dispatchTouchEvent", { type: "touchMove",
    touchPoints: [mk(bx - 80, by, 1), mk(bx + 80, by, 2)] }, sid);
  await sleep(60);
  const during5 = await frameEval("({zoom: zoom})");
  await send("Input.dispatchTouchEvent", { type: "touchEnd", touchPoints: [] }, sid);
  await sleep(1200);
  const after5 = await frameEval(
    `(function(){ var p = screenToImg(${midLocal[0]},${midLocal[1]});`
    + " return {zoom: zoom, img: [p[0], p[1]]}; })()");
  const pnav = await ev("(function(){ try { return new URL(location.href)"
    + ".searchParams.get('pnav'); } catch (e) { return 'ERR'; } })()");
  const dImg = (before5 && after5)
    ? Math.hypot(after5.img[0] - before5.img[0], after5.img[1] - before5.img[1]) : NaN;
  // 把漂移换算成 **CSS 像素**再判：用户感知的是屏幕像素，不是原图像素。
  // 为什么不用原图像素卡死：CDP 派发的触摸坐标会被 Chrome 的
  // "touch adjustment"（把触点吸附到目标元素中心、几像素内）微调，
  // 我们无从得知手指的**真实**落点 ⇒ 这里只能验"功能对不对"，精度交给 ⑤c。
  const driftCss = Number.isFinite(dImg)
    ? dImg * (geo5.vp[0] / (geo5.iw / Math.pow(2, after5.zoom))) : NaN;
  console.log(`捏合前 ${JSON.stringify(before5)} → 过程中 ${JSON.stringify(during5)}`
    + ` → 捏合后 ${JSON.stringify(after5)}  pnav=${pnav}`
    + `  漂移 ${dImg.toFixed(2)} 原图像素 ≈ ${driftCss.toFixed(2)} CSS px`);
  const ok5 = !!(after5 && before5 && after5.zoom === before5.zoom + 1 && driftCss < 4);
  record("⑤ 双指捏合：缩放级 +1（中点下的地物不跑，手指级精度）", ok5,
    `zoom ${before5 && before5.zoom} → ${after5 && after5.zoom}，`
    + `漂移 ${driftCss.toFixed(2)} CSS px，pnav=${pnav}`);
  record("⑤b 捏合结果回传给了 Streamlit", typeof pnav === "string" && /,1$/.test(pnav), `${pnav}`);

  // ============ ⑤c 合成捏合：坐标完全可控，卡**算法精度** ============
  // 为什么还要这一条：⑤ 验的是"真手指的点按能不能捏合"（接线对不对），
  // 但它的坐标会被浏览器微调，量不出算法本身准不准。
  // 这里自己派发 pointer 事件、坐标由我们给定 ⇒ 锚点漂移必须≈0，
  // 否则就是 centerOn/zoomAtPoint 的数学错了（用户会看到"一捏地图就自己跳"）。
  const CX = geo5.vp[0] / 2, CY = geo5.vp[1] / 2;
  const syn = await frameEval(`(function(){
    tap.t = 0;
    function pd(type, id, x, y, buttons) {
      frame.dispatchEvent(new PointerEvent(type, {clientX:x, clientY:y, button:0,
        buttons:buttons, bubbles:true, cancelable:true, pointerId:id,
        pointerType:'touch', isPrimary:id===1}));
    }
    const z0 = zoom;
    const p0 = screenToImg(${CX}, ${CY});
    pd('pointerdown', 1, ${CX - 40}, ${CY}, 1);
    pd('pointerdown', 2, ${CX + 40}, ${CY}, 1);
    pd('pointermove', 1, ${CX - 80}, ${CY}, 1);
    pd('pointermove', 2, ${CX + 80}, ${CY}, 1);
    pd('pointerup',   1, ${CX - 80}, ${CY}, 0);
    pd('pointerup',   2, ${CX + 80}, ${CY}, 0);
    const p1 = screenToImg(${CX}, ${CY});
    return {z0:z0, z1:zoom, d:Math.hypot(p1[0]-p0[0], p1[1]-p0[1])};
  })()`);
  console.log("合成捏合:", JSON.stringify(syn));
  record("⑤c 合成捏合：锚点零漂移（算法精度）",
    !!(syn && syn.z1 === syn.z0 + 1 && syn.d < 1.0),
    `zoom ${syn && syn.z0} → ${syn && syn.z1}，漂移 ${syn && syn.d.toFixed(3)} 原图像素`);
} else {
  record("⑤ 双指捏合：缩放级 +1 且中点下的地物不跑", false, "没找到组件/几何");
}

console.log("\n" + JSON.stringify({
  roofDz: info.dz, singleTap: single, doubleTap: dbl, afterDrag,
  wantSnapped: want, rawUnsnapped: info.raw,
}, null, 2));

const allOk = results.every(r => r.ok);
console.log(allOk ? `\n✓ 全部通过（${results.length} 项）`
  : `\n✗ 有失败项：${results.filter(r => !r.ok).map(r => r.name).join(" / ")}`);
try { child.kill(); } catch {}
process.exit(allOk ? 0 : 1);
