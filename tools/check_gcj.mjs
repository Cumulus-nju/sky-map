// 校验高德卫星底图的 GCJ-02 纠偏是否与 OSM（WGS84）对齐。
// 做法：同一块 Web Mercator 地面范围，分别用 OSM 和高德(纠偏)渲染 700x700，
//       然后叠在一张图上对比 —— 对齐则建筑轮廓重合，错位则重影。
import { spawn } from "node:child_process";
import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const OUT = process.argv[2] || join(tmpdir(), "gjcheck.png");
const port = 9337;
const EDGE = "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe";
const profile = mkdtempSync(join(tmpdir(), "cdp-gj-"));
const child = spawn(EDGE, ["--headless=new", "--disable-gpu", "--no-first-run",
  `--remote-debugging-port=${port}`, `--user-data-dir=${profile}`,
  "--window-size=1500,800", "--hide-scrollbars", "about:blank"], { stdio: "ignore" });
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
const send = (m, p = {}, s) => new Promise((res) => {
  const mid = ++id; pending.set(mid, res);
  ws.send(JSON.stringify({ id: mid, method: m, params: p, ...(s ? { sessionId: s } : {}) }));
});
const { result: t } = await send("Target.createTarget", { url: "about:blank" });
const { result: att } = await send("Target.attachToTarget", { targetId: t.targetId, flatten: true });
const sid = att.sessionId;
await send("Runtime.enable", {}, sid);

// 鼓楼校区：北大楼一带，z=17 的两张相邻瓦片范围做底
const Z = 17, X = 108780, Y = 53203;
const PAGE = `<!DOCTYPE html><html><head><meta charset="utf-8">
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<style>body{margin:0;background:#111}.row{display:flex}.m{width:700px;height:700px;position:relative}
.tag{position:absolute;z-index:999;left:8px;top:8px;background:#000c;color:#fff;padding:3px 9px;
border-radius:6px;font:12px sans-serif}</style></head><body>
<div class="row"><div class="m" id="a"><div class="tag" id="ta">A</div></div><div class="m" id="b"><div class="tag" id="tb">B</div></div></div>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script>
const Z=${Z}, CX=${X}+0.5, CY=${Y}+0.5;
// 取该瓦片中心为视图中心
function tileCenter(z,x,y){
  const n=Math.pow(2,z);
  const lon=(x/n)*360-180;
  const lat=Math.atan(Math.sinh(Math.PI*(1-2*y/n)))*180/Math.PI;
  return [lat,lon];
}
const CENTER = tileCenter(Z, CX, CY);
const A = L.map('a',{zoomControl:false,attributionControl:false}).setView(CENTER,Z);
const B = L.map('b',{zoomControl:false,attributionControl:false}).setView(CENTER,Z);
L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png',{maxZoom:19}).addTo(A);

/* GCJ-02 纠偏（与 map_build.py 同一套公式） */
const PI=Math.PI, A_=6378245.0, EE=0.00669342162296594323;
function tfLat(x,y){let r=-100+2*x+3*y+0.2*y*y+0.1*x*y+0.2*Math.sqrt(Math.abs(x));
 r+=(20*Math.sin(6*x*PI)+20*Math.sin(2*x*PI))*2/3; r+=(20*Math.sin(y*PI)+40*Math.sin(y/3*PI))*2/3;
 r+=(160*Math.sin(y/12*PI)+320*Math.sin(y*PI/30))*2/3; return r;}
function tfLon(x,y){let r=300+x+2*y+0.1*x*x+0.1*x*y+0.1*Math.sqrt(Math.abs(x));
 r+=(20*Math.sin(6*x*PI)+20*Math.sin(2*x*PI))*2/3; r+=(20*Math.sin(x*PI)+40*Math.sin(x/3*PI))*2/3;
 r+=(150*Math.sin(x/12*PI)+300*Math.sin(x/30*PI))*2/3; return r;}
function wgs2gcj(lat,lon){
 const dlat0=tfLat(lon-105,lat-35), dlon0=tfLon(lon-105,lat-35);
 const rad=lat/180*PI; let magic=1-EE*Math.sin(rad)**2; const sm=Math.sqrt(magic);
 const dlat=(dlat0*180)/((A_*(1-EE))/(magic*sm)*PI);
 const dlon=(dlon0*180)/(A_/sm*Math.cos(rad)*PI);
 return [lat+dlat, lon+dlon];
}
function offsetMeters(lat,lon){
 const [glat,glon]=wgs2gcj(lat,lon);
 const W=2*PI*6378137;
 const px=lo=>lo*W/360, py=la=>Math.log(Math.tan((90+la)*PI/360))*6378137;
 return [px(glon)-px(lon), py(glat)-py(lat)];
}
const OFF = offsetMeters(CENTER[0],CENTER[1]);
const T = L.TileLayer.extend({
  getTileUrl(coords){
    const z=this._getZoomForUrl(), s=Math.pow(2,z);
    const cx=coords.x+(OFF[0]/(2*PI*6378137)*s), cy=coords.y-(OFF[1]/(2*PI*6378137)*s);
    return L.Util.template(this._url,{x:Math.round(cx),y:Math.round(cy),z:z,s:1});
  }
});
new T('https://webst0{s}.is.autonavi.com/appmaptile?style=6&x={x}&y={y}&z={z}',
      {maxZoom:18,noWrap:true}).addTo(B);
document.getElementById('ta').textContent='OSM (WGS84)';
document.getElementById('tb').textContent='高德卫星 (已 GCJ-02 纠偏)';
window.__ready=false;
setTimeout(()=>{window.__ready=true;},11000);
</script></body></html>`;

const dataUrl = "data:text/html;charset=utf-8," + encodeURIComponent(PAGE);
await send("Page.navigate", { url: dataUrl }, sid);
await sleep(14000);
const { result: shot } = await send("Page.captureScreenshot", { format: "png", clip: { x: 0, y: 0, width: 1400, height: 700, scale: 1 } }, sid);
if (shot?.data) writeFileSync(OUT, Buffer.from(shot.data, "base64"));
console.log("对比图 ->", OUT);
ws.close(); child.kill(); process.exit(0);
