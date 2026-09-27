// 在真实浏览器里逐一测瓦片源：能否取到 + 图片是否有内容（排除 API KEY 占位图）
import { spawn } from "node:child_process";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const port = 9336;
const EDGE = "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe";
const profile = mkdtempSync(join(tmpdir(), "cdp-tile-"));
const child = spawn(EDGE, ["--headless=new", "--disable-gpu", "--no-first-run",
  `--remote-debugging-port=${port}`, `--user-data-dir=${profile}`, "about:blank"], { stdio: "ignore" });
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
let id = 0; const pending = new Map();
ws.onmessage = (ev) => { const m = JSON.parse(ev.data); if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); } };
const send = (method, params = {}, sessionId) => new Promise((res) => {
  const mid = ++id; pending.set(mid, res);
  ws.send(JSON.stringify({ id: mid, method, params, ...(sessionId ? { sessionId } : {}) }));
});
const { result: t } = await send("Target.createTarget", { url: "about:blank" });
const { result: att } = await send("Target.attachToTarget", { targetId: t.targetId, flatten: true });
const sid = att.sessionId;
await send("Runtime.enable", {}, sid);

// z=17 鼓楼中心
const Z = 17, X = 108780, Y = 53203;
const tiles = {
  osm: `https://tile.openstreetmap.org/${Z}/${X}/${Y}.png`,
  osm_de: `https://tile.openstreetmap.de/${Z}/${X}/${Y}.png`,
  opentopomap: `https://a.tile.opentopomap.org/${Z}/${X}/${Y}.png`,
  carto_light: `https://a.basemaps.cartocdn.com/light_all/${Z}/${X}/${Y}.png`,
  gaode_street: `https://webrd01.is.autonavi.com/appmaptile?lang=zh_cn&size=1&scale=1&style=8&x=${X}&y=${Y}&z=${Z}`,
  gaode_sat: `https://webst01.is.autonavi.com/appmaptile?style=6&x=${X}&y=${Y}&z=${Z}`,
  esri_imagery: `https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/${Z}/${Y}/${X}`,
  esri_street: `https://server.arcgisonline.com/ArcGIS/rest/services/World_Street_Map/MapServer/tile/${Z}/${Y}/${X}`,
  arcgis_topo: `https://services.arcgisonline.com/ArcGIS/rest/services/World_Topo_Map/MapServer/tile/${Z}/${Y}/${X}`,
  tianditu_vec: `https://t0.tianditu.gov.cn/DataServer?T=vec_w&x=${X}&y=${Y}&l=${Z}`,
};

const expr = `(async () => {
  const urls = ${JSON.stringify(tiles)};
  const out = {};
  for (const [k, u] of Object.entries(urls)) {
    out[k] = await new Promise((res) => {
      const img = new Image();
      img.crossOrigin = 'anonymous';
      const timer = setTimeout(() => res({ ok: false, reason: 'timeout' }), 20000);
      img.onload = () => {
        clearTimeout(timer);
        // 抽 6x6 采样点判断是否纯色 / 占位图
        try {
          const c = document.createElement('canvas'); c.width = 32; c.height = 32;
          const g = c.getContext('2d'); g.drawImage(img, 0, 0, 32, 32);
          const d = g.getImageData(0, 0, 32, 32).data;
          const set = new Set();
          for (let i = 0; i < d.length; i += 4) set.add(d[i] + ',' + d[i+1] + ',' + d[i+2]);
          res({ ok: true, w: img.naturalWidth, h: img.naturalHeight, uniq: set.size });
        } catch (e) { res({ ok: true, w: img.naturalWidth, h: img.naturalHeight, uniq: -1, note: 'canvas tainted' }); }
      };
      img.onerror = () => { clearTimeout(timer); res({ ok: false, reason: 'load-error' }); };
      img.src = u;
    });
  }
  return out;
})()`;

const r = await send("Runtime.evaluate", { expression: expr, returnByValue: true, awaitPromise: true }, sid);
const val = r.result?.result?.value;
console.log("浏览器内瓦片源实测（uniq 颜色数越高越像真地图；个位数=占位图）\n");
for (const [k, v] of Object.entries(val || {})) {
  const verdict = !v.ok ? `✗ ${v.reason}` : (v.uniq > 300 ? "✓ 正常" : v.uniq > 60 ? "△ 偏简" : "✗ 疑似占位图");
  console.log(`${k.padEnd(14)} ${verdict}  ${v.ok ? `${v.w}x${v.h} uniq=${v.uniq}` : ""}`);
}
ws.close(); child.kill(); process.exit(0);
