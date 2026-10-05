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
  // ⚠ 绕开本机系统代理：代理出口被 Streamlit 拉黑，线上会 HTTP 444
  //   （详见 cdp_dblclick_pick.mjs 里的注释）
  "--no-proxy-server",
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

// ============ 多图打卡点的弹窗：一次一幅 + 能切换 + **不许超出地图框** ============
// 用户 2026-10-05："点完序号，图可能会超出我的框框，导致看不到"。
// 老做法把一个点的所有作品竖着堆进弹窗（3 幅 ≈ 1100px > 地图容器高）⇒ 被裁。
// 新做法：一次只展示一幅 + 缩略图条切换。判据直接量**几何**：
// 弹窗矩形必须完整落在地图容器内（这条比"看起来对"可靠）。
const popTest = await inFrame(
  "typeof w.eval('typeof DATA') === 'string' && w.eval('typeof DATA') === 'object'",
  `(function(){
  const multi = (DATA.spots || []).find(sp => (sp.shots || []).filter(s => s.src).length > 1);
  if (!multi) return {skip: '本地没有多图打卡点'};
  const n = multi.shots.filter(s => s.src).length;
  const m = markerBySid[multi.sid];
  if (!m) return {err: 'no marker for ' + multi.sid};
  m.openPopup();
  const read = () => ((document.querySelector('.pnav span') || {}).textContent || '').trim();
  const mapBox = document.getElementById('map').getBoundingClientRect();
  const el = document.querySelector('.leaflet-popup');
  const r = el ? el.getBoundingClientRect() : null;
  const out = {sid: multi.sid, n: n,
               thumbs: document.querySelectorAll('.pthumbs img').length,
               first: read()};
  const t2 = document.querySelectorAll('.pthumbs img')[1];
  if (t2) t2.dispatchEvent(new MouseEvent('click', {bubbles:true, cancelable:true}));
  out.second = read();
  out.popH = r ? Math.round(r.height) : null;
  out.mapH = Math.round(mapBox.height);
  out.inside = r ? (r.top >= mapBox.top - 1 && r.bottom <= mapBox.bottom + 1
                    && r.left >= mapBox.left - 1 && r.right <= mapBox.right + 1) : null;
  // 量出**每条边差多少**（超出为负/正），不然只知道"不通过"没法修
  out.delta = r ? {
    top: Math.round(r.top - mapBox.top),          // <0 = 从上面冒出去
    bottom: Math.round(mapBox.bottom - r.bottom), // <0 = 从下面冒出去
    left: Math.round(r.left - mapBox.left),
    right: Math.round(mapBox.right - r.right),
  } : null;
  out.markerY = Math.round(m.getLatLng ? 0 : 0);
  return out;
})()`);
console.log("弹窗（多图）:", JSON.stringify(popTest));
record("多图打卡点：弹窗一次一幅 + 缩略图能切换",
  !!(popTest && !popTest.skip && popTest.thumbs === popTest.n
     && popTest.first.indexOf("第 1 / " + popTest.n + " 幅") >= 0
     && popTest.second.indexOf("第 2 / " + popTest.n + " 幅") >= 0),
  JSON.stringify(popTest));
record("弹窗完整落在地图框内（不再被裁掉）",
  !!(popTest && (popTest.skip || popTest.inside === true)),
  `弹窗高 ${popTest && popTest.popH}px / 地图高 ${popTest && popTest.mapH}px · inside=${popTest && popTest.inside}`);
// ============ 标记不做缩略图预览 + 地图里不出现姓名/昵称 ============
// 用户 2026-10-05：① "点完序号后退出来，预览框不会消失 —— 干脆就别要预览框了"
//                 ② "名字/昵称不要在打卡地图里显示"
// ⚠ 判据必须包含 **payload 里也不许有 author 字段**：只在渲染时藏起来不够 ——
//   名字留在 JSON 里，任何人看网页源码都能搜到，等于没藏。
//   （作者信息只在**管理后台**可见，那是登录后才能看的地方。）
const priv = await inFrame(
  "typeof w.eval('typeof DATA') === 'string' && w.eval('typeof DATA') === 'object'",
  `(function(){
  const authors = [];
  (DATA.spots || []).forEach(sp => (sp.shots || []).forEach(s => {
    if (s.author) authors.push(String(s.author));
  }));
  const payloadHasAuthor = (DATA.spots || []).some(sp =>
    (sp.shots || []).some(s => Object.prototype.hasOwnProperty.call(s, 'author')));
  const unresolvedHasAuthor = (DATA.unresolved || []).some(u =>
    Object.prototype.hasOwnProperty.call(u, 'author'));
  const icon = document.querySelector('.leaflet-marker-icon');
  if (icon) icon.dispatchEvent(new MouseEvent('click', {bubbles:true, cancelable:true}));
  const text = document.body.innerText || '';
  const cardText = Array.from(document.querySelectorAll('.card'))
                        .map(c => c.innerText).join(' ');
  return {
    nSpots: (DATA.spots || []).length, payloadHasAuthor, unresolvedHasAuthor,
    markerExtras: document.querySelectorAll('.pinwrap, img.cover').length,
    leakedOnPage: authors.filter(a => a && text.includes(a)).length,
    leakedOnCard: authors.filter(a => a && cardText.includes(a)).length,
    sampleBase64: authors.slice(0, 2).map(a => btoa(unescape(encodeURIComponent(a))).slice(0, 12)),
  };
})()`);
console.log("隐私/标记检查:", JSON.stringify(priv));
record("序号标记上不再有缩略图预览框",
  !!(priv && priv.markerExtras === 0), `markerExtras=${priv && priv.markerExtras}`);
record("姓名/昵称不在地图上出现（弹窗、列表都查）",
  !!(priv && priv.leakedOnPage === 0 && priv.leakedOnCard === 0),
  `弹窗/页面命中 ${priv && priv.leakedOnPage} · 列表命中 ${priv && priv.leakedOnCard}`);
record("姓名连 payload 里都没有（否则网页源码可搜到）",
  !!(priv && priv.payloadHasAuthor === false && priv.unresolvedHasAuthor === false),
  `spots 带 author=${priv && priv.payloadHasAuthor} · unresolved 带 author=${priv && priv.unresolvedHasAuthor}`);

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
