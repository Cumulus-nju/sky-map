// 验证「外框锁视野」是否真的生效：
//   1) 载入生成的地图 HTML
//   2) 读 map.options 里的 maxBounds / maxBoundsViscosity / minZoom
//   3) 故意 zoomOut 多次，确认缩不回框外
//   4) 确认外框四角在视野内
// 用法: node tools/cdp_frame_check.mjs <map.html> [screenshot.png]
import { spawn } from "node:child_process";
import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const url = process.argv[2];
const outPng = process.argv[3] || join(tmpdir(), "frame.png");
const port = 9377;
const EDGE = "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe";
const profile = mkdtempSync(join(tmpdir(), "cdp-f-"));
const child = spawn(EDGE, ["--headless=new", "--disable-gpu", "--no-first-run",
  `--remote-debugging-port=${port}`, `--user-data-dir=${profile}`,
  "--window-size=1400,900", "--hide-scrollbars", "about:blank"], { stdio: "ignore" });
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
let id = 0; const pending = new Map();
ws.onmessage = (ev) => { const m = JSON.parse(ev.data); if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); } };
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
await sleep(15000);

const evl = async (expr) => {
  const r = await send("Runtime.evaluate", { expression: expr, returnByValue: true }, sid);
  if (r.result?.exceptionDetails) return { __err: r.result.exceptionDetails.text };
  return r.result?.result?.value ?? r.result;
};

console.log("=== 1) 地图与外框配置 ===");
console.log(JSON.stringify(await evl(`(() => {
  if(typeof map === 'undefined' || !map) return {err:'map 未定义（脚本没跑起来？）'};
  const f = DATA.campuses.gulou.frame;
  return {
    campus: currentCampus,
    zoom: map.getZoom(),
    minZoom: map.getMinZoom(),
    maxZoom: map.getMaxZoom(),
    bounds: map.getBounds().toBBoxString(),
    maxBounds: map.options.maxBounds ? map.options.maxBounds.toBBoxString() : null,
    viscosity: map.options.maxBoundsViscosity,
    frame: f,
    frameSize: DATA.campuses.gulou.frameSize,
    center: DATA.campuses.gulou.center,
    fitZoom: frameFitZoom('gulou'),
  };
})()`), null, 2));

console.log("\n=== 2) 狂缩 15 次，看能否缩出框外 ===");
console.log(JSON.stringify(await evl(`(() => {
  const before = map.getZoom();
  for(let i=0;i<15;i++) map.zoomOut();
  return { before, after: map.getZoom(), minZoom: map.getMinZoom(),
           boundsAfter: map.getBounds().toBBoxString(),
           frame: DATA.campuses.gulou.frame };
})()`), null, 2));

console.log("\n=== 3) 判断框是否仍在视野内（四角是否都可见）===");
console.log(JSON.stringify(await evl(`(() => {
  const f = DATA.campuses.gulou.frame;
  const b = map.getBounds();
  const sw = L.latLng(f[0], f[1]), ne = L.latLng(f[2], f[3]);
  return {
    frame_sw_visible: b.contains(sw),
    frame_ne_visible: b.contains(ne),
    frameInsideView: b.contains(sw) && b.contains(ne),
    viewSouth: +b.getSouth().toFixed(6), viewNorth: +b.getNorth().toFixed(6),
    viewWest: +b.getWest().toFixed(6),  viewEast: +b.getEast().toFixed(6),
    frameSouth: f[0], frameNorth: f[2], frameWest: f[1], frameEast: f[3],
  };
})()`), null, 2));

console.log("\n=== 4) 尝试平移到框外，看边界是否硬限制 ===");
console.log(JSON.stringify(await evl(`(() => {
  map.setView([35.0, 119.5], map.getZoom(), {animate:false});   // 故意跑到南京东北远处
  const b = map.getBounds();
  const mb = map.options.maxBounds;
  return {
    attempted: [35.0, 119.5],
    centerAfter: [+map.getCenter().lat.toFixed(6), +map.getCenter().lng.toFixed(6)],
    withinMaxBounds: mb ? mb.contains(map.getCenter()) : null,
    viewStillOverlapsFrame: b.overlaps(L.latLngBounds([[DATA.campuses.gulou.frame[0],DATA.campuses.gulou.frame[1]],[DATA.campuses.gulou.frame[2],DATA.campuses.gulou.frame[3]]])),
  };
})()`), null, 2));

console.log("\n=== 5) 切到仙林 / 苏州，读数是否随之变化 ===");
for (const key of ["xianlin", "suzhou"]) {
  const r = await evl(`(() => {
    switchCampus('${key}');
    const f = DATA.campuses['${key}'].frame;
    return { campus: currentCampus, zoom: map.getZoom(), minZoom: map.getMinZoom(),
             fitZoom: frameFitZoom('${key}'),
             maxBounds: map.options.maxBounds.toBBoxString(),
             frame: f, size: DATA.campuses['${key}'].frameSize };
  })()`);
  console.log(`  ${key}: ` + JSON.stringify(r));
  await sleep(1200);
}

// 回到鼓楼并截图
await evl(`switchCampus('gulou')`);
await sleep(2500);
const shot = await send("Page.captureScreenshot", { format: "png" }, sid);
if (shot?.result?.data) writeFileSync(outPng, Buffer.from(shot.result.data, "base64"));
console.log("\n截图 ->", outPng);
ws.close(); child.kill(); process.exit(0);
