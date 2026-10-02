"""把「外框」内的底图拼成**一张静态图**，并提供 像素 ↔ 经纬度 的换算。

为什么要它（2026-10-02 用户提的方案，比实时地图更合适）
------------------------------------------------------
用 Leaflet 实时地图有两个老问题：
  1. **视野贴合很难做准**：视图尺寸要等渲染出来才知道，且容器尺寸因屏幕而异，
     服务端算的缩放级总差一点，结果"蓝框"不是太大就是太小（就是之前被否掉的"长条"）；
  2. 底图美术不可控（瓦片是现成的，做不了统一调色/压暗/加标注）。

改成静态图之后两件事都简单了：
  * 图片就是"外框内的一切"，比例、留白、调色都可以随意处理；
  * 点击选点用**纯数学换算**，没有视图尺寸参与的余地 —— 图上第几个像素，
    就对应固定那个经纬度，永远对得上。

坐标系推导（真正的关键，别改错）
--------------------------------
Web Mercator，缩放 z 时整个世界是 `256·2^z` 像素的正方形：

    world_px(z) = 256 · 2^z
    px(lon, z)  = (lon + 180) / 360 · world_px(z)
    py(lat, z)  = (1 − ln(tan(φ) + sec(φ)) / π) / 2 · world_px(z)      φ = lat·π/180

于是"外框"对应像素矩形 (px0, py0)~(px1, py1)。我们取这张矩形内的瓦片拼成大图，
再缩放到目标宽度 W。缩放比 `s = W / (px1−px0)`，点图的经纬度换算就是：

    lon = X / s · 360 / world_px(z) − 180
    lat = atan(sinh(π · (1 − 2 · Y / s / world_px(z)))) · 180/π

图像宽高比恒等于外框的 Web-Mercator 宽高比，所以**图上不会有多余区域**。
"""
from __future__ import annotations

import base64
import io
import math
import threading
import time
from dataclasses import dataclass
from pathlib import Path

TILE_SIZE = 256
# 瓦片缓存目录（已 gitignore）。避免反复请求同一张瓦片，也便于离线复现。
CACHE_DIR = Path(__file__).resolve().parent / "data" / "tile_cache"
# OSM 的瓦片使用政策要求能识别来源的 UA；别用默认的 python-requests。
USER_AGENT = "sky-map-campus-contest/1.0 (Nanjing University campus sky photo contest; contact: organizer)"

# ---------------------------------------------------------------- 底图源
#
# 2026-10-02 定的：**用谷歌卫星影像**（用户要求，理由很直接 —— 画质明显高一大截，
# 建筑纹理、树木、操场都清楚，高德卫星在同样尺寸下糊得多）。
#
# ⚠ 坐标系结论（别再来回改）：谷歌卫星瓦片**零偏移**即与 WGS84/OSM 对齐。
#   判据不是"看着像"，而是客观指标：把 OSM 建筑轮廓画上去，量"轮廓处的影像
#   边缘强度提升"，零偏移 1.065 > GCJ 平移 1.022（`tools/_probe_google_gcj.py`）。
#   这与"高德街道图是 GCJ-02、必须纠偏"并不矛盾 —— 卫星影像和矢量图是两套东西。
#
# ⚠ 使用条款提醒：谷歌瓦片用于公开发布的网站，严格说需要对应的授权/配额。
#   这里因为**图片是预生成并入库的**（运行时不请求谷歌），影响面小；
#   若日后要大规模商用，需换成有明确授权的影像源。
GOOGLE_SAT = "https://mt{s}.google.com/vt/lyrs=s&x={x}&y={y}&z={z}"
GOOGLE_SUBDOMAINS = "0123"
DEFAULT_TILE_KEY = "google_sat"
_LOCK = threading.Lock()


@dataclass(frozen=True)
class FrameImage:
    """一张拼好的外框底图 + 它对外的换算参数。

    横竖用**各自的**缩放比（`scale_x` / `scale_y`）：裁剪与缩放都取整过，
    两个方向可能差最后一丁点，分开存才能保证换算与图像像素严格一致。
    """

    png: bytes
    width: int
    height: int
    zoom: int
    scale_x: float                    # 缩放比：世界像素 -> 输出图像像素（横向）
    scale_y: float                    # 同（纵向）
    origin_x: float                   # 外框左上角在世界像素坐标里的 x
    origin_y: float                   # 外框左上角在世界像素坐标里的 y
    south: float
    west: float
    north: float
    east: float

    # ---------------------------------------------------------- 换算
    def px_to_latlng(self, x: float, y: float) -> tuple[float, float]:
        """图像上的像素 (x, y) -> (lat, lon)。原点左上，y 向下。"""
        return latlng_from_image_px(
            x, y, zoom=self.zoom, scale_x=self.scale_x, scale_y=self.scale_y,
            origin_x=self.origin_x, origin_y=self.origin_y,
        )

    def latlng_to_px(self, lat: float, lon: float) -> tuple[float, float]:
        """(lat, lon) -> 图像像素 (x, y)。"""
        return image_px_from_latlng(
            lat, lon, zoom=self.zoom, scale_x=self.scale_x, scale_y=self.scale_y,
            origin_x=self.origin_x, origin_y=self.origin_y,
        )

    def data_url(self) -> str:
        return "data:image/png;base64," + base64.b64encode(self.png).decode("ascii")


# ---------------------------------------------------------------- 投影

def world_px(zoom: int) -> float:
    return TILE_SIZE * (2 ** zoom)


def _lon_to_px(lon: float, zoom: int) -> float:
    return (lon + 180.0) / 360.0 * world_px(zoom)


def _lat_to_px(lat: float, zoom: int) -> float:
    lat = max(-85.05112878, min(85.05112878, lat))     # Mercator 极点截断
    phi = math.radians(lat)
    return (1.0 - math.log(math.tan(phi) + 1.0 / math.cos(phi)) / math.pi) / 2.0 * world_px(zoom)


def _px_to_lat(y: float, zoom: int) -> float:
    n = math.pi * (1.0 - 2.0 * y / world_px(zoom))
    return math.degrees(math.atan(math.sinh(n)))


def _px_to_lon(x: float, zoom: int) -> float:
    return x / world_px(zoom) * 360.0 - 180.0


def image_px_from_latlng(lat: float, lon: float, *, zoom: int, scale_x: float, scale_y: float,
                         origin_x: float, origin_y: float) -> tuple[float, float]:
    """经纬度 -> 输出图像像素。"""
    return ((_lon_to_px(lon, zoom) - origin_x) * scale_x,
            (_lat_to_px(lat, zoom) - origin_y) * scale_y)


def latlng_from_image_px(x: float, y: float, *, zoom: int, scale_x: float, scale_y: float,
                         origin_x: float, origin_y: float) -> tuple[float, float]:
    """输出图像像素 -> 经纬度。"""
    wx = origin_x + (x / scale_x if scale_x else 0.0)
    wy = origin_y + (y / scale_y if scale_y else 0.0)
    return (_px_to_lat(wy, zoom), _px_to_lon(wx, zoom))


def choose_zoom(south: float, west: float, north: float, east: float,
                target_width: int, *, min_zoom: int = 12, max_zoom: int = 17) -> int:
    """选一个让外框宽度≈target_width 的缩放级，并限制在合理区间。"""
    for z in range(max_zoom, min_zoom - 1, -1):
        w = _lon_to_px(east, z) - _lon_to_px(west, z)
        if w <= target_width:
            return z
    return min_zoom


# ---------------------------------------------------------------- 瓦片下载

def _tile_path(template_key: str, z: int, x: int, y: int) -> Path:
    return CACHE_DIR / template_key / str(z) / str(x) / f"{y}.png"


def _fetch_tile(url: str, dest: Path, *, timeout: float = 15.0,
                extra_headers: dict | None = None) -> bytes | None:
    """取一张瓦片（命中本地缓存就直接读）。失败返回 None，不抛异常。"""
    if dest.exists() and dest.stat().st_size > 0:
        return dest.read_bytes()
    try:
        import requests
    except Exception:
        return None
    headers = {"User-Agent": USER_AGENT, "Referer": "https://www.openstreetmap.org/"}
    if extra_headers:
        headers.update(extra_headers)
    for attempt in range(3):
        try:
            r = requests.get(url, timeout=timeout, headers=headers)
            if r.status_code == 200 and r.content:
                with _LOCK:
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_bytes(r.content)
                return r.content
            if r.status_code in (403, 429):      # 被限流：退避后重试
                time.sleep(1.5 * (attempt + 1))
                continue
            return None
        except Exception:
            time.sleep(0.4 * (attempt + 1))
    return None


def _render_tiles(template: str, template_key: str, z: int,
                  tx0: int, ty0: int, tx1: int, ty1: int,
                  progress=None, *, tile_shift: tuple[float, float] = (0.0, 0.0),
                  subdomains: str = "abc", extra_headers: dict | None = None
                  ) -> tuple[object, int, int, int]:
    """把瓦片区域拼成一张大图。返回 (PIL.Image, 命中缓存数, 下载数, 失败数)。

    `tile_shift` 是**瓦片索引**的平移量，用于 GCJ-02 纠偏：高德瓦片是 GCJ-02，
    要在 WGS84 的网格上取到正确影像，得把请求的瓦片索引整体平移一段。
    """
    from PIL import Image

    nx, ny = tx1 - tx0 + 1, ty1 - ty0 + 1
    canvas = Image.new("RGB", (nx * TILE_SIZE, ny * TILE_SIZE), (238, 238, 234))
    hit = down = miss = 0
    total = nx * ny
    done = 0
    n_tiles = 2 ** z
    subs = list(subdomains or "a")
    for ty in range(ty0, ty1 + 1):
        for tx in range(tx0, tx1 + 1):
            done += 1
            if progress:
                progress(done, total)
            # 平移后的真实瓦片索引（GCJ-02 纠偏就在这一步）
            rtx = int(round(tx + tile_shift[0]))
            rty = int(round(ty + tile_shift[1]))
            if not (0 <= rtx < n_tiles and 0 <= rty < n_tiles):
                miss += 1
                continue
            dest = _tile_path(template_key, z, rtx, rty)
            cached = dest.exists() and dest.stat().st_size > 0
            sub = subs[(rtx + rty) % len(subs)]
            url = (template.replace("{z}", str(z)).replace("{x}", str(rtx))
                   .replace("{y}", str(rty)).replace("{s}", sub))
            raw = _fetch_tile(url, dest, extra_headers=extra_headers)
            if raw is None:
                miss += 1
                continue
            if cached:
                hit += 1
            else:
                down += 1
            try:
                tile = Image.open(io.BytesIO(raw)).convert("RGB")
                if tile.size != (TILE_SIZE, TILE_SIZE):
                    tile = tile.resize((TILE_SIZE, TILE_SIZE))
                canvas.paste(tile, ((tx - tx0) * TILE_SIZE, (ty - ty0) * TILE_SIZE))
            except Exception:
                miss += 1
    return canvas, hit, down, miss


def build_frame_image(south: float, west: float, north: float, east: float, *,
                      target_width: int = 1500, zoom: int | None = None,
                      tile_url: str = GOOGLE_SAT,
                      tile_key: str = DEFAULT_TILE_KEY,
                      tile_shift: tuple[float, float] = (0.0, 0.0),
                      subdomains: str = GOOGLE_SUBDOMAINS,
                      extra_headers: dict | None = None,
                      buildings: list[dict] | None = None,
                      building_style: dict | None = None,
                      progress=None) -> FrameImage:
    """把外框内的底图拼成一张静态图（输出宽度≈target_width）。

    `buildings` 若给了（`submission_data.load_buildings` 的格式，经纬度 WGS84），
    就叠一层建筑轮廓 —— 这是"看着有真实感"的关键：卫星影像本身看不出哪栋是哪栋，
    描一层轮廓立刻有"规划图/沙盘"的质感。
    """
    z = zoom if zoom is not None else choose_zoom(south, west, north, east, target_width)

    px0, py0 = _lon_to_px(west, z), _lat_to_px(north, z)     # 外框左上
    px1, py1 = _lon_to_px(east, z), _lat_to_px(south, z)     # 外框右下
    tile_x0, tile_y0 = math.floor(px0 / TILE_SIZE), math.floor(py0 / TILE_SIZE)
    tile_x1, tile_y1 = math.floor(px1 / TILE_SIZE), math.floor(py1 / TILE_SIZE)
    n_tiles = 2 ** z
    tile_x0, tile_x1 = max(0, tile_x0), min(n_tiles - 1, tile_x1)
    tile_y0, tile_y1 = max(0, tile_y0), min(n_tiles - 1, tile_y1)

    canvas, hit, down, miss = _render_tiles(
        tile_url, tile_key, z, tile_x0, tile_y0, tile_x1, tile_y1, progress,
        tile_shift=tile_shift, subdomains=subdomains, extra_headers=extra_headers)

    # 从拼好的大图里裁出外框，再缩放到目标宽度
    left = px0 - tile_x0 * TILE_SIZE
    top = py0 - tile_y0 * TILE_SIZE
    right = px1 - tile_x0 * TILE_SIZE
    bottom = py1 - tile_y0 * TILE_SIZE
    crop = canvas.crop((int(round(left)), int(round(top)),
                        int(round(right)), int(round(bottom))))
    scale = target_width / (px1 - px0)
    out_h = max(1, int(round(crop.height * scale)))
    crop = crop.resize((target_width, out_h))

    # 裁剪/缩放都取过整，所以原点与缩放比都用"取整后的真实值"反推，
    # 保证换算与图像像素严格一致（别用未取整的 px0，会累积零点几像素的偏差）。
    real_origin_x = tile_x0 * TILE_SIZE + round(left)
    real_origin_y = tile_y0 * TILE_SIZE + round(top)
    span_x = round(right) - round(left)
    span_y = round(bottom) - round(top)
    scale_x = target_width / span_x if span_x else 1.0
    scale_y = out_h / span_y if span_y else scale_x

    # 建筑轮廓：在**缩放后的图**上画，用同一套 real_origin/scale 换算，
    # 所以与底图严格对齐（不需要再考虑 GCJ 偏移 —— 影像是靠平移瓦片网格对齐的，
    # 建筑是 WGS84 坐标，直接画在 WGS84 的像素网格上即可）。
    if buildings:
        _draw_buildings(crop, buildings, z, real_origin_x, real_origin_y,
                        scale_x, scale_y, building_style or {})

    buf = io.BytesIO()
    crop.save(buf, format="PNG", optimize=True)

    return FrameImage(
        png=buf.getvalue(),
        width=target_width,
        height=out_h,
        zoom=z,
        scale_x=scale_x,
        scale_y=scale_y,
        origin_x=real_origin_x,
        origin_y=real_origin_y,
        south=south, west=west, north=north, east=east,
    )


def _draw_buildings(img, buildings: list[dict], zoom: int, origin_x: float, origin_y: float,
                    scale_x: float, scale_y: float, style: dict) -> None:
    """把 OSM 建筑轮廓描到图上（原地修改 img）。

    轮廓颜色/线宽可调。默认给"暖白描边 + 半透明填充"，在卫星影像上既清晰
    又不像纯地图那样盖住实景。
    """
    from PIL import Image, ImageDraw

    outline = tuple(style.get("outline", (255, 226, 148)))
    fill = style.get("fill")                       # 可为 None（只描边）
    width = int(style.get("width", 2))
    roof_only = style.get("roof_only", False)      # 只描屋顶（有 height 的）

    layer = img.convert("RGBA")
    fill_layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    d_fill = ImageDraw.Draw(fill_layer)
    d_line = ImageDraw.Draw(layer)

    for b in buildings:
        if roof_only and not b.get("height"):
            continue
        for ring in b.get("rings", []):
            pts = []
            for lon, lat in ring:
                x = (_lon_to_px(lon, zoom) - origin_x) * scale_x
                y = (_lat_to_px(lat, zoom) - origin_y) * scale_y
                pts.append((x, y))
            if len(pts) < 3:
                continue
            if fill:
                # 多边形填充用 alpha 合成，避免直接盖住影像
                d_fill.polygon(pts, fill=tuple(fill))
            d_line.line(pts + [pts[0]], fill=outline, width=width, joint="curve")

    if fill:
        layer = Image.alpha_composite(layer, fill_layer)
        d_line = ImageDraw.Draw(layer)
        for b in buildings:
            if roof_only and not b.get("height"):
                continue
            for ring in b.get("rings", []):
                pts = [((_lon_to_px(lon, zoom) - origin_x) * scale_x,
                        (_lat_to_px(lat, zoom) - origin_y) * scale_y) for lon, lat in ring]
                if len(pts) >= 3:
                    d_line.line(pts + [pts[0]], fill=outline, width=width, joint="curve")

    img.paste(layer.convert("RGB"))



# ---------------------------------------------------------------- 本地缓存
#
# 为什么要缓存：拼一张图要请求几十张瓦片（一张 1200px 的图约 20~60 张）。
# 如果每次点击选点都重新拼，用户每点一下都要等好几秒，还会被瓦片源限流。
# 图的尺寸/内容与"外框 + 目标宽度 + 缩放级"一一对应，所以直接用这些做键。
IMAGE_CACHE = Path(__file__).resolve().parent / "data" / "frame_images"


def _cache_key(campus_key: str, target_width: int, zoom: int, fmt: str,
               tile_key: str = "osm") -> Path:
    # ⚠ 文件名里必须带 tile_key：否则"先用 OSM 生成、再换卫星"会命中旧缓存，
    #   看起来就像"换了瓦片源却没生效"（这个坑真踩过：样品图跑出来是街道图）。
    return IMAGE_CACHE / f"{campus_key}_{tile_key}_w{target_width}_z{zoom}.{fmt}"


def cached_frame_image(campus_key: str, south: float, west: float, north: float, east: float, *,
                       target_width: int = 1400, zoom: int | None = None, fmt: str = "jpg",
                       jpeg_quality: int = 85, progress=None,
                       tile_url: str = GOOGLE_SAT,
                       tile_key: str = DEFAULT_TILE_KEY,
                       tile_shift: tuple[float, float] = (0.0, 0.0),
                       subdomains: str = GOOGLE_SUBDOMAINS,
                       extra_headers: dict | None = None,
                       buildings: list[dict] | None = None,
                       building_style: dict | None = None) -> FrameImage:
    """带本地缓存的 `build_frame_image`：命中就直接读文件，秒开。

    fmt 默认 jpg —— 底图是照片型内容，JPEG 比 PNG 小一个数量级
    （1200px 底图 PNG ≈ 1.1 MB，JPEG q85 ≈ 250 KB），内嵌成 data URL 时这点很关键。

    ⚠️ 传入 `buildings` / `building_style` 时会**绕过缓存**（描边属于后期处理，
    缓存按"底图 + 尺寸"做键，不适合再叠加参数；这类调用本来也不多）。
    """
    z = zoom if zoom is not None else choose_zoom(south, west, north, east, target_width)
    path = _cache_key(campus_key, target_width, z, fmt, tile_key)
    meta_path = path.with_suffix(path.suffix + ".json")
    simple = buildings is None and not building_style

    if simple and path.exists() and path.stat().st_size > 0 and meta_path.exists():
        try:
            import json as _json
            meta = _json.loads(meta_path.read_text(encoding="utf-8"))
            return FrameImage(png=path.read_bytes(), **meta)
        except Exception:
            pass      # 缓存坏了就重做，不要因此报错

    img = build_frame_image(south, west, north, east, target_width=target_width, zoom=z,
                            tile_url=tile_url, tile_key=tile_key, tile_shift=tile_shift,
                            subdomains=subdomains, extra_headers=extra_headers,
                            buildings=buildings, building_style=building_style,
                            progress=progress)
    if simple and fmt == "jpg":
        from PIL import Image
        buf = io.BytesIO()
        Image.open(io.BytesIO(img.png)).convert("RGB").save(
            buf, format="JPEG", quality=jpeg_quality, optimize=True)
        img = FrameImage(png=buf.getvalue(), **{k: getattr(img, k) for k in (
            "width", "height", "zoom", "scale_x", "scale_y", "origin_x", "origin_y",
            "south", "west", "north", "east")})

    if not simple:
        return img       # 带后期处理的组合不进缓存

    IMAGE_CACHE.mkdir(parents=True, exist_ok=True)
    path.write_bytes(img.png)
    try:
        import json as _json
        meta_path.write_text(_json.dumps({k: getattr(img, k) for k in (
            "width", "height", "zoom", "scale_x", "scale_y", "origin_x", "origin_y",
            "south", "west", "north", "east")}, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:
        pass
    return img


if __name__ == "__main__":      # 手工生成一下，肉眼看看效果
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from frame_util import frame_of

    for key in ("gulou", "xianlin", "suzhou"):
        s, w, n, e = frame_of(key)
        img = cached_frame_image(key, s, w, n, e, target_width=1400)
        out = Path(__file__).resolve().parent / "data" / "tile_check" / f"frame_{key}.png"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(img.png)
        tl = img.px_to_latlng(0, 0)
        br = img.px_to_latlng(img.width, img.height)
        print(f"{key}: {img.width}x{img.height} zoom={img.zoom} "
              f"大小={len(img.png) // 1024}KB -> {out}")
        print(f"    左上 {tl[0]:.6f},{tl[1]:.6f}  期望 {n:.6f},{w:.6f}")
        print(f"    右下 {br[0]:.6f},{br[1]:.6f}  期望 {s:.6f},{e:.6f}")


