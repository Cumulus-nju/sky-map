// 验证静态底图选点：模拟点击图上某点，读出回传的 pick，并与该点应有的经纬度比对。
// 用法: node tools/cdp_pick_test.mjs <app_url> [outPng]
import { spawn } from "node:child_process";
import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const url = process.argv[2] || "http://localhost:8501/";
const outPng = process.argv[3] || join(tmpdir(), "pick.png");
const port = 9521;
const EDGE = "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe";
const profile = mkdtempSync(join(tmpdir(), "cdp-pick-"));
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

// 等选点组件里的底图出现
let ready = false;
for (let i = 0; i < 30; i++) {
  await sleep(3000);
  ready = await ev(`(() => {
    for (const f of document.querySelectorAll('iframe')) {
      try { const d = f.contentDocument; if (d && d.getElementById('wrap') && d.getElementById('base')) return true; } catch (e) {}
    }
    return false;
  })()`);
  if (ready) break;
}
console.log("组件就绪:", ready);
await sleep(3000);

// 取组件（wrap）在**页面坐标系**里的位置，以及组件内部的换算参数
const geo = await ev(`(() => {
  for (const f of document.querySelectorAll('iframe')) {
    try {
      const d = f.contentDocument; if (!d || !d.getElementById('wrap')) continue;
      const w = d.getElementById('wrap');
      const fr = f.getBoundingClientRect();
      const wr = w.getBoundingClientRect();
      return {
        found: true,
        pageLeft: fr.left + wr.left, pageTop: fr.top + wr.top,
        w: wr.width, h: wr.height,
        imgW: d.getElementById('base').naturalWidth || null,
      };
    } catch (e) { return { err: String(e) }; }
  }
  return { found: false };
})()`);
console.log("组件几何:", JSON.stringify(geo));

// 点在组件内部（35%, 42% 处），并记录归一化位置以便回算期望经纬度
const fx = 0.35, fy = 0.42;
const cx = (geo.pageLeft ?? 0) + (geo.w ?? 400) * fx;
const cy = (geo.pageTop ?? 0) + (geo.h ?? 400) * fy;
for (const type of ["mousePressed", "mouseReleased"]) {
  await send("Input.dispatchMouseEvent", {
    type, x: Math.round(cx), y: Math.round(cy), button: "left", clickCount: 1, buttons: 1,
  }, sid);
}
await sleep(9000);   // 等 Streamlit rerun 完成

const after = await ev(`(() => {
  const p = new URLSearchParams(location.search);
  const t = document.body.innerText || '';
  return {
    url: location.href,
    pick: p.get('pick'),
    pickCampus: p.get('pick_campus'),
    storedOk: /已选机位/.test(t),
    storedLine: (t.match(/已选机位：[^\\n]*/) || [])[0] || null,
    hint: (t.match(/点一下你拍照站的位置[^\\n]*/) || [])[0] || null,
  };
})()`);

const { result: shot } = await send("Page.captureScreenshot", { format: "png" }, sid);
if (shot?.data) writeFileSync(outPng, Buffer.from(shot.data, "base64"));

// 回算"这个归一化位置"应有的经纬度，供人工/脚本比对（Python 侧同一张图的换算）
console.log(JSON.stringify({
  ready, clickedAt: [Math.round(cx), Math.round(cy)], clickedFrac: [fx, fy],
  after, consoleErrors: errs.slice(0, 6), screenshot: outPng,
}, null, 2));
try { child.kill(); } catch {}
process.exit(0);
