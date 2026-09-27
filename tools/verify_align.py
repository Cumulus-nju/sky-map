"""像素级校验：高德卫星 + GCJ-02 偏移后，OSM 北大楼轮廓是否压在影像中的北大楼上。

纯 Python 拼瓦片 + 自行绘制轮廓，不依赖浏览器，便于逐像素比对。

用法: python tools/verify_align.py
输出: data/tile_check/align_<name>.png
"""
from __future__ import annotations

import io
import json
import math
import sys
import urllib.request
from pathlib import Path

from PIL import Image, ImageDraw

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
from map_build import gcj_offset_meters  # noqa: E402

OUT = HERE / "data" / "tile_check"
OUT.mkdir(parents=True, exist_ok=True)

Z = 18
UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Referer": "https://www.nju.edu.cn/",
}
WORLD = 2 * math.pi * 6378137.0


def merc(la: float, lo: float) -> tuple[float, float]:
    x = lo * WORLD / 360.0
    y = math.log(math.tan((90.0 + la) * math.pi / 360.0)) * 6378137.0
    return x, y


def wgs_to_tilepx(la: float, lo: float, z: int) -> tuple[float, float]:
    """WGS84 → 瓦片像素坐标（浮点，z 层）。"""
    x, y = merc(la, lo)
    n = 256 * (2 ** z)
    return (x + WORLD / 2) / WORLD * n, (WORLD / 2 - y) / WORLD * n


def fetch(url: str) -> Image.Image | None:
    try:
        req = urllib.request.Request(url, headers=UA)
        return Image.open(io.BytesIO(urllib.request.urlopen(req, timeout=25).read())).convert("RGB")
    except Exception:
        return None


def mosaic(center_lat: float, center_lon: float, template: str, z: int, half: int = 2,
           apply_gcj: bool = False) -> tuple[Image.Image, tuple[int, int]]:
    """拼 (2*half+1)^2 张瓦片，返回整图与拼图左上角对应的瓦片坐标 (ctx-half, cty-half)。"""
    px, py = wgs_to_tilepx(center_lat, center_lon, z)
    if apply_gcj:
        dx, dy = gcj_offset_meters(center_lat, center_lon)
        px += dx / (WORLD / (256 * (2 ** z)))
        py -= dy / (WORLD / (256 * (2 ** z)))
    ctx, cty = int(px // 256), int(py // 256)
    W = (2 * half + 1) * 256
    img = Image.new("RGB", (W, W), (25, 25, 30))
    for j in range(-half, half + 1):
        for i in range(-half, half + 1):
            tx, ty = ctx + i, cty + j
            url = template.format(z=z, x=tx, y=ty, s=1)
            t = fetch(url)
            if t:
                img.paste(t, ((i + half) * 256, (j + half) * 256))
    return img, (ctx - half, cty - half)


def to_mosaic(la: float, lo: float, z: int, origin: tuple[int, int],
              apply_gcj: bool = False) -> tuple[float, float]:
    """任意 WGS84 点 → 该拼图的像素坐标（统一以瓦片原点为参照，避免两图错位）。"""
    px, py = wgs_to_tilepx(la, lo, z)
    if apply_gcj:
        dx, dy = gcj_offset_meters(la, lo)
        px += dx / (WORLD / (256 * (2 ** z)))
        py -= dy / (WORLD / (256 * (2 ** z)))
    return px - origin[0] * 256, py - origin[1] * 256


def main() -> int:
    geo = json.loads((HERE / "data" / "raw" / "gulou_buildings.geojson").read_text(encoding="utf-8"))
    feat = next((f for f in geo["features"] if "北大楼" in (f["properties"].get("name") or "")), None)
    if not feat:
        print("没找到北大楼轮廓")
        return 1
    rings = (
        [feat["geometry"]["coordinates"][0]] if feat["geometry"]["type"] == "Polygon"
        else [p[0] for p in feat["geometry"]["coordinates"]]
    )
    flat = [pt for r in rings for pt in r]
    c_lon = sum(p[0] for p in flat) / len(flat)
    c_lat = sum(p[1] for p in flat) / len(flat)
    print(f"北大楼 OSM 中心: {c_lat:.6f}, {c_lon:.6f}")
    print(f"GCJ 偏移: {gcj_offset_meters(c_lat, c_lon)}")

    for name, tpl, gcj in (
        ("osm", "https://tile.openstreetmap.org/{z}/{x}/{y}.png", False),
        ("amap", "https://webst0{s}.is.autonavi.com/appmaptile?style=6&x={x}&y={y}&z={z}", True),
    ):
        img, origin = mosaic(c_lat, c_lon, tpl, Z, half=1, apply_gcj=gcj)
        d = ImageDraw.Draw(img)
        for r in rings:
            pts = [to_mosaic(p[1], p[0], Z, origin, apply_gcj=gcj) for p in r]
            d.polygon(pts, outline=(255, 40, 40), width=3)
            d.ellipse([pts[0][0] - 5, pts[0][1] - 5, pts[0][0] + 5, pts[0][1] + 5], fill=(255, 255, 0))
        path = OUT / f"align_{name}.png"
        img.save(path)
        print(f"  {name}: 瓦片原点 {origin} -> {path.name}"
              f"  {'(已加 GCJ 偏移)' if gcj else ''}")
    print("\n判读：两张图里红框都应套在同一栋楼上；若 amap 图红框明显偏离建筑，说明偏移方向/大小有误。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
