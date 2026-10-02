# -*- coding: utf-8 -*-
"""用 OSM **矢量数据**自绘校园底图（替代瓦片拼接）。

为什么自绘（2026-10-02 用户提议，收益很实在）
---------------------------------------------
底图是静态图。用别人的瓦片 = 被它的分辨率锁死：要 1400px 就只有 1400px，放大必糊。
自绘则**想要多少像素就画多少**，线与字永远锐利；配色、层级、留白全可控。
（顺带绕开了瓦片服务的使用条款问题。）

数据来源（都在仓库里，运行时不联网）
------------------------------------
    data/raw/<campus>_buildings.geojson   建筑轮廓（原有）
    data/raw/vector_<campus>.geojson      道路/水体/绿地（tools/fetch_vector_layers.py 取）
    data/raw/campus_boundaries.json       校园官方边界多边形

⚠️ 坐标口径必须与瓦片路径（static_basemap）**完全一致**，否则点选会错。
   两者都返回 `static_basemap.FrameImage`，且 `origin_*`/`scale_*` 都用
   "取整后的真实裁剪范围"反推。改动这里务必跑 tools/test_static_basemap.py。

绘制层级（从下到上）：
    底色 -> 绿地/场地 -> 水面与河道 -> 校园底色 -> 道路（按等级）-> 建筑
"""
from __future__ import annotations

import io
import json
import math
from pathlib import Path

HERE = Path(__file__).resolve().parent
RAW = HERE / "data" / "raw"
IMAGE_CACHE = HERE / "data" / "frame_images"

# 配色（暖色浅底 + 橙色主干道）。饱和度调高一档：第一版太淡、整张图发灰、
# 绿地与水面几乎看不出来 —— 地图要好读，层次就得拉开。
C = {
    "bg":        (242, 238, 229),
    "green":     (203, 219, 183),
    "pitch":     (196, 216, 176),
    "sand":      (231, 223, 208),
    "commercial": (234, 228, 217),
    "water":     (158, 200, 226),
    "waterway":  (146, 192, 222),
    "campus":    (250, 247, 239),
    "road_main":  (245, 176, 58),
    "road_main_c": (222, 158, 40),
    "road_2nd":  (255, 255, 255),
    "road_2nd_c": (219, 212, 199),
    "road_minor": (255, 255, 255),
    "road_minor_c": (226, 220, 209),
    "path":      (232, 226, 214),
    "building":  (219, 210, 194),
    "building_e": (198, 187, 168),
    "label":     (68, 60, 50),
    "label_halo": (255, 255, 255),
    "accent":    (206, 84, 40),
}

# 道路等级 -> (线宽系数, 填充色键, 描边色键)
ROAD_STYLE = {
    "motorway": (3.6, "road_main", "road_main_c"),
    "trunk": (3.6, "road_main", "road_main_c"),
    "primary": (3.4, "road_main", "road_main_c"),
    "secondary": (2.6, "road_2nd", "road_2nd_c"),
    "tertiary": (2.2, "road_2nd", "road_2nd_c"),
    "residential": (1.7, "road_minor", "road_minor_c"),
    "unclassified": (1.6, "road_minor", "road_minor_c"),
    "service": (1.2, "road_minor", "road_minor_c"),
    "living_street": (1.4, "road_minor", "road_minor_c"),
    "pedestrian": (1.4, "path", None),
    "footway": (0.9, "path", None),
    "path": (0.9, "path", None),
    "cycleway": (1.0, "path", None),
    "steps": (0.8, "path", None),
    "track": (1.0, "path", None),
}
ROAD_ORDER = ["footway", "path", "steps", "cycleway", "track", "service",
              "living_street", "residential", "unclassified", "pedestrian",
              "tertiary", "secondary", "primary", "trunk", "motorway"]

GREEN_LANDUSE = {"grass", "forest", "meadow", "recreation_ground", "village_green",
                 "park", "garden", "cemetery", "orchard", "allotments"}
SAND_LANDUSE = {"sand", "bare_soil", "construction", "brownfield", "landfill"}
COMMERCIAL_LANDUSE = {"commercial", "retail", "industrial", "railway"}

_FONT = [r"C:\Windows\Fonts\msyh.ttc", r"C:\Windows\Fonts\msyhbd.ttc",
         r"C:\Windows\Fonts\simhei.ttf"]


def _font(size: int):
    from PIL import ImageFont
    for p in _FONT:
        if Path(p).exists():
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                pass
    return ImageFont.load_default()


def _load_json(p: Path):
    return json.loads(p.read_text(encoding="utf-8"))


# 绘图用的"世界像素"缩放级。数值本身不影响结果（只是像素单位的基准），
# 但要与 static_basemap 的换算保持一致，所以固定成 16。
ZOOM = 16


class Projector:
    """经纬度 -> 图像像素（Web Mercator，与 static_basemap 同一口径）。"""

    def __init__(self, s, w, n, e, width, height=None):
        self.x0, self.x1 = self._lon(w), self._lon(e)
        self.y0, self.y1 = self._lat(n), self._lat(s)
        self.k = width / (self.x1 - self.x0)
        self.kh = (height / (self.y1 - self.y0)) if height else self.k
        self.height = height or int(round((self.y1 - self.y0) * self.k))

    @staticmethod
    def _lon(lon):
        return (lon + 180.0) / 360.0

    @staticmethod
    def _lat(lat):
        """Web Mercator 归一化 y（与 static_basemap._lat_to_px 同式）。"""
        lat = max(-85.05112878, min(85.05112878, lat))
        p = math.radians(lat)
        return (1.0 - math.log(math.tan(p) + 1.0 / math.cos(p)) / math.pi) / 2.0

    def pt(self, lon, lat):
        return ((self._lon(lon) - self.x0) * self.k, (self._lat(lat) - self.y0) * self.kh)


def _rings_of(geom):
    t, c = geom.get("type"), geom.get("coordinates") or []
    if t == "Polygon":
        return [c[0]] if c else []
    if t == "MultiPolygon":
        return [p[0] for p in c if p]
    return []


def _lines_of(geom):
    t, c = geom.get("type"), geom.get("coordinates") or []
    if t == "LineString":
        return [c]
    if t == "MultiLineString":
        return c
    return []


def render_frame_image(campus: str, s: float, w: float, n: float, e: float, *,
                       width: int = 2000) -> "object":
    """画一张底图并返回 `static_basemap.FrameImage`（与瓦片路径同接口、同坐标口径）。

    坐标口径的关键：绘图范围与 origin/scale 都由**同一组取整后的世界像素**推出，
    与瓦片路径（static_basemap）完全一致 —— 这样点选换算不用区分底图怎么来的。
    """
    import static_basemap as sb
    from PIL import Image, ImageDraw

    vec_path = RAW / f"vector_{campus}.geojson"
    if not vec_path.exists():
        raise FileNotFoundError(
            f"缺 {vec_path.name}，先跑 tools/fetch_vector_layers.py {campus}")
    vec = _load_json(vec_path)
    blds = _load_json(RAW / f"{campus}_buildings.geojson")
    try:
        bounds = _load_json(RAW / "campus_boundaries.json").get(campus, {})
    except Exception:
        bounds = {}

    # 用 static_basemap 的世界像素公式，并把范围取整（对齐到整数像素）
    ox = round(sb._lon_to_px(w, ZOOM))
    oy = round(sb._lat_to_px(n, ZOOM))
    span_x = max(1, round(sb._lon_to_px(e, ZOOM)) - ox)
    span_y = max(1, round(sb._lat_to_px(s, ZOOM)) - oy)

    W = int(width)
    scale_x = W / span_x
    H = max(1, int(round(span_y * scale_x)))
    scale_y = H / span_y

    # 把取整后的像素范围反解成经纬度，作为画布的地理范围
    lat_n = sb._px_to_lat(oy, ZOOM)
    lat_s = sb._px_to_lat(oy + span_y, ZOOM)
    lon_w = sb._px_to_lon(ox, ZOOM)
    lon_e = sb._px_to_lon(ox + span_x, ZOOM)
    proj = Projector(lat_s, lon_w, lat_n, lon_e, W, H)
    z = W / 1400.0

    img = Image.new("RGB", (W, H), C["bg"])
    d = ImageDraw.Draw(img, "RGBA")
    feats = vec["features"]

    def draw_polys(pred, color):
        for f in feats:
            pr = f.get("properties") or {}
            if not pred(pr):
                continue
            for ring in _rings_of(f.get("geometry") or {}):
                pts = [proj.pt(lo, la) for lo, la in ring]
                if len(pts) >= 3:
                    d.polygon(pts, fill=color)

    # 1) 绿地 / 场地 / 商业
    draw_polys(lambda p: p.get("leisure") in ("park", "garden", "pitch", "sports_centre")
               or p.get("landuse") in GREEN_LANDUSE
               or p.get("natural") in ("wood", "scrub", "grassland"), C["green"])
    draw_polys(lambda p: p.get("leisure") == "pitch" or p.get("landuse") == "grass",
               C["pitch"])
    draw_polys(lambda p: p.get("landuse") in SAND_LANDUSE, C["sand"])
    draw_polys(lambda p: p.get("landuse") in COMMERCIAL_LANDUSE, C["commercial"])

    # 2) 水面与河道
    draw_polys(lambda p: p.get("natural") == "water" or p.get("waterway") == "riverbank"
               or "water" in p, C["water"])
    for f in feats:
        pr = f.get("properties") or {}
        if "waterway" not in pr:
            continue
        for line in _lines_of(f.get("geometry") or {}):
            pts = [proj.pt(lo, la) for lo, la in line]
            if len(pts) >= 2:
                wd = max(2, int(round(6 * z))) if pr.get("waterway") in ("river", "canal") \
                    else max(1, int(round(2.5 * z)))
                d.line(pts, fill=C["waterway"], width=wd, joint="curve")

    # 3) 校园底色
    gj = (bounds or {}).get("geojson") or {}
    if gj.get("type") in ("Polygon", "MultiPolygon"):
        for ring in _rings_of(gj):
            pts = [proj.pt(lo, la) for lo, la in ring]
            if len(pts) >= 3:
                d.polygon(pts, fill=C["campus"])
    for f in feats:
        pr = f.get("properties") or {}
        if pr.get("amenity") == "university":
            for ring in _rings_of(f.get("geometry") or {}):
                pts = [proj.pt(lo, la) for lo, la in ring]
                if len(pts) >= 3:
                    d.polygon(pts, fill=C["campus"])

    # 4) 道路：低等级先画，高等级盖在上面
    roads = [f for f in feats if (f.get("properties") or {}).get("highway")]
    for lvl in ROAD_ORDER:
        style = ROAD_STYLE.get(lvl)
        if not style:
            continue
        mult, fill_key, case_key = style
        lw = max(1, int(round(mult * 3.0 * z)))
        for f in roads:
            if f["properties"]["highway"] != lvl:
                continue
            for line in _lines_of(f.get("geometry") or {}):
                pts = [proj.pt(lo, la) for lo, la in line]
                if len(pts) < 2:
                    continue
                if case_key:
                    d.line(pts, fill=C[case_key],
                           width=lw + max(1, int(round(1.6 * z))), joint="curve")
                d.line(pts, fill=C[fill_key], width=lw, joint="curve")

    # 5) 建筑（描边 + 填充，轮廓更清晰）
    for feat in blds.get("features", []):
        for ring in _rings_of(feat.get("geometry") or {}):
            pts = [proj.pt(lo, la) for lo, la in ring]
            if len(pts) >= 3:
                d.polygon(pts, fill=C["building"], outline=C["building_e"],
                          width=max(1, int(round(0.9 * z))))

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return sb.FrameImage(
        png=buf.getvalue(), width=W, height=H, zoom=ZOOM,
        scale_x=scale_x, scale_y=scale_y, origin_x=ox, origin_y=oy,
        south=s, west=w, north=n, east=e,
    )


def cached_vector_frame(campus: str, s, w, n, e, *, width: int = 2000,
                        quality: int = 88) -> "object":
    """带本地缓存的版本（存 JPEG，体积小）；命中直接读，秒开。"""
    import static_basemap as sb
    from PIL import Image

    path = IMAGE_CACHE / f"{campus}_vector_w{width}.jpg"
    meta_path = path.with_suffix(path.suffix + ".json")
    if path.exists() and path.stat().st_size > 0 and meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            return sb.FrameImage(png=path.read_bytes(), **meta)
        except Exception:
            pass

    fi = render_frame_image(campus, s, w, n, e, width=width)
    img = Image.open(io.BytesIO(fi.png)).convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality, optimize=True)
    meta = {k: getattr(fi, k) for k in (
        "width", "height", "zoom", "scale_x", "scale_y", "origin_x", "origin_y",
        "south", "west", "north", "east")}
    IMAGE_CACHE.mkdir(parents=True, exist_ok=True)
    path.write_bytes(buf.getvalue())
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    return sb.FrameImage(png=buf.getvalue(), **meta)
