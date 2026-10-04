# -*- coding: utf-8 -*-
"""2.5D **立体**底图渲染（站牌式轴测风格）。

用户给的参考（2026-10-03 桌面截图《临顿路站综合信息图》）：
    建筑做**轴测挤出**（看得见墙面 + 屋顶 + 投影阴影）、树做圆簇、
    地面（路/水/绿地）保持**平面色块**、POI 用彩色药丸标签。

为什么这样画不会破坏"点选"（关键，别改错）
------------------------------------------
实时地图那一套之所以难做准，是因为**透视投影**下屏幕像素与经纬度不是线性关系。
而这里的立体感来自**平行（轴测）投影**：

    图像坐标 = 地面仿射映射 + 高度向量 × 建筑高度

`高度向量` 只作用在建筑自身的绘制上，**地面平面到图像的映射仍是仿射的** ——
也就是 `static_basemap` / `vector_basemap` 那套 `像素 ↔ 经纬度` 换算**原样成立**。
（这也是为什么返回的仍是同一个 `FrameImage`，上层点选一行都不用改。）

副作用与对策：屋顶相对足迹有位移，点到"屋顶"会偏出真实楼基。
`roof_offset_px()` 把这个位移量暴露出来，`frame_picker` 用它可以做
"点在屋顶上 → 贴回该楼真实足迹"的吸附。
"""
from __future__ import annotations

import hashlib
import io
import json
import math
from pathlib import Path

import vector_basemap as vb
from vector_basemap import (C, IMAGE_CACHE, RAW, ZOOM, Projector, _area_rings,
                            _lines_of, _load_json, _rings_of, _font)

# ------------------------------------------------------------------ 风格参数
# 屋顶相对足迹的偏移方向（单位 = 该建筑的高度像素）。观众在**右下方**：
# 所以屋顶往上、略偏左，看得见南墙与东墙。
VIEW = (-0.34, -1.0)
# 光照方向（从**左上**打光）：墙面明暗与投影都按它算。
LIGHT = (-0.62, -0.78)
# 投影偏移（单位 = 高度像素），与 VIEW 反向、略压扁。
# ⚠ 阴影是**从楼基扫掠出去**的（见 _draw_scene 里的注释），不是"整体平移的足迹" ——
#   后者会让楼和影子之间空一段，整片楼像浮在天上。
SHADOW = (0.52, 0.62)
SHADOW_ALPHA = 0.30
SHADOW_BLUR = 3.4
# 长影子的**压缩**（用户："有几栋楼的影子貌似太长了，视觉上不太协调"）。
# 鼓楼 OSM 里标着 40 层的楼 ⇒ 线性算出来影子 130+ px，横跨半条街，整张图的尺度就乱了。
# 但**矮楼的影子是好看的**，不能整体缩短 ⇒ 只在超过 SOFT 之后平滑饱和到 MAX。
SHADOW_SOFT_PX = 18.0       # 到这个长度之前完全线性（保持"同一个太阳"的手感）
SHADOW_MAX_PX = 30.0        # 渐近上限（再高的楼也不会更长）


def shadow_offset(dz: float, z: float) -> tuple[float, float]:
    """由楼高（像素）算投影偏移。矮楼线性、高楼饱和。

    `z` 是"相对 1400px 画布的缩放"，所以 SOFT/MAX 两个阈值是分辨率无关的。
    """
    if dz <= 0:
        return (0.0, 0.0)
    soft, cap = SHADOW_SOFT_PX * z, SHADOW_MAX_PX * z
    if dz <= soft:
        k = dz
    else:
        k = soft + (cap - soft) * math.tanh((dz - soft) / max(1e-6, cap - soft))
    return (SHADOW[0] * k, SHADOW[1] * k)
# 高度夸张系数：1.0 = 真实比例。太大会挡住后排、也太"假"。
EXAG = 1.0

WALL_TOP = (226, 218, 203)      # 迎光面
WALL_SIDE = (166, 155, 138)     # 背光面（**全局默认**）
# ↑ 2026-10-03 用户要求"压深墙面对比度"：原来 186,176,159 太浅，
#   1:1 看整片像"浅色纸片"。压深后屋顶（238,232,221）与墙面拉开，体块感更强，
#   这也更接近卫星影像的观感（影像里屋顶亮、墙面/阴影深）。
# ⚠ 2026-10-04 用户拍板：**仙林仍要压深前那档浅色**。因为上面那次压深是"全局色板"，
#   仙林被一起带深了一档，而用户此前明确认可的是仙林浅的那版 ⇒ 拆成"每校区一套"。
#   要单独调某校区就往这里加一条；没写的校区用全局 `WALL_SIDE`。
#   ⚠ 这张表**必须**进 `_style_tag()`，否则改了色板缓存不失效，会看到旧图（老坑）。
WALL_SIDE_BY_CAMPUS: dict[str, tuple[int, int, int]] = {
    "xianlin": (186, 176, 159),
}
ROOF = (238, 232, 221)          # 屋顶偏亮 —— 参考图里屋顶是最亮的一层
ROOF_EDGE = (166, 156, 140)
# 树：三档绿做圆簇（受光面 -> 暗面），与绿地底色（green/forest）区分得开
TREE_DARK = (120, 152, 104)
TREE_MID = (146, 176, 124)
TREE_LIGHT = (176, 199, 148)
# 密度与层数 —— **这是底图体积的开关**：树是高频内容，画太多 JPEG 能到 1 MB。
# 实测仙林（南雍山整片林）：step 9z/3 层 ⇒ 1.03 MB；step 12z/2 层 ⇒ 明显回落。
# 底图要 base64 内嵌进 HTML，手机上打开快慢全看这个，所以宁可稀一点。
TREE_STEP_Z = 16.0          # 林地撒点网格间距 = TREE_STEP_Z × z（像素）
TREE_DENSITY = 0.70         # 网格点被采纳的概率
TREE_LAYERS = 2             # 每个树冠画几层圆（2 = 暗底 + 亮顶）

DEFAULT_FLOORS = 4              # 没标层数时的默认（南大教学楼多为 4~6 层）
METRES_PER_FLOOR = 3.3

# ---------------------------------------------------------------- 建筑外观
#
# 用户反馈（2026-10-03）：「屋子都是纯立方体，有点没意思，也不够美观」。
# 所以一栋楼不再等于"一个填充块 + 一个屋顶"，而是分五层画出来：
#   ① 底部环境光遮蔽 —— 贴地一圈暗带，把楼"压"在地上（不然像贴纸）
#   ② 墙面 —— 按朝向分明暗
#   ③ 楼层线 —— 有几层画几条，远看就是窗带，**这一步最能破"光板盒子"**
#   ④ 屋顶 —— 比墙面亮一档，外加一圈女儿墙（收进去一点、再深一点）
#   ⑤ 屋顶家具 —— 水箱/机房/AHU 的小凸起（确定性随机）
# 再按**功能**给色调：宿舍偏砖红、医院/科研偏冷、办公偏灰。整片楼才会"有生活"。
TYPE_TINT: dict[str, tuple[int, int, int]] = {
    "dormitory":      (11, -5, -15),
    "apartments":     (10, -4, -13),
    "residential":    (10, -4, -13),
    "house":          (9, -3, -12),
    "hospital":       (-9, 2, 9),
    "office":         (-8, 0, 6),
    "commercial":     (2, 1, -1),
    "retail":         (2, 1, -1),
    "train_station":  (-7, -1, 5),
    "transportation": (-7, -1, 5),
    "stadium":        (-5, 2, 7),
    "civic":          (-3, 1, 3),
    "construction":   (-15, -11, -9),
}
# 苏州那种"按建筑组团名上色"的表在 glb_relief.py 里（模型名只有那边用得到）
# 没标层数时按功能给个合理默认（不然"公寓"和"门卫"一样高，天际线就很假）
TYPE_FLOORS: dict[str, float] = {
    "apartments": 6.5, "residential": 6.0, "dormitory": 6.0,
    "hospital": 9.0, "office": 8.0, "hotel": 8.0,
    "train_station": 12.0, "transportation": 10.0, "stadium": 10.0,
    "house": 2.0, "garage": 1.5, "garages": 1.5, "shed": 1.5,
    "construction": 3.0, "commercial": 5.0, "retail": 4.0,
}
AO_ALPHA = 96                   # 底部遮蔽的不透明度（0~255）
FLOOR_LINE_ALPHA = 92           # 楼层线的不透明度
ROOF_MIN_PX = 16.0              # 屋顶小于这个尺寸就不做女儿墙/家具（做了反而脏）

# ------------------------------------------- 校园范围虚线 + 校外"挡视线"的高楼
# 用户 2026-10-04 要求：
#   ① 「鼓楼校区南边有两三栋太高的**非学校**的楼，遮着学校内部建筑了，把这几个去掉」
#      —— 轴测是**向上挤出**的：南边的高楼往上长，正好横跨校园内部 ⇒ 直接不画。
#      判据 = 在**校园边界南侧**且高度 ≥ SKIP_OUTSOUTH_H。
#      实测正好命中那 3 栋（两栋 132 m + 广海大厦 120 m），下一档只到 100 m ⇒ 不会误伤。
#   ② 「在图上实际地标一下校园的范围（蓝色虚线之类的）」
#      —— ⚠ 这与**外框 frame** 不是一回事：frame = 校园 + 周边 150 m 缓冲，
#      而这条虚线画的是**校园本身的边界**（`data/raw/campus_boundaries.json`）。
DRAW_CAMPUS_BOUNDARY = True
BOUNDARY_COLOR = (38, 108, 214)     # 蓝色虚线（用户要求）
BOUNDARY_WIDTH = 3
BOUNDARY_DASH = 14.0                # 实线段长（像素，随 z 缩放）
BOUNDARY_GAP = 9.0                  # 间隔（像素，随 z 缩放）
SKIP_OUTSOUTH_H = 120.0             # 校园南侧超过这个高度的**校外**建筑不画


def _tint_of(props: dict) -> tuple[int, int, int]:
    t = str(props.get("building") or "").strip()
    return TYPE_TINT.get(t, (0, 0, 0))


def _palette(props: dict, seed, z: float, campus: str | None = None):
    """按功能 + 确定性抖动，给这栋楼一套 (屋顶, 迎光墙, 背光墙, 轮廓) 颜色。

    抖动是关键：一排一模一样的楼看着就是"复制的方块"，深浅错开才有街区感。
    背光墙面（对比度）按校区取值：见 `WALL_SIDE_BY_CAMPUS`。
    """
    off = _tint_of(props)
    k = 0.94 + 0.12 * _hash01(float(seed), 11.0)
    side = WALL_SIDE_BY_CAMPUS.get(str(campus or "").lower(), WALL_SIDE)

    def f(c):
        return tuple(max(0, min(255, int(round((c[i] + off[i]) * k)))) for i in range(3))

    return f(ROOF), f(WALL_TOP), f(side), f(ROOF_EDGE)


def _point_in_ring(x, y, ring) -> bool:
    inside = False
    n = len(ring)
    for i in range(n):
        x1, y1 = ring[i]
        x2, y2 = ring[(i + 1) % n]
        if (y1 > y) != (y2 > y):
            xin = (x2 - x1) * (y - y1) / (y2 - y1) + x1 if y2 != y1 else x1
            if x < xin:
                inside = not inside
    return inside


def _skipped_indexes(blds, bounds) -> set:
    """挑出**不画**的建筑索引：校园南侧那些又高又在校园外的楼。

    用户 2026-10-04：「鼓楼校区南边有两三栋太高的**非学校**的楼，遮着学校内部建筑了」。
    轴测是**向上挤出**的 —— 南边的高楼往上长，正好横跨校园内部的建筑；而它们又不在
    校园里，留着只会挡视线 ⇒ 直接不画（连同它们的投影一起，因为投影也在 items 里）。

    ⚠ 用自带的 `_point_in_ring`（射线法）判位置，**千万不要引入 shapely**：
    云端 `requirements.txt` 里没有 shapely（它只用于本地抓图/建模），
    一旦底图缓存失效需要在云端重新渲染，就会 ImportError ⇒ 退回瓦片。
    """
    geo = (bounds or {}).get("geojson")
    if not geo:
        return set()
    try:
        rings = geo["coordinates"]
    except Exception:
        return set()
    outer = rings[0] if rings else []          # 只取外环求南界
    if len(outer) < 3:
        return set()
    s_lat = min(pt[1] for pt in outer)

    skip = set()
    for i, feat in enumerate(blds.get("features", [])):
        props = feat.get("properties") or {}
        if _height_m(props, i) < SKIP_OUTSOUTH_H:
            continue
        for ring in _rings_of(feat.get("geometry") or {}):
            if len(ring) < 3:
                continue
            cy = sum(p[1] for p in ring) / len(ring)
            if cy < s_lat:                     # 质心在校园南界之外
                skip.add(i)
                break
    return skip


def _dashed_polygon(d, pts, color, width, dash, gap) -> None:
    """画**虚线**多边形（PIL 没有原生虚线，只能按段拆）。`pts` 已是像素坐标。"""
    n = len(pts)
    for i in range(n):
        x0, y0 = pts[i]
        x1, y1 = pts[(i + 1) % n]
        seg = math.hypot(x1 - x0, y1 - y0)
        if seg <= 0.5:
            continue
        ux, uy = (x1 - x0) / seg, (y1 - y0) / seg
        t = 0.0
        while t < seg:
            e = min(t + dash, seg)
            d.line([(x0 + ux * t, y0 + uy * t), (x0 + ux * e, y0 + uy * e)],
                   fill=color, width=width)
            t = e + gap


def _draw_campus_boundary(d, proj, bounds, z) -> None:
    """在底图上画**校园范围**的蓝色虚线。

    ⚠ 与 `frame`（= 校园 + 周边 150 m 缓冲，也是整张图的外框）**不是一回事**：
    这条线画的是**校园本身的边界**（`data/raw/campus_boundaries.json` 的 geojson），
    让同学一眼看出"哪儿才算校园里"。

    画两遍：先白色略粗打底、再蓝色 —— 这样压在深色屋顶或浅色地面上都看得清。
    """
    geo = (bounds or {}).get("geojson")
    if not geo:
        return
    try:
        rings = geo["coordinates"]
    except Exception:
        return
    dash, gap = BOUNDARY_DASH * z, BOUNDARY_GAP * z
    for ring in rings:
        pts = [proj.pt(lo, la) for lo, la in ring]
        if len(pts) < 3:
            continue
        _dashed_polygon(d, pts, (255, 255, 255), BOUNDARY_WIDTH + 2, dash, gap)
        _dashed_polygon(d, pts, BOUNDARY_COLOR, BOUNDARY_WIDTH, dash, gap)


def _roof_furniture(ring, seed, ppm, exag: float):
    """屋顶上的小凸起（水箱/机房/AHU）。返回 [(四点矩形, 高度像素), ...]。

    只在**够大的屋顶**上放：小楼顶（一二十像素）放上去只会变成脏点，
    而大屋顶上几个小方块立刻就有"这是真建筑"的信息量。
    """
    xs = [p[0] for p in ring]
    ys = [p[1] for p in ring]
    w, h = max(xs) - min(xs), max(ys) - min(ys)
    if min(w, h) < ROOF_MIN_PX:
        return []
    out = []
    n = 1 + int(_hash01(float(seed), 21.0) * 3.0)          # 1~3 个
    for i in range(n):
        for att in range(6):
            fx = 0.22 + 0.56 * _hash01(float(seed), 30.0 + i * 7 + att)
            fy = 0.22 + 0.56 * _hash01(float(seed), 40.0 + i * 7 + att)
            px, py = min(xs) + fx * w, min(ys) + fy * h
            if not _point_in_ring(px, py, ring):
                continue
            bw = (3.5 + 5.0 * _hash01(float(seed), 50.0 + i)) * ppm * exag
            bh = (3.0 + 4.0 * _hash01(float(seed), 60.0 + i)) * ppm * exag
            dz = (2.0 + 2.6 * _hash01(float(seed), 70.0 + i)) * ppm * exag
            out.append(([(px - bw / 2, py - bh / 2), (px + bw / 2, py - bh / 2),
                         (px + bw / 2, py + bh / 2), (px - bw / 2, py + bh / 2)], dz))
            break
    return out



def _shade(color, f):
    return tuple(max(0, min(255, int(round(c * f)))) for c in color)


def _px_per_m(proj, lat):
    """图像像素 / 米（中心处，数值差分求，避免手推墨卡托导数出错）。"""
    x0, _ = proj.pt(120.0, lat)
    x1, _ = proj.pt(120.0 + 0.001, lat)
    y0 = proj.pt(120.0, lat)[1]
    y1 = proj.pt(120.0, lat + 0.001)[1]
    return abs((x1 - x0) / (0.001 * 111320.0 * math.cos(math.radians(lat)))), \
        abs((y1 - y0) / (0.001 * 110540.0))


def _height_m(props, key):
    """建筑高度（米）：height 优先，其次 building:levels，再按**功能**给默认层数。

    完全没标注时用**确定性抖动**（按 id 哈希）—— 一排楼高矮完全一样会很假，
    而随机又不能每次渲染都变（缓存/复现会炸）。
    """
    for k in ("height", "building:height"):
        v = props.get(k)
        if v:
            try:
                h = float(str(v).split(";")[0].replace("m", "").strip())
                if 2.0 <= h <= 400.0:
                    return h
            except Exception:
                pass
    for k in ("building:levels", "levels"):
        v = props.get(k)
        if v:
            try:
                lv = float(str(v).split(";")[0].strip())
                if 0.5 <= lv <= 120:
                    return lv * METRES_PER_FLOOR
            except Exception:
                pass
    t = str(props.get("building") or "").strip()
    floors = TYPE_FLOORS.get(t, DEFAULT_FLOORS)
    seed = str(props.get("id") or props.get("name") or key)
    dig = int(hashlib.md5(seed.encode("utf-8")).hexdigest()[:6], 16)
    return floors * METRES_PER_FLOOR * (0.86 + 0.28 * (dig / 0xFFFFFF))


def _outward_normal(p, q, cx, cy):
    """边 (p→q) 的**朝外**法向（用"指向远离质心"那一侧判断）。"""
    dx, dy = q[0] - p[0], q[1] - p[1]
    n = (dy, -dx)
    ln = math.hypot(*n) or 1.0
    n = (n[0] / ln, n[1] / ln)
    mx, my = (p[0] + q[0]) / 2.0, (p[1] + q[1]) / 2.0
    if (mx - cx) * n[0] + (my - cy) * n[1] < 0:
        n = (-n[0], -n[1])
    return n


def roof_offset_px(height_m: float, px_per_m: float) -> tuple[float, float]:
    """某高度的建筑，屋顶相对足迹的像素位移（点选吸附要用）。"""
    dz = height_m * px_per_m * EXAG
    return (VIEW[0] * dz, VIEW[1] * dz)


def roof_shapes(campus: str, s, w, n, e, *, width: int = 2000) -> dict:
    """导出**每栋楼的屋顶多边形**（原图像素），供 `frame_picker` 做点选吸附。

    为什么要吸附（用户 2026-10-04 拍板）：
        "点到楼顶其实说明是在楼里面拍的，自然要吸附回去。"
        立体底图下屋顶相对足迹有个位移，同学点到屋顶时落点会**偏出真实楼基**
        （偏移量 = 建筑高度）。但"点屋顶"这个动作本身就说明拍摄者在楼里/楼上，
        所以要贴回**那栋楼的足迹**。

    几何（轴测 = 平行投影，见模块 docstring）：
        屋顶 = 足迹 + VIEW × 高度像素
    ⇒ 吸附就是**反向平移** `p - VIEW×dz`，不需要投影反解，也不依赖任何相机参数。

    返回：
        {"v": [vx, vy], "b": [[dz, [x0,y0,x1,y1,...]], ...]}
        `b` **按绘制顺序（远 → 近）**排列：前端命中多栋时取**最后一个**命中的，
        这样"被前排楼挡住的屋顶"不会被误吸附（视觉上看到的确实是前排那栋）。
    """
    P = _project(s, w, n, e, width)
    proj, ppm = P["proj"], P["ppm"]
    blds = _load_json(RAW / f"{campus}_buildings.geojson")
    try:
        bounds = _load_json(RAW / "campus_boundaries.json").get(campus, {})
    except Exception:
        bounds = {}
    # ⚠ 必须和 `_render` 用**同一套排除**：底图不画那几栋楼了，吸附却还认得它们的话，
    #   同学点到那片空白会被"吸"到一栋看不见的楼上（几何不同源 = 静默错位）。
    skip = _skipped_indexes(blds, bounds)

    items = []
    for idx, feat in enumerate(blds.get("features", [])):
        if idx in skip:
            continue
        props = feat.get("properties") or {}
        dz = _height_m(props, idx) * ppm * EXAG
        if dz < 1.0:
            continue          # 位移不到 1 px，吸不吸附一个样，不必白传几何
        for ring in _rings_of(feat.get("geometry") or {}):
            pts = [proj.pt(lo, la) for lo, la in ring]
            if len(pts) >= 3:
                items.append((max(p[1] for p in pts), dz, pts))
    items.sort(key=lambda t: t[0])        # 与 `_draw_scene` 同一套远近顺序

    out = []
    for _, dz, pts in items:
        flat = []
        for x, y in pts:
            flat.append(round(x + VIEW[0] * dz, 1))
            flat.append(round(y + VIEW[1] * dz, 1))
        out.append([round(dz, 1), flat])
    return {"v": [VIEW[0], VIEW[1]], "b": out}


def _hash01(*nums) -> float:
    """确定性的 [0,1) 伪随机（同样的输入永远给同样的结果，渲染才可复现/可缓存）。"""
    h = hashlib.md5(("|".join(f"{n:.3f}" for n in nums)).encode("utf-8")).digest()
    return int.from_bytes(h[:4], "big") / 2 ** 32


def _tree_points(proj, vec, z):
    """行道树（`natural=tree_row`）：沿折线按固定间距放树。"""
    pts = []
    spacing = max(5.0, 8.0 * z)
    for f in vec.get("features", []):
        pr = f.get("properties") or {}
        if pr.get("natural") != "tree_row":
            continue
        for line in _lines_of(f.get("geometry") or {}):
            path = [proj.pt(lo, la) for lo, la in line]
            for i in range(len(path) - 1):
                (x0, y0), (x1, y1) = path[i], path[i + 1]
                seg = math.hypot(x1 - x0, y1 - y0)
                n = max(1, int(seg // spacing))
                for k in range(n):
                    t = (k + 0.5) / n
                    x, y = x0 + (x1 - x0) * t, y0 + (y1 - y0) * t
                    pts.append((x, y, (3.0 + 1.4 * _hash01(x, y)) * z))
            if len(path) == 1:
                x, y = path[0]
                pts.append((x, y, 3.4 * z))
    return pts


def _tree_points_sat(campus: str, proj, z):
    """读「从卫星影像提取的树」`data/raw/<campus>_trees.geojson`。

    为什么走文件而不是渲染时联网看图：底图渲染必须**离线可复现**，
    而且这个提取过程重（要影像 + 形态学 + 网格统计）。
    提取工具：`tools/extract_trees_from_sat.py`。
    树的大小分三档（属性 size），来自影像里该处树冠的稠密程度。
    """
    p = RAW / f"{campus}_trees.geojson"
    if not p.exists():
        return None
    data = _load_json(p)
    pts = []
    for f in data.get("features", []):
        lon, lat = f["geometry"]["coordinates"]
        size = int((f.get("properties") or {}).get("size", 1))
        r = (2.9 + 1.0 * size) * z
        pts.append((*proj.pt(lon, lat), r))
    return pts


def _tree_points_full(proj, vec, z, size, campus: str | None = None):
    """行道树 + 成片林地撒点（林地撒点需要画布尺寸做掩膜）。

    若该校区有**卫星影像提取的树**（`<campus>_trees.geojson`），就**以它为准**
    （用影像里的真实树冠分布），不再叠加 OSM 的 wood/tree_row ——
    否则同一片林子会被画两遍、密度翻倍。
    """
    from PIL import Image, ImageDraw

    if campus:
        sat = _tree_points_sat(campus, proj, z)
        if sat is not None:
            return sat

    pts = _tree_points(proj, vec, z)
    areas = []
    for f in vec.get("features", []):
        pr = f.get("properties") or {}
        if pr.get("natural") in ("wood", "scrub") or pr.get("landuse") == "forest":
            areas += _area_rings(f.get("geometry") or {}, pr)
    if not areas:
        return pts

    mask = Image.new("1", size, 0)
    md = ImageDraw.Draw(mask)
    for ring in areas:
        p = [proj.pt(lo, la) for lo, la in ring]
        if len(p) >= 3:
            md.polygon(p, fill=1)
    m = mask.load()
    step = max(7.0, TREE_STEP_Z * z)
    for iy in range(int(size[1] / step) + 2):
        for ix in range(int(size[0] / step) + 2):
            x = (ix + _hash01(ix, iy, 1.0)) * step
            y = (iy + _hash01(ix, iy, 2.0)) * step
            if not (0 <= x < size[0] and 0 <= y < size[1]):
                continue
            if m[int(x), int(y)] and _hash01(ix, iy, 3.0) < TREE_DENSITY:
                pts.append((x, y, (2.6 + 1.6 * _hash01(ix, iy, 4.0)) * z))
    return pts


def _visible_face(n) -> bool:
    """这个朝向的墙看不看得见。相机在右下方 ⇒ 法向朝右下（= −VIEW 方向）的墙可见。"""
    return n[0] * (-VIEW[0]) + n[1] * (-VIEW[1]) > 0.02


def _draw_building(d, it, z, floor_px):
    """画一栋楼：墙面（含底遮蔽 + 楼层线）-> 屋顶（含女儿墙）-> 屋顶家具。"""
    pts, dz = it["ring"], it["dz"]
    roof_c, top_c, side_c, edge_c = it["pal"]
    ox, oy = VIEW[0] * dz, VIEW[1] * dz
    cx = sum(p[0] for p in pts) / len(pts)
    cy = sum(p[1] for p in pts) / len(pts)

    # ① 墙面 + ② 楼层线 + ③ 底部遮蔽
    n_floors = int(round(dz / floor_px)) if floor_px > 0 else 0
    for i in range(len(pts)):
        p, q = pts[i], pts[(i + 1) % len(pts)]
        n = _outward_normal(p, q, cx, cy)
        lam = max(0.0, -(n[0] * LIGHT[0] + n[1] * LIGHT[1]))
        col = tuple(int(side_c[k] + (top_c[k] - side_c[k]) * lam) for k in range(3))
        d.polygon([p, q, (q[0] + ox, q[1] + oy), (p[0] + ox, p[1] + oy)], fill=col)
        if not _visible_face(n):
            continue                      # 背面的墙会被屋顶/自身挡住，不浪费笔画
        # 底部环境光遮蔽：贴地一圈暗带
        ao = min(3.2, dz * 0.20)
        if ao >= 0.8:
            d.polygon([p, q, (q[0] + VIEW[0] * ao, q[1] + VIEW[1] * ao),
                       (p[0] + VIEW[0] * ao, p[1] + VIEW[1] * ao)],
                      fill=_shade(col, 0.72) + (AO_ALPHA,))
        # 楼层线：有几层画几条，远看就是窗带
        if 2 <= n_floors <= 14 and dz > 8:
            line_c = _shade(col, 0.86) + (FLOOR_LINE_ALPHA,)
            for k in range(1, n_floors):
                t = k / n_floors
                ax, ay = p[0] + ox * t, p[1] + oy * t
                bx, by = q[0] + ox * t, q[1] + oy * t
                d.line([(ax, ay), (bx, by)], fill=line_c, width=max(1, int(round(0.55 * z))))

    # ④ 屋顶 + 女儿墙
    roof = [(x + ox, y + oy) for x, y in pts]
    d.polygon(roof, fill=roof_c, outline=edge_c, width=max(1, int(round(0.7 * z))))
    xs = [p[0] for p in roof]
    ys = [p[1] for p in roof]
    if min(max(xs) - min(xs), max(ys) - min(ys)) > ROOF_MIN_PX:
        mx = sum(xs) / len(xs)
        my = sum(ys) / len(ys)
        inset = [(mx + (x - mx) * 0.88, my + (y - my) * 0.88) for x, y in roof]
        d.polygon(inset, fill=_shade(roof_c, 1.05), outline=_shade(edge_c, 1.06),
                  width=max(1, int(round(0.5 * z))))

    # ⑤ 屋顶家具：小方块（水箱/机房），也按轴测挤出，带同方向的墙面明暗
    for rect, fdz in it["furn"]:
        fo = (VIEW[0] * fdz, VIEW[1] * fdz)
        fx = sum(p[0] for p in rect) / 4
        fy = sum(p[1] for p in rect) / 4
        for i in range(4):
            p, q = rect[i], rect[(i + 1) % 4]
            n = _outward_normal(p, q, fx, fy)
            if not _visible_face(n):
                continue
            lam = max(0.0, -(n[0] * LIGHT[0] + n[1] * LIGHT[1]))
            col = tuple(int(side_c[k] + (top_c[k] - side_c[k]) * lam * 0.8) for k in range(3))
            d.polygon([p, q, (q[0] + fo[0], q[1] + fo[1]), (p[0] + fo[0], p[1] + fo[1])],
                      fill=col)
        d.polygon([(x + fo[0], y + fo[1]) for x, y in rect],
                  fill=_shade(roof_c, 0.94), outline=edge_c)


def _draw_scene(img, d, proj, blds, vec, px_per_m, z, *, shadows=True, trees=True,
                campus: str | None = None, skip=None):
    """把**建筑（轴测挤出 + 细节）与树（圆簇）**按同一套"远近"顺序画出来。

    树和建筑必须一起排序：树要能挡住后面的楼，楼也要能挡住后面的树。

    `skip` = 不画的建筑索引集合（校外挡视线的南侧高楼，见 `_skipped_indexes`）。
    在这里排除，**投影也一并排除** —— 因为投影是由同一份 `items` 生成的，
    否则会出现"楼没了、影子还在"。
    """
    from PIL import Image, ImageDraw, ImageFilter

    skip = skip or set()
    items = []          # dict: kind / ring|center / dz|r / key
    for idx, feat in enumerate(blds.get("features", [])):
        if idx in skip:
            continue
        props = feat.get("properties") or {}
        dz = _height_m(props, idx) * px_per_m * EXAG
        pal = _palette(props, idx, z, campus)
        for ring in _rings_of(feat.get("geometry") or {}):
            pts = [proj.pt(lo, la) for lo, la in ring]
            if len(pts) < 3:
                continue
            items.append({"kind": "bld", "ring": pts, "dz": dz, "pal": pal,
                          "furn": _roof_furniture(pts, idx, px_per_m, EXAG),
                          "key": max(p[1] for p in pts)})
    if trees:
        for (x, y, r) in _tree_points_full(proj, vec, z, (img.width, img.height),
                                           campus=campus):
            items.append({"kind": "tree", "c": (x, y), "r": r, "key": y + r})
    items.sort(key=lambda t: t["key"])       # 图像 y 小的（北/远）先画

    # 1) 投影层：**只给建筑**做投影（树不投影）。
    #
    # 为什么树不投影（2026-10-03 实测）：树的影子要进这层模糊，而模糊出来的
    # 是**连续 alpha 渐变**，会把 PNG 的可压缩性彻底毁掉 ——
    # 同一张仙林：树带影子 PNG 892 KB，树不带影子 PNG 355 KB（体积差 2.5 倍），
    # 而 r≈5 px 的树影在图上几乎看不见。底图要 base64 内嵌进 HTML，
    # 所以这里明确取舍：**宁可少一层几乎看不见的影子**。
    if shadows:
        sh = Image.new("L", (img.width, img.height), 0)
        sd = ImageDraw.Draw(sh)
        for it in items:
            if it["kind"] != "bld":
                continue
            pts = it["ring"]
            ox, oy = shadow_offset(it["dz"], z)
            # ⚠ **必须从楼基"扫掠"出去**（楼基 + 远端 + 每条边的连接带三者并集）。
            #   只画"整体平移后的足迹"是错的：楼底面和影子之间会空出一段，
            #   整片楼看起来**像浮在天上**（2026-10-03 用户一眼看出来的就是这个）。
            #   真实投影一定是从楼脚连着延伸出去的。
            sd.polygon(pts, fill=255)
            sd.polygon([(x + ox, y + oy) for x, y in pts], fill=255)
            for i in range(len(pts)):
                p, q = pts[i], pts[(i + 1) % len(pts)]
                sd.polygon([p, q, (q[0] + ox, q[1] + oy), (p[0] + ox, p[1] + oy)], fill=255)
        sh = sh.filter(ImageFilter.GaussianBlur(SHADOW_BLUR))
        sh = sh.point(lambda v: int(v * SHADOW_ALPHA))
        layer = Image.new("RGBA", img.size, (58, 52, 44, 0))
        layer.putalpha(sh)
        img.paste(Image.alpha_composite(img.convert("RGBA"), layer).convert("RGB"), (0, 0))
        d = ImageDraw.Draw(img, "RGBA")

    # 2) 逐个画
    floor_px = METRES_PER_FLOOR * px_per_m * EXAG
    for it in items:
        if it["kind"] == "tree":
            x, y = it["c"]
            r = it["r"]
            cy = y - r * 0.55
            d.ellipse([x - r, cy - r * 0.92, x + r, cy + r * 0.92], fill=TREE_DARK)
            if TREE_LAYERS >= 2:
                d.ellipse([x - r * 0.80, cy - r * 0.90, x + r * 0.62, cy + r * 0.06],
                          fill=TREE_MID)
            if TREE_LAYERS >= 3:
                d.ellipse([x - r * 0.48, cy - r * 0.84, x + r * 0.30, cy - r * 0.02],
                          fill=TREE_LIGHT)
            continue
        _draw_building(d, it, z, floor_px)
    return img, d


def _draw_buildings(img, d, proj, blds, px_per_m, z, *, shadows=True, order_key=None,
                    vec=None, trees=False):
    """兼容旧签名：只画建筑（`_draw_scene` 的前身，保留给只想画楼的调用方）。"""
    return _draw_scene(img, d, proj, blds, vec or {"features": []}, px_per_m, z,
                       shadows=shadows, trees=trees)


def render_frame_image(campus: str, s: float, w: float, n: float, e: float, *,
                       width: int = 2000, exag: float | None = None,
                       shadows: bool = True) -> "object":
    """画一张**立体**底图，返回与瓦片/平面两条路径**同口径**的 FrameImage。"""
    global EXAG
    old_exag = EXAG
    if exag is not None:
        EXAG = float(exag)
    try:
        return _render(campus, s, w, n, e, width=width, shadows=shadows)
    finally:
        EXAG = old_exag


def _project(s, w, n, e, width):
    """外框 + 目标宽度 → 投影与换算参数。

    ⚠ **`_render()` 与 `roof_shapes()` 必须共用这一份**：吸附用的几何要和
    实际画出来的那张图**严格同源**，否则"差一点点"在点选上看不出来、但会一直错。
    取整与换算口径与 `vector_basemap` / `static_basemap` 完全一致。
    """
    import static_basemap as sb

    ox = round(sb._lon_to_px(w, ZOOM))
    oy = round(sb._lat_to_px(n, ZOOM))
    span_x = max(1, round(sb._lon_to_px(e, ZOOM)) - ox)
    span_y = max(1, round(sb._lat_to_px(s, ZOOM)) - oy)
    W = int(width)
    scale_x = W / span_x
    H = max(1, int(round(span_y * scale_x)))
    scale_y = H / span_y
    lat_n = sb._px_to_lat(oy, ZOOM)
    lat_s = sb._px_to_lat(oy + span_y, ZOOM)
    lon_w = sb._px_to_lon(ox, ZOOM)
    lon_e = sb._px_to_lon(ox + span_x, ZOOM)
    proj = Projector(lat_s, lon_w, lat_n, lon_e, W, H)
    ppm_x, ppm_y = _px_per_m(proj, (lat_n + lat_s) / 2.0)
    return {"proj": proj, "z": W / 1400.0, "W": W, "H": H,
            "ppm": (ppm_x + ppm_y) / 2.0,
            "ox": ox, "oy": oy, "scale_x": scale_x, "scale_y": scale_y}


def _render(campus, s, w, n, e, *, width, shadows):
    import static_basemap as sb
    from PIL import Image, ImageDraw

    vec = _load_json(RAW / f"vector_{campus}.geojson")
    blds = _load_json(RAW / f"{campus}_buildings.geojson")
    try:
        bounds = _load_json(RAW / "campus_boundaries.json").get(campus, {})
    except Exception:
        bounds = {}

    P = _project(s, w, n, e, width)
    proj, z, W, H, ppm = P["proj"], P["z"], P["W"], P["H"], P["ppm"]
    skip = _skipped_indexes(blds, bounds)

    img = Image.new("RGB", (W, H), C["bg"])
    d = ImageDraw.Draw(img, "RGBA")
    vb.draw_ground_layers(d, proj, vec, bounds, z)      # 地面：平面色块，不动
    img, d = _draw_scene(img, d, proj, blds, vec, ppm, z,
                         shadows=shadows, trees=True, campus=campus, skip=skip)
    if DRAW_CAMPUS_BOUNDARY:                            # 校园范围蓝色虚线
        _draw_campus_boundary(d, proj, bounds, z)

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return sb.FrameImage(png=buf.getvalue(), width=W, height=H, zoom=ZOOM,
                         scale_x=P["scale_x"], scale_y=P["scale_y"],
                         origin_x=P["ox"], origin_y=P["oy"],
                         south=s, west=w, north=n, east=e)


def cached_relief_frame(campus: str, s, w, n, e, *, width: int = 2000) -> "object":
    """带本地缓存的立体底图（PNG/JPEG 自动挑更小的那个）。"""
    import static_basemap as sb

    stem = IMAGE_CACHE / f"{campus}_relief_w{width}"
    path = sb.find_cached(stem)
    if path is not None:
        try:
            meta = sb.read_meta(path)
            style = meta.pop("_style", None)
            if style == _style_tag() and sb.frame_matches(meta, s, w, n, e):
                return sb.FrameImage(png=path.read_bytes(), **meta)
        except Exception:
            pass
    fi = render_frame_image(campus, s, w, n, e, width=width)
    fields = {k: getattr(fi, k) for k in (
        "width", "height", "zoom", "scale_x", "scale_y", "origin_x", "origin_y",
        "south", "west", "north", "east")}
    path, data = sb.write_best_image(stem, fi.png)
    # ⚠ `_style` 只进落盘 meta，**绝不能**混进 FrameImage 的构造参数
    #   （2026-10-03 的事故：混进去 => TypeError => 被静默退回瓦片）
    sb.write_meta(path, {**fields, "_style": _style_tag()})
    return sb.FrameImage(png=data, **fields)


def _trees_fingerprint() -> str:
    """树数据指纹：**树变了底图必须重画**。

    ⚠ 2026-10-04 补的洞：`_style_tag()` 原来只看风格常量，不看树数据。
    于是重新提取了 `data/raw/<campus>_trees.geojson` 之后，缓存判定"风格没变"
    直接返回旧图 —— 底图上还是**旧树**，而你会以为提取没生效（和老坑"缓存陈旧"
    同一个性质：文件对了，看到的是旧的）。
    """
    h = hashlib.md5()
    try:
        for p in sorted(RAW.glob("*_trees.geojson")):
            st = p.stat()
            h.update(f"{p.name}:{st.st_size}:{int(st.st_mtime)}".encode("utf-8"))
    except OSError:
        pass
    return h.hexdigest()[:8]


def _style_tag() -> str:
    """风格指纹：参数一改，缓存自动失效（免得看到的是旧图还以为没生效）。

    ⚠ 新增任何影响画面的常量（尤其 `WALL_SIDE_BY_CAMPUS` 这种表），
    **必须**接进这里。老坑：色板改了但指纹没变 ⇒ 返回旧缓存图 ⇒ 以为没生效。
    树数据同理，走 `_trees_fingerprint()`。
    """
    raw = (f"{VIEW}|{LIGHT}|{SHADOW}|{SHADOW_ALPHA}|{SHADOW_BLUR}|{EXAG}|"
           f"{WALL_TOP}|{WALL_SIDE}|{ROOF}|{sorted(WALL_SIDE_BY_CAMPUS.items())}|"
           f"{DRAW_CAMPUS_BOUNDARY}|{BOUNDARY_COLOR}|{BOUNDARY_WIDTH}|"
           f"{BOUNDARY_DASH}|{BOUNDARY_GAP}|{SKIP_OUTSOUTH_H}|"
           f"{_trees_fingerprint()}")
    return hashlib.md5(raw.encode("utf-8")).hexdigest()[:10]


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from frame_util import frame_of

    keys = sys.argv[1:] or ["gulou", "xianlin"]
    out_dir = Path(__file__).resolve().parent / "data" / "tile_check"
    out_dir.mkdir(parents=True, exist_ok=True)
    for key in keys:
        s, w, n, e = frame_of(key)
        fi = render_frame_image(key, s, w, n, e, width=1400)
        out = out_dir / f"relief_{key}.png"
        out.write_bytes(fi.png)
        print(f"{key}: {fi.width}x{fi.height} {len(fi.png)//1024}KB -> {out}")
