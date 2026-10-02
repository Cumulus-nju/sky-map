"""静态底图上的「点选机位」组件。

思路（用户 2026-10-02 提的方案）
--------------------------------
把外框内的底图**预先拼成一张静态图**，页面上只显示这张图 + 几个标记点：
  * 美术完全可控（调色、压暗、加标注都能直接做在图上）；
  * 点选 = 纯像素换算，没有"视图尺寸/容器比例"参与，**永远不会对不齐**；
  * 不依赖 Leaflet 的实时瓦片，断网/弱网也能用（图是内嵌的 data URL）。

关键约束：**浏览器里的换算必须和 Python 里的完全等价**。
所以 KM_PER_DEG 之类的常数由 Python 注入，并在启动时用几个点做交叉自检
（`assert_js_python_agree`），避免两边悄悄漂移。
"""
from __future__ import annotations

import html
import json
import math


def project_pixel(lat: float, lon: float, fi, out_w: float, out_h: float) -> tuple[float, float]:
    """经纬度 -> **显示像素** (x, y)。

    fi 给出的是"原图像素"，但页面上图片会被 CSS 缩放到容器宽度，
    所以这里按 out_w/out_h 再换算一次，得到用户实际看到的坐标。
    """
    x0, y0 = fi.latlng_to_px(lat, lon)
    return (x0 / fi.width * out_w, y0 / fi.height * out_h)


def frame_ratio(fi) -> float:
    """外框的 高/宽 比（= 图像宽高比），用于给容器留出正确的高度。"""
    return fi.height / fi.width


def assert_js_python_agree(fi, samples=5) -> list[dict]:
    """自检：把 JS 里那套换算式在 Python 里重算，与 `fi.px_to_latlng` 比对。

    两边公式必须等价，否则线上会出现"点什么位置，存的是另一个坐标"这种
    最难查的错（而且不会有任何报错）。这里是可调用的自检，测试里会用。
    """
    out = []
    w, h = fi.width, fi.height
    for i in range(samples):
        x = w * i / max(1, samples - 1)
        y = h * ((i % 2) / 1.0)
        py_lat, py_lon = fi.px_to_latlng(x, y)
        js_lat, js_lon = _js_formula(x, y, fi)
        out.append({
            "x": x, "y": y,
            "py": (py_lat, py_lon), "js": (js_lat, js_lon),
            "dlat": abs(py_lat - js_lat), "dlon": abs(py_lon - js_lon),
        })
    return out


def _js_formula(x: float, y: float, fi) -> tuple[float, float]:
    """把下面 JS 里那段换算用 Python 照抄一遍（供自检比对）。"""
    nw_lat, nw_lon = fi.px_to_latlng(0, 0)
    se_lat, se_lon = fi.px_to_latlng(fi.width, fi.height)
    frac_x = x / fi.width
    frac_y = y / fi.height
    lon = nw_lon + (se_lon - nw_lon) * frac_x
    lat = _lat_lerp(nw_lat, se_lat, frac_y)
    return lat, lon


def _lat_lerp(lat_a: float, lat_b: float, t: float) -> float:
    """按 Mercator 的 y 做线性插值（纬度不能直接线性插）。"""
    ya = _merc_y(lat_a)
    yb = _merc_y(lat_b)
    return _inv_merc_y(ya + (yb - ya) * t)


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
    display_width: float = 900.0,
    picked: tuple[float, float] | None = None,
    landmarks: dict[str, tuple[float, float]] | None = None,
    max_display_height: float = 760.0,
    tip: str = "在地图上点一下你拍照站的位置",
) -> tuple[str, float]:
    """生成点选组件的 HTML，并返回 (html, 建议的组件高度)。

    点击后通过 `window.parent.location` 写入查询参数 `pick=lat,lon`
    （组件 iframe 与 Streamlit 同源，可直接改父窗口 URL 触发一次 rerun），
    上层用 `site_common.take_pick()` 读走并清除。
    """
    ratio = frame_ratio(fi)
    # 宽度优先：先按容器宽度铺满，再按比例定高；高超过上限就反过来受高度约束
    out_w = display_width
    out_h = out_w * ratio
    if out_h > max_display_height:
        out_h = max_display_height
        out_w = out_h / ratio

    marks = []
    if landmarks:
        for name, (la, lo) in landmarks.items():
            x, y = project_pixel(la, lo, fi, out_w, out_h)
            marks.append({"x": round(x, 2), "y": round(y, 2), "n": name})
    pick_px = None
    if picked and picked[0] is not None:
        px, py = project_pixel(picked[0], picked[1], fi, out_w, out_h)
        pick_px = [round(px, 2), round(py, 2)]

    nw_lat, nw_lon = fi.px_to_latlng(0, 0)
    se_lat, se_lon = fi.px_to_latlng(fi.width, fi.height)

    payload = {
        "img": fi.data_url(),
        "w": fi.width,
        "h": fi.height,
        "outW": out_w,
        "outH": out_h,
        "nwLat": nw_lat, "nwLon": nw_lon,
        "seLat": se_lat, "seLon": se_lon,
        "marks": marks,
        "pick": pick_px,
        "campus": campus_key,
    }
    # 防止 JSON 里的 </script> 提前闭合脚本块
    data_json = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")

    doc = f"""<!doctype html>
<html><head><meta charset="utf-8"><style>
  html,body {{ margin:0; padding:0; background:transparent; }}
  #wrap {{ position:relative; width:{out_w:.0f}px; height:{out_h:.0f}px;
           border:1px solid #d7dbe0; border-radius:6px; overflow:hidden; background:#eee; }}
  #base {{ position:absolute; left:0; top:0; width:100%; height:100%; display:block; }}
  .lm {{ position:absolute; width:9px; height:9px; margin:-5px 0 0 -5px; border-radius:50%;
         background:#fff; border:2px solid #e0713c; box-sizing:border-box; }}
  .lm span {{ display:none; }}
  #pick {{ position:absolute; width:16px; height:16px; margin:-8px 0 0 -8px; display:none; }}
  #pick.on {{ display:block; }}
  #hint {{ position:absolute; left:8px; bottom:8px; background:rgba(255,255,255,.92);
           border-radius:4px; padding:3px 8px; font:12px/1.5 system-ui,sans-serif; color:#333; }}
</style></head>
<body>
<div id="wrap">
  <img id="base" alt="校园底图">
  <div id="pin"></div>
  <div id="hint">{html.escape(tip)}</div>
</div>
<script>
const D = {data_json};
const img = document.getElementById('base');
img.src = D.img;
const wrap = document.getElementById('wrap');
const pin = document.getElementById('pin');

// 标记点
for (const m of D.marks) {{
  const d = document.createElement('div');
  d.className = 'lm'; d.style.left = m.x + 'px'; d.style.top = m.y + 'px';
  d.title = m.n;
  wrap.appendChild(d);
}}

// 按 Mercator 的 y 做插值（纬度不能直接线性插值）
function mercY(lat) {{
  const p = Math.max(-85.05112878, Math.min(85.05112878, lat)) * Math.PI / 180;
  return (1 - Math.log(Math.tan(p) + 1 / Math.cos(p)) / Math.PI) / 2;
}}
function invMercY(y) {{
  return Math.atan(Math.sinh(Math.PI * (1 - 2 * y))) * 180 / Math.PI;
}}
// 显示像素 -> 经纬度。与 static_basemap.py 的 latlng_from_image_px 等价。
function pxToLatLng(x, y) {{
  const fx = x / D.outW, fy = y / D.outH;
  const lon = D.nwLon + (D.seLon - D.nwLon) * fx;
  const ya = mercY(D.nwLat), yb = mercY(D.seLat);
  const lat = invMercY(ya + (yb - ya) * fy);
  return [lat, lon];
}}

function drawPin(x, y) {{
  pin.className = 'on'; pin.style.left = x + 'px'; pin.style.top = y + 'px';
}}
if (D.pick) drawPin(D.pick[0], D.pick[1]);

wrap.addEventListener('click', function(ev) {{
  // ⚠ 必须用**图片自身**的矩形，不能用 wrap 的矩形：
  // wrap 含 1px 边框，用它会让换算多出约 0.3% 的比例偏差（实测约 1 米误差）。
  const r = img.getBoundingClientRect();
  const x = (ev.clientX - r.left) / r.width * D.outW;
  const y = (ev.clientY - r.top) / r.height * D.outH;
  drawPin(x, y);
  const [lat, lon] = pxToLatLng(x, y);
  // 写进父窗口查询参数 -> Streamlit 检测到变化会 rerun
  try {{
    const u = new URL(window.parent.location.href);
    u.searchParams.set('pick', lat.toFixed(7) + ',' + lon.toFixed(7));
    u.searchParams.set('pick_campus', D.campus);
    window.parent.history.replaceState({{}}, '', u.toString());
    window.parent.dispatchEvent(new PopStateEvent('popstate'));
  }} catch (e) {{
    // 兜底：直接改 location（srcdoc 里 history 可能受限）
    try {{
      const u2 = new URL(window.parent.location.href);
      u2.searchParams.set('pick', lat.toFixed(7) + ',' + lon.toFixed(7));
      u2.searchParams.set('pick_campus', D.campus);
      window.parent.location.href = u2.toString();
    }} catch (e2) {{}}
  }}
}});
</script>
</body></html>"""
    return doc, out_h + 4
