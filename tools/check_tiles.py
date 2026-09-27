"""瓦片源可用性实测：按真实经纬度取瓦片，拼成 3x3 预览图，肉眼确认底图是否正常。

用法: python tools/check_tiles.py
输出: data/tile_check/<provider>.png
"""
from __future__ import annotations

import io
import math
import urllib.request
from pathlib import Path

from PIL import Image

HERE = Path(__file__).resolve().parent.parent
OUT = HERE / "data" / "tile_check"
OUT.mkdir(parents=True, exist_ok=True)

# 南京大学鼓楼校区北大楼一带
LAT, LON, Z = 32.0566, 118.7736, 17

UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Referer": "https://www.nju.edu.cn/",
}

PROVIDERS = {
    "osm": "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
    "osm_de": "https://tile.openstreetmap.de/{z}/{x}/{y}.png",
    "opentopomap": "https://a.tile.opentopomap.org/{z}/{x}/{y}.png",
    "carto_light": "https://a.basemaps.cartocdn.com/light_all/{z}/{x}/{y}.png",
    "carto_voyager": "https://a.basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}.png",
    "esri_imagery": "https://server.arcgisonline.com/ArcGIS/rest/services/"
                    "World_Imagery/MapServer/tile/{z}/{y}/{x}",
    "gaode_street": "https://webrd0{s}.is.autonavi.com/appmaptile?lang=zh_cn&size=1&scale=1&style=8&x={x}&y={y}&z={z}",
    "gaode_sat": "https://webst0{s}.is.autonavi.com/appmaptile?style=6&x={x}&y={y}&z={z}",
}


def deg2tile(lat: float, lon: float, z: int) -> tuple[int, int]:
    n = 2 ** z
    x = int((lon + 180.0) / 360.0 * n)
    lat_r = math.radians(lat)
    y = int((1.0 - math.asinh(math.tan(lat_r)) / math.pi) / 2.0 * n)
    return x, y


def fetch(url: str) -> bytes | None:
    try:
        req = urllib.request.Request(url, headers=UA)
        return urllib.request.urlopen(req, timeout=25).read()
    except Exception:
        return None


def main() -> int:
    cx, cy = deg2tile(LAT, LON, Z)
    print(f"中心瓦片 z={Z} x={cx} y={cy}  (lat={LAT}, lon={LON})")
    grid = [(dx, dy) for dy in (-1, 0, 1) for dx in (-1, 0, 1)]

    results = []
    for name, tpl in PROVIDERS.items():
        mosaic = Image.new("RGB", (256 * 3, 256 * 3), (30, 30, 30))
        ok, colors = 0, 0
        for i, (dx, dy) in enumerate(grid):
            url = tpl.format(z=Z, x=cx + dx, y=cy + dy, s=(i % 4) + 1)
            raw = fetch(url)
            if not raw:
                continue
            try:
                im = Image.open(io.BytesIO(raw)).convert("RGB")
            except Exception:
                continue
            mosaic.paste(im, ((i % 3) * 256, (i // 3) * 256))
            ok += 1
        # 用颜色多样性粗略判断是不是"占位图/纯色图"
        small = mosaic.resize((24, 24))
        colors = len(set(small.getdata()))
        path = OUT / f"{name}.png"
        mosaic.save(path)
        verdict = "OK" if ok == 9 and colors > 60 else ("可疑" if ok == 9 else f"缺{9 - ok}块")
        results.append((name, ok, colors, verdict))
        print(f"  {name:14s} 取到 {ok}/9 块, 颜色数 {colors:4d}  -> {verdict}   {path.name}")

    print("\n结论（颜色数低 = 可能是 API KEY REQUIRED 之类的占位图）：")
    for name, ok, colors, verdict in sorted(results, key=lambda r: -r[2]):
        print(f"  {name:14s} {verdict}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
