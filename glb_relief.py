# -*- coding: utf-8 -*-
"""苏州校区底图：把 **真三维 GLB 模型**按**轴测（平行）投影**渲染成静态底图。

和鼓楼/仙林的区别
------------------
鼓楼/仙林只有 OSM 足迹，做法是"足迹 + 挤出高度"（`relief_basemap.py`）。
苏州有**完整三维模型**（建筑/山体/水面/道路/树都建了模），所以直接把三角面按
平行投影画出来 —— 立体感是模型自带的，墙面明暗按**真实法向**算，不是猜的。

为什么用平行投影（关键）
------------------------
透视投影下"屏幕像素 ↔ 经纬度"不是线性关系，点选就废了。
平行（轴测）投影是**仿射**的：

    img = 地面仿射映射 + 高度向量 × 高度

高度只作用在建筑自身，**地面平面到图像的映射仍是仿射** ⇒
`static_basemap` 那套 `像素 ↔ 经纬度` 换算原样成立，返回的仍是一个 `FrameImage`。

坐标系
------
模型是**局部米制**：X 向东、Y 向北（GLB 里 Y 是高度、Z = −Y_north）。
到 WGS84 靠 `data/suzhou_georef.json`（`tools/register_suzhou.py` 用卫星影像解出来的）。
"""
from __future__ import annotations

import hashlib
import io
import json
import math
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
RAW = HERE / "data" / "raw"
IMAGE_CACHE = HERE / "data" / "frame_images"
GEOREF = HERE / "data" / "suzhou_georef.json"

# 轴测参数：与 relief_basemap 保持一致，三个校区看起来才是"一套图"
VIEW_H = (-0.34, -1.0)          # 高度 -> 图像位移（单位：高度像素）
# 光的处理：**屋顶与墙面分开判**（一条斜光做不到"屋顶最亮 + 墙面有朝向差"两件事）
#   墙面：只看**水平**光向，明暗系数在 WALL_MIN~WALL_MAX 之间 —— 这个差是立体感的本体
#   屋顶：朝天的面按 ROOF_F 给亮（屋脊/斜屋面按竖直程度平滑过渡）
LIGHT_H = np.array([-0.53, 0.85])       # 水平光向（东、北），单位向量
# ⚠ 下限别压太低：压到 0.66 时西区那几片"宿舍/科研"楼（几何本来就密）会糊成一团深灰，
#   整张图立刻变重、也和鼓楼/仙林那种轻快的调子不像一套。0.74~0.96 刚好。
WALL_MIN, WALL_MAX = 0.74, 0.96
ROOF_F = 0.99
AMBIENT = 0.60                          # 兼容旧字段（现在由上面三个常数决定明暗）
EXAG = 1.0                              # 高度夸张系数
MIN_AREA_PX = 2.4                       # 小于这个面积的三角面不画（见下）

# 图层配色（沿用 vector_basemap 的暖色浅底 + 蓝绿水系）
PAL = {
    "Site base / base":        (229, 222, 208),
    "Landscape / green":       (203, 219, 183),
    "Landscape / lawn":        (211, 226, 192),
    "Landscape / paving":      (233, 227, 214),
    "Landscape / road":        (248, 246, 241),
    "Landscape / water":       (158, 200, 226),
    "运动场 / pitch":           (196, 216, 176),
    "运动场 / sport":           (206, 191, 167),
    "运动场 / line":            (252, 252, 250),
    "庄里山 / hill":            (176, 199, 156),
    "庄里山 / trail":           (228, 216, 198),
    # 建筑：墙/顶各材质 —— 体积感靠"**屋顶最亮 + 墙面因朝向分明暗**"，
    # 与 relief_basemap 同一套逻辑（参考图里屋顶就是最亮的一层）
    "light-stone":  (222, 214, 200),
    "west-brick":   (198, 187, 173),
    "east-brick":   (201, 190, 176),
    "stone":        (216, 208, 195),
    "tile":         (186, 172, 154),
    "roof":         (240, 235, 224),
    "glass":        (150, 174, 190),
    "red-frame":    (178, 158, 146),
    "paving":       (230, 224, 210),
    # 树
    "tree-a":       (152, 181, 132),
    "tree-b":       (167, 194, 145),
    "tree-c":       (180, 203, 153),
    "trunk":        (150, 132, 112),
}
# 默认色（没登记的图层）
DEFAULT_COL = (222, 216, 203)

# 画在地面、之后不再被建筑遮挡的图层（按顺序）
GROUND_ORDER = [
    "Site base / base",
    "Landscape / lawn",
    "Landscape / green",
    "运动场 / pitch",
    "运动场 / sport",
    "运动场 / line",
    "Landscape / paving",
    "Landscape / road",
    "Landscape / water",
]
# 山体（有高度，但要先于建筑画）
TERRAIN_3D = ["庄里山 / hill", "庄里山 / trail"]
# 树（与建筑一起按远近排序）
TREE_GROUPS = ["Trees / tree-a", "Trees / tree-b", "Trees / tree-c", "Trees / trunk"]
# 建筑细节（窗带）：地图尺度下是噪点，默认不画
DETAIL_SKIP = ("glass", "red-frame")


# 建筑细节（与 relief_basemap 同一套做法，只是这里的几何来自真三维模型）
#
# 用户反馈（2026-10-03）：「屋子都是纯立方体，有点没意思，也不够美观」。
# 苏州的模型本身是真几何，但都是**平屋顶现代楼**，成片看就是一片米色方块。
# 所以两件事：
#   ① 按**建筑组团名**给不同色调（模型 building_type 全是 other，只有名字有信息）；
#   ② 在 935 个**真实屋顶**上放屋顶家具（水箱/机房/AHU），用总平面里真实的层高。
# 数据来源 `data/raw/suzhou_plan_buildings.geojson`（含 height / num_floors）。

# 组团名关键字 -> RGB 偏移。
# ⚠ 幅度要**克制**：第一版给了 ±14，结果整片楼变成橙的/蓝的，把原来清淡的调子毁了。
#   这里只用来"区分功能"，不靠它做美观 —— 体积感交给明暗对比（AMBIENT + 墙/顶差）。
NAME_TINT: list[tuple[tuple[str, ...], tuple[int, int, int]]] = [
    (("图书馆", "大礼堂", "北大楼"), (4, -1, -5)),
    (("学生书院", "学生生活"),      (7, -3, -9)),
    (("科研", "科创"),              (-5, 2, 6)),
    (("食堂",),                     (6, -3, -8)),
    (("文体", "体育", "运动"),       (-4, 1, 5)),
    (("园林",),                     (-3, 3, -2)),
    (("校门",),                     (3, -1, -4)),
]
FURN_MIN_ROOF_M = 16.0          # 屋顶小于这个尺寸（米）就不放家具
FURN_ALPHA = 255


def _is_building_group(name: str) -> bool:
    """这个图层是不是"建筑"（地面层/山体/树/场地底板都不算）。"""
    if name in GROUND_ORDER or name in TERRAIN_3D:
        return False
    if name.startswith("Trees /") or name.startswith("Site base"):
        return False
    return True


def _draw_shadows(img, plan_features, geo, proj, ppm, d, *, exag: float = EXAG):
    """建筑投影：用总平面 935 个足迹 + **真实层高**，从楼基**扫掠**出去。

    参数与 `relief_basemap` 共用同一组常数（SHADOW / SHADOW_ALPHA / SHADOW_BLUR），
    这样三个校区的影子是同一个太阳、同一个观感。
    """
    from PIL import Image, ImageDraw as _ID, ImageFilter

    from relief_basemap import SHADOW_ALPHA, SHADOW_BLUR, shadow_offset

    sh = Image.new("L", (img.width, img.height), 0)
    sd = _ID.Draw(sh)
    z = img.width / 1400.0
    for f in plan_features:
        props = f.get("properties") or {}
        try:
            hm = float(props.get("height") or 0.0)
        except Exception:
            hm = 0.0
        if hm <= 0.2:
            continue
        ox, oy = shadow_offset(hm * ppm * exag, z)
        for ring in f["geometry"]["coordinates"]:
            pts = []
            for X, Y in ring:
                lon, lat = geo.to_lonlat(X, Y)
                pts.append(proj.pt(float(lon), float(lat)))
            if len(pts) < 3:
                continue
            sd.polygon(pts, fill=255)                                   # 楼基
            sd.polygon([(x + ox, y + oy) for x, y in pts], fill=255)     # 影子远端
            for i in range(len(pts)):                                   # 中间扫掠带
                p, q = pts[i], pts[(i + 1) % len(pts)]
                sd.polygon([p, q, (q[0] + ox, q[1] + oy), (p[0] + ox, p[1] + oy)],
                           fill=255)
    sh = sh.filter(ImageFilter.GaussianBlur(SHADOW_BLUR))
    sh = sh.point(lambda v: int(v * SHADOW_ALPHA))
    layer = Image.new("RGBA", img.size, (58, 52, 44, 0))
    layer.putalpha(sh)
    img.paste(Image.alpha_composite(img.convert("RGBA"), layer).convert("RGB"), (0, 0))


def _group_tint(name: str) -> tuple[int, int, int]:
    for keys, off in NAME_TINT:
        if any(k in name for k in keys):
            return off
    return (0, 0, 0)


def _tinted(color, off):
    return tuple(max(0, min(255, int(c + o))) for c, o in zip(color, off))


def _roof_furniture_local(props, seed):
    """给一个建筑足迹生成屋顶家具（局部米制）：[(四点矩形, 高度米), ...]。"""
    hm = props.get("height") or 0.0
    try:
        hm = float(hm)
    except Exception:
        hm = 0.0
    ring = props["_ring"]
    xs = [p[0] for p in ring]
    ys = [p[1] for p in ring]
    w, h = max(xs) - min(xs), max(ys) - min(ys)
    if min(w, h) < FURN_MIN_ROOF_M:
        return []
    out = []
    for i in range(1 + int(_h01(seed, 21.0) * 3.0)):
        for att in range(6):
            fx = 0.22 + 0.56 * _h01(seed, 30.0 + i * 7 + att)
            fy = 0.22 + 0.56 * _h01(seed, 40.0 + i * 7 + att)
            px, py = min(xs) + fx * w, min(ys) + fy * h
            if not _in_ring(px, py, ring):
                continue
            bw = 4.0 + 6.0 * _h01(seed, 50.0 + i)
            bh = 3.5 + 5.0 * _h01(seed, 60.0 + i)
            fh = 2.0 + 2.8 * _h01(seed, 70.0 + i)
            out.append(([(px - bw / 2, py - bh / 2), (px + bw / 2, py - bh / 2),
                         (px + bw / 2, py + bh / 2), (px - bw / 2, py + bh / 2)], fh, hm))
            break
    return out


def _h01(*nums) -> float:
    h = hashlib.md5("|".join(f"{n:.3f}" for n in nums).encode("utf-8")).digest()
    return int.from_bytes(h[:4], "big") / 2 ** 32


def _in_ring(x, y, ring) -> bool:
    inside = False
    n = len(ring)
    for i in range(n):
        x1, y1 = ring[i]
        x2, y2 = ring[(i + 1) % n]
        if (y1 > y) != (y2 > y) and y2 != y1:
            if x < (x2 - x1) * (y - y1) / (y2 - y1) + x1:
                inside = not inside
    return inside


def _color_of(group: str):
    if group in PAL:
        return PAL[group]
    part = group.split("/")[-1].strip()
    return PAL.get(part, DEFAULT_COL)


class Geo:
    """局部米制 (X 东, Y 北, H 上) <-> WGS84 的双向换算。

    ⚠ 实现在 `suzhou_georef.py`（配准参数的唯一入口，与人工校准台同一套字段口径）。
    这里保留别名是为了别处 `from glb_relief import Geo` 不炸。
    """

    def __new__(cls, georef=None):
        import suzhou_georef
        return suzhou_georef.Geo(georef)


def _px_per_m(proj, lat):
    x0 = proj.pt(120.0, lat)[0]
    x1 = proj.pt(120.001, lat)[0]
    m_per_deg = 111320.0 * math.cos(math.radians(lat))
    return abs(x1 - x0) / (0.001 * m_per_deg)


def roof_shapes(campus: str, s, w, n, e, *, width: int = 2000) -> dict:
    """苏州的**屋顶多边形**（原图像素），供 `frame_picker` 做"点屋顶 → 贴回足迹"。

    ⚠ 苏州和前两个校区**机制不同**，别照抄 `relief_basemap.roof_shapes`：
    这里的楼是 GLB 模型的三角面**真三维渲染**，不是"足迹挤出"。但吸附只需要
    "屋顶在图上哪儿"，而：

      · 轴测是**平行投影** ⇒ 平屋顶 = 底面 + VIEW_H × 高度像素（严格成立）；
      · 总平面 `suzhou_plan_buildings.geojson` 有 **935 个真实足迹 + 真实 height**
        （例：r0001 是 29.88 m / 9 层），文档记的配准结果也确认它与模型"逐米吻合"。

    所以用"足迹 + height"重建屋顶是可靠的（**坡屋顶会略有偏差**，可接受 ——
    吸附本来就是"贴回楼里"的近似，不是测量）。

    坐标约定（**查证过，别改**）：总平面 ring 的点是**局部米制 (东, 北)**，
    直接 `geo.to_lonlat(X, Y)` —— 与 `_draw_shadows()` 里的用法一致。
    """
    import json

    import static_basemap as sb
    import suzhou_georef
    import vector_basemap as vb

    geo = suzhou_georef.Geo(suzhou_georef.load(GEOREF))

    ZOOM = 16
    ox = round(sb._lon_to_px(w, ZOOM))
    oy = round(sb._lat_to_px(n, ZOOM))
    span_x = max(1, round(sb._lon_to_px(e, ZOOM)) - ox)
    span_y = max(1, round(sb._lat_to_px(s, ZOOM)) - oy)
    W = int(width)
    scale_x = W / span_x
    H = max(1, int(round(span_y * scale_x)))
    lat_n = sb._px_to_lat(oy, ZOOM)
    lat_s = sb._px_to_lat(oy + span_y, ZOOM)
    lon_w = sb._px_to_lon(ox, ZOOM)
    lon_e = sb._px_to_lon(ox + span_x, ZOOM)
    proj = vb.Projector(lat_s, lon_w, lat_n, lon_e, W, H)
    ppm = _px_per_m(proj, (lat_n + lat_s) / 2.0)

    try:
        plan = json.loads((RAW / "suzhou_plan_buildings.geojson")
                          .read_text(encoding="utf-8"))["features"]
    except Exception:
        return {"v": list(VIEW_H), "b": []}

    items = []
    for f in plan:
        props = f.get("properties") or {}
        try:
            hm = float(props.get("height") or 0.0)
        except Exception:
            hm = 0.0
        if hm <= 0.2:
            continue
        dz = hm * ppm * EXAG
        if dz < 1.0:
            continue
        for ring in f["geometry"]["coordinates"]:
            pts = []
            for X, Y in ring:
                lon, lat = geo.to_lonlat(X, Y)
                pts.append(proj.pt(float(lon), float(lat)))
            if len(pts) >= 3:
                items.append((max(p[1] for p in pts), dz, pts))
    items.sort(key=lambda t: t[0])          # 远（北）→ 近（南），与绘制同向

    out = []
    for _, dz, pts in items:
        flat = []
        for x, y in pts:
            flat.append(round(x + VIEW_H[0] * dz, 1))
            flat.append(round(y + VIEW_H[1] * dz, 1))
        out.append([round(dz, 1), flat])
    return {"v": [VIEW_H[0], VIEW_H[1]], "b": out}


def _sort_key(V, F):
    """画家算法排序键：横跨整个图层的三角面用平均 y（图像坐标，小 = 北 = 远）。"""
    return V[F][:, :, 1].mean(axis=1)


def _render(campus: str, s: float, w: float, n: float, e: float, *, width: int,
            skip_detail: bool = True, min_area_px: float | None = None,
            shadows: bool = True):
    import static_basemap as sb
    from PIL import Image, ImageDraw

    import suzhou_georef
    geo = suzhou_georef.Geo(suzhou_georef.load(GEOREF))
    npz = np.load(RAW / "suzhou_glb.npz")

    # ---- 与瓦片/自绘两条路径完全一样的取整与换算口径
    ZOOM = 16
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

    import vector_basemap as vb
    proj = vb.Projector(lat_s, lon_w, lat_n, lon_e, W, H)
    ppm = _px_per_m(proj, (lat_n + lat_s) / 2.0)

    img = Image.new("RGB", (W, H), vb.C["bg"])
    d = ImageDraw.Draw(img, "RGBA")

    def project(V):
        """局部米制顶点 (N,3) -> 屏幕像素 (N,2)（轴测：地面仿射 + 高度位移）。"""
        E = V[:, 0]
        N = V[:, 2] * -1.0            # GLB 的 Z 指向南 ⇒ 北 = -Z
        Up = V[:, 1]
        lon, lat = geo.to_lonlat(E, N)
        xs = np.empty(len(V)); ys = np.empty(len(V))
        for i in range(len(V)):
            xs[i], ys[i] = proj.pt(lon[i], lat[i])
        k = Up * ppm * EXAG
        return np.stack([xs + VIEW_H[0] * k, ys + VIEW_H[1] * k], axis=1), \
               np.stack([xs, ys], axis=1)

    def group_tris(name):
        vk, fk = f"{name}_v", f"{name}_f"
        if vk not in npz:
            return None
        V = npz[vk].astype(np.float64)
        F = npz[fk].astype(np.int64)
        return V, F

    def shade(V, F, base):
        """按三角面**真实法向**着色（这是"立体感"的来源）。

        屋顶与墙面**分开判**：
          · 近乎水平的面（屋顶/雨棚）—— 朝天，给最亮
          · 近乎竖直的面（墙）—— 只按**水平光向**分深浅，朝向差就是立体感
          · 中间（坡屋顶/檐口）—— 平滑过渡
        之前用一条斜光算到底，结果"南墙比屋顶亮、背光墙又闷又土"。
        """
        a, b, c = V[F[:, 0]], V[F[:, 1]], V[F[:, 2]]
        nrm = np.cross(b - a, c - a)
        ln = np.linalg.norm(nrm, axis=1, keepdims=True)
        ln[ln == 0] = 1.0
        nrm = nrm / ln
        # 局部系 -> 世界系：X 东、Z 是"南"（转成北 = -Z）、Y 上
        nw = np.stack([nrm[:, 0], -nrm[:, 2], nrm[:, 1]], axis=1)
        up = np.abs(nw[:, 2])
        lam_h = np.clip(nw[:, 0] * LIGHT_H[0] + nw[:, 1] * LIGHT_H[1], 0.0, 1.0)
        f_wall = WALL_MIN + (WALL_MAX - WALL_MIN) * lam_h
        t = np.clip((up - 0.30) / 0.45, 0.0, 1.0)
        f = f_wall * (1.0 - t) + ROOF_F * t
        return [tuple(int(min(255, max(0, round(ch * fi)))) for ch in base) for fi in f]

    def draw_group(name, *, layer: str):
        g = group_tris(name)
        if g is None:
            return 0
        V, F = g
        if layer == "flat":
            V = V.copy(); V[:, 1] = 0.0        # 地面层压平（模型本身也是平的）
        P, _ = project(V)
        # 按**组团名**给色调：宿舍偏砖红、科研偏冷、食堂偏暖……
        # 模型里 building_type 全是 other，只有名字带功能信息（这一步让成片楼不再一个色）
        base = _tinted(_color_of(name), _group_tint(name))
        cols = shade(V, F, base)
        # 面积过滤：地图尺度下看不见的小三角不画（窗带/树叶碎面非常多）
        tri = P[F]
        area = np.abs((tri[:, 1, 0] - tri[:, 0, 0]) * (tri[:, 2, 1] - tri[:, 0, 1]) -
                      (tri[:, 2, 0] - tri[:, 0, 0]) * (tri[:, 1, 1] - tri[:, 0, 1])) * 0.5
        keep = area >= (MIN_AREA_PX if min_area_px is None else min_area_px)
        order = np.argsort(_sort_key(P, F)[keep])[::-1]     # 北（远）先画
        idx = np.nonzero(keep)[0][order]
        for i in idx:
            t = tri[i]
            d.polygon([tuple(t[0]), tuple(t[1]), tuple(t[2])], fill=cols[i])
        # 屋顶家具：跟在这一组后面画（同一组团内部，远近平序误差很小）
        if layer == "3d" and name in furn_by_group:
            _draw_furniture(furn_by_group[name], base, geo, proj, ppm, d)
        return len(idx)

    def _draw_furniture(specs, base, geo_, proj_, ppm_, dr):
        """在屋顶上画小方块（水箱/机房）。用**真实的楼高**把方块抬到屋顶平面。"""
        wall = _tinted(base, (-14, -12, -10))
        top = _tinted(base, (10, 9, 7))
        for rect_m, fh_m, hm in specs:
            pts = []
            for (X, Y) in rect_m:
                lon, lat = geo_.to_lonlat(X, Y)
                pts.append(proj_.pt(float(lon), float(lat)))
            up = (VIEW_H[0] * hm * ppm_ * EXAG, VIEW_H[1] * hm * ppm_ * EXAG)
            fo = (VIEW_H[0] * fh_m * ppm_ * EXAG, VIEW_H[1] * fh_m * ppm_ * EXAG)
            base_pts = [(x + up[0], y + up[1]) for x, y in pts]
            fx = sum(p[0] for p in base_pts) / 4
            fy = sum(p[1] for p in base_pts) / 4
            for i in range(4):
                p, q = base_pts[i], base_pts[(i + 1) % 4]
                dx, dy = q[0] - p[0], q[1] - p[1]
                ln = math.hypot(dx, dy) or 1.0
                nx, ny = dy / ln, -dx / ln
                if (fx - p[0]) * nx + (fy - p[1]) * ny < 0:
                    nx, ny = -nx, -ny
                if nx * (-VIEW_H[0]) + ny * (-VIEW_H[1]) <= 0.02:
                    continue                      # 背面不画
                lam = max(0.0, -(nx * -0.62 + ny * -0.78))
                col = tuple(int(wall[k] + (top[k] - wall[k]) * lam * 0.75) for k in range(3))
                dr.polygon([p, q, (q[0] + fo[0], q[1] + fo[1]),
                            (p[0] + fo[0], p[1] + fo[1])], fill=col)
            dr.polygon([(x + fo[0], y + fo[1]) for x, y in base_pts],
                       fill=top, outline=_tinted(base, (-26, -24, -20)))

    # ---- 屋顶家具 & 投影：都要用总平面的 935 个**真实足迹 + 真实层高**
    plan_features: list = []
    try:
        plan_features = json.loads(
            (RAW / "suzhou_plan_buildings.geojson").read_text(encoding="utf-8"))["features"]
    except Exception:
        plan_features = []

    # 屋顶家具：按"落在哪个组团的包围盒里"归组，跟在该组团后面画。
    # 为什么这么做：模型本身没有屋顶设备，成片平屋顶看着就是一片米色方块；
    # 而总平面里每栋楼都有真实 height/num_floors，在屋顶上摆几个小方块立刻就有信息量。
    furn_by_group: dict[str, list] = {}
    try:
        boxes = {}
        for g in [n[:-2] for n in npz.files if n.endswith("_v") and _is_building_group(n[:-2])]:
            V = npz[f"{g}_v"]
            boxes[g] = (float(V[:, 0].min()), float(V[:, 0].max()),
                        float(V[:, 2].min()), float(V[:, 2].max()))
        for i, f in enumerate(plan_features):
            ring = f["geometry"]["coordinates"][0]
            props = dict(f["properties"])
            props["_ring"] = ring
            cx = sum(p[0] for p in ring) / len(ring)
            gz = -(sum(p[1] for p in ring) / len(ring))       # 北 = -Z
            best, bestd = None, 1e18
            for g, (x0, x1, z0, z1) in boxes.items():
                if x0 - 2 <= cx <= x1 + 2 and z0 - 2 <= gz <= z1 + 2:
                    best = g
                    break
                d2 = max(x0 - cx, 0.0, cx - x1) ** 2 + max(z0 - gz, 0.0, gz - z1) ** 2
                if d2 < bestd:
                    bestd, best = d2, g
            if best:
                furn_by_group.setdefault(best, []).extend(_roof_furniture_local(props, i))
    except Exception:
        furn_by_group = {}

    # ---- 地面层（不参与三维排序）
    for name in GROUND_ORDER:
        draw_group(name, layer="flat")

    # ---- 2) 山体（有高度，但要先于建筑，免得盖住建筑）
    for name in TERRAIN_3D:
        draw_group(name, layer="3d")

    # ---- 2.5) 建筑投影。
    # ⚠ 苏州原来是**完全没有影子**的，三校区摆在一起就露馅（鼓楼/仙林有）。
    #   而且影子必须**从楼基扫掠出去**：只画"整体平移的足迹"会让楼和影子之间空一段，
    #   整片楼像浮在天上（鼓楼犯过这个错，用户一眼就看出来了）。
    if shadows:
        _draw_shadows(img, plan_features, geo, proj, ppm, d)
        d = ImageDraw.Draw(img, "RGBA")

    # ---- 3) 树 + 建筑：一起按远近排序（树能挡住后面的楼，楼也能挡住后面的树）
    objs = []
    for name in list(npz.files):
        if not name.endswith("_v"):
            continue
        g = name[:-2]
        if g in GROUND_ORDER or g in TERRAIN_3D or g.startswith("Trees /"):
            continue
        part = g.split("/")[-1].strip()
        if skip_detail and part in DETAIL_SKIP:
            continue
        objs.append(g)
    for name in TREE_GROUPS:
        if f"{name}_v" in npz:
            objs.append(name)

    # 用一个统一的"远近"排序：按各图层底面的最北点
    keys = []
    for name in objs:
        V, F = group_tris(name)
        keys.append((float(V[:, 2].max()), name))     # Z 越大越靠南 = 越近
    keys.sort()                                       # 远的（北）先画
    drawn = 0
    for _, name in keys:
        drawn += draw_group(name, layer="3d")

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return sb.FrameImage(png=buf.getvalue(), width=W, height=H, zoom=ZOOM,
                         scale_x=scale_x, scale_y=scale_y, origin_x=ox, origin_y=oy,
                         south=s, west=w, north=n, east=e)


def render_frame_image(campus, s, w, n, e, *, width=2000, **kw):
    return _render(campus, s, w, n, e, width=width, **kw)


def _style_tag():
    """风格指纹：风格参数**或配准参数**一变，缓存就失效（否则看到的是旧图还以为没生效）。"""
    try:
        g = GEOREF.read_text(encoding="utf-8")
    except Exception:
        g = ""
    raw = (f"{VIEW_H}|{LIGHT_H.tolist()}|{WALL_MIN}|{WALL_MAX}|{ROOF_F}|{EXAG}"
           f"|{MIN_AREA_PX}|{PAL}|{DETAIL_SKIP}|{NAME_TINT}|{g}")
    return hashlib.md5(raw.encode("utf-8")).hexdigest()[:10]


def _frame_meta(fi, style: str) -> tuple[dict, dict]:
    """拆成 (FrameImage 字段, 落盘 meta)。**必须拆开**：

    `_style` 是我们自己加的缓存指纹，不是 `FrameImage` 的字段。
    2026-10-03 踩过：把它一起 `FrameImage(png=..., **meta)` 传进去 ⇒ TypeError ⇒
    被 `frame_image_for` 的"退回瓦片"兜底**静默吞掉**，页面看起来正常，
    实际给的是一张瓦片图（`_style` 那行就是那次事故的记号）。
    """
    fields = {k: getattr(fi, k) for k in (
        "width", "height", "zoom", "scale_x", "scale_y", "origin_x", "origin_y",
        "south", "west", "north", "east")}
    return fields, {**fields, "_style": style}


def cached_frame_image(campus, s, w, n, e, *, width=2000):
    """带缓存的立体底图（PNG/JPEG 自动挑更小的那个）。风格参数或配准一变就自动重渲。"""
    import static_basemap as sb

    stem = IMAGE_CACHE / f"{campus}_glb_w{width}"
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
    fields, meta = _frame_meta(fi, _style_tag())
    path, data = sb.write_best_image(stem, fi.png)
    sb.write_meta(path, meta)
    return sb.FrameImage(png=data, **fields)


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(HERE))
    from frame_util import frame_of
    key = sys.argv[1] if len(sys.argv) > 1 else "suzhou"
    wpx = int(sys.argv[2]) if len(sys.argv) > 2 else 1400
    s, w, n, e = frame_of(key)
    fi = render_frame_image(key, s, w, n, e, width=wpx)
    out = HERE / "data" / "tile_check" / f"glb_{key}.png"
    out.write_bytes(fi.png)
    print(f"{key}: {fi.width}x{fi.height} {len(fi.png)//1024}KB -> {out}")
