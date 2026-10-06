// 按**手机尺寸**把几个页面各截一张图，用来肉眼验收移动端排版。
//
// 为什么需要它：这套问题**只有看图才知道**。本项目的移动端坑都是肉眼看出来的
// （2026-10-05 侧边栏盖住地图、z-index 999991 把触屏事件全接走；
//  2026-10-06 侧边栏收起后同学找不到「打卡点地图」「作品投票」两个页面）。
// 光看代码或跑函数测试都发现不了。
//
// 用法：
//   node tools\cdp_phone_shots.mjs                       # 默认 http://localhost:8502
//   node tools\cdp_phone_shots.mjs http://localhost:8501
//   node tools\cdp_phone_shots.mjs http://localhost:8501 --desktop   # 顺带截桌面尺寸
//
// 产物：_phone_<页面>.png（存在当前目录），同时打印每页的首屏文字与链接列表，
//       便于在终端里先扫一眼有没有明显不对。
//
// ⚠ 本地截图前**必须重启 streamlit**：起服务时带了 `--server.fileWatcherType none`，
//   旧进程跑旧模块 —— 2026-10-06 我就这样截了一轮"改了代码但图没变"的旧图。
import { spawn } from "node:child_process";
import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const BASE = process.argv[2] || "http://localhost:8502";
const DESKTOP = process.argv.includes("--desktop");
const EDGE = "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe";
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const PHONE = { w: 390, h: 844, dsf: 2, mobile: true, tag: "phone" };
const DESK = { w: 1500, h: 1100, dsf: 1, mobile: false, tag: "desktop" };

const PAGES = [
  { path: "/", name: "投稿" },
  { path: "/投票", name: "投票" },
  { path: "/打卡点地图", name: "地图" },
];

async function shoot(dev) {
  const port = 9300 + (dev.mobile ? 1 : 2);
  const args = ["--headless=new", "--disable-gpu", "--no-first-run",
    // ⚠ 绕开本机系统代理（代理出口被 Streamlit 拉黑，线上会 HTTP 444）
    "--no-proxy-server",
    `--remote-debugging-port=${port}`,
    `--user-data-dir=${mkdtempSync(join(tmpdir(), "cdp-shot-"))}`,
    `--window-size=${dev.w},${dev.h}`, "--hide-scrollbars", "about:blank"];
  const child = spawn(EDGE, args, { stdio: "ignore" });

  let ws;
  for (let i = 0; i < 60; i++) {
    try {
      const j = await (await fetch(`http://127.0.0.1:${port}/json/version`)).json();
      if (j.webSocketDebuggerUrl) { ws = new WebSocket(j.webSocketDebuggerUrl); break; }
    } catch {}
    await sleep(300);
  }
  await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej; });
  let id = 0; const pending = new Map();
  // 控制台报错：验地图这类改动时，"有没有 JS 报错"和"看着对不对"一样重要 ——
  // 渲染错一半、另一半照旧能看，只截图是发现不了的。
  let errors = [];
  ws.onmessage = (e) => {
    const m = JSON.parse(e.data);
    if (m.method === "Runtime.exceptionThrown") {
      errors.push("EXC: " + (m.params?.exceptionDetails?.text
        || m.params?.exceptionDetails?.exception?.description || ""));
    } else if (m.method === "Runtime.consoleAPICalled" && m.params?.type === "error") {
      errors.push("ERR: " + (m.params.args || [])
        .map((a) => a.value ?? a.description ?? "").join(" "));
    }
    if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); }
  };
  const send = (method, params = {}, s) => new Promise((res) => {
    const mid = ++id; pending.set(mid, res);
    ws.send(JSON.stringify({ id: mid, method, params, ...(s ? { sessionId: s } : {}) }));
  });

  const { result: t } = await send("Target.createTarget", { url: "about:blank" });
  const { result: att } = await send("Target.attachToTarget",
    { targetId: t.targetId, flatten: true });
  const sid = att.sessionId;
  await send("Runtime.enable", {}, sid);
  await send("Page.enable", {}, sid);
  const ev = async (expr) => {
    const r = await send("Runtime.evaluate", { expression: expr, returnByValue: true }, sid);
    return r.result?.result?.value;
  };

  // ⚠ 机型仿真必须在**导航之前**设好：Streamlit 首屏就按窗口宽度决定侧边栏收放
  await send("Emulation.setDeviceMetricsOverride",
    { width: dev.w, height: dev.h, deviceScaleFactor: dev.dsf, mobile: dev.mobile }, sid);
  if (dev.mobile) {
    await send("Emulation.setTouchEmulationEnabled", { enabled: true, maxTouchPoints: 5 }, sid);
  }

  for (const p of PAGES) {
    errors = [];
    await send("Page.navigate", { url: BASE + p.path }, sid);
    let ready = false;
    for (let i = 0; i < 40; i++) {
      await sleep(1000);
      const txt = await ev("document.body ? document.body.innerText : ''");
      // 认「页面导航」这条 caption：它由 site_common.top_nav 渲染，出现了就说明
      // 首屏已经画完（比"等固定秒数"可靠）。
      if (txt && txt.includes("页面导航")) { ready = true; break; }
    }
    await sleep(1000);
    const info = await ev(`(function(){
      return { title: document.title,
               first: document.body.innerText.replace(/\\n+/g,' | ').slice(0, 200),
               links: [...document.querySelectorAll('a')].map(a => a.innerText.trim())
                        .filter(Boolean).slice(0, 8),
               sidebar: (function(){ const sb=document.querySelector('section.stSidebar');
                 if(!sb) return 'none'; const b=sb.getBoundingClientRect();
                 return getComputedStyle(sb).visibility + ' w=' + Math.round(b.width)
                        + ' left=' + Math.round(b.left); })() };
    })()`);
    const shot = await send("Page.captureScreenshot", { format: "png" }, sid);
    const file = join(process.cwd(), `_${dev.tag}_${p.name}.png`);
    writeFileSync(file, Buffer.from(shot.result.data, "base64"));
    console.log("=".repeat(62));
    console.log(`[${dev.tag}] ${p.path}  ready=${ready}  侧边栏: ${info.sidebar}`);
    console.log(`  标题: ${info.title}`);
    console.log(`  链接: ${JSON.stringify(info.links)}`);
    console.log(`  首屏: ${info.first}`);
    console.log(`  控制台报错: ${errors.length ? errors.length + " 条" : "无 ✓"}`);
    for (const e of errors.slice(0, 5)) console.log(`      ${e.slice(0, 160)}`);
    console.log(`  截图: ${file}`);
  }
  try { child.kill(); } catch {}
}

await shoot(PHONE);
if (DESKTOP) await shoot(DESK);
