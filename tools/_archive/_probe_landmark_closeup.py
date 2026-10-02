# -*- coding: utf-8 -*-
"""渲染一个已知建筑的近景，直接判断 GCJ 平移方向（不靠统计指标）。

用鼓楼校区「体育馆」（OSM 里明确有名字、坐标 32.0601622,118.7742394）做靶子：
  渲染它周围 ~500 m 的近景，把它的 OSM 轮廓描成红色。
  * 红色轮廓压在体育馆屋顶上 ⇒ 该方向对；
  * 轮廓落在旁边街道/别的楼上 ⇒ 方向错或没纠偏。

用法：python tools/_probe_landmark_closeup.py
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import static_basemap as sb  # noqa: E402

KEY = "gulou"
TARGET = "体育馆"
ZOOM = 17
TILE = 256
HALF_M = 300.0        # 近景半边长（米）


def main() -> int:
    import map_build
    from submission_data import load_buildings

    blds = load_buildings(KEY)
    hit = next((b for b in blds if (b["name"] or "") == TARGET), None)
    if not hit:
        print(f"没找到「{TARGET}」")
        return 1
    clat, clon = hit["center"]
    print(f"靶子「{TARGET}」 WGS84 = {clat:.7f}, {clon:.7f}")

    # 经纬度半边长
    dlat = HALF_M / 111320.0
    dlon = HALF_M / (111320.0 * math.cos(math.radians(clat)))
    s, n = clat - dlat, clat + dlat
    w, e = clon - dlon, clon + dlon

    off = map_build.gcj_offset_meters(clat, clon)
    world = sb.world_px(ZOOM)
    dx = off[0] / (2 * math.pi * 6378137.0) * world / TILE
    dy = -off[1] / (2 * math.pi * 6378137.0) * world / TILE
    print(f"GCJ 米偏移=({off[0]:+.1f}, {off[1]:+.1f}) 瓦片平移=({dx:+.3f}, {dy:+.3f})")

    for sign, tag in ((1, "plus"), (0, "zero"), (-1, "minus")):
        img = sb.build_frame_image(
            s, w, n, e, target_width=800, zoom=ZOOM,
            tile_url="https://webst0{s}.is.autonavi.com/appmaptile?style=6&x={x}&y={y}&z={z}",
            tile_key="amap_sat",
            tile_shift=(sign * dx, sign * dy),
            subdomains="1234",
            extra_headers={"Referer": "https://www.amap.com/"},
            buildings=[hit],
            building_style={"outline": (255, 0, 0), "width": 3},
        )
        out = HERE / "data" / "tile_check" / f"closeup_{tag}.png"
        out.write_bytes(img.png)
        print(f"  {tag:6s} -> {out.name}  {img.width}x{img.height}")
    print("\n看这三张：红框套住体育馆屋顶的那张 = 正确方向。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
