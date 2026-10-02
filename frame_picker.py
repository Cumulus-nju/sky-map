"""静态底图上的「点选机位」组件（支持缩放与拖拽）。

设计要点（改之前先读）
----------------------
1. **图片是死的，视图是活的**。底图就是外框内的一张静态图，页面上只显示它的
   一部分（"当前窗口"），缩放/拖拽只改这个窗口，不改图。
2. **点选 = 纯数学换算**，绝不依赖视图尺寸。点击位置先换成原图像素，再换成经纬度：

      原图 x = (点击处相对视口的比例 × 窗口宽) + 窗口左上角在原图中的 x
      经纬度 = fi.px_to_latlng(原图 x, 原图 y)

   因为窗口本身也是用同一套坐标算出来的，所以**任何缩放级下都严格对齐**。
3. **状态怎么跨 rerun 保留**：组件是 iframe，没有返回值通道，所以用查询参数回传：
       pick = lat,lon        点选了哪个位置
       pnav = lat,lon,z      当前视图中心与缩放级
   Streamlit 侧读走 `pick`（`site_common.take_pick`），`pnav` 则保留着（幂等，不必清）。
4. 缩放级 z：0 = 整张图铺满视口；每 +1 显示范围减半。所以窗口比 viewport 大，
   用 CSS scale 把图片拉起来 —— 相当于对静态图做无级放大。

⚠️ 换算公式在 Python 侧也有一份（`_js_formula`），必须保持一致，由
   `assert_js_python_agree` + `tools/test_static_basemap.py` 守着。
"""
from __future__ import annotations

import html
import json
import math

# 缩放级上限：0=整图，1=2×，2=4×。
# 不给更高是因为底图本身分辨率有限，再放大就是马赛克，对"点准一栋楼"没帮助。
MAX_ZOOM = 2


def frame_ratio(fi) -> float:
    """外框的 高/宽 比（= 图像宽高比）。"""
    return fi.height / fi.width


def project_pixel(lat: float, lon: float, fi, out_w: float, out_h: float) -> tuple[float, float]:
    """经纬度 -> **视口显示像素**（z=0 时的坐标，用于放地标/标记点）。"""
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
    display_width: float = 880.0,
    picked: tuple[float, float] | None = None,
    nav: tuple[float, float, int] | None = None,
    landmarks: dict[str, tuple[float, float]] | None = None,
    max_display_height: float = 700.0,
    tip: str = "滚轮 / 双指缩放，拖动平移，点一下选机位",
) -> tuple[str, float]:
    """生成点选组件的 HTML，返回 (html, 建议组件高度)。

    `nav` = (lat, lon, zoom) 是上次的视图状态；给了就恢复，没给就以 `picked`
    或外框中心为中心、z=0。
    """
    ratio = frame_ratio(fi)
    out_w = display_width
    out_h = out_w * ratio
    if out_h > max_display_height:       # 太高就按高度反推宽度
        out_h = max_display_height
        out_w = out_h / ratio

    marks = []
    if landmarks:
        for name, (la, lo) in landmarks.items():
            x, y = project_pixel(la, lo, fi, out_w, out_h)
            if 0 <= x <= out_w and 0 <= y <= out_h:
                marks.append({"x": round(x, 2), "y": round(y, 2), "n": name})
    pick_px = None
    if picked and picked[0] is not None:
        px, py = project_pixel(picked[0], picked[1], fi, out_w, out_h)
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
        "outW": out_w, "outH": out_h,
        "nwLat": nw_lat, "nwLon": nw_lon, "seLat": se_lat, "seLon": se_lon,
        "marks": marks, "pick": pick_px, "campus": campus_key,
        "initLat": n_lat, "initLon": n_lon, "initZoom": n_z,
        "maxZoom": MAX_ZOOM,
    }
    # 防止 JSON 里的 </script> 提前闭合脚本块
    data_json = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")

    doc = f"""<!doctype html>
<html><head><meta charset="utf-8"><style>
  html,body {{ margin:0; padding:0; background:transparent; overflow:hidden;
               font:13px/1.5 system-ui,-apple-system,"Microsoft YaHei",sans-serif; }}
  #frame {{ position:relative; width:{out_w:.0f}px; height:{out_h:.0f}px;
            border:1px solid #cfd6dd; border-radius:6px; overflow:hidden;
            background:#e9ecef; cursor:grab; touch-action:none; }}
  #frame.drag {{ cursor:grabbing; }}
  #stage {{ position:absolute; left:0; top:0; transform-origin:0 0; }}
  #base {{ display:block; image-rendering:auto; }}
  .lm {{ position:absolute; width:10px; height:10px; margin:-5px 0 0 -5px;
         border-radius:50%; background:#fff; border:2px solid #e0713c;
         box-sizing:border-box; pointer-events:none; }}
  #pick {{ position:absolute; width:18px; height:18px; margin:-9px 0 0 -9px;
           display:none; pointer-events:none; }}
  #pick.on {{ display:block; }}
  #pick::before, #pick::after {{ content:''; position:absolute; background:#e33; }}
  #pick::before {{ left:8px; top:0; width:2px; height:18px; }}
  #pick::after  {{ left:0; top:8px; width:18px; height:2px; }}
  #hint {{ position:absolute; left:8px; bottom:8px; background:rgba(255,255,255,.92);
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

let zoom = D.initZoom;          // 缩放级
let cx = null, cy = null;       // 视图中心（原图像素）

// 把经纬度换成原图像素（与 Python 的 fi.latlng_to_px 等价）
function mercY(lat) {{
  const p = Math.max(-85.05112878, Math.min(85.05112878, lat)) * Math.PI / 180;
  return (1 - Math.log(Math.tan(p) + 1 / Math.cos(p)) / Math.PI) / 2;
}}
function invMercY(y) {{
  return Math.atan(Math.sinh(Math.PI * (1 - 2 * y))) * 180 / Math.PI;
}}
function latLngToImgPx(lat, lon) {{
  const lonf = v => (v + 180) / 360;
  const fx = (lonf(lon) - lonf(D.nwLon)) / (lonf(D.seLon) - lonf(D.nwLon));
  const ya = mercY(D.nwLat), yb = mercY(D.seLat);
  const fy = (mercY(lat) - ya) / (yb - ya);
  return [fx * D.iw, fy * D.ih];
}}
// 原图像素 -> 经纬度。与 static_basemap.latlng_from_image_px 等价。
function imgPxToLatLng(x, y) {{
  const lonf = v => (v + 180) / 360;
  const fx = x / D.iw, fy = y / D.ih;
  const lon = D.nwLon + (D.seLon - D.nwLon) * fx;
  const ya = mercY(D.nwLat), yb = mercY(D.seLat);
  return [invMercY(ya + (yb - ya) * fy), lon];
}}

// 缩放级 z 下，视口对应的"原图窗口"尺寸：每升一级范围减半
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
  const scale = D.outW / w;          // 原图 -> 视口的缩放比
  const left = cx - w / 2, top = cy - h / 2;
  stage.style.transform = 'translate(' + (-left * scale) + 'px,' + (-top * scale) + 'px) scale(' + scale + ')';
  img.style.width = D.iw + 'px';
  img.style.height = D.ih + 'px';
  // 标记点：位置跟图走，但点本身大小不随放大而变大（否则放大后像大圆饼）
  for (const el of frame.querySelectorAll('.lm')) {{
    const ix = parseFloat(el.dataset.ix), iy = parseFloat(el.dataset.iy);
    const sx = (ix - left) * scale, sy = (iy - top) * scale;
    const vis = sx >= -12 && sx <= D.outW + 12 && sy >= -12 && sy <= D.outH + 12;
    el.style.display = vis ? 'block' : 'none';
    el.style.left = sx + 'px';
    el.style.top = sy + 'px';
  }}
  const p = document.getElementById('pick');
  if (p && p.dataset.ix !== undefined) {{
    const ix = parseFloat(p.dataset.ix), iy = parseFloat(p.dataset.iy);
    p.style.left = ((ix - left) * scale) + 'px';
    p.style.top = ((iy - top) * scale) + 'px';
  }}
  zin.disabled = zoom >= D.maxZoom;
  zout.disabled = zoom <= 0;
}}

// 建立地标（图片加载完再建，保证尺寸已知）
img.onload = function () {{
  for (const m of D.marks) {{
    const el = document.createElement('div');
    el.className = 'lm'; el.dataset.ix = m.x / D.outW * D.iw;
    el.dataset.iy = m.y / D.outH * D.ih; el.title = m.n;
    frame.appendChild(el);
  }}
  const p = document.getElementById('pick');
  if (D.pick && p) {{
    p.dataset.ix = D.pick[0] / D.outW * D.iw;
    p.dataset.iy = D.pick[1] / D.outH * D.ih;
    p.classList.add('on');
  }}
  if (cx === null) {{
    const c = latLngToImgPx(D.initLat, D.initLon);
    cx = c[0]; cy = c[1];
  }}
  render();
}};
img.src = D.img;

// ---------------- 交互：滚轮缩放 / 拖动平移 / 点击选点 ----------------
frame.addEventListener('wheel', function (ev) {{
  ev.preventDefault();
  const r = frame.getBoundingClientRect();
  const w = winW(), h = winH(), scale = D.outW / w;
  const left = cx - w / 2, top = cy - h / 2;
  const ix = left + (ev.clientX - r.left) / scale;
  const iy = top + (ev.clientY - r.top) / scale;
  const dir = ev.deltaY < 0 ? 1 : -1;
  const nz = Math.max(0, Math.min(D.maxZoom, zoom + dir));
  if (nz === zoom) return;
  const fracX = (ix - left) / w, fracY = (iy - top) / h;
  zoom = nz;
  const nw = winW(), nh = winH();
  cx = ix + (0.5 - fracX) * nw;
  cy = iy + (0.5 - fracY) * nh;
  render();
  report(false);
}}, {{ passive: false }});

let down = null, moved = false;
frame.addEventListener('pointerdown', function (ev) {{
  down = {{ x: ev.clientX, y: ev.clientY, cx: cx, cy: cy }};
  moved = false; frame.classList.add('drag');
  frame.setPointerCapture(ev.pointerId);
}});
frame.addEventListener('pointermove', function (ev) {{
  if (!down) return;
  const dx = ev.clientX - down.x, dy = ev.clientY - down.y;
  if (Math.abs(dx) + Math.abs(dy) > 4) moved = true;
  if (!moved) return;
  const w = winW(), scale = D.outW / w;
  cx = down.cx - dx / scale; cy = down.cy - dy / scale;
  render();
}});
frame.addEventListener('pointerup', function (ev) {{
  frame.classList.remove('drag');
  const wasDrag = moved; down = null;
  if (wasDrag) {{ report(false); return; }}
  // 单击 = 选点
  const r = frame.getBoundingClientRect();
  const w = winW(), h = winH(), scale = D.outW / w;
  const left = cx - w / 2, top = cy - h / 2;
  const ix = left + (ev.clientX - r.left) / scale;
  const iy = top + (ev.clientY - r.top) / scale;
  const p = document.getElementById('pick');
  p.dataset.ix = ix; p.dataset.iy = iy; p.classList.add('on');
  const [lat, lon] = imgPxToLatLng(ix, iy);
  render();
  report(true, lat, lon);
}});

zin.addEventListener('click', function () {{ zoom = Math.min(D.maxZoom, zoom + 1); render(); report(false); }});
zout.addEventListener('click', function () {{ zoom = Math.max(0, zoom - 1); render(); report(false); }});

// ---------------- 回传 ----------------
function navLatLng() {{
  return imgPxToLatLng(cx, cy);
}}
function report(isPick, lat, lon) {{
  try {{
    const u = new URL(window.parent.location.href);
    const nl = navLatLng();
    u.searchParams.set('pnav', nl[0].toFixed(7) + ',' + nl[1].toFixed(7) + ',' + zoom);
    if (isPick && lat !== undefined) {{
      u.searchParams.set('pick', lat.toFixed(7) + ',' + lon.toFixed(7));
      u.searchParams.set('pick_campus', D.campus);
    }}
    window.parent.history.replaceState({{}}, '', u.toString());
    window.parent.dispatchEvent(new PopStateEvent('popstate'));
  }} catch (e) {{
    try {{
      const u2 = new URL(window.parent.location.href);
      const nl2 = navLatLng();
      u2.searchParams.set('pnav', nl2[0].toFixed(7) + ',' + nl2[1].toFixed(7) + ',' + zoom);
      if (isPick && lat !== undefined) {{
        u2.searchParams.set('pick', lat.toFixed(7) + ',' + lon.toFixed(7));
        u2.searchParams.set('pick_campus', D.campus);
      }}
      window.parent.location.href = u2.toString();
    }} catch (e2) {{}}
  }}
}}
</script>
</body></html>"""
    return doc, out_h + 4
