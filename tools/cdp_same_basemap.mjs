// **"成品地图的底图 == 投稿选机位那张图"** 的端到端验证（用户 2026-10-05 的要求）。
//
// 为什么不能只看代码：两边各自调用 `frame_image_for(...)` 也能"看起来对"，
// 但真正要保证的是——**同一张图**（同一个文件、同一份字节）。
// 所以这里把两页的 data URL 抓出来**逐字节比对**：
//   投稿页  → 组件里的 `D.img`
//   成品页  → 内嵌地图里的 `DATA.campuses[<校区>].relief`
// 相等才算过。顺带验：成品页默认底图就是 relief、overlay 图真的加载出来了。
//
// 用法: node tools/cdp_same_basemap.mjs [app_url]
import { spawn } from "node:child_process";
import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const root = (process.argv[2] || "http://localhost:8501/").replace(/\/$/, "") + "/";
const mapPage = root + encodeURIComponent("打卡点地图");
const port = 9581;
const EDGE = "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe";
const profile = mkdtempSync(join(tmpdir(), "cdp-samebase-"));
const child = spawn(EDGE, ["--headless=new", "--disable-gpu", "--no-first-run",
  `--remote-debugging-port=${port}`, `--user-data-dir=${profile}`,
  "--window-size=1500,1900", "--hide-scrollbars", "about:blank"], { stdio: "ignore" });
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

let ws;
for (let i = 0; i < 60; i++) {
  try {
    const j = await (await fetch(`http://127.0.0.1:${port}/json/version`)).json();
    if (j.webSocketDebuggerUrl) { ws = new WebSocket(j.webSocketDebuggerUrl); break; }
  } catch {}
  await sleep(300);
}
await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej; });
let id = 0; const pending = new Map(); const errs = [];
ws.onmessage = (e) => {
  const m = JSON.parse(e.data);
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
// 在 iframe 里求值：用"门牌"认哪个 iframe 才是我们要的
const inFrame = async (gate, inner, tries = 30, gap = 1500) => {
  const probe = "(function(){ for (const f of document.querySelectorAll('iframe')) {"
    + " try { const w = f.contentWindow; if (!w) continue;"
    + "  if (!(" + gate + ")) continue;"
    + "  return {v: w.eval(" + JSON.stringify(inner) + "), found:true};"
    + " } catch (e) {} } return {found:false}; })()";
  for (let i = 0; i < tries; i++) {
    const r = await ev(probe);
    if (r && r.found) return r.v;
    await sleep(gap);
  }
  return { __noframe: true };
};

const results = [];
const record = (name, ok, detail) => {
  results.push({ name, ok });
  console.log(`${ok ? "✓" : "✗"} ${name}${detail ? "  " + detail : ""}`);
};

// ---------- ① 投稿页：选机位那张图 ----------
await send("Page.navigate", { url: root }, sid);
const pick = await inFrame("w.eval(\"typeof doPick\") === 'function'",
  "({campus: D.campus, img: D.img})");
if (!pick || pick.__noframe) { console.log("✗ 投稿页组件没就绪"); try { child.kill(); } catch {} process.exit(1); }
console.log(`投稿页：校区=${pick.campus}，底图 data URL 长度=${pick.img.length}`);

// ---------- ② 成品页：内嵌地图的立体底图 ----------
await send("Page.navigate", { url: mapPage }, sid);
const got = await inFrame(
  "typeof w.eval('typeof DATA') === 'string' && w.eval('typeof DATA') === 'object'",
  `({base: baseLayer, nRelief: reliefLayers.length,
     relief: DATA.campuses['${pick.campus}'].relief,
     imgs: Array.from(document.querySelectorAll('img.leaflet-image-layer'))
             .map(i => ({w: i.naturalWidth, h: i.naturalHeight, op: i.style.opacity})),
     nCampuses: Object.keys(DATA.campuses).length,
     missing: Object.keys(DATA.campuses).filter(k => !DATA.campuses[k].relief)})`);
if (!got || got.__noframe) { console.log("✗ 成品页内嵌地图没就绪"); try { child.kill(); } catch {} process.exit(1); }
console.log("成品页:", JSON.stringify({ base: got.base, nRelief: got.nRelief, imgs: got.imgs, missing: got.missing }));

record("成品页默认底图就是自绘立体底图", got.base === "relief", `baseLayer=${got.base}`);
record("立体底图 overlay 已加进地图", got.nRelief >= 1, `${got.nRelief} 层`);
record("overlay 图片真的解码出来了（不是裂图）",
  Array.isArray(got.imgs) && got.imgs.some(i => i.w > 100), JSON.stringify(got.imgs));
const same = typeof got.relief === "string" && got.relief === pick.img;
record("★ 成品页底图与投稿选机位**是同一张图**（逐字节相同）", same,
  same ? `长度都是 ${pick.img.length}` : `成品页=${got.relief ? got.relief.length : "无"}，投稿页=${pick.img.length}`);
record("三个校区都带上了立体底图", Array.isArray(got.missing) && got.missing.length === 0,
  `缺：${JSON.stringify(got.missing)}`);

// 等地图铺好再截图（imageOverlay 是异步解码的）
await sleep(4000);
const shot = await send("Page.captureScreenshot", { format: "png" }, sid);
const outPng = join(tmpdir(), "sky_map_page_relief.png");
writeFileSync(outPng, Buffer.from(shot.result.data, "base64"));
console.log("截图:", outPng);
console.log("consoleErrors:", JSON.stringify(errs.slice(0, 5)));

const bad = results.filter(r => !r.ok);
console.log(bad.length ? `\n✗ ${bad.length} 项不通过` : "\n✓ 全部通过（底图已统一）");
try { child.kill(); } catch {}
process.exit(bad.length ? 1 : 0);
