// 精细对齐验证：同一 WGS84 点（北大楼）分别画在 OSM 与"高德+偏移"底图上，
// 并叠加该建筑的 OSM 轮廓。对齐则轮廓与影像中的北大楼重合、圆点落在楼上。
import { spawn } from "node:child_process";
import { mkdtempSync, writeFileSync, readFileSync, existsSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const OUT = process.argv[2] || join(tmpdir(), "align_check.png");
const GEOJSON = process.argv[3];
const port = 9340;
const EDGE = "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe";
const profile = mkdtempSync(join(tmpdir(), "cdp-al-"));
const child = spawn(EDGE, ["--headless=new", "--disable-gpu", "--no-first-run",
  `--remote-debugging-port=${port}`, `--user-data-dir=${profile}`,
  "--window-size=1600,900", "--hide-scrollbars", "about:blank"], { stdio: "ignore" });
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
const send = (method, params = {}, s) => new Promise((res) => {
  const mid = ++id; pending.set(mid, res);
  ws.send(JSON.stringify({ id: mid, method, params, ...(s ? { sessionId: s } : {}) }));
});
const { result: t } = await send("Target.createTarget", { url: "about:blank" });
const { result: att } = await send("Target.attachToTarget", { targetId: t.targetId, flatten: true });
const sid = att.sessionId;
await send("Runtime.enable", {}, sid);

// 取北大楼的 OSM 轮廓
let beidalou = null;
if (GEOJSON && existsSync(GEOJSON)) {
  const gj = JSON.parse(readFileSync(GEOJSON, "utf8"));
  const f = gj.features.find(x => (x.properties?.name || "").includes("北大楼"));
  if (f) {
    const rings = f.geometry.type === "Polygon" ? [f.geometry.coordinates[0]]
      : f.geometry.type === "MultiPolygon" ? f.geometry.coordinates.map(p => p[0]) : [];
    beidalou = rings.map(r => r.map(p => [p[1], p[0]]));
  }
}
console.log("北大楼轮廓环数:", beidalou ? beidalou.length : 0);

const CENTER = [32.0566, 118.7736], Z = 18;
const PAGE = `<!DOCTYPE html><html><head><meta charset="utf-8">
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<style>body{margin:0;background:#111;display:flex}.m{width:790px;height:790px;position:relative}
.tag{position:absolute;z-index:999;left:8px;top:8px;background:#000c;color:#fff;padding:4px 10px;
border-radius:6px;font:13px sans-serif}</style></head><body>
<div class="m" id="a"><div class="tag">A: OSM(WGS84) + 北大楼轮廓</div></div>
<div class="m" id="b"><div class="tag">B: 高德卫星 + GCJ偏移 + 平移后的轮廓</div></div>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script>
const PI=Math.PI, A_=6378245.0, EE=0.00669342162296594323;
function out(lat,lon){return !(72.004<=lon&&lon<=137.8347&&0.8293<=lat&&lat<=55.8271);}
function tfLat(x,y){let r=-100+2*x+3*y+0.2*y*y+0.1*x*y+0.2*Math.sqrt(Math.abs(x));
 r+=(20*Math.sin(6*x*PI)+20*Math.sin(2*x*PI))*2/3;r+=(20*Math.sin(y*PI)+40*Math.sin(y/3*PI))*2/3;
 r+=(160*Math.sin(y/12*PI)+320*Math.sin(y*PI/30))*2/3;return r;}
function tfLon(x,y){let r=300+x+2*y+0.1*x*x+0.1*x*y+0.1*Math.sqrt(Math.abs(x));
 r+=(20*Math.sin(6*x*PI)+20*Math.sin(2*x*PI))*2/3;r+=(20*Math.sin(x*PI)+40*Math.sin(x/3*PI))*2/3;
 r+=(150*Math.sin(x/12*PI)+300*Math.sin(x/30*PI))*2/3;return r;}
function wgs2gcj(lat,lon){ if(out(lat,lon)) return [lat,lon];
 const d0=tfLat(lon-105,lat-35), d1=tfLon(lon-105,lat-35);
 const rad=lat/180*PI; const magic=1-EE*Math.sin(rad)**2; const sm=Math.sqrt(magic);
 return [lat+(d0*180)/((A_*(1-EE))/(magic*sm)*PI), lon+(d1*180)/(A_/sm*Math.cos(rad)*PI)];}
const W=2*PI*6378137;
const OFF = (()=>{const g=wgs2gcj(${CENTER[0]},${CENTER[1]});
 const px=lo=>lo*W/360, py=la=>Math.log(Math.tan((90+la)*PI/360))*6378137;
 return [px(g[1])-px(${CENTER[1]}), py(g[0])-py(${CENTER[0]})];})();

const ring = ${JSON.stringify(beidalou || [])};
const A = L.map('a',{zoomControl:false,attributionControl:false}).setView(${JSON.stringify(CENTER)},${Z});
const B = L.map('b',{zoomControl:false,attributionControl:false}).setView(${JSON.stringify(CENTER)},${Z});
L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png',{maxZoom:19}).addTo(A);
const T = L.TileLayer.extend({getTileUrl(c){
  const z=this._getZoomForUrl(), s=Math.pow(2,z);
  const cx=c.x+(OFF[0]/W*s), cy=c.y-(OFF[1]/W*s);
  return L.Util.template(this._url,{x:Math.round(cx),y:Math.round(cy),z:z,s:1});}});
new T('https://webst0{s}.is.autonavi.com/appmaptile?style=6&x={x}&y={y}&z={z}',{maxZoom:18,noWrap:true}).addTo(B);

ring.forEach(r=>{
  L.polygon(r,{color:'#ff3b30',weight:2,fill:false}).addTo(A);
  L.polygon(r.map(p=>[p[0]+OFF[1]/111320, p[1]+OFF[0]/(111320*Math.cos(p[0]*PI/180))]),
            {color:'#ff3b30',weight:2,fill:false}).addTo(B);
});
// 同一个 WGS84 点
L.circleMarker(${JSON.stringify(CENTER)},{radius:5,color:'#fff',weight:2,fillColor:'#ff3b30',fillOpacity:1}).addTo(A);
L.circleMarker([${CENTER[0]}+OFF[1]/111320, ${CENTER[1]}+OFF[0]/(111320*Math.cos(${CENTER[0]}*PI/180))],
  {radius:5,color:'#fff',weight:2,fillColor:'#ff3b30',fillOpacity:1}).addTo(B);
window.__off = OFF;
</script></body></html>`;

await send("Page.navigate", { url: "data:text/html;charset=utf-8," + encodeURIComponent(PAGE) }, sid);
await sleep(13000);
const probe = await send("Runtime.evaluate", { expression: "({off: window.__off, ready: true})", returnByValue: true }, sid);
console.log("偏移(米):", JSON.stringify(probe.result?.result?.value));
const shot = await send("Page.captureScreenshot", { format: "png" });
if (shot?.error) console.log("截图错误:", JSON.stringify(shot.error));
if (shot?.result?.data) {
  writeFileSync(OUT, Buffer.from(shot.result.data, "base64"));
  console.log("对齐图 ->", OUT);
} else {
  console.log("截图无数据");
}
ws.close(); child.kill(); process.exit(0);
