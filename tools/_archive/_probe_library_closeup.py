# -*- coding: utf-8 -*-
"""近景对质：拿一个又大又好认的建筑，两种候选偏移各渲染一张。

用「南京大学图书馆（鼓楼）」（OSM: 32.056412,118.776015，面积 3.8e-7，好认）。
同时画：
  * 红色 = 该建筑的 OSM 轮廓
  * 红色十字 = 轮廓中心
  * 白底黑字 = 偏移方案
这样"轮廓有没有套住这栋楼"是一眼可判的硬事实，不需要任何统计。

候选（由 map_build 的 GCJ 米偏移换算成瓦片索引平移）：
  -0.95 tile / 0 / +0.95 tile 三档（zoom 16 时 1 个瓦片≈611 m，偏移≈580 m）

用法：python tools/_probe_library_closeup.py
"""
from __future__ import annotations

import io
import math
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import static_basemap as sb  # noqa: E402

TARGET = "南京大学图书馆（鼓楼）"
ZOOM = 18
HALF_M = 220.0


def main() -> int:
    from PIL import Image, ImageDraw
    from map_build import gcj_offset_meters
    from submission_data import load_buildings

    blds = load_buildings("gulou")
    hit = next((b for b in blds if (b["name"] or "") == TARGET), None)
    if not hit:
        print(f"没找到 {TARGET}")
        return 1
    clat, clon = hit["center"]
    print(f"靶子 {TARGET}  WGS84 = {clat:.7f}, {clon:.7f}")

    dlat = HALF_M / 111320.0
    dlon = HALF_M / (111320.0 * math.cos(math.radians(clat)))
    s, n = clat - dlat, clat + dlat
    w, e = clon - dlon, clon + dlon

    off = gcj_offset_meters(clat, clon)
    world = sb.world_px(ZOOM)
    dx = off[0] / (2 * math.pi * 6378137.0) * world / 256
    dy = -off[1] / (2 * math.pi * 6378137.0) * world / 256
    print(f"GCJ 米偏移=({off[0]:+.1f}, {off[1]:+.1f})  → 瓦片平移 (±{dx:.2f}, ±{dy:.2f})")

    for sign, tag in ((0, "z0"), (-1, "neg"), (1, "pos")):
        img = sb.build_frame_image(
            s, w, n, e, target_width=760, zoom=ZOOM,
            tile_url="https://webst0{s}.is.autonavi.com/appmaptile?style=6&x={x}&y={y}&z={z}",
            tile_key="amap_sat",
            tile_shift=(sign * dx, sign * dy),
            subdomains="1234",
            extra_headers={"Referer": "https://www.amap.com/"},
            buildings=[hit],
            building_style={"outline": (255, 0, 0), "width": 2},
        )
        im = Image.open(io.BytesIO(img.png)).convert("RGB")
        d = ImageDraw.Draw(im)
        # 轮廓中心十字（图中点附近）
        cx, cy = img.width / 2, img.height / 2
        d.line([cx - 14, cy, cx + 14, cy], fill=(255, 0, 0), width=2)
        d.line([cx, cy - 14, cx, cy + 14], fill=(255, 0, 0), width=2)
        d.rectangle([4, 4, 120, 26], fill=(255, 255, 255))
        d.text((10, 9), f"shift {sign:+d} ({tag})", fill=(200, 0, 0))
        out = HERE / "data" / "tile_check" / f"lib_{tag}.png"
        im.save(out)
        print(f"   {tag:4s} -> {out.name}  {img.width}x{img.height}")
    print("\n看三张：红轮廓正好套住图书馆屋顶、十字落在楼中心的那张 = 正确偏移。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
