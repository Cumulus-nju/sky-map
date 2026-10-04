// **端到端**验证：在真实浏览器里对"屋顶"发一次右键点击，检查回传的 pick 是**楼基**坐标。
//
// 与 cdp_snap_check.mjs 的分工：
//   · cdp_snap_check 只调用 `snapToFootprint()` 函数本身；
//   · 本脚本走**完整链路** contextmenu → snapToFootprint → pin → report → URL 的 pick 参数。
// 为什么必须分开做：函数测全绿、但那行 `contextmenu` 处理器写错（变量名/顺序）时，
// 同学实际点选会**完全失效或存错坐标**，而函数测试发现不了。
//
// 判据：点击后 URL 里的 pick 必须 ≈「吸附后的经纬度」，且明显 ≠「未吸附的经纬度」。
//
// 用法: node tools/cdp_snap_click.mjs [app_url]
import { spawn } from "node:child_process";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const url = process.argv[2] || "http://localhost:8501/";
const port = 9541;
const EDGE = "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe";
const profile = mkdtempSync(join(tmpdir(), "cdp-snapclick-"));
const child = spawn(EDGE, ["--headless=new", "--disable-gpu", "--no-first-run",
  `--remote-debugging-port=${port}`, `--user-data-dir=${profile}`,
  // ⚠ 窗口必须够高：选点组件在页面中部，1500×1200 时算出的点击坐标会落到视口外，
  //   事件派发不进去 → pick 恒为 null（那样会误判成"吸附坏了"）。
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
let id = 0; const pending = new Map(); const errs = [];
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
await send("Page.navigate", { url }, sid);
const ev = async (e) => {
  const r = await send("Runtime.evaluate", { expression: e, returnByValue: true }, sid);
  if (r.result?.exceptionDetails) return { __err: r.result.exceptionDetails.text };
  return r.result?.result?.value;
};

// 在 iframe 里挑一个"够高"的楼（位移小的话 raw 与 snapped 分不开），
// 给出：原图上的屋顶内部点、它在 #frame 里的 CSS 坐标、以及两种期望经纬度。
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
  "            frameRect: [fr.left, fr.top],",
  "            snapPt: [g[0], g[1]],",
  "            want: imgPxToLatLng(g[0], g[1]),",
  "            raw: imgPxToLatLng(px, py)};",
  "  }",
  "  return {err:'no suitable roof visible'};",
  "})()",
].join("\n");

const WAIT_READY = [
  "(() => {",
  "  for (const f of document.querySelectorAll('iframe')) {",
  "    let w; try { w = f.contentWindow; } catch (e) { continue; }",
  "    if (!w) continue;",
  "    try { if (w.eval(\"typeof snapToFootprint\") === 'function') {",
  "      const r = f.getBoundingClientRect();",
  "      return {ok: true, iframeRect: [r.left, r.top]};",
  "    } } catch (e) {}",
  "  }",
  "  return {ok: false};",
  "})()",
].join("\n");

let ready = null;
for (let i = 0; i < 25; i++) {
  await sleep(3000);
  ready = await ev(WAIT_READY);
  if (ready && ready.ok) break;
}
console.log("组件就绪:", JSON.stringify(ready));
if (!ready || !ready.ok) { try { child.kill(); } catch {} process.exit(1); }

const info = await ev(
  "(() => { const fs = document.querySelectorAll('iframe');" +
  " for (const f of fs) { try { const w = f.contentWindow;" +
  "  if (w && w.eval(\"typeof snapToFootprint\") === 'function') return w.eval(" +
  JSON.stringify(PICK_PROBE) + "); } catch (e) {} } return {err:'lost'}; })()");
console.log("挑中的屋顶:", JSON.stringify(info));
if (!info || info.err) { try { child.kill(); } catch {} process.exit(1); }

const pageX = Math.round(ready.iframeRect[0] + info.frameRect[0] + info.inFrame[0]);
const pageY = Math.round(ready.iframeRect[1] + info.frameRect[1] + info.inFrame[1]);
console.log(`点击位置: frame 内 (${info.inFrame[0].toFixed(1)}, ${info.inFrame[1].toFixed(1)})`
            + ` → 页面 (${pageX}, ${pageY})`);

// 为什么用 dispatchEvent 而不是 CDP 合成鼠标事件：
//   headless 下 `Input.dispatchMouseEvent(button:'right')` **不一定**生成 contextmenu
//   （实测 pick 恒为 null，白绕一圈），而我们要验的恰恰是 contextmenu 处理器里的
//   吸附链路 —— 直接派发该事件，跳过"浏览器→事件"那一段（那不是我们的代码）。
const DISPATCH = ("(function(){"
  + "var e=new MouseEvent('contextmenu',{clientX:__X__,clientY:__Y__,"
  + "bubbles:true,cancelable:true,button:2});"
  + "frame.dispatchEvent(e);"
  + "return {pin:[+pin.dataset.ix,+pin.dataset.iy], on:pin.classList.contains('on')};"
  + "})()")
  .replace("__X__", String(info.inFrame[0]))
  .replace("__Y__", String(info.inFrame[1]));

const fired = await ev(
  "(() => { for (const f of document.querySelectorAll('iframe')) {"
  + " try { const w = f.contentWindow; if (!w) continue;"
  + "  if (w.eval(\"typeof snapToFootprint\") !== 'function') continue;"
  + "  return w.eval(" + JSON.stringify(DISPATCH) + ");"
  + " } catch (e) {} } return {err:'no-frame'}; })()");
console.log("contextmenu 处理结果:", JSON.stringify(fired));
await sleep(9000);          // 等 Streamlit rerun

// ⚠ pick 参数**读走即删**（`site_common.take_pick()` 里调了 `clear_qp("pick")`），
//   所以 rerun 之后 URL 里已经没有了 —— 必须看页面渲染出来的「已选机位：lat, lon」。
const after = await ev(
  "(() => { const p = new URLSearchParams(location.search);"
  + " const t = (document.body.innerText || '');"
  + " const m = t.match(/已选机位：([0-9.\\-]+),\\s*([0-9.\\-]+)/);"
  + " return {urlPick: p.get('pick'), campus: p.get('pick_campus'),"
  + "         shown: m ? [parseFloat(m[1]), parseFloat(m[2])] : null,"
  + "         shownRaw: m ? m[0] : null}; })()");

const got = after.shown || [];
const want = info.want, raw = info.raw;
const dWant = got.length === 2 ? Math.max(Math.abs(got[0] - want[0]), Math.abs(got[1] - want[1])) : NaN;
const dRaw = got.length === 2 ? Math.max(Math.abs(got[0] - raw[0]), Math.abs(got[1] - raw[1])) : NaN;

console.log(JSON.stringify({
  shown_on_page: after.shownRaw, url_pick_after_read: after.urlPick,
  pin_after_click: fired && fired.pin, snapPt_expected: info.snapPt,
  want_snapped: want, raw_unspapped: raw,
  diff_vs_snapped_deg: dWant, diff_vs_raw_deg: dRaw,
  consoleErrors: errs.slice(0, 6),
}, null, 2));

// pin 是**原图像素**，应当被吸附到 snapPt（而不是停在点击处 imgPt）。
// 容差 1 px：点击坐标是我们从 JS 浮点算出来再传回去的，往返会有零点几像素的舍入
// （实测差 0.14 / 0.21 px），拿 0.06 去卡会把"正常工作"判成失败。
const pinOk = !!(fired && fired.pin && fired.on)
  && Math.abs(fired.pin[0] - info.snapPt[0]) < 1.0
  && Math.abs(fired.pin[1] - info.snapPt[1]) < 1.0;
const movedOk = Math.hypot(info.snapPt[0] - info.imgPt[0], info.snapPt[1] - info.imgPt[1]) > 1;

// pick 只保留 4 位小数（≈11 m），所以容差取 1.5e-4 度
const ok = Number.isFinite(dWant) && dWant <= 1.5e-4 && dRaw > 1e-4 && pinOk && movedOk;
console.log(ok
  ? `\n✓ 端到端通过：pin 被吸附到楼基（偏移 ${Math.hypot(info.snapPt[0] - info.imgPt[0], info.snapPt[1] - info.imgPt[1]).toFixed(1)} px），`
    + `pick 也落在楼基（与吸附值差 ${dWant.toExponential(2)}°，与屋顶值差 ${dRaw.toExponential(2)}°）`
  : `\n✗ 端到端失败：pinOk=${pinOk} movedOk=${movedOk} dWant=${dWant} dRaw=${dRaw}`);
try { child.kill(); } catch {}
process.exit(ok ? 0 : 1);
