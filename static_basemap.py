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


def _fetch_tile(url: str, dest: Path, *, timeout: float = 15.0) -> bytes | None:
    """取一张瓦片（命中本地缓存就直接读）。失败返回 None，不抛异常。"""
    if dest.exists() and dest.stat().st_size > 0:
        return dest.read_bytes()
    try:
        import requests
    except Exception:
        return None
    for attempt in range(3):
        try:
            r = requests.get(url, timeout=timeout,
                             headers={"User-Agent": USER_AGENT, "Referer": "https://www.openstreetmap.org/"})
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
                  progress=None) -> tuple[object, int, int, int]:
    """把瓦片区域拼成一张大图。返回 (PIL.Image, 命中缓存数, 下载数, 失败数)。"""
    from PIL import Image

    nx, ny = tx1 - tx0 + 1, ty1 - ty0 + 1
    canvas = Image.new("RGB", (nx * TILE_SIZE, ny * TILE_SIZE), (238, 238, 234))
    hit = down = miss = 0
    total = nx * ny
    done = 0
    for ty in range(ty0, ty1 + 1):
        for tx in range(tx0, tx1 + 1):
            done += 1
            if progress:
                progress(done, total)
            dest = _tile_path(template_key, z, tx, ty)
            cached = dest.exists() and dest.stat().st_size > 0
            url = (template.replace("{z}", str(z)).replace("{x}", str(tx))
                   .replace("{y}", str(ty)).replace("{s}", "a"))
            raw = _fetch_tile(url, dest)
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
                      tile_url: str = "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
                      tile_key: str = "osm", progress=None) -> FrameImage:
    """把外框内的底图拼成一张静态图（输出宽度≈target_width）。"""
    z = zoom if zoom is not None else choose_zoom(south, west, north, east, target_width)

    px0, py0 = _lon_to_px(west, z), _lat_to_px(north, z)     # 外框左上
    px1, py1 = _lon_to_px(east, z), _lat_to_px(south, z)     # 外框右下
    tile_x0, tile_y0 = math.floor(px0 / TILE_SIZE), math.floor(py0 / TILE_SIZE)
    tile_x1, tile_y1 = math.floor(px1 / TILE_SIZE), math.floor(py1 / TILE_SIZE)
    n_tiles = 2 ** z
    tile_x0, tile_x1 = max(0, tile_x0), min(n_tiles - 1, tile_x1)
    tile_y0, tile_y1 = max(0, tile_y0), min(n_tiles - 1, tile_y1)

    canvas, hit, down, miss = _render_tiles(tile_url, tile_key, z,
                                            tile_x0, tile_y0, tile_x1, tile_y1, progress)

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


# ---------------------------------------------------------------- 本地缓存
#
# 为什么要缓存：拼一张图要请求几十张瓦片（一张 1200px 的图约 20~60 张）。
# 如果每次点击选点都重新拼，用户每点一下都要等好几秒，还会被瓦片源限流。
# 图的尺寸/内容与"外框 + 目标宽度 + 缩放级"一一对应，所以直接用这些做键。
IMAGE_CACHE = Path(__file__).resolve().parent / "data" / "frame_images"


def _cache_key(campus_key: str, target_width: int, zoom: int, fmt: str) -> Path:
    return IMAGE_CACHE / f"{campus_key}_w{target_width}_z{zoom}.{fmt}"


def cached_frame_image(campus_key: str, south: float, west: float, north: float, east: float, *,
                       target_width: int = 1400, zoom: int | None = None, fmt: str = "jpg",
                       jpeg_quality: int = 85, progress=None) -> FrameImage:
    """带本地缓存的 `build_frame_image`：命中就直接读文件，秒开。

    fmt 默认 jpg —— 底图是照片型内容，JPEG 比 PNG 小一个数量级
    （1200px 底图 PNG ≈ 1.1 MB，JPEG q85 ≈ 250 KB），内嵌成 data URL 时这点很关键。
    """
    z = zoom if zoom is not None else choose_zoom(south, west, north, east, target_width)
    path = _cache_key(campus_key, target_width, z, fmt)
    meta_path = path.with_suffix(path.suffix + ".json")

    if path.exists() and path.stat().st_size > 0 and meta_path.exists():
        try:
            import json as _json
            meta = _json.loads(meta_path.read_text(encoding="utf-8"))
            return FrameImage(png=path.read_bytes(), **meta)
        except Exception:
            pass      # 缓存坏了就重做，不要因此报错

    img = build_frame_image(south, west, north, east, target_width=target_width, zoom=z,
                            progress=progress)
    if fmt == "jpg":
        from PIL import Image
        buf = io.BytesIO()
        Image.open(io.BytesIO(img.png)).convert("RGB").save(
            buf, format="JPEG", quality=jpeg_quality, optimize=True)
        img = FrameImage(png=buf.getvalue(), **{k: getattr(img, k) for k in (
            "width", "height", "zoom", "scale_x", "scale_y", "origin_x", "origin_y",
            "south", "west", "north", "east")})

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


