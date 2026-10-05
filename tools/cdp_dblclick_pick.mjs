// 选点手势的端到端验证（2026-10-05：**电脑右键单击** / **手机双击轻点**）。
//
// 为什么要单独一个脚本：把选点从"右键"改成"双击"以后，**入口事件换了**。
// 函数级测试（snapToFootprint / inPoly 那些）全绿也证明不了"同学双击一下真能选中"——
// 计数窗口、位移阈值、"拖动后作废连击"任何一处写错，症状都是**双击毫无反应**（静默）。
//
// **选点手势现在是按设备分开的**，所以判据也分两路：
//   电脑：提示写"右键"；**左键单击、左键双击都不选**；**右键单击**选中且落点 = 吸附后楼基
//   手机：提示写"双指/双击轻点"；**双击轻点**选中；点一下→拖动→再点一下**不**选（拖动作废连击）
//   手机：真·触屏双击轻点（CDP `Input.dispatchTouchEvent` + 390×844 机型仿真）端到端选中
//         —— 这一条才是"手机用户到底交不交得了稿"的答案
//   手机：真·触屏双指捏合：缩放级 +1、中点下的地物不跑，并回传给 Streamlit
//   ⑤c 合成捏合（坐标完全可控）：锚点漂移必须 ≈0 —— 把"算法准不准"与
//      "浏览器触摸坐标微调带来的噪声"分开，否则量到的漂移说不清是谁的
//   ⑥ 缩放按钮 +1/−1 仍然有效（本次把滚轮/双指/按钮合并成了 zoomAtPoint）
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
  // ⚠ `--no-proxy-server` 不能省（2026-10-05 实测）：本机系统代理
  //   （127.0.0.1:7897，com.vortex.helper）会把请求打到**被 Streamlit 拉黑的出口节点**，
  //   线上验收会立刻拿到 HTTP 444 "source IP address not allowed"。
  //   脚本要验的是"站点本身行不行"，所以必须绕开本机代理走直连（直连实测 200 正常）。
  "--no-proxy-server",
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
// ⚠ 线上（Streamlit Cloud）**冷启动可能 1~2 分钟**（休眠唤醒 + 容器拉起），
//   本地十几秒就好。用同一套 45 秒去等线上，会得到"组件始终没就绪"的假失败
//   —— 2026-10-05 就被这个晃了一次，以为线上坏了。
const REMOTE = !/^https?:\/\/(localhost|127\.0\.0\.1)/.test(url);
const READY_TRIES = REMOTE ? 120 : 30;          // ×1.5s = 180s / 45s
const waitReady = async (label) => {
  for (let i = 0; i < READY_TRIES; i++) {
    await sleep(1500);
    const r = await ev(IN_FRAME("'ok'"));
    if (r && r.found) return true;
    if (REMOTE && i > 0 && i % 20 === 0) console.log(`   …等线上冷启动 ${(i * 1.5).toFixed(0)}s`);
  }
  console.log(`✗ ${label}: 组件始终没就绪（等了 ${(READY_TRIES * 1.5).toFixed(0)}s）`);
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
  "    return {dz: dz, imgPt: [px, py],",
  // ⚠ 加 frame.clientLeft/clientTop：组件内部已经把"1px 边框偏移"修掉了
  //   （见 framePicker 的 frameOrigin()），探针这侧也必须用**内容盒**原点，
  //   否则派发下去的坐标会比预期偏 1 CSS 像素（z=0 时约 2.3 原图像素）。
  "            inFrame: [frame.clientLeft + sx, frame.clientTop + sy],",
  "            frameRect: [fr.left, fr.top], vp: [vw(), vh()],",
  "            snapPt: [g[0], g[1]],",
  "            want: imgPxToLatLng(g[0], g[1]),",
  "            raw: imgPxToLatLng(px, py)};",
  "  }",
  "  return {err:'no suitable roof visible'};",
  "})()",
].join("\n");

// ---------------- 合成事件（验"逻辑"）----------------
// `pt` 是 pointerType，**必须传对**：手势现在按设备分开了 ——
// 电脑认**右键**（contextmenu），双击轻点**只在触屏上**算选点，
// 所以拿 pt='mouse' 还是 'touch' 发事件，预期结果完全不同。
const seq = (parts, pt = "touch") => "(function(){"
  + "tap.t = 0;"                                  // 每个用例前清掉连击残留，免得用例相互污染
  + "function pd(type,x,y,buttons){"
  + "  frame.dispatchEvent(new PointerEvent(type,{clientX:x,clientY:y,"
  + "    button:0,buttons:buttons,bubbles:true,cancelable:true,"
  + "    pointerId:1,pointerType:" + JSON.stringify(pt) + ",isPrimary:true}));}"
  + parts
  + "function urlPick(){try{return new URL(window.parent.location.href)"
  + "  .searchParams.get('pick');}catch(e){return 'ERR:'+e;}}"
  + "return {on: pin.classList.contains('on'), pick: urlPick(),"
  + "        pin: (pin.dataset.ix===undefined?null:[+pin.dataset.ix,+pin.dataset.iy]),"
  + "        tapSet: tap.t !== 0};"
  + "})()";

const TAP = (x, y) => `pd('pointerdown',${x},${y},1);pd('pointerup',${x},${y},0);`;
// 电脑端选点：右键单击（contextmenu）。合成它而不是用 Input.dispatchMouseEvent，
// 理由同 cdp_snap_click.mjs：headless 下合成右键**不一定**派发 contextmenu。
const RCLICK = (x, y) => `frame.dispatchEvent(new MouseEvent('contextmenu',{clientX:${x},`
  + `clientY:${y},bubbles:true,cancelable:true,button:2}));`;
// 拖动：位移 > 3px 阈值 ⇒ 认定成拖动（而不是点击）
const DRAG = (x, y) => `pd('pointerdown',${x},${y},1);`
  + `pd('pointermove',${x + 4},${y + 4},1);pd('pointerup',${x + 4},${y + 4},0);`;

// ---------------- 逐用例 ----------------
const results = [];
const record = (name, ok, detail) => {
  results.push({ name, ok });
  console.log(`${ok ? "✓" : "✗"} ${name}${detail ? "  " + detail : ""}`);
};

const navigate = async () => {
  await send("Page.navigate", { url }, sid);
};

// ============ ① 电脑端：提示写着右键 / 左键单击与双击都**不**选 ============
await navigate();                                   // ⚠ 别忘了先导航（target 出生在 about:blank）
if (!await waitReady("① 电脑端")) { try { child.kill(); } catch {} process.exit(1); }
const info = await frameEval(PICK_PROBE);
console.log("挑中的屋顶:", JSON.stringify(info));
if (!info || info.err) { try { child.kill(); } catch {} process.exit(1); }
const [fx, fy] = info.inFrame;
console.log(`组件内坐标 (${fx.toFixed(1)}, ${fy.toFixed(1)})，吸附位移 ${info.dz.toFixed(0)} px`);

// 电脑端提示必须是"右键"那套 —— 用户 2026-10-05 的原话：
// "电脑端和以前一样是右键单击选点啊"，提示里不能写"双击"。
const tipDesk = await frameEval("hint.textContent");
record("① 电脑端提示 = 滚轮/右键那套",
  typeof tipDesk === "string" && tipDesk.indexOf("滚轮") >= 0
  && tipDesk.indexOf("右键") >= 0 && tipDesk.indexOf("双指") < 0
  && tipDesk.indexOf("双击") < 0, `"${tipDesk}"`);

const single = await frameEval(seq(TAP(fx, fy), "mouse"));
record("①b 电脑左键单击**不**选中", !!(single && !single.on && !single.pick),
  JSON.stringify(single));

// 连点两下左键也**不**能选中：电脑端只认右键，不能凭空多出一条没人要求的新手势
const dblMouse = await frameEval(seq(TAP(fx, fy) + TAP(fx, fy), "mouse"));
record("①c 电脑左键双击**也不**选中（只认右键）",
  !!(dblMouse && !dblMouse.on && !dblMouse.pick), JSON.stringify(dblMouse));

// ============ ② 电脑端：右键单击 = 选中，且落点是吸附后的楼基 ============
const want = info.want;
const rc = await frameEval(seq(RCLICK(fx, fy), "mouse"));
const got = rc && rc.pick ? rc.pick.split(",").map(Number) : null;
const dWant = got ? Math.max(Math.abs(got[0] - want[0]), Math.abs(got[1] - want[1])) : NaN;
// ⚠ 容差 3 原图像素（而不是 1）：浏览器会把派发的事件坐标**按整数取整**
//   （MouseEventInit 的 clientX/clientY 是 long），z=0 时 1 CSS px ≈ 1/0.442 ≈ 2.3
//   原图像素 ⇒ 拿 1px 去卡，会把"完全正常"误判成失败（第一版就报了这个假失败）。
const pinOk = !!(rc && rc.on && rc.pin)
  && Math.abs(rc.pin[0] - info.snapPt[0]) < 3.0
  && Math.abs(rc.pin[1] - info.snapPt[1]) < 3.0;
// 另外确认**吸附真发生了**（落针不能停在点击处）
const snapMoved = !!(rc && rc.pin)
  && Math.hypot(rc.pin[0] - info.imgPt[0], rc.pin[1] - info.imgPt[1]) > 1;
record("② 电脑右键单击选中，坐标 = 吸附后的楼基",
  !!got && dWant <= 1.5e-4 && pinOk && snapMoved,
  `pick=${rc && rc.pick} 期望≈${want.map(v => v.toFixed(4))} 差 ${dWant.toExponential(2)}°`
  + ` on=${rc && rc.on} 吸附位移=${rc && rc.pin ? Math.hypot(rc.pin[0] - info.imgPt[0], rc.pin[1] - info.imgPt[1]).toFixed(1) : "?"}px`);

// ============ ③ 手机端（合成触屏）：双击轻点选中，拖动作废连击 ============
// 拖动会 report(false) 触发 Streamlit rerun（组件会被重建），所以单独重载一次页面。
await navigate();
if (!await waitReady("③ 手机手势")) { try { child.kill(); } catch {} process.exit(1); }
const info3 = await frameEval(PICK_PROBE);
if (!info3 || info3.err) { console.log("✗ ③ 挑不到屋顶:", JSON.stringify(info3)); try { child.kill(); } catch {} process.exit(1); }
const [gx, gy] = info3.inFrame;

// 先验正的：双击轻点必须选中（否则下面那个"没选中"说明不了任何事）
const dblTouch = await frameEval(seq(TAP(gx, gy) + TAP(gx, gy), "touch"));
record("③ 手机双击轻点选中", !!(dblTouch && dblTouch.on && dblTouch.pick),
  JSON.stringify({ on: dblTouch && dblTouch.on, pick: dblTouch && dblTouch.pick }));

// 再验反的：点一下 → 拖动 → 再点一下 ⇒ 中间拖动过，不能算双击
// ⚠ **必须重新加载页面**：上面的 ③ 已经成功选过一次，pin 还亮着、URL 里也还挂着 pick，
//   不重载就根本分不出"这次到底有没有选中"（第一版就栽在这儿，报了个假失败）。
await navigate();
if (!await waitReady("③b 拖动作废连击")) { try { child.kill(); } catch {} process.exit(1); }
const info3b = await frameEval(PICK_PROBE);
if (!info3b || info3b.err) { console.log("✗ ③b 挑不到屋顶:", JSON.stringify(info3b)); try { child.kill(); } catch {} process.exit(1); }
const [hx, hy] = info3b.inFrame;
const afterDrag = await frameEval(seq(TAP(hx, hy) + DRAG(hx, hy) + TAP(hx, hy), "touch"));
record("③b 手机：拖动作废连击（拖完立刻点一下不算双击）",
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

// ============ ⑦ 视图变化**不能**触发 Streamlit rerun（否则交互时"一闪一闪"）============
// 用户 2026-10-05 反馈："和地图交互时经常一闪一闪的（可能是重定位？），手感很怪很卡顿"。
// 根因：report() 每写一次视图就 dispatchEvent(popstate)，而 Streamlit 靠 popstate 重跑脚本
// ⇒ 每拖一下 / 每滚一格，整个组件被**重建**，那张 ~1MB 的底图重新解码 ⇒ 闪 + 卡。
// 修法：视图变化只 replaceState 写 URL（pnav 照样留着），只有**选点**才触发 rerun。
// 判据两条：父页面 popstate 计数 == 0，且组件里打的存活标记还在（说明没被重建）。
await navigate();
if (!await waitReady("⑦ 视图变化不重跑")) { try { child.kill(); } catch {} process.exit(1); }
await ev("(function(){ window.__pop = 0;"
  + " window.addEventListener('popstate', function(){ window.__pop++; }); return true; })()");
await frameEval("(function(){ window.__alive = 1; return 1; })()");
const interacted = await frameEval(`(function(){
  const r = frame.getBoundingClientRect();
  const mx = r.left + vw() / 2, my = r.top + vh() / 2;
  const z0 = zoom;
  for (let i = 0; i < 3; i++) {
    frame.dispatchEvent(new WheelEvent('wheel', {clientX: mx, clientY: my, deltaY: -100,
      bubbles: true, cancelable: true}));
  }
  const zAfterWheel = zoom;
  // 再拖一次（拖动结束也会 report(false)）
  const pd = (type, x, y, buttons) => frame.dispatchEvent(new PointerEvent(type,
    {clientX: x, clientY: y, button: 0, buttons: buttons, bubbles: true, cancelable: true,
     pointerId: 1, pointerType: 'mouse', isPrimary: true}));
  pd('pointerdown', mx, my, 1); pd('pointermove', mx + 20, my + 12, 1);
  pd('pointerup', mx + 20, my + 12, 0);
  return {z0: z0, zAfterWheel: zAfterWheel};
})()`);
await sleep(4000);   // 真有 rerun 的话，足够把组件换掉
const after7 = await ev("({pop: window.__pop, nIframe: document.querySelectorAll('iframe').length})");
const alive = await frameEval("window.__alive === 1");
record("⑦ 拖动/缩放不再触发 Streamlit 重跑（不闪不卡）",
  !!(after7 && after7.pop === 0 && alive === true),
  `popstate=${after7 && after7.pop}（期望 0）· 组件存活=${alive}（期望 true）`
  + ` · zoom ${interacted && interacted.z0}→${interacted && interacted.zAfterWheel}`);

// ============ ⑨ **重新选点**时，页面下方"已选机位"必须跟着更新 ============
// 用户 2026-10-05："现在这版我重新选点，下面的已选机位不会动诶"。
// ⚠ 现有用例只验了"第一次选点"，第二次选点这条路径**没有任何覆盖** —— 补上。
// 判据：连选两个明显不同的点，页面上那句"已选机位：lat, lon"必须**两次都变**，
//       且各自贴在对应位置（不能停在第一次的坐标上）。
await navigate();
if (!await waitReady("⑨ 重新选点")) { try { child.kill(); } catch {} process.exit(1); }
const readPicked = "(function(){ const t = document.body.innerText || '';"
  + " const m = t.match(/已选机位：([0-9.\\-]+),\\s*([0-9.\\-]+)/);"
  + " return m ? [parseFloat(m[1]), parseFloat(m[2])] : null; })()";
const p9a = await frameEval(PICK_PROBE);
if (!p9a || p9a.err) { console.log("✗ ⑨ 挑不到屋顶:", JSON.stringify(p9a)); try { child.kill(); } catch {} process.exit(1); }
await frameEval(seq(RCLICK(p9a.inFrame[0], p9a.inFrame[1]), "mouse"));
await sleep(11000);                                  // 等 Streamlit 重跑 + 回显
const boxA = await ev(readPicked);
console.log(`  第 1 次选点：页面显示 ${JSON.stringify(boxA)}，期望≈${p9a.want.map(v => v.toFixed(4))}`);

const p9b = await frameEval(PICK_PROBE);            // 组件已重建，重新探针
if (!p9b || p9b.err) { console.log("✗ ⑨ 第二次挑不到屋顶:", JSON.stringify(p9b)); try { child.kill(); } catch {} process.exit(1); }
// 换一个**明显不同**的位置（往右下挪 70px，并确保还在框内）
const offX = Math.min(70, Math.max(10, p9b.vp[0] - p9b.inFrame[0] - 12));
const offY = Math.min(70, Math.max(10, p9b.vp[1] - p9b.inFrame[1] - 12));
const bx2 = p9b.inFrame[0] + offX, by2 = p9b.inFrame[1] + offY;
// ⚠ 期望值必须按**实际派发的那个点**算（不能拿第一次的屋顶期望值来比 —— 那是我第一版的错）
const expectB = await frameEval(`(function(){ var raw = screenToImg(${bx2}, ${by2});`
  + " var p = snapToFootprint(raw[0], raw[1]); return imgPxToLatLng(p[0], p[1]); })()");
// 装个 popstate 计数器：用来证明"事件确实发出去了，问题只可能在后端"
await ev("(function(){ window.__pop9 = 0;"
  + " window.addEventListener('popstate', function(){ window.__pop9++; }); return true; })()");
const rcb = await frameEval(seq(RCLICK(bx2, by2), "mouse"));
console.log(`  第 2 次右键：组件内 ${JSON.stringify(rcb)} · popstate=${JSON.stringify(await ev("window.__pop9"))}`);
await sleep(11000);
const boxB = await ev(readPicked);
console.log(`  第 2 次选点：页面显示 ${JSON.stringify(boxB)}（比第 1 次挪了 ${offX},${offY}px）`);

const changed = !!(boxA && boxB) &&
  (Math.abs(boxA[0] - boxB[0]) > 1e-6 || Math.abs(boxA[1] - boxB[1]) > 1e-6);
// 第二次的值必须真的落在第二次点的地方（不能只"变了"而是变到别处）
const okA = !!boxA && Math.max(Math.abs(boxA[0] - p9a.want[0]), Math.abs(boxA[1] - p9a.want[1])) <= 2e-4;
const okB = !!boxB && Math.max(Math.abs(boxB[0] - expectB[0]), Math.abs(boxB[1] - expectB[1])) <= 2e-4;
record("⑨ 重新选点：页面下方「已选机位」跟着更新",
  changed && okA && okB,
  `第1次 ${JSON.stringify(boxA)}（对=${okA}） → 第2次 ${JSON.stringify(boxB)}（对=${okB}）`
  + ` · 两次不同=${changed}`);
// 用户 2026-10-05："选点后还是会闪一下"。根因之一：初始 CSS 尺寸是 1200×980
// （宽度没按高反推），等图加载完 fitToParent() 再缩成 884×980 ⇒ 肉眼一次横向跳。
// 判据：payload 里的 outW/outH 比例必须与组件实际尺寸比例一致（<1%）。
// ============ ⑧ 组件初始尺寸 == 终态比例（否则每次重建都"先宽后窄"跳一下）============
const geo8 = await frameEval("({outW: D.outW, outH: D.outH, vw: vw(), vh: vh()})");
const rInit = geo8 ? geo8.outW / geo8.outH : NaN;
const rNow = geo8 ? geo8.vw / geo8.vh : NaN;
record("⑧ 组件初始尺寸即终态比例（重建不跳/不闪）",
  Number.isFinite(rInit) && Math.abs(rInit - rNow) < 0.01,
  `初始 ${geo8 && geo8.outW}×${geo8 && geo8.outH}（比 ${Number(rInit).toFixed(3)}）`
  + ` vs 实际 ${geo8 && geo8.vw}×${geo8 && geo8.vh}（比 ${Number(rNow).toFixed(3)}）`);

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
  roofDz: info.dz, leftSingle: single, leftDouble: dblMouse, rightClick: rc,
  touchDouble: dblTouch, afterDrag,
  wantSnapped: want, rawUnsnapped: info.raw,
}, null, 2));

const allOk = results.every(r => r.ok);
console.log(allOk ? `\n✓ 全部通过（${results.length} 项）`
  : `\n✗ 有失败项：${results.filter(r => !r.ok).map(r => r.name).join(" / ")}`);
try { child.kill(); } catch {}
process.exit(allOk ? 0 : 1);
