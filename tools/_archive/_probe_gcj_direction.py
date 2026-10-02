# -*- coding: utf-8 -*-
"""高德卫星瓦片的 GCJ-02 网格平移方向——**做实验定死**，不靠推理。

问题：高德瓦片是 GCJ-02 坐标系，OSM/我们的外框是 WGS84，两者差约 500 m。
      要在 WGS84 的像素网格上取到"正确位置"的影像，瓦片索引必须平移。
      但往哪个方向平移（+offset 还是 -offset）**推理很容易搞反**，
      搞反了就是整体飘 500 m —— 而"飘了"在单张图上未必一眼看出来。

方法：同一块区域，分别用 +offset / 0 / -offset 取瓦片拼图，看哪一张的
      **卫星影像与 OSM 建筑轮廓对得上**。OSM 建筑轮廓（WGS84）在 WGS84 网格上
      是按坐标直接画的，所以只要影像对齐了它，方向就对了。

用法：python tools/_probe_gcj_direction.py
输出：data/tile_check/gcj_dir_{plus,zero,minus}.png（人工/视觉比对）
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import static_basemap as sb  # noqa: E402
from frame_util import frame_of  # noqa: E402

# 用鼓楼校区中心做实验：那一带建筑密、有操场和主干道，对齐与否一眼能看出
KEY = "gulou"
ZOOM = 16
TILE = 256


def gcj_offset_meters(lat: float, lon: float) -> tuple[float, float]:
    """WGS84 -> GCJ-02 的米偏移。与 map_build.gcj_offset_meters 同一套公式。"""
    import map_build
    return map_build.gcj_offset_meters(lat, lon)


def tile_shift(offset_m: tuple[float, float], zoom: int) -> tuple[float, float]:
    """把米偏移换算成**瓦片索引**的平移量。"""
    world = sb.world_px(zoom)
    dx = offset_m[0] / (2 * math.pi * 6378137.0) * world / TILE
    dy = -offset_m[1] / (2 * math.pi * 6378137.0) * world / TILE
    return dx, dy


def render(sign: int, out_name: str, *, with_buildings: bool = True) -> Path:
    s, w, n, e = frame_of(KEY)
    lat, lon = (s + n) / 2, (w + e) / 2
    off = gcj_offset_meters(lat, lon)
    dx, dy = tile_shift(off, ZOOM)
    print(f"校区={KEY} zoom={ZOOM}  米偏移=({off[0]:.1f}, {off[1]:.1f})  "
          f"瓦片平移=({dx:+.3f}, {dy:+.3f}) x sign={sign:+d}")

    blds = None
    if with_buildings:
        from submission_data import load_buildings
        blds = load_buildings(KEY)
        print(f"   建筑轮廓 {len(blds)} 栋")

    img = sb.build_frame_image(
        s, w, n, e, target_width=1100, zoom=ZOOM,
        tile_url="https://webst0{s}.is.autonavi.com/appmaptile?style=6&x={x}&y={y}&z={z}",
        tile_key="amap_sat",
        tile_shift=(sign * dx, sign * dy),
        subdomains="1234",
        extra_headers={"Referer": "https://www.amap.com/"},
        buildings=blds,
        # 红色描边：与影像对比强，对没对齐一眼可辨
        building_style={"outline": (255, 40, 40), "width": 2},
    )
    out = HERE / "data" / "tile_check" / out_name
    out.write_bytes(img.png)
    print(f"   -> {out}  ({img.width}x{img.height}, {len(img.png)//1024}KB)")
    return out


if __name__ == "__main__":
    for sign, name in ((1, "gcj_dir_plus.png"), (0, "gcj_dir_zero.png"), (-1, "gcj_dir_minus.png")):
        render(sign, name)
    print("\n看这三张图：红色建筑轮廓压住卫星影像里真实屋顶的那张，方向就是对的。")
