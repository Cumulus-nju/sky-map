"""静态底图上的「点选机位」组件（缩放 + 拖拽 + 右键选点）。

设计要点（改之前先读）
----------------------
1. **图片是死的，视图是活的**。底图就是外框内的一张静态图，页面上只显示它的
   一部分（"当前窗口"），缩放/拖拽只改这个窗口，不改图。
2. **点选 = 纯数学换算**，绝不依赖视图尺寸。点击位置先换成原图像素，再换成经纬度：

       原图 x = (点击处相对视口的比例 × 窗口宽) + 窗口左上角在原图中的 x
       经纬度 = fi.px_to_latlng(原图 x, 原图 y)

   因为窗口本身也是用同一套坐标算出来的，所以**任何缩放级下都严格对齐**。
3. **交互映射（2026-10-02 按用户要求定的）**：
       滚轮 / 双指  = 缩放
       **左键按住**  = 拖动平移
       **右键**      = 选点
   为什么右键选点：左键要用来拖动，如果左键也选点，用户想拖动却总误选、
   或者想选点却轻微移动就变成拖动 —— 拆开之后两个操作都干脆。
4. **宽度自适应**：Streamlit 的 `components.html` 只收整数宽度，写死会在窄屏被裁。
   所以组件自己监听父窗口尺寸，把 `#frame` 的宽度设成父容器宽度（并夹住上下限），
   顺带把 iframe 高度也调好（否则下方会留一大块空白）。
5. **状态怎么跨 rerun 保留**：组件是 iframe，没有返回值通道，所以用查询参数回传：
       pick = lat,lon        点选了哪个位置
       pnav = lat,lon,z      当前视图中心与缩放级
   Streamlit 侧读走 `pick`（`site_common.take_pick`），`pnav` 则保留（幂等，不必清）。

⚠️ 换算公式在 Python 侧也有一份（`_js_formula`），必须保持一致，由
   `assert_js_python_agree` + `tools/test_static_basemap.py` 守着。
"""
from __future__ import annotations

import html
import json
import math

# 缩放级上限：0=整图铺满，1=2×，2=4×。
# 不给更高是因为底图本身分辨率有限，再放大就是马赛克，对"点准一栋楼"没帮助。
MAX_ZOOM = 2
# 组件宽度自适应时的上下限（px）
MIN_W, MAX_W = 320.0, 1220.0


def frame_ratio(fi) -> float:
    """外框的 高/宽 比（= 图像宽高比）。"""
    return fi.height / fi.width


def project_pixel(lat: float, lon: float, fi, out_w: float, out_h: float) -> tuple[float, float]:
    """经纬度 -> 视口显示像素（z=0 时的坐标，用于放地标/标记点）。"""
    x0, y0 = fi.latlng_to_px(lat, lon)
    return (x0 / fi.width * out_w, y0 / fi.height * out_h)


def assert_js_python_agree(fi, samples=5) -> list[dict]:
    """自检：JS 的像素->经纬度公式与 Python 的 `fi.px_to_latlng` 是否等价。

    两边公式一旦漂移，症状是"点 A 存 B"且**完全没有报错** —— 所以必须测。
    """
    out = []
    w, h = fi.width, fi.height
    for i in range(samples):
        x = w * i / max(1, samples - 1)
        y = h * ((i % 2) / 1.0)
        py_lat, py_lon = fi.px_to_latlng(x, y)
        js_lat, js_lon = _js_formula(x, y, fi)
        out.append({
            "x": x, "y": y, "py": (py_lat, py_lon), "js": (js_lat, js_lon),
            "dlat": abs(py_lat - js_lat), "dlon": abs(py_lon - js_lon),
        })
    return out


def _js_formula(x: float, y: float, fi) -> tuple[float, float]:
    """把 JS 里那段换算用 Python 照抄（供自检比对）。"""
    nw_lat, nw_lon = fi.px_to_latlng(0, 0)
    se_lat, se_lon = fi.px_to_latlng(fi.width, fi.height)
    lon = nw_lon + (se_lon - nw_lon) * (x / fi.width)
    lat = _lat_lerp(nw_lat, se_lat, y / fi.height)
    return lat, lon


def _lat_lerp(lat_a: float, lat_b: float, t: float) -> float:
    """按 Mercator 的 y 线性插值（纬度不能直接线性插）。"""
    return _inv_merc_y(_merc_y(lat_a) + (_merc_y(lat_b) - _merc_y(lat_a)) * t)


def _merc_y(lat: float) -> float:
    lat = max(-85.05112878, min(85.05112878, lat))
    phi = math.radians(lat)
    return (1.0 - math.log(math.tan(phi) + 1.0 / math.cos(phi)) / math.pi) / 2.0


def _inv_merc_y(y: float) -> float:
    return math.degrees(math.atan(math.sinh(math.pi * (1.0 - 2.0 * y))))


def build_picker_html(
    fi,
    *,
    campus_key: str,
    display_width: float = 1100.0,
    picked: tuple[float, float] | None = None,
    nav: tuple[float, float, int] | None = None,
    landmarks: dict[str, tuple[float, float]] | None = None,
    max_display_height: float = 860.0,
    tip: str = "滚轮缩放 · 左键按住拖动 · 右键选点",
) -> tuple[str, float]:
    """生成点选组件的 HTML，返回 (html, 建议组件高度)。

    `nav` = (lat, lon, zoom) 是上次的视图状态；给了就恢复，没给就以 `picked`
    或外框中心为中心、z=0。
    """
    ratio = frame_ratio(fi)
    base_w = min(MAX_W, max(MIN_W, display_width))
    base_h = min(max_display_height, base_w * ratio)

    marks = []
    if landmarks:
        for name, (la, lo) in landmarks.items():
            x, y = project_pixel(la, lo, fi, base_w, base_h)
            if 0 <= x <= base_w and 0 <= y <= base_h:
                marks.append({"x": round(x, 2), "y": round(y, 2), "n": name})
    pick_px = None
    if picked and picked[0] is not None:
        px, py = project_pixel(picked[0], picked[1], fi, base_w, base_h)
        pick_px = [round(px, 2), round(py, 2)]

    nw_lat, nw_lon = fi.px_to_latlng(0, 0)
    se_lat, se_lon = fi.px_to_latlng(fi.width, fi.height)

    # 初始视图：有 nav 用 nav，否则以 picked / 外框中心为准
    if nav and len(nav) == 3 and nav[2] is not None:
        n_lat, n_lon, n_z = nav[0], nav[1], int(nav[2])
    elif picked and picked[0] is not None:
        n_lat, n_lon, n_z = picked[0], picked[1], 1
    else:
        n_lat, n_lon, n_z = (nw_lat + se_lat) / 2, (nw_lon + se_lon) / 2, 0
    n_z = max(0, min(MAX_ZOOM, n_z))

    payload = {
        "img": fi.data_url(), "iw": fi.width, "ih": fi.height,
        "outW": base_w, "outH": base_h,
        "nwLat": nw_lat, "nwLon": nw_lon, "seLat": se_lat, "seLon": se_lon,
        "marks": marks, "pick": pick_px, "campus": campus_key,
        "initLat": n_lat, "initLon": n_lon, "initZoom": n_z,
        "maxZoom": MAX_ZOOM, "minW": MIN_W, "maxW": MAX_W, "maxH": max_display_height,
    }
    # 防止 JSON 里的 </script> 提前闭合脚本块
    data_json = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")

    doc = f"""<!doctype html>
<html><head><meta charset="utf-8"><style>
  html,body {{ margin:0; padding:0; background:transparent; overflow:hidden;
               font:13px/1.5 system-ui,-apple-system,"Microsoft YaHei",sans-serif; }}
  #frame {{ position:relative; width:{base_w:.0f}px; height:{base_h:.0f}px;
            border:1px solid #cfd6dd; border-radius:6px; overflow:hidden;
            background:#e9ecef; cursor:grab; touch-action:none; }}
  #frame.drag {{ cursor:grabbing; }}
  #stage {{ position:absolute; left:0; top:0; transform-origin:0 0; }}
  #base {{ display:block; user-select:none; -webkit-user-drag:none; }}
  .lm {{ position:absolute; width:10px; height:10px; margin:-5px 0 0 -5px;
         border-radius:50%; background:#fff; border:2px solid #e0713c;
         box-sizing:border-box; pointer-events:none; }}
  #pick {{ position:absolute; width:18px; height:18px; margin:-9px 0 0 -9px;
           display:none; pointer-events:none; }}
  #pick.on {{ display:block; }}
  #pick::before, #pick::after {{ content:''; position:absolute; background:#e33; }}
  #pick::before {{ left:8px; top:0; width:2px; height:18px; }}
  #pick::after  {{ left:0; top:8px; width:18px; height:2px; }}
  #hint {{ position:absolute; left:8px; bottom:8px; background:rgba(255,255,255,.93);
           border-radius:4px; padding:3px 8px; color:#333; pointer-events:none; }}
  #zoom {{ position:absolute; right:8px; top:8px; display:flex; flex-direction:column; gap:4px; }}
  #zoom button {{ width:30px; height:30px; border:1px solid #c6ccd2; border-radius:4px;
                  background:rgba(255,255,255,.95); cursor:pointer; font-size:16px;
                  line-height:1; padding:0; color:#333; }}
  #zoom button:disabled {{ opacity:.4; cursor:default; }}
</style></head>
<body>
<div id="frame">
  <div id="stage"><img id="base" alt="校园底图"></div>
  <div id="pick"></div>
  <div id="zoom">
    <button id="zin" title="放大">+</button>
    <button id="zout" title="缩小">−</button>
  </div>
  <div id="hint">{html.escape(tip)}</div>
</div>
<script>
const D = {data_json};
const frame = document.getElementById('frame');
const stage = document.getElementById('stage');
const img = document.getElementById('base');
const zin = document.getElementById('zin'), zout = document.getElementById('zout');
const pin = document.getElementById('pick');

let zoom = D.initZoom;          // 缩放级
let cx = null, cy = null;       // 视图中心（原图像素）

// ---------------- 投影（必须与 static_basemap.py 等价，测试会校验）----------------
function mercY(lat) {{
  const p = Math.max(-85.05112878, Math.min(85.05112878, lat)) * Math.PI / 180;
  return (1 - Math.log(Math.tan(p) + 1 / Math.cos(p)) / Math.PI) / 2;
}}
function invMercY(y) {{
  return Math.atan(Math.sinh(Math.PI * (1 - 2 * y))) * 180 / Math.PI;
}}
const lonf = v => (v + 180) / 360;
function latLngToImgPx(lat, lon) {{
  const fx = (lonf(lon) - lonf(D.nwLon)) / (lonf(D.seLon) - lonf(D.nwLon));
  const ya = mercY(D.nwLat), yb = mercY(D.seLat);
  return [fx * D.iw, ((mercY(lat) - ya) / (yb - ya)) * D.ih];
}}
function imgPxToLatLng(x, y) {{
  const lon = D.nwLon + (D.seLon - D.nwLon) * (x / D.iw);
  const ya = mercY(D.nwLat), yb = mercY(D.seLat);
  return [invMercY(ya + (yb - ya) * (y / D.ih)), lon];
}}

// ---------------- 视图 ----------------
function vw() {{ return frame.clientWidth; }}
function vh() {{ return frame.clientHeight; }}
function winW() {{ return D.iw / Math.pow(2, zoom); }}
function winH() {{ return D.ih / Math.pow(2, zoom); }}

function clampCenter() {{
  const w = winW(), h = winH();
  cx = Math.max(w / 2, Math.min(D.iw - w / 2, cx));
  cy = Math.max(h / 2, Math.min(D.ih - h / 2, cy));
}}

function render() {{
  clampCenter();
  const w = winW(), h = winH();
  const scale = vw() / w;
  const left = cx - w / 2, top = cy - h / 2;
  stage.style.transform = 'translate(' + (-left * scale) + 'px,' + (-top * scale) + 'px) scale(' + scale + ')';
  img.style.width = D.iw + 'px';
  img.style.height = D.ih + 'px';
  for (const el of frame.querySelectorAll('.lm')) {{
    const ix = parseFloat(el.dataset.ix), iy = parseFloat(el.dataset.iy);
    const sx = (ix - left) * scale, sy = (iy - top) * scale;
    el.style.display = (sx >= -12 && sx <= vw() + 12 && sy >= -12 && sy <= vh() + 12) ? 'block' : 'none';
    el.style.left = sx + 'px';
    el.style.top = sy + 'px';
  }}
  if (pin.dataset.ix !== undefined) {{
    pin.style.left = ((parseFloat(pin.dataset.ix) - left) * scale) + 'px';
    pin.style.top = ((parseFloat(pin.dataset.iy) - top) * scale) + 'px';
  }}
  zin.disabled = zoom >= D.maxZoom;
  zout.disabled = zoom <= 0;
}}

// 屏幕坐标 -> 原图像素
function screenToImg(clientX, clientY) {{
  const r = frame.getBoundingClientRect();
  const w = winW(), h = winH(), scale = vw() / w;
  return [(cx - w / 2) + (clientX - r.left) / scale,
          (cy - h / 2) + (clientY - r.top) / scale];
}}

// ---------------- 宽度自适应 ----------------
// Streamlit 的 components.html 只收整数宽度，写死会在窄屏被裁掉、宽屏两侧留白。
// 所以自己量父容器：既设 #frame 宽度，也把 iframe 高度调成内容高度（否则下方留空白）。
function fitToParent() {{
  let target = D.outW;
  // 视口高度：地图别高到把下面的表单整个推走
  let maxH = D.outH;
  try {{
    const pw = window.parent ? window.parent.innerWidth : 0;
    if (pw) target = Math.min(D.maxW, Math.max(D.minW, pw - 170));
    const ph = window.parent ? window.parent.innerHeight : 0;
    if (ph) maxH = Math.max(380, Math.min(D.outH, ph * 0.74));
  }} catch (e) {{}}
  frame.style.width = Math.round(target) + 'px';
  const ratio = D.ih / D.iw;
  frame.style.height = Math.round(Math.min(target * ratio, maxH)) + 'px';
  render();
  try {{
    if (window.frameElement) {{
      const h = frame.getBoundingClientRect().height + 6;
      window.frameElement.style.height = h + 'px';
      window.frameElement.setAttribute('height', Math.round(h));
    }}
  }} catch (e) {{}}
}}
window.addEventListener('resize', fitToParent);

// ---------------- 交互 ----------------
// 滚轮 = 缩放（以鼠标位置为锚）
frame.addEventListener('wheel', function (ev) {{
  ev.preventDefault();
  const before = screenToImg(ev.clientX, ev.clientY);
  const w = winW(), h = winH();
  const fracX = (before[0] - (cx - w / 2)) / w, fracY = (before[1] - (cy - h / 2)) / h;
  const nz = Math.max(0, Math.min(D.maxZoom, zoom + (ev.deltaY < 0 ? 1 : -1)));
  if (nz === zoom) return;
  zoom = nz;
  const nw = winW(), nh = winH();
  cx = before[0] + (0.5 - fracX) * nw;
  cy = before[1] + (0.5 - fracY) * nh;
  render(); report(false);
}}, {{ passive: false }});

// 左键按住拖动 = 平移；右键 = 选点
//
// ⚠ 位移用"钳位后的实际位移"来算，不能直接用鼠标位移：
//   视图贴到边界时 clampCenter() 会吃掉一部分位移，若仍按鼠标全量移动，
//   图像就会与光标"脱手"（拖着拖着光标和地图对不上）。
//   注意 z=0 时窗口 == 整图，本来就无处可移（钳位全吃掉）—— 那是**正确行为**，
//   不是 bug：想平移请先放大。
let drag = null;
frame.addEventListener('pointerdown', function (ev) {{
  if (ev.button === 2) return;            // 右键交给 contextmenu 处理
  if (ev.button !== 0) return;
  drag = {{ x: ev.clientX, y: ev.clientY, cx: cx, cy: cy, moved: false }};
  frame.classList.add('drag');
  try {{ frame.setPointerCapture(ev.pointerId); }} catch (e) {{}}
}});
frame.addEventListener('pointermove', function (ev) {{
  if (!drag) return;
  const dx = ev.clientX - drag.x, dy = ev.clientY - drag.y;
  if (!drag.moved && Math.abs(dx) + Math.abs(dy) > 3) drag.moved = true;
  if (!drag.moved) return;
  const scale = vw() / winW();
  const wantX = drag.cx - dx / scale;      // 按鼠标位移的目标中心
  const wantY = drag.cy - dy / scale;
  cx = wantX; cy = wantY;
  clampCenter();                            // 边界处会被夹住
  // 把"被夹住的那部分"从基准点里扣掉，保证光标下的地物不跑
  drag.cx += (cx - wantX);
  drag.cy += (cy - wantY);
  render();
}});
frame.addEventListener('pointerup', function (ev) {{
  if (!drag) return;
  const moved = drag.moved; drag = null;
  frame.classList.remove('drag');
  if (moved) report(false);
}});
frame.addEventListener('pointercancel', function () {{
  drag = null; frame.classList.remove('drag');
}});

// 右键选点（必须屏蔽浏览器右键菜单，否则弹菜单把操作打断）
frame.addEventListener('contextmenu', function (ev) {{
  ev.preventDefault();
  const p = screenToImg(ev.clientX, ev.clientY);
  pin.dataset.ix = p[0]; pin.dataset.iy = p[1]; pin.classList.add('on');
  const ll = imgPxToLatLng(p[0], p[1]);
  render();
  report(true, ll[0], ll[1]);
}});

zin.addEventListener('click', function () {{ zoom = Math.min(D.maxZoom, zoom + 1); render(); report(false); }});
zout.addEventListener('click', function () {{ zoom = Math.max(0, zoom - 1); render(); report(false); }});

img.onload = function () {{
  for (const m of D.marks) {{
    const el = document.createElement('div');
    el.className = 'lm';
    el.dataset.ix = m.x / D.outW * D.iw;
    el.dataset.iy = m.y / D.outH * D.ih;
    el.title = m.n;
    frame.appendChild(el);
  }}
  if (D.pick) {{
    pin.dataset.ix = D.pick[0] / D.outW * D.iw;
    pin.dataset.iy = D.pick[1] / D.outH * D.ih;
    pin.classList.add('on');
  }}
  if (cx === null) {{
    const c = latLngToImgPx(D.initLat, D.initLon);
    cx = c[0]; cy = c[1];
  }}
  fitToParent();
}};
img.src = D.img;

// ---------------- 回传（查询参数 -> Streamlit rerun）----------------
function report(isPick, lat, lon) {{
  const nl = imgPxToLatLng(cx, cy);
  // 4 位小数 ≈ 11 m，远细于"能点准一栋楼"；位数少能让坐标显示更好看
  const navStr = nl[0].toFixed(4) + ',' + nl[1].toFixed(4) + ',' + zoom;
  const pickStr = isPick && lat !== undefined ? (lat.toFixed(4) + ',' + lon.toFixed(4)) : null;
  try {{
    const u = new URL(window.parent.location.href);
    u.searchParams.set('pnav', navStr);
    if (pickStr) {{ u.searchParams.set('pick', pickStr); u.searchParams.set('pick_campus', D.campus); }}
    window.parent.history.replaceState({{}}, '', u.toString());
    window.parent.dispatchEvent(new PopStateEvent('popstate'));
  }} catch (e) {{
    try {{
      const u2 = new URL(window.parent.location.href);
      u2.searchParams.set('pnav', navStr);
      if (pickStr) {{ u2.searchParams.set('pick', pickStr); u2.searchParams.set('pick_campus', D.campus); }}
      window.parent.location.href = u2.toString();
    }} catch (e2) {{}}
  }}
}}
</script>
</body></html>"""
    return doc, base_h + 8
