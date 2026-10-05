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

// ============ 弹窗内容：多图时"一次一幅 + 缩略图切换" ============
// ⚠ 聚类阈值收到 5 m 后，本地演示数据**不再合并**（21 幅 = 21 个点），
//   所以造不出真实的多图点 —— 直接拿现有 spot **复制一份 shots** 喂给 shotPanel()，
//   验的是"面板会不会把多幅全堆出来"这件正事（新设计必须只有 1 张主图 + 缩略图条）。
const panel = await inFrame(
  "typeof w.eval('typeof DATA') === 'string' && w.eval('typeof DATA') === 'object'",
  `(function(){
  const base = (DATA.spots || []).find(sp => (sp.shots || []).length > 0);
  if (!base) return {skip: '本地没有带作品的点'};
  const sp = JSON.parse(JSON.stringify(base));
  sp.shots = sp.shots.concat(JSON.parse(JSON.stringify(sp.shots)));   // 变成 2 幅
  const n = sp.shots.length;
  const html1 = shotPanel(sp, 0), html2 = shotPanel(sp, 1);
  const count = (s, re) => (s.match(re) || []).length;
  return {n: n,
    oneMain: [count(html1, /class="pmain"/g), count(html2, /class="pmain"/g)],
    thumbs: [count(html1, /class="pthumbs"/g), count(html2, /class="pthumbs"/g)],
    thumbImgs: count(html1, /data-i="/g),
    head1: (html1.match(/第 \\d+ \\/ \\d+ 幅/) || [])[0] || '',
    head2: (html2.match(/第 \\d+ \\/ \\d+ 幅/) || [])[0] || ''};
})()`);
console.log("面板（合成 2 幅）:", JSON.stringify(panel));
record("多图：面板一次只放一张主图 + 缩略图条 + 第 i/N 幅",
  !!(panel && !panel.skip && panel.oneMain[0] === 1 && panel.oneMain[1] === 1
     && panel.thumbs[0] === 1 && panel.thumbs[1] === 1 && panel.thumbImgs >= 2
     && /第 1 \/ \d+ 幅/.test(panel.head1) && /第 2 \/ \d+ 幅/.test(panel.head2)),
  JSON.stringify(panel));

// ============ 点弹窗**内部**不能把弹窗关掉 ============
// 用户 2026-10-05："点击画之间切换的时候，预览框会消失，需要重新点序号才能再看到"。
// 根因：点击冒泡到地图 ⇒ 触发 Leaflet 默认的"点地图关弹窗"。
const stayOpen = await inFrame(
  "typeof w.eval('typeof DATA') === 'string' && w.eval('typeof DATA') === 'object'",
  `(function(){
  const sid = Object.keys(markerBySid)[0];
  if (!sid) return {err: 'no marker'};
  map.closePopup();
  markerBySid[sid].openPopup();
  const el = document.querySelector('.leaflet-popup');
  if (!el) return {err: 'popup not open'};
  const inner = el.querySelector('.leaflet-popup-content') || el;
  inner.dispatchEvent(new MouseEvent('click', {bubbles:true, cancelable:true}));
  return {after: !!document.querySelector('.leaflet-popup')};
})()`);
console.log("点弹窗内部:", JSON.stringify(stayOpen));
record("点弹窗内部（换图/看内容）不会把弹窗关掉",
  !!(stayOpen && stayOpen.after === true), JSON.stringify(stayOpen));

// ============ 逐个序号量一遍：**每个可见**弹窗都得完整落在框内 ============
// 用户 2026-10-05："在没放大地图的时候，点序号后图还是会超出去"。
// 只测一个序号不够 —— 靠近上边缘的那个才是最难的（上方没地方，得翻到下面去）。
// ⚠ 只扫**当前视野内可见**的序号：视野锁在本校区外框内，别的校区的点用户根本点不到，
//   量它们只会得到"偏出几万像素"这种没意义的数（第一版就这么误报过）。
const sweepSids = await inFrame(
  "typeof w.eval('typeof DATA') === 'string' && w.eval('typeof DATA') === 'object'",
  `Object.keys(markerBySid).filter(sid =>
     map.getBounds().contains(markerBySid[sid].getLatLng()))`);
const sweep = [];
for (let i = 0; i < (sweepSids || []).length; i++) {
  const sid = sweepSids[i];
  await inFrame(
    "typeof w.eval('typeof DATA') === 'string' && w.eval('typeof DATA') === 'object'",
    `(function(){ map.closePopup(); markerBySid[${JSON.stringify(sid)}].openPopup(); return 1; })()`);
  await sleep(430);          // 等图片加载 + keepPopupInView 的重排跑完
  const m = await inFrame(
    "typeof w.eval('typeof DATA') === 'string' && w.eval('typeof DATA') === 'object'",
    `(function(){
      const el = document.querySelector('.leaflet-popup');
      const box = document.getElementById('map').getBoundingClientRect();
      if (!el) return {sid: ${JSON.stringify(sid)}, missing: true};
      const b = el.getBoundingClientRect();
      return {sid: ${JSON.stringify(sid)}, h: Math.round(b.height),
              top: Math.round(b.top - box.top), bottom: Math.round(box.bottom - b.bottom),
              left: Math.round(b.left - box.left), right: Math.round(box.right - b.right),
              flipped: el.classList.contains('pop-below')};
    })()`);
  if (m) sweep.push(m);
}
const worst = sweep.reduce((a, b) => {
  const score = x => Math.min(x.top, x.bottom, x.left, x.right);
  return (!a || score(b) < score(a)) ? b : a;
}, null);
const allInside = sweep.length > 0 && sweep.every(x => !x.missing
  && x.top >= -1 && x.bottom >= -1 && x.left >= -1 && x.right >= -1);
console.log(`逐个数号量弹窗：可见 ${sweep.length} 个，最差 ->`, JSON.stringify(worst));
console.log("  翻转(pop-below)的序号数:", sweep.filter(x => x.flipped).length);
record("每个**可见**序号的弹窗都完整落在框内（含未放大时靠边的）", allInside,
  `共 ${sweep.length} 个 · 最差边距 top=${worst && worst.top} bottom=${worst && worst.bottom} `
  + `left=${worst && worst.left} right=${worst && worst.right}（≥0 才算在里面）`);
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
