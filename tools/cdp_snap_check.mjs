// 在**真实浏览器**里验证"点屋顶 → 贴回楼基"。
//
// 为什么不只信 Python 测试：`frame_picker.snap_to_footprint()` 只是把 JS 里那份
// 逻辑**照抄**了一遍，两边一起错的话测试照样全绿。真正要确认的是：
//   · 投稿页 iframe 里那份 JS 确实拿到了 `D.roofs` 几何；
//   · `snapToFootprint()` 在实际页面里工作正常、位移量 = |VIEW| × dz。
//
// 用法: node tools/cdp_snap_check.mjs [app_url]
import { spawn } from "node:child_process";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const url = process.argv[2] || "http://localhost:8501/";
const port = 9531;
const EDGE = "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe";
const profile = mkdtempSync(join(tmpdir(), "cdp-snap-"));
const child = spawn(EDGE, ["--headless=new", "--disable-gpu", "--no-first-run",
  `--remote-debugging-port=${port}`, `--user-data-dir=${profile}`,
  "--window-size=1500,1200", "--hide-scrollbars", "about:blank"], { stdio: "ignore" });
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

// 在 iframe 里跑的那段：直接调用页面自己的 snapToFootprint（不是我们重写的版本）
const INNER = [
  "(function(){",
  "  if (typeof D === 'undefined' || !D.roofs || !D.roofs.b) return {err:'no roofs'};",
  "  var R = D.roofs, out = [];",
  "  for (var i = R.b.length - 1; i >= 0 && out.length < 6; i--) {",
  "    var dz = R.b[i][0], flat = R.b[i][1], m = flat.length / 2;",
  "    var minx=1e9,maxx=-1e9,miny=1e9,maxy=-1e9;",
  "    for (var k=0;k<m;k++){var x=flat[2*k],y=flat[2*k+1];",
  "      if(x<minx)minx=x; if(x>maxx)maxx=x; if(y<miny)miny=y; if(y>maxy)maxy=y;}",
  "    var cx=(minx+maxx)/2, cy=(miny+maxy)/2;",
  "    if (!inPoly(cx, cy, flat)) continue;",
  "    var g = snapToFootprint(cx, cy);",
  "    out.push({dz: dz,",
  "              roof: [+cx.toFixed(1), +cy.toFixed(1)],",
  "              ground: [+g[0].toFixed(1), +g[1].toFixed(1)],",
  "              moved: +Math.hypot(g[0]-cx, g[1]-cy).toFixed(2)});",
  "  }",
  "  return {view: R.v, roofs: R.b.length, samples: out};",
  "})()",
].join("\n");

const PROBE = [
  "(() => {",
  "  const out = {iframes: document.querySelectorAll('iframe').length};",
  "  for (const f of document.querySelectorAll('iframe')) {",
  "    let w; try { w = f.contentWindow; } catch (e) { continue; }",
  "    if (!w) continue;",
  "    let probe;",
  "    try { probe = w.eval(\"typeof snapToFootprint\"); } catch (e) { continue; }",
  "    if (probe !== 'function') continue;",
  "    out.found = true;",
  "    try { out.detail = w.eval(" + JSON.stringify(INNER) + "); }",
  "    catch (e) { out.detail = {err: String(e)}; }",
  "    break;",
  "  }",
  "  return out;",
  "})()",
].join("\n");

// 等选点组件就绪（本地站点很快，给 60s 足够）
let res = null;
for (let i = 0; i < 20; i++) {
  await sleep(3000);
  res = await ev(PROBE);
  if (res && res.found && res.detail && res.detail.samples && res.detail.samples.length) break;
}
console.log(JSON.stringify({url, ...res, consoleErrors: errs.slice(0, 6)}, null, 2));

// 位移量必须 = |VIEW| × dz（VIEW = [-0.34, -1.0] ⇒ 模长 1.05624）
const V = Math.hypot(0.34, 1.0);
const samples = (res && res.detail && res.detail.samples) || [];
let bad = 0;
for (const s of samples) {
  if (Math.abs(s.moved - V * s.dz) > 0.15) bad++;
}
console.log(`\n位移量自检: ${samples.length} 个样本，${bad} 个不符合 |VIEW|×dz (|VIEW|=${V.toFixed(5)})`);
try { child.kill(); } catch {}
process.exit(samples.length && !bad ? 0 : 1);
