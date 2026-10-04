// **线上完整投稿链路**端到端测试（可 dry-run）。
//
// 为什么要这个：文档里记着"完整投稿链路（网页提交→存库→地图显示）仍未实测，
// 是唯一未验证环节"。本地测过，但线上（Streamlit Cloud + Supabase）从没跑通。
//
// 关键背景（2026-10-04 查明，两次踩坑）：
//   1. 线上页面有多个执行上下文，**默认那个只是外层空壳**（bodyLen=0）；
//      真正的应用在另一个上下文（`stAppViewContainer` 在里面）。
//      ⇒ 必须按 `document.querySelector('[data-testid=stAppViewContainer]')` 定位。
//   2. Streamlit **渐进渲染**：stApp 出现时只有上半页，上传控件/提交按钮还没出来。
//      ⇒ 必须等 `input[type=file]` 出现才算整页就绪。
//
// 填表方式：不派发鼠标坐标（表单在可滚动容器里、且应用在子 frame 中），
// 而是**直接在应用上下文里操作 DOM** + 用 CDP `DOM.setFileInputFiles` 传文件。
// Streamlit 的输入框要走 React 的 value setter + input/change 事件 + blur 才会生效。
//
// 用法:
//   node tools/cdp_live_submit_e2e.mjs --dry-run     # 只填到"提交前"，不点提交
//   node tools/cdp_live_submit_e2e.mjs --submit      # 真的提交（会写入线上库！）
import { spawn } from "node:child_process";
import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const url = "https://azzjin9anwdbyykyd3dyh6.streamlit.app/";
const PHOTO = "C:\\Users\\Administrator\\.dsh\\sky_map\\data\\photos\\demo_P0001.jpg";
const DO_SUBMIT = process.argv.includes("--submit");
const TITLE = "【链路自测】自动化投稿";
const AUTHOR = "链路自测（可删）";

const port = 9581;
const EDGE = "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe";
const profile = mkdtempSync(join(tmpdir(), "cdp-submit-"));
const child = spawn(EDGE, ["--headless=new", "--disable-gpu", "--no-first-run",
  `--remote-debugging-port=${port}`, `--user-data-dir=${profile}`,
  "--window-size=1500,1700", "--hide-scrollbars", "about:blank"], { stdio: "ignore" });
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
const contexts = [];
let id = 0; const pending = new Map(); const errs = [];
ws.onmessage = (ev) => {
  const m = JSON.parse(ev.data);
  if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); return; }
  if (m.method === "Runtime.executionContextCreated") {
    contexts.push({ id: m.params.context.id, origin: m.params.context.origin });
  }
  if (m.method === "Runtime.consoleAPICalled" && m.params.type === "error")
    errs.push((m.params.args || []).map(a => a.value ?? a.description).join(" ").slice(0, 160));
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
await send("DOM.enable", {}, sid);
await send("Page.navigate", { url }, sid);

// returnByValue=false 时返回 objectId，供 DOM.setFileInputFiles 用
const evalRaw = async (expr, ctxId, byValue = true) => {
  const r = await send("Runtime.evaluate",
    { expression: expr, returnByValue: byValue, contextId: ctxId }, sid);
  return r.result;
};
const evIn = async (expr, ctxId) => {
  const r = await send("Runtime.evaluate",
    { expression: expr, returnByValue: true, contextId: ctxId }, sid);
  if (r.result?.exceptionDetails) {
    return { __err: r.result.exceptionDetails.exception?.description
                    || r.result.exceptionDetails.text };
  }
  return r.result?.result?.value;
};

const steps = [];
const step = (name, ok, detail = "") => {
  steps.push({ name, ok, detail });
  console.log(`  ${ok ? "✓" : "✗"} ${name}${detail ? "  " + detail : ""}`);
};

// ---------------------------------------------------------------- 找上下文
let appCtx = null, pickCtx = null;
for (let i = 0; i < 40; i++) {
  await sleep(3000);
  for (const c of contexts.slice()) {
    if (!appCtx) {
      const has = await evIn(
        "(!!document.querySelector('[data-testid=\"stAppViewContainer\"]'))", c.id);
      if (has === true) appCtx = c;
    }
    if (!pickCtx) {
      const f = await evIn("(typeof snapToFootprint)", c.id);
      if (f === "function") pickCtx = c;
    }
  }
  if (appCtx && pickCtx) break;
  if (appCtx) {
    const ready = await evIn("(!!document.querySelector('input[type=file]'))", appCtx.id);
    if (ready === true && pickCtx) break;
  }
}
console.log(`应用上下文 ctx=${appCtx ? appCtx.id : "?"}，点选组件上下文 ctx=${pickCtx ? pickCtx.id : "?"}`);
if (!appCtx) { console.log("✗ 找不到应用上下文"); try { child.kill(); } catch {} process.exit(1); }

// 等整页就绪
let ready = false;
for (let i = 0; i < 30; i++) {
  if (await evIn("(!!document.querySelector('input[type=file]'))", appCtx.id) === true) {
    ready = true; break;
  }
  await sleep(4000);
}
await sleep(4000);

// ---------------------------------------------------------------- 0. 基线
const baseline = await evIn(`(() => {
  const t = document.body.innerText || '';
  const m = t.match(/已收稿\\s*([0-9]+)\\s*幅/);
  return {count: m ? parseInt(m[1]) : null, hasSubmit: t.includes('提交投稿')};
})()`, appCtx.id);
console.log(`\n整页就绪=${ready}  基线已收稿=${JSON.stringify(baseline)}`);
step("页面就绪（含上传控件与提交按钮）", ready && baseline && baseline.hasSubmit === true);

// ---------------------------------------------------------------- 1. 选点
if (pickCtx) {
  const picked = await evIn(`(() => {
    const R = D.roofs;
    if (!R || !R.b) return {err:'no roofs'};
    for (let i = R.b.length - 1; i >= 0; i--) {
      const dz = R.b[i][0], flat = R.b[i][1], m = flat.length/2;
      if (dz < 30) continue;
      let minx=1e9,maxx=-1e9,miny=1e9,maxy=-1e9;
      for (let k=0;k<m;k++){const x=flat[2*k],y=flat[2*k+1];
        if(x<minx)minx=x; if(x>maxx)maxx=x; if(y<miny)miny=y; if(y>maxy)maxy=y;}
      const px=(minx+maxx)/2, py=(miny+maxy)/2;
      if (!inPoly(px,py,flat)) continue;
      const g = snapToFootprint(px, py);
      const w = winW(), h = winH(), scale = vw()/w;
      const sx = (px-(cx-w/2))*scale, sy = (py-(cy-h/2))*scale;
      if (sx < 5 || sy < 5 || sx > vw()-5 || sy > vh()-5) continue;
      frame.dispatchEvent(new MouseEvent('contextmenu',
        {clientX: sx, clientY: sy, bubbles: true, cancelable: true, button: 2}));
      return {dz: dz, want: imgPxToLatLng(g[0], g[1])};
    }
    return {err:'no suitable roof'};
  })()`, pickCtx.id);
  console.log(`  选点结果: ${JSON.stringify(picked)}`);
  step("在图上点选机位（含屋顶吸附）", !!(picked && picked.want));
  await sleep(9000);           // 等 Streamlit rerun 把坐标落进 session
}

// ---------------------------------------------------------------- 2. 填表
// Streamlit 的输入框必须走 React 的 value setter，否则 React 状态不更新。
const SETFN = `
  window.__dshSet = function(sel, val) {
    const el = document.querySelector(sel);
    if (!el) return 'not-found:' + sel;
    const proto = el.tagName === 'TEXTAREA'
      ? window.HTMLTextAreaElement.prototype : window.HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
    setter.call(el, val);
    el.dispatchEvent(new Event('input', { bubbles: true }));
    el.dispatchEvent(new Event('change', { bubbles: true }));
    el.dispatchEvent(new KeyboardEvent('keydown',
      {key:'Enter', code:'Enter', keyCode:13, which:13, bubbles:true}));
    el.blur();
    return 'ok';
  };
  window.__dshByLabel = function(text) {
    for (const w of document.querySelectorAll(
        '[data-testid="stTextInput"], [data-testid="stTextArea"]')) {
      if ((w.innerText || '').includes(text)) return w.querySelector('input, textarea');
    }
    return null;
  };
`;
await evIn(SETFN, appCtx.id);

const fill = async (label, value, waitMs = 7000) => {
  const r = await evIn(`(() => {
    const el = window.__dshByLabel(${JSON.stringify(label)});
    if (!el) return 'label-not-found';
    const proto = el.tagName === 'TEXTAREA'
      ? window.HTMLTextAreaElement.prototype : window.HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
    setter.call(el, ${JSON.stringify(value)});
    el.dispatchEvent(new Event('input', { bubbles: true }));
    el.dispatchEvent(new Event('change', { bubbles: true }));
    el.dispatchEvent(new KeyboardEvent('keydown',
      {key:'Enter', code:'Enter', keyCode:13, which:13, bubbles:true}));
    el.blur();
    return 'ok';
  })()`, appCtx.id);
  await sleep(waitMs);
  return r;
};

const rTitle = await fill("作品名", TITLE);
step("填「作品名」", rTitle === "ok", String(rTitle));
const rAuthor = await fill("姓名 / 昵称", AUTHOR);
step("填「姓名 / 昵称」", rAuthor === "ok", String(rAuthor));
const rLoc = await fill("位置描述", "北大楼前草坪（链路自测）", 7000);
step("填「位置描述」", rLoc === "ok", String(rLoc));

// ---------------------------------------------------------------- 3. 上传照片
let upOk = false;
try {
  const obj = await evalRaw("document.querySelector('input[type=file]')", appCtx.id, false);
  const objectId = obj?.result?.objectId;
  if (objectId) {
    await send("DOM.setFileInputFiles", { files: [PHOTO], objectId }, sid);
    upOk = true;
  }
} catch (e) {
  step("上传照片", false, String(e));
}
await sleep(12000);            // 上传 + Streamlit rerun
const upState = await evIn(`(() => {
  const t = document.body.innerText || '';
  return {hasPreview: t.includes('投稿预览') || t.includes('照片里没有可用'),
          hasExif: /器材|拍摄时间/.test(t)};
})()`, appCtx.id);
step("上传照片（页面出现预览/解析结果）", upOk && upState && (upState.hasPreview || upState.hasExif),
     JSON.stringify(upState));

// ---------------------------------------------------------------- 4. 勾选授权
const ck = await evIn(`(() => {
  const box = document.querySelector('[data-testid="stCheckbox"] input[type=checkbox]');
  if (!box) return 'not-found';
  if (!box.checked) box.click();
  return box.checked ? 'checked' : 'still-unchecked';
})()`, appCtx.id);
await sleep(6000);
step("勾选「原创与授权声明」", ck === "checked", String(ck));

// ---------------------------------------------------------------- 5. 提交前快照
const pre = await evIn(`(() => {
  const t = document.body.innerText || '';
  return {hasSubmit: t.includes('提交投稿'),
          warn: (t.match(/还差：[^\\n]*/) || [])[0] || null,
          title: !!window.__dshByLabel('作品名'),
          count: (t.match(/已收稿\\s*([0-9]+)\\s*幅/) || [])[1] || null};
})()`, appCtx.id);
console.log(`  提交前状态: ${JSON.stringify(pre)}`);

const { result: shot1 } = await send("Page.captureScreenshot", { format: "png" }, sid);
const shotA = "C:\\Users\\Administrator\\.dsh\\sky_map\\data\\tile_check\\submit_before.png";
if (shot1?.data) writeFileSync(shotA, Buffer.from(shot1.data, "base64"));
console.log("  提交前截图:", shotA);

if (!DO_SUBMIT) {
  console.log("\n=== dry-run：已停在「提交」之前，未写入任何数据 ===");
  try { child.kill(); } catch {}
  process.exit(steps.every(s => s.ok) ? 0 : 1);
}

// ---------------------------------------------------------------- 6. 真的提交
const clicked = await evIn(`(() => {
  const btns = [...document.querySelectorAll('button')];
  const b = btns.find(x => (x.innerText || '').includes('提交投稿'));
  if (!b) return 'btn-not-found';
  b.click();
  return 'clicked';
})()`, appCtx.id);
console.log(`\n提交按钮: ${clicked}`);
await sleep(25000);            // 等保存 + rerun

const after = await evIn(`(() => {
  const t = document.body.innerText || '';
  return {ok: /投稿成功|已收到|感谢投稿/.test(t),
          msg: (t.match(/(投稿成功|已收到|感谢投稿)[^\\n]*/) || [])[0] || null,
          count: (t.match(/已收稿\\s*([0-9]+)\\s*幅/) || [])[1] || null,
          err: (t.match(/还差：[^\\n]*/) || [])[0] || null,
          head: t.replace(/\\s+/g,' ').slice(0, 200)};
})()`, appCtx.id);
console.log("提交后:", JSON.stringify(after, null, 2));

const { result: shot2 } = await send("Page.captureScreenshot", { format: "png" }, sid);
const shotB = "C:\\Users\\Administrator\\.dsh\\sky_map\\data\\tile_check\\submit_after.png";
if (shot2?.data) writeFileSync(shotB, Buffer.from(shot2.data, "base64"));
console.log("提交后截图:", shotB);

const okCount = after && baseline && after.count && baseline.count
  && parseInt(after.count) === parseInt(baseline.count) + 1;
step("提交成功（计数 +1）", !!(after && (after.ok || okCount)),
     `基线 ${baseline && baseline.count} → 现在 ${after && after.count}`);

console.log("\n=== 结果 ===");
for (const s of steps) console.log(`  ${s.ok ? "✓" : "✗"} ${s.name} ${s.detail || ""}`);
console.log("consoleErrors:", errs.slice(0, 5));
try { child.kill(); } catch {}
process.exit(steps.every(s => s.ok) ? 0 : 1);
