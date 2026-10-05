"""静态底图上的「点选机位」组件（缩放 + 拖拽 + 右键选点）。

设计要点（改之前先读）
----------------------
1. **图片是死的，视图是活的**。底图就是外框内的一张静态图，页面上只显示它的
   一部分（"当前窗口"），缩放/拖拽只改这个窗口，不改图。
2. **点选 = 纯数学换算**，绝不依赖视图尺寸。点击位置先换成原图像素，再换成经纬度：

       原图 x = (点击处相对视口的比例 × 窗口宽) + 窗口左上角在原图中的 x
       经纬度 = fi.px_to_latlng(原图 x, 原图 y)

   因为窗口本身也是用同一套坐标算出来的，所以**任何缩放级下都严格对齐**。
3. **交互映射（2026-10-05 定稿）**：
       滚轮 / 双指  = 缩放
       **按住**      = 拖动平移
       **右键单击**  = 选点（**电脑端**，与 2026-10-02 起的老习惯一致）
       **双击轻点**  = 选点（**仅手机/触屏**）

   为什么要分两套：**电脑一定要保留右键单击**（用户 2026-10-05 明确要求，
   桌面同学早就习惯了），而**手机没有右键**，机位又是**必填** ——
   不给手机一个手势，手机用户就交不了稿 ⇒ 手机上补"双击轻点"。

   为什么手机是双击、而不是长按：拖动 = 按住 + **有位移**，双击 = 两次 **无位移** 的
   轻点，两者天然正交；而**长按**要跟拖动抢"同一根手指、同一个起点"，只能靠计时器
   + 几像素阈值去猜，用户按下后稍一犹豫就把平移变成选点（用户 2026-10-05 否掉了长按）。
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
# 尺寸自适应时的上下限（px）。上限放宽一点，让地图能占满竖向空间。
MIN_W, MAX_W = 320.0, 1500.0


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


def point_in_poly(x: float, y: float, flat) -> bool:
    """射线法（`flat` = [x0,y0,x1,y1,...]）。**必须与 JS `inPoly` 等价**。"""
    inside = False
    n = len(flat) // 2
    j = n - 1
    for i in range(n):
        xi, yi = flat[2 * i], flat[2 * i + 1]
        xj, yj = flat[2 * j], flat[2 * j + 1]
        if ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / (yj - yi) + xi):
            inside = not inside
        j = i
    return inside


def snap_to_footprint(x: float, y: float, roofs: dict | None) -> tuple[float, float]:
    """`snapToFootprint` 的 Python 版（两边必须一致，测试会比对）。

    立体底图上"屋顶"相对真实楼基有位移（= 建筑高度）。点到屋顶时反向平移
    `VIEW × dz` 就回到楼基 —— 因为轴测是平行投影，这个关系是严格的。

    `roofs["b"]` 是**远→近**排列（与底图绘制顺序一致），所以**倒着**遍历：
    第一个命中的就是视觉上真正压在最上面的那栋（否则被前排楼挡住的屋顶会被误吸附）。
    """
    if not roofs or not roofs.get("b"):
        return (x, y)
    vx, vy = roofs["v"]
    for dz, poly in reversed(roofs["b"]):
        if point_in_poly(x, y, poly):
            return (x - vx * dz, y - vy * dz)
    return (x, y)


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
    display_width: float = 1200.0,
    picked: tuple[float, float] | None = None,
    nav: tuple[float, float, int] | None = None,
    landmarks: dict[str, tuple[float, float]] | None = None,
    max_display_height: float = 980.0,
    tip: str = "滚轮缩放 · 左键按住拖动 · 右键选点",
    tip_mobile: str = "双指缩放 · 单指拖动 · 双击轻点选点",
    roofs: dict | None = None,
) -> tuple[str, float]:
    """生成点选组件的 HTML，返回 (html, 建议组件高度)。

    `nav` = (lat, lon, zoom) 是上次的视图状态；给了就恢复，没给就以 `picked`
    或外框中心为中心、z=0。

    `roofs`（可选）= 立体底图的**建筑屋顶多边形**（`relief_basemap.roof_shapes()`
    或 `glb_relief.roof_shapes()` 的返回值）。给了就启用**点选吸附**：

        立体底图上"屋顶"相对真实楼基有个位移（位移 = 建筑高度），同学点到屋顶时
        落点会偏出那栋楼。但"点屋顶"这个动作本身就说明**拍摄者在楼里/楼上**，
        所以应该贴回**那栋楼的足迹**（用户 2026-10-04 拍板要的）。

    轴测是平行投影 ⇒ 吸附 = 反向平移 `p - VIEW×dz`，不需要投影反解。
    瓦片 / 平面底图没有这个位移，传 None 即可（行为与以前一模一样）。
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
        "tip": tip, "tipMobile": tip_mobile,
        "initLat": n_lat, "initLon": n_lon, "initZoom": n_z,
        "maxZoom": MAX_ZOOM, "minW": MIN_W, "maxW": MAX_W, "maxH": max_display_height,
        "roofs": roofs or None,
    }
    # 防止 JSON 里的 </script> 提前闭合脚本块
    data_json = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")

    doc = f"""<!doctype html>
<html><head><meta charset="utf-8"><style>
  html,body {{ margin:0; padding:0; background:transparent; overflow:hidden;
               font:13px/1.5 system-ui,-apple-system,"Microsoft YaHei",sans-serif; }}
  #frame {{ position:relative; width:{base_w:.0f}px; height:{base_h:.0f}px;
            border:1px solid #cfd6dd; border-radius:6px; overflow:hidden;
            background:#e9ecef; cursor:grab; touch-action:none;
            /* 双击现在是选点手势 ⇒ 必须禁掉双击选中文字 / iOS 长按弹出菜单，
               否则双击会顺手把「提示」那行字选蓝，看起来像选点失败了。
               touch-action:none 同时也挡掉了浏览器的"双击放大页面"。*/
            user-select:none; -webkit-user-select:none; -webkit-touch-callout:none; }}
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
const hint = document.getElementById('hint');

// 提示文案**按设备给**：手机没有滚轮，桌面没有"双指"。
// 判据用 `pointer: coarse`（主要指针是"粗"的 = 手指），**不用** maxTouchPoints ——
// 带触摸屏的笔记本 maxTouchPoints 也 > 0，但它主要用鼠标，提示"双指"反而把人带偏。
// （桌面同学看到的仍是"滚轮/左键"那套，用户确认过的桌面习惯不变。）
// ⚠ 必须与实现一致：提示里写的手势，代码里就得有 —— "双指"曾经只是写在提示里、
//   根本没实现（2026-10-04 发现），这种文案比不写还坏。
const COARSE = !!(window.matchMedia && window.matchMedia('(pointer: coarse)').matches);
if (COARSE && D.tipMobile) hint.textContent = D.tipMobile;
hint.dataset.tip = hint.textContent;

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

// ---------------- 点选吸附（仅立体底图）----------------
// 轴测是**平行投影**：屋顶 = 足迹 + VIEW × 楼高。
// 所以"点在屋顶上"时把落点**反向平移**就回到了真实楼基 —— 不需要投影反解，
// 也不依赖任何相机参数。几何由 relief_basemap.roof_shapes() 导出（与实际底图同源）。
function inPoly(x, y, p) {{
  let inside = false;
  for (let i = 0, j = p.length - 2; i < p.length; j = i, i += 2) {{
    const xi = p[i], yi = p[i + 1], xj = p[j], yj = p[j + 1];
    if (((yi > y) !== (yj > y)) && (x < (xj - xi) * (y - yi) / (yj - yi) + xi))
      inside = !inside;
  }}
  return inside;
}}
function snapToFootprint(x, y) {{
  const R = D.roofs;
  if (!R || !R.b || !R.b.length) return [x, y];
  // `R.b` 是**远 -> 近**排列（与底图绘制顺序一致）。倒着找：第一个命中的就是
  // **最靠近观众的那栋**，也就是视觉上真正压在上面的那栋 —— 否则被前排楼挡住的
  // 屋顶也会被误吸附（用户看到的明明是前排那栋）。
  for (let i = R.b.length - 1; i >= 0; i--) {{
    const dz = R.b[i][0];
    if (inPoly(x, y, R.b[i][1])) return [x - R.v[0] * dz, y - R.v[1] * dz];
  }}
  return [x, y];
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

// ---------------- 尺寸自适应 ----------------
// ⚠️ 关键：**必须保持图片自身的宽高比**（= 外框的宽高比）。
//
// 上一版我把它铺满整个宽容器，结果容器是宽扁的（1220×745，比 1.64）而框是竖长的
// （比 1.11）—— 为了铺满宽度，竖直方向只显示了外框的一半多点，**校园被裁在小窗口里
// 又显得偏**（用户反馈："校园范围本身就没截全"）。
//
// 正确做法：按"框的比例"取一个尽量大的尺寸，同时受可用宽、可用高限制，
// 宽高同乘一个比例缩放 —— 这样整张图（=整个外框）完整显示、比例不变形。
function fitToParent() {{
  let availW = D.outW, availH = D.maxH;
  try {{
    // 可用宽 = **组件自己的视口宽**。Streamlit 用 width="stretch" 把这个 iframe
    // 拉满它分配到的空间，所以 window.innerWidth 就是"有多少宽度可用"的准确答案。
    //
    // 为什么不再用 `parent.innerWidth - 170`（老写法）：170 是**猜的侧边栏宽度**。
    // 手机上侧边栏是收起的（site_common: initial_sidebar_state="auto"），
    // 再减 170 等于白白把地图压窄一截（实测 390px 的屏少用 38px）；
    // 而如果按"收起了就不减"改成减 24，又会算出比容器还宽的值、右侧被裁。
    // 直接用自身视口宽，两种屏幕都不用猜。
    const own = window.innerWidth;
    // 下限只做"别离谱"的兜底（120）：**不能**再夹回 MIN_W(320) ——
    // 窄屏手机可能比 320 还窄，硬夹会让地图比容器宽、右边被裁掉。
    if (own) availW = Math.min(D.maxW, Math.max(120, own - 2));
    const ph = window.parent ? window.parent.innerHeight : 0;
    if (ph) availH = Math.min(D.maxH, Math.max(320, ph - 90));
  }} catch (e) {{}}
  // 按框的比例等比缩放到可用区域内（宽高用同一个 k）
  const k = Math.min(availW / D.iw, availH / D.ih);
  const w = Math.round(D.iw * k), h = Math.round(D.ih * k);
  frame.style.width = w + 'px';
  frame.style.height = h + 'px';
  render();
  try {{
    if (window.frameElement) {{
      const fh = h + 6;
      window.frameElement.style.height = fh + 'px';
      window.frameElement.setAttribute('height', Math.round(fh));
    }}
  }} catch (e) {{}}
}}
window.addEventListener('resize', fitToParent);

// ---------------- 交互 ----------------
// 滚轮 = 缩放（以鼠标位置为锚）。锚点算法在 zoomAtPoint 里，与双指捏合共用。
frame.addEventListener('wheel', function (ev) {{
  ev.preventDefault();
  if (zoomAtPoint(zoom + (ev.deltaY < 0 ? 1 : -1), ev.clientX, ev.clientY)) report(false);
}}, {{ passive: false }});

// ---------------- 缩放锚点：把某个屏幕点上看到的地物"钉住" ----------------
// 滚轮、双指捏合、缩放按钮**共用这一套**。
// 为什么非要共用：各写一遍必然漂移，症状是"滚轮缩放是对的、双指一捏位置就跑了"，
// 而且两边都不报错。
function centerOn(p, clientX, clientY) {{
  const r = frame.getBoundingClientRect();
  const w = winW(), h = winH(), scale = vw() / w;
  cx = p[0] + w / 2 - (clientX - r.left) / scale;
  cy = p[1] + h / 2 - (clientY - r.top) / scale;
  clampCenter();
}}
function zoomAtPoint(nz, clientX, clientY) {{
  nz = Math.max(0, Math.min(D.maxZoom, nz));
  if (nz === zoom) return false;
  const before = screenToImg(clientX, clientY);   // 缩放前，这个屏幕点下面是哪个地物
  zoom = nz;
  centerOn(before, clientX, clientY);             // 缩放后让它还落在同一个屏幕点
  render();
  return true;
}}

// ---------------- 拖动 / 双指捏合 / 双击，都走 pointer 事件 ----------------
// 鼠标和手指派的都是 pointer 事件 ⇒ **一套处理**就够（不必再写一份 touch 分支）。
// 用 `ptrs` 记住当前按下的每个 pointer，才能分辨"一指 = 拖动/双击"与"两指 = 捏合"。
//
// ⚠ 位移用"钳位后的实际位移"来算，不能直接用指针位移：
//   视图贴到边界时 clampCenter() 会吃掉一部分位移，若仍按指针全量移动，
//   图像就会与光标"脱手"（拖着拖着光标和地图对不上）。
//   注意 z=0 时窗口 == 整图，本来就无处可移（钳位全吃掉）—— 那是**正确行为**，
//   不是 bug：想平移请先放大。
const ptrs = new Map();                 // pointerId -> {{x, y}}（当前按下的所有指针）
let drag = null;
let pinch = null;                       // {{d0, z0, mid, dirty}}

// **双击轻点 = 选点（仅触屏）**；电脑端是**右键单击**选点（见文件头"交互映射"）。
//
// 为什么手机非有双击不可：触屏没有右键，而机位是**必填** ——
// 不给手机一个选点手势，手机用户就交不了稿。
//
// 为什么不用浏览器自带的 `dblclick` 事件：触屏上 `dblclick` 各浏览器行为不一
// （不少手机根本不派发），而 `pointerdown/pointerup` 鼠标和手指走的是同一套 ——
// 自己数"两次无位移的轻点"最稳，再按 `pointerType` 把鼠标排除掉。
//
// 与拖动为什么不会打架：拖动一定会先 `moved`（位移 >3px）⇒ 直接作废连击计数；
// 双击则两次都没有位移。两者互斥，不需要计时器去猜"用户是想拖还是想选"。
const DBL_MS = 350;   // 两次轻点的最大间隔
const DBL_PX = 28;    // 两次轻点的最大偏移（手指比鼠标抖，给宽一点）
let tap = {{ t: 0, x: 0, y: 0 }};

function twoPts() {{
  const a = [];
  for (const p of ptrs.values()) a.push(p);
  return a;
}}
function distOf(a, b) {{ return Math.hypot(a[0] - b[0], a[1] - b[1]); }}

frame.addEventListener('pointerdown', function (ev) {{
  if (ev.button === 2) return;            // 右键交给 contextmenu 处理
  if (ev.button !== 0) return;
  ptrs.set(ev.pointerId, {{ x: ev.clientX, y: ev.clientY }});
  try {{ frame.setPointerCapture(ev.pointerId); }} catch (e) {{}}
  if (ptrs.size === 1) {{
    drag = {{ x: ev.clientX, y: ev.clientY, cx: cx, cy: cy, moved: false }};
    frame.classList.add('drag');
  }} else if (ptrs.size === 2) {{
    // 第二根手指落下 = 要捏合 ⇒ 取消拖动与连击
    // （不取消的话，两指操作结束时会被当成"拖动"回传，甚至凑巧算成一次选点）
    drag = null;
    frame.classList.remove('drag');
    tap.t = 0;
    const q = twoPts();
    const a = [q[0].x, q[0].y], b = [q[1].x, q[1].y];
    pinch = {{ d0: distOf(a, b), z0: zoom,
              mid: [(a[0] + b[0]) / 2, (a[1] + b[1]) / 2], dirty: false }};
  }}
}});

frame.addEventListener('pointermove', function (ev) {{
  if (ptrs.has(ev.pointerId)) ptrs.set(ev.pointerId, {{ x: ev.clientX, y: ev.clientY }});

  // 两指：捏合缩放 + 跟着两指中点平移（手机的主用缩放方式）
  if (pinch && ptrs.size >= 2) {{
    const q = twoPts();
    const a = [q[0].x, q[0].y], b = [q[1].x, q[1].y];
    const d = distOf(a, b), m = [(a[0] + b[0]) / 2, (a[1] + b[1]) / 2];
    // 手指间距每翻一倍 = 变一级。底图缩放级是**整数**（0/1/2，上限见 MAX_ZOOM）：
    // 不做连续缩放，一是底图过 4× 就是马赛克、二是 zoom 会写进 pnav，
    // 整数级才能保证 rerun 之后视图一模一样地恢复。
    const step = pinch.d0 > 8 ? Math.round(Math.log2(d / pinch.d0)) : 0;
    const nz = Math.max(0, Math.min(D.maxZoom, pinch.z0 + step));
    const anchor = screenToImg(pinch.mid[0], pinch.mid[1]);   // 旧中点下面是哪个地物
    zoom = nz;
    centerOn(anchor, m[0], m[1]);                             // 把它挪到新中点
    pinch.mid = m;
    pinch.dirty = true;
    render();
    return;
  }}

  if (!drag) return;
  const dx = ev.clientX - drag.x, dy = ev.clientY - drag.y;
  if (!drag.moved && Math.abs(dx) + Math.abs(dy) > 3) drag.moved = true;
  if (!drag.moved) return;
  const scale = vw() / winW();
  const wantX = drag.cx - dx / scale;      // 按指针位移的目标中心
  const wantY = drag.cy - dy / scale;
  cx = wantX; cy = wantY;
  clampCenter();                            // 边界处会被夹住
  // 把"被夹住的那部分"从基准点里扣掉，保证光标下的地物不跑
  drag.cx += (cx - wantX);
  drag.cy += (cy - wantY);
  render();
}});

frame.addEventListener('pointerup', function (ev) {{
  const wasPinch = !!pinch;
  ptrs.delete(ev.pointerId);
  if (pinch && ptrs.size < 2) {{
    // 捏合**结束时才回传**：中间过程每帧都 report 会触发一串 Streamlit rerun，
    // 手机上直接卡成幻灯片。
    const dirty = pinch.dirty;
    pinch = null;
    if (dirty) report(false);
  }}
  if (wasPinch || !drag) return;          // 捏合/右键：不参与拖动与双击
  const moved = drag.moved; drag = null;
  frame.classList.remove('drag');
  if (moved) {{
    tap.t = 0;                            // 拖动过 -> 作废上一次轻点，避免"拖完再点"被误判成双击
    report(false);
    return;
  }}
  // ⚠ **双击轻点只在触屏上算选点**：电脑端照旧是**右键单击**选点。
  //   桌面习惯保持 2026-10-02 定下的那套（用户 2026-10-05 明确：
  //   "电脑端和以前一样是右键单击选点啊"），所以鼠标的两次左键点击**不产生选点**——
  //   免得电脑上凭空多出一条没人要求的新手势。
  if (ev.pointerType === 'mouse') return;
  const now = performance.now();
  if (now - tap.t <= DBL_MS && Math.hypot(ev.clientX - tap.x, ev.clientY - tap.y) <= DBL_PX) {{
    tap.t = 0;                            // 用掉这次连击，避免"点三下"被算成两次双击
    doPick(ev.clientX, ev.clientY);
  }} else {{
    tap.t = now; tap.x = ev.clientX; tap.y = ev.clientY;
  }}
}});
frame.addEventListener('pointercancel', function (ev) {{
  ptrs.delete(ev.pointerId);
  if (ptrs.size < 2) pinch = null;
  drag = null; frame.classList.remove('drag');
}});

// 选点只此一处实现：双击（桌面/手机）与右键（桌面兜底）都走它，
// 免得两条路径各写一遍吸附+落针+回传，改一处漏一处。
function doPick(clientX, clientY) {{
  const raw = screenToImg(clientX, clientY);
  const p = snapToFootprint(raw[0], raw[1]);      // 点到屋顶 -> 贴回楼基
  pin.dataset.ix = p[0]; pin.dataset.iy = p[1]; pin.classList.add('on');
  const ll = imgPxToLatLng(p[0], p[1]);
  render();
  flashSnap(raw, p);
  report(true, ll[0], ll[1]);
}}

// 桌面右键选点（兜底，不进提示文案）。
// 必须屏蔽浏览器右键菜单，否则弹菜单把操作打断。
frame.addEventListener('contextmenu', function (ev) {{
  ev.preventDefault();
  doPick(ev.clientX, ev.clientY);
}});

// 吸附发生时给一句提示 —— 否则同学会奇怪"我点的位置怎么自己变了"
let snapTimer = null;
function flashSnap(raw, p) {{
  if (!hint) return;
  if (hint.dataset.tip === undefined) hint.dataset.tip = hint.textContent;
  const moved = Math.hypot(p[0] - raw[0], p[1] - raw[1]) > 0.5;
  hint.textContent = moved ? '已吸附到楼基（你点的是楼顶）' : hint.dataset.tip;
  if (snapTimer) clearTimeout(snapTimer);
  snapTimer = setTimeout(function () {{ hint.textContent = hint.dataset.tip; }}, 2600);
}}

// 缩放按钮：以**视图中心**为锚（等于视图中心不动），手机上的兜底缩放方式
zin.addEventListener('click', function () {{
  const r = frame.getBoundingClientRect();
  if (zoomAtPoint(zoom + 1, r.left + vw() / 2, r.top + vh() / 2)) report(false);
}});
zout.addEventListener('click', function () {{
  const r = frame.getBoundingClientRect();
  if (zoomAtPoint(zoom - 1, r.left + vw() / 2, r.top + vh() / 2)) report(false);
}});

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
// ⚠ **只有"选点"才主动触发 rerun，视图变化只写 URL**（2026-10-05 修，用户反馈
//   "和地图交互时经常一闪一闪的，手感很怪很卡顿"）。
//
// 原因：Streamlit 是靠 `popstate` 重跑脚本的。以前每拖一下 / 每滚一格都
// `dispatchEvent(popstate)` ⇒ 整个 `components.html` 被**重建** ⇒ 那张约 1 MB 的
// 底图 data URL 要重新解码、重新布局 ⇒ 肉眼就是"闪一下 + 卡"。
// 而 `pnav` 的唯一用途是"下次 rerun 时恢复视图"，**写进 URL 就够了**；
// 真正的 rerun 交给选点去触发，那一次报告**自带 pnav**，所以视图照样恢复。
//
// 判据（tools/cdp_dblclick_pick.mjs 第⑦项）：滚 3 格 + 拖 1 次后，
// 父页面的 popstate 计数必须为 0，且组件里的存活标记还在（没被重建）。
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
    // 只有选点才让 Streamlit 重跑（视图变化不重跑）
    if (isPick) window.parent.dispatchEvent(new PopStateEvent('popstate'));
  }} catch (e) {{
    // 兜底（history 被跨域等限制）：整页跳转会"闪"得更狠，所以**只在选点时**做
    if (!isPick) return;
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
