// 窄屏 / 宽屏的**布局**验证：地图有没有被挤扁、有没有被侧边栏盖住。
//
// 为什么单独一个脚本：这套问题**肉眼才看得出来**，而且失败方式很阴 ——
// 2026-10-05 实测：`initial_sidebar_state` 写死 "expanded" 时，手机上（390px）
// 侧边栏照样展开占 300px，把地图挤成右边一条窄缝；更糟的是 `stSidebar`
// 的 z-index 是 999991，**它盖在地图上面** ⇒ 触屏事件全被侧边栏接走，
// 组件收不到任何 pointer 事件（不是"双击没实现"，是"压根没收到"）。
// 所以判据必须包含"地图中心点上，最上层元素是不是那个 iframe"。
//
// 用法: node tools/cdp_mobile_layout.mjs [app_url]
import { spawn } from "node:child_process";
import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const url = process.argv[2] || "http://localhost:8501/";
const EDGE = "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe";
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const CASES = [
  { name: "桌面 1500×1700", w: 1500, h: 1700, mobile: false, touch: false,
    wantSidebar: true, shot: "sky_map_layout_desktop.png" },
  { name: "手机 390×844", w: 390, h: 844, mobile: true, touch: true,
    wantSidebar: false, shot: "sky_map_layout_phone.png" },
];

async function runCase(c, port) {
  const EDGE_ARGS = ["--headless=new", "--disable-gpu", "--no-first-run",
    // ⚠ 绕开本机系统代理：代理出口被 Streamlit 拉黑，线上会 HTTP 444
    //   （详见 cdp_dblclick_pick.mjs 里的注释）
    "--no-proxy-server",
    `--remote-debugging-port=${port}`,
    `--user-data-dir=${mkdtempSync(join(tmpdir(), "cdp-layout-"))}`,
    `--window-size=${c.w},${c.h}`, "--hide-scrollbars", "about:blank"];
  const child = spawn(EDGE, EDGE_ARGS, { stdio: "ignore" });
  let ws;
  for (let i = 0; i < 60; i++) {
    try {
      const j = await (await fetch(`http://127.0.0.1:${port}/json/version`)).json();
      if (j.webSocketDebuggerUrl) { ws = new WebSocket(j.webSocketDebuggerUrl); break; }
    } catch {}
    await sleep(300);
  }
  await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej; });
  let id = 0; const pending = new Map();
  ws.onmessage = (e) => { const m = JSON.parse(e.data);
    if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); } };
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
  const IN_FRAME = (inner) =>
    "(function(){ for (const f of document.querySelectorAll('iframe')) {"
    + " try { const w = f.contentWindow; if (!w) continue;"
    + "  if (w.eval(\"typeof doPick\") !== 'function') continue;"
    + "  return {v: w.eval(" + JSON.stringify(inner) + "), found: true};"
    + " } catch (e) {} } return {found: false}; })()";
  const frameEval = async (inner) => { const r = await ev(IN_FRAME(inner)); return r.found ? r.v : { __noframe: 1 }; };

  // ⚠ 机型仿真必须在**导航之前**设好：Streamlit 是在首屏就按窗口宽度决定侧边栏收放的，
  //   先导航再改宽度，量到的是"按旧宽度布好的局"（会被误判成"auto 没生效"）。
  if (c.mobile) {
    await send("Emulation.setDeviceMetricsOverride",
      { width: c.w, height: c.h, deviceScaleFactor: 3, mobile: true }, sid);
  } else {
    await send("Emulation.setDeviceMetricsOverride",
      { width: c.w, height: c.h, deviceScaleFactor: 1, mobile: false }, sid);
  }
  if (c.touch) await send("Emulation.setTouchEmulationEnabled", { enabled: true, maxTouchPoints: 5 }, sid);
  await send("Page.navigate", { url }, sid);

  let ready = false;
  for (let i = 0; i < 30; i++) { await sleep(1500); if ((await frameEval("'ok'")) === "ok") { ready = true; break; } }
  if (!ready) { try { child.kill(); } catch {} return { name: c.name, err: "组件没就绪" }; }

  await ev("(function(){ for (const f of document.querySelectorAll('iframe')) { try {"
    + " if (f.contentWindow.eval(\"typeof doPick\") !== 'function') continue;"
    + " f.scrollIntoView({block:'center'}); return true; } catch(e){} } return false; })()");
  await sleep(900);

  const m = await ev(`(function(){
    let f = null;
    for (const q of document.querySelectorAll('iframe')) {
      try { if (q.contentWindow.eval("typeof doPick") === 'function') { f = q; break; } } catch(e){}
    }
    if (!f) return {err:'no iframe'};
    const r = f.getBoundingClientRect();
    const cx = Math.round(r.left + r.width / 2), cy = Math.round(r.top + r.height / 2);
    const top = document.elementFromPoint(cx, cy);
    const sb = document.querySelector('section.stSidebar');
    let sbRect = null;
    if (sb) { const b = sb.getBoundingClientRect(); const s = getComputedStyle(sb);
      sbRect = {rect:[b.left,b.top,b.width,b.height], vis:s.visibility, transform:s.transform}; }
    // 侧边栏是否**真的可见/可点**（收起时 Streamlit 会把它移出视口，宽高还在）
    const sbVisible = !!(sbRect && sbRect.vis === 'visible'
      && sbRect.rect[2] > 50 && sbRect.rect[0] + sbRect.rect[2] > 8);
    return {win:[window.innerWidth, window.innerHeight],
            iframe:[r.left, r.top, r.width, r.height],
            mapCenter:[cx, cy],
            topAtCenter: top ? (top.tagName + (top.id ? '#' + top.id : '')
                              + '.' + (top.className||'').toString().slice(0,24)) : null,
            topIsIframe: !!(top && top.tagName === 'IFRAME'
                            && (function(){try{return top.contentWindow.eval("typeof doPick")==='function'}catch(e){return false}})()),
            sidebar: sbRect, sidebarVisible: sbVisible};
  })()`);

  const shot = await send("Page.captureScreenshot", { format: "png" }, sid);
  const out = join(tmpdir(), c.shot);
  try { writeFileSync(out, Buffer.from(shot.result.data, "base64")); } catch {}
  try { child.kill(); } catch {}
  return { name: c.name, ...m, shot: out, wantSidebar: c.wantSidebar, caseW: c.w };
}

const rows = [];
for (let i = 0; i < CASES.length; i++) {
  const r = await runCase(CASES[i], 9561 + i);
  rows.push(r);
  console.log(`\n===== ${r.name} =====`);
  console.log(JSON.stringify(r, null, 1));
}

let bad = 0;
console.log("\n---- 判据 ----");
for (const r of rows) {
  if (r.err) { console.log(`✗ ${r.name}: ${r.err}`); bad++; continue; }
  // ① 地图中心必须是那个 iframe（否则"能看见"但"点不到"）
  const ok1 = r.topIsIframe;
  // ② 窄屏不许把地图挤成一条缝
  const ok2 = r.wantSidebar ? true : (r.iframe[2] >= 0.75 * r.win[0]);
  // ③ 宽屏侧边栏要保持展开（桌面观感不能因为改成 auto 就变了）
  const ok3 = r.wantSidebar ? r.sidebarVisible : !r.sidebarVisible;
  const ok = ok1 && ok2 && ok3;
  if (!ok) bad++;
  console.log(`${ok ? "✓" : "✗"} ${r.name}`
    + `  地图中心可点=${ok1}(顶层是 ${r.topAtCenter})`
    + `  地图宽 ${r.iframe ? r.iframe[2] : "?"}px / 视口 ${r.win ? r.win[0] : "?"}px`
    + `  侧边栏可见=${r.sidebarVisible}(期望 ${r.wantSidebar})`
    + `  截图 ${r.shot}`);
}
console.log(bad ? `\n✗ ${bad} 项不通过` : "\n✓ 布局检查全过");
process.exit(bad ? 1 : 0);
