// 线上站点验收：进 Streamlit Cloud 的投稿页 iframe，量外框与视野锁定是否已生效。
// 判据：maxBounds 应包含 118.766207/118.782631（新版 frame），而不是旧 bbox 的 118.767759/118.779466。
// 用法: node tools/cdp_live_verify.mjs [url] [screenshot.png]
import { spawn } from "node:child_process";
import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const url = process.argv[2] || "https://azzjin9anwdbyykyd3dyh6.streamlit.app/";
const outPng = process.argv[3] || join(tmpdir(), "live.png");
const port = 9381;
const EDGE = "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe";
const profile = mkdtempSync(join(tmpdir(), "cdp-live-"));
const child = spawn(EDGE, ["--headless=new", "--disable-gpu", "--no-first-run",
  `--remote-debugging-port=${port}`, `--user-data-dir=${profile}`,
  "--window-size=1400,1000", "--hide-scrollbars", "about:blank"], { stdio: "ignore" });
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function wsUrl() {
  for (let i = 0; i < 80; i++) {
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
  if (m.method === "Runtime.consoleAPICalled" && m.params?.type === "error")
    errs.push((m.params.args || []).map(a => a.value ?? a.description ?? "").join(" ").slice(0, 160));
  if (m.method === "Runtime.exceptionThrown")
    errs.push("EXC: " + (m.params?.exceptionDetails?.text || "").slice(0, 160));
  if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); }
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

// 免费层可能休眠，多等一会儿；轮询直到 iframe 里出现 leaflet
const evl = async (expr) => {
  const r = await send("Runtime.evaluate", { expression: expr, returnByValue: true }, sid);
  if (r.result?.exceptionDetails) return { __err: r.result.exceptionDetails.text };
  return r.result?.result?.value ?? r.result;
};
let ready = false;
for (let i = 0; i < 48; i++) {          // 最多等 8 分钟：免费层休眠唤醒 + 重新部署
  await sleep(10000);
  const s = await evl(`(() => {
    const f = document.querySelector('iframe');
    if(!f) return {leaflet:false, txt:(document.body.innerText||'').slice(0,80)};
    let w; try{ w=f.contentWindow; }catch(e){ return {leaflet:false, err:'cross-origin'}; }
    const lm = w.document && w.document.querySelector('.leaflet-container');
    return {leaflet: !!lm, txt:(document.body.innerText||'').slice(0,60)};
  })()`);
  console.log(`  [${(i+1)*10}s] leaflet=${s?.leaflet}  ${s?.txt ? JSON.stringify(s.txt) : ""}`);
  if (s?.leaflet) { ready = true; break; }
}
if (!ready) console.log("⚠ 等待超时：地图 iframe 未就绪（可能还在唤醒，或云端构建中）");

console.log("\n=== 线上投稿页实测 ===");
console.log(JSON.stringify(await evl(`(() => {
  const f = document.querySelector('iframe');
  if (!f) return { err: '没有 iframe' };
  let w; try { w = f.contentWindow; } catch (e) { return { err: 'cross-origin' }; }
  const d = w.document; if (!d) return { err: 'no document' };
  const lm = d.querySelector('.leaflet-container');
  let info = {};
  try {
    const mapObj = w.map;
    if (mapObj && mapObj.options) {
      const b = mapObj.getBounds();
      info = {
        zoom: mapObj.getZoom(),
        minZoom: mapObj.getMinZoom(),
        maxBounds: (mapObj.options.maxBounds || null) ? String(mapObj.options.maxBounds.toBBoxString()) : null,
        viscosity: mapObj.options.maxBoundsViscosity,
        view: [+b.getSouth().toFixed(6), +b.getWest().toFixed(6), +b.getNorth().toFixed(6), +b.getEast().toFixed(6)],
      };
    } else info = { note: '拿不到 map 实例' };
  } catch (e) { info = { err: String(e).slice(0,160) }; }
  return { leafletPresent: !!lm, mapW: lm?lm.clientWidth:null, mapH: lm?lm.clientHeight:null,
           cam: w.DATA ? JSON.stringify(w.DATA.campuses.gulou.frame) : null, ...info };
})()`), null, 2));

console.log("\n=== 判定 ===");
// 注意：Runtime.evaluate 的返回可能被包装，这里直接取字符串
const mbRaw = await evl(`(() => {
  const f=document.querySelector('iframe'); if(!f) return "";
  let w; try{w=f.contentWindow;}catch(e){return "";}
  try {
    return (w.map && w.map.options && w.map.options.maxBounds)
      ? String(w.map.options.maxBounds.toBBoxString()) : "";
  } catch(e) { return ""; }
})()`);
const mb = typeof mbRaw === "string" ? mbRaw : "";
if (!mb) {
  console.log("✗ 拿不到 maxBounds —— 页面可能还在唤醒/构建中，稍后重跑本脚本");
} else {
  const isNew = mb.includes("118.766207") && mb.includes("118.782631");
  const isOld = mb.includes("118.767759") || mb.includes("118.779466");
  console.log(`  maxBounds = ${mb}`);
  if (isNew) console.log("  ✓ 已是新版外框（校园+周边 150 m 缓冲）—— 部署成功");
  else if (isOld) console.log("  ✗ 仍是旧 bbox —— 还没部署完，稍后再试");
  else console.log("  ? 数值既不像新框也不像旧框，需人工看");
}

console.log("\n=== 控制台错误 ===");
console.log(errs.length ? errs.slice(0, 8).join("\n") : "（无）");

const shot = await send("Page.captureScreenshot", { format: "png" }, sid);
if (shot?.result?.data) writeFileSync(outPng, Buffer.from(shot.result.data, "base64"));
console.log("\n截图 ->", outPng);
ws.close(); child.kill(); process.exit(0);
