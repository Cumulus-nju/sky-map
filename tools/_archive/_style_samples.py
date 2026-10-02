# -*- coding: utf-8 -*-
"""底图美术方案样品：同一块区域出几个风格，供挑选。

为什么要出样品而不是直接定：用户明确说"好看优先、伪真实也行"，
而"好看"没有客观判据，猜错就是白做。所以同一张底图出 3~4 个风格，看图选。

标签（地标名）在**构建时烘焙进图片**，所以：
  * 云端不需要装中文字体（本机 Windows 有微软雅黑就够）；
  * 浏览器端零字体依赖，永远不会出现"方框字"。

用法：python tools/_style_samples.py [campus]
输出：data/tile_check/style_{a,b,c,d}.png
"""
from __future__ import annotations

import io
import math
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import static_basemap as sb  # noqa: E402
from frame_util import frame_of  # noqa: E402

FONT_CANDIDATES = [
    r"C:\Windows\Fonts\msyhbd.ttc",   # 微软雅黑 粗
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\simhei.ttf",   # 黑体
]
W = 1100


def load_font(size: int):
    from PIL import ImageFont
    for p in FONT_CANDIDATES:
        if Path(p).exists():
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                continue
    return ImageFont.load_default()


AMAP_SAT = "https://webst0{s}.is.autonavi.com/appmaptile?style=6&x={x}&y={y}&z={z}"
GOOGLE_SAT = "https://mt{s}.google.com/vt/lyrs=s&x={x}&y={y}&z={z}"
GOOGLE_HYBRID = "https://mt{s}.google.com/vt/lyrs=y&x={x}&y={y}&z={z}"
GOOGLE_SUBS = "0123"


def _gcj_tile_shift(campus: str, zoom: int) -> tuple[float, float]:
    """GCJ-02 的瓦片索引平移量（有些源需要，有些不需要，实测为准）。"""
    from map_build import gcj_offset_meters
    from campus_config import campus as get_campus
    clat, clon = get_campus(campus).center
    off = gcj_offset_meters(clat, clon)
    world = sb.world_px(zoom)
    return (off[0] / (2 * math.pi * 6378137.0) * world / 256,
            -off[1] / (2 * math.pi * 6378137.0) * world / 256)


def base_image(campus: str, source: str = "google", *, gcj: bool = False,
               zoom: int | None = None):
    """取底图。

    ⚠ 必须显式传 tile_url/tile_key：`cached_frame_image` 的默认值是 OSM 街道图。
      这个坑踩过 —— 忘了传就"换了瓦片源却没生效"，样品图跑出来是街道图。

    gcj=True 时对瓦片索引做 GCJ-02 平移（高德/谷歌中国区的坐标争议就靠它试出来）。
    """
    s, w, n, e = frame_of(campus)
    if source == "google":
        url, key, subs = GOOGLE_SAT, "google_sat", GOOGLE_SUBS
        headers = None
    elif source == "google_hybrid":
        url, key, subs = GOOGLE_HYBRID, "google_hyb", GOOGLE_SUBS
        headers = None
    else:
        url, key, subs = AMAP_SAT, "amap_sat", "1234"
        headers = {"Referer": "https://www.amap.com/"}
    z = zoom if zoom is not None else sb.choose_zoom(s, w, n, e, W)
    shift = _gcj_tile_shift(campus, z) if gcj else (0.0, 0.0)
    return sb.cached_frame_image(
        campus, s, w, n, e, target_width=W, fmt="png", zoom=z,
        tile_url=url, tile_key=key + ("_gcj" if gcj else ""), subdomains=subs,
        extra_headers=headers, tile_shift=shift,
    )


def landmark_pts(campus: str, fi):
    """地标 -> 图像像素。"""
    from campus_config import campus as get_campus
    cfg = get_campus(campus)
    out = []
    for name, (la, lo) in cfg.landmarks.items():
        x0, y0 = fi.latlng_to_px(la, lo)
        if -30 <= x0 <= fi.width + 30 and -30 <= y0 <= fi.height + 30:
            out.append((name, x0, y0))
    return out


def draw_labels(img, pts, *, color=(255, 255, 255), halo=(0, 0, 0),
                size=17, dot=(255, 190, 90), dot_r=3, max_labels=None):
    from PIL import ImageDraw
    d = ImageDraw.Draw(img)
    font = load_font(size)
    shown = pts[:max_labels] if max_labels else pts
    for name, x, y in shown:
        d.ellipse([x - dot_r, y - dot_r, x + dot_r, y + dot_r], fill=dot,
                  outline=(0, 0, 0, 120))
        # 描边文字（halo）保证在任何背景上都读得清
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                if dx or dy:
                    d.text((x + 7 + dx, y - size / 2 + dy), name, font=font, fill=halo)
        d.text((x + 7, y - size / 2), name, font=font, fill=color)
    return img


def vignette(img, strength=0.55):
    """四周压暗，把视线收到中心 —— 很便宜的"高级感"。"""
    from PIL import Image, ImageDraw, ImageFilter
    w, h = img.size
    mask = Image.new("L", (w, h), 0)
    d = ImageDraw.Draw(mask)
    pad = int(min(w, h) * 0.06)
    d.ellipse([-pad, -pad, w + pad, h + pad], fill=255)
    mask = mask.filter(ImageFilter.GaussianBlur(min(w, h) * 0.09))
    dark = Image.new("RGB", (w, h), (8, 10, 16))
    return Image.composite(img, Image.blend(img, dark, strength), mask)


# ---------------------------------------------------------------- 风格

def style_a(campus, fi, pts):
    """A 自然卫星 + 标签：保留实景，只做轻微提亮/饱和，加暗角与标签。"""
    from PIL import Image, ImageEnhance
    im = Image.open(io.BytesIO(fi.png)).convert("RGB")
    im = ImageEnhance.Color(im).enhance(1.10)
    im = ImageEnhance.Contrast(im).enhance(1.06)
    im = vignette(im, 0.42)
    draw_labels(im, pts, color=(255, 255, 255), halo=(0, 0, 0), size=18)
    return im


def style_b(campus, fi, pts):
    """B 单色墨蓝 + 暖橙标签：明显"设计过"，标签最跳（推荐给打卡点地图）。"""
    from PIL import Image, ImageEnhance, ImageOps
    im = Image.open(io.BytesIO(fi.png)).convert("L")
    im = ImageOps.autocontrast(im, cutoff=1)
    im = ImageEnhance.Contrast(im).enhance(0.86)
    # 映射到墨蓝调色板
    rgb = ImageOps.colorize(im, black=(14, 20, 34), white=(196, 210, 224))
    rgb = vignette(rgb, 0.5)
    draw_labels(rgb, pts, color=(255, 176, 84), halo=(10, 12, 18), size=18,
                dot=(255, 140, 60), dot_r=4)
    return rgb


def style_c(campus, fi, pts):
    """C 淡彩去饱和 + 白标签：清爽、打印友好，地图感强。"""
    from PIL import Image, ImageEnhance
    im = Image.open(io.BytesIO(fi.png)).convert("RGB")
    im = ImageEnhance.Color(im).enhance(0.42)
    im = ImageEnhance.Brightness(im).enhance(1.10)
    im = ImageEnhance.Contrast(im).enhance(0.96)
    im = vignette(im, 0.22)
    draw_labels(im, pts, color=(40, 48, 60), halo=(255, 255, 255), size=18,
                dot=(230, 90, 60), dot_r=4)
    return im


def style_d(campus, fi, pts):
    """D 自然卫星 + 半透明建筑勾边 + 标签：既有实景又有"规划图"结构感。"""
    from PIL import Image
    from submission_data import load_buildings
    im = Image.open(io.BytesIO(fi.png)).convert("RGB")
    blds = load_buildings(campus)
    # 在半透明图层上描边再合成，避免死白线
    layer = Image.new("RGBA", im.size, (0, 0, 0, 0))
    from PIL import ImageDraw
    d = ImageDraw.Draw(layer)
    for b in blds:
        for ring in b.get("rings", []):
            pts2 = [fi.latlng_to_px(lat, lon) for lon, lat in ring]
            if len(pts2) >= 3:
                d.line(pts2 + [pts2[0]], fill=(255, 238, 190, 120), width=1)
    im = Image.alpha_composite(im.convert("RGBA"), layer).convert("RGB")
    im = vignette(im, 0.38)
    draw_labels(im, pts, color=(255, 255, 255), halo=(0, 0, 0), size=18)
    return im


def style_g1(campus, fi, pts):
    """G1 谷歌原生 + 极轻暗角：几乎不动，只收一下视线。"""
    from PIL import Image
    im = Image.open(io.BytesIO(fi.png)).convert("RGB")
    return vignette(im, 0.28)


def style_g2(campus, fi, pts):
    """G2 干净增艳：轻微提饱和+对比+提亮，最"商业地图"的一版。"""
    from PIL import Image, ImageEnhance
    im = Image.open(io.BytesIO(fi.png)).convert("RGB")
    im = ImageEnhance.Color(im).enhance(1.18)
    im = ImageEnhance.Contrast(im).enhance(1.08)
    im = ImageEnhance.Brightness(im).enhance(1.05)
    return vignette(im, 0.34)


def style_g3(campus, fi, pts):
    """G3 电影感暖调：偏暖+压暗四周，往"黄昏航拍"靠。"""
    from PIL import Image, ImageEnhance
    im = Image.open(io.BytesIO(fi.png)).convert("RGB")
    r, g, b = im.split()
    r = r.point(lambda v: min(255, int(v * 1.06 + 4)))
    b = b.point(lambda v: max(0, int(v * 0.94)))
    im = Image.merge("RGB", (r, g, b))
    im = ImageEnhance.Contrast(im).enhance(1.12)
    im = ImageEnhance.Brightness(im).enhance(0.97)
    return vignette(im, 0.46)


def style_g4(campus, fi, pts):
    """G4 高级灰：低饱和+高对比，冷静克制，适合配深色文字。"""
    from PIL import Image, ImageEnhance, ImageOps
    im = Image.open(io.BytesIO(fi.png)).convert("RGB")
    im = ImageEnhance.Color(im).enhance(0.55)
    im = ImageOps.autocontrast(im.convert("L"), cutoff=1).convert("RGB")
    im = ImageEnhance.Contrast(im).enhance(1.05)
    im = ImageEnhance.Brightness(im).enhance(1.06)
    return vignette(im, 0.34)


STYLES = {"a": style_a, "b": style_b, "c": style_c, "d": style_d,
          "g1": style_g1, "g2": style_g2, "g3": style_g3, "g4": style_g4}


def main() -> int:
    campus = sys.argv[1] if len(sys.argv) > 1 else "gulou"
    which = sys.argv[2] if len(sys.argv) > 2 else "google"
    gcj = (len(sys.argv) > 3 and sys.argv[3] == "gcj")
    fi = base_image(campus, which, gcj=gcj)
    pts = []          # 用户明确说：不要标签
    print(f"校区={campus} 源={which}{'(GCJ平移)' if gcj else ''} "
          f"底图 {fi.width}x{fi.height}  标签=关闭")
    outdir = HERE / "data" / "tile_check"
    prefix = "g" if which.startswith("google") else "a"
    for key in ("g1", "g2", "g3", "g4"):
        try:
            im = STYLES[key](campus, fi, pts)
            p = outdir / f"style_{prefix}{key}_{'gcj' if gcj else 'wgs'}.png"
            im.save(p)
            print(f"  {key} -> {p.name}  ({im.width}x{im.height}, "
                  f"{p.stat().st_size // 1024}KB)")
        except Exception as exc:
            print(f"  {key} 失败：{type(exc).__name__}: {exc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
