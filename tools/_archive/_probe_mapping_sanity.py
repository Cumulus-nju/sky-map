# -*- coding: utf-8 -*-
"""确认"经纬度 → 图上像素"这套映射本身没搞反 —— 用地标自证。

前几次判方向失败，有一个我必须先排除的嫌疑：**我的经纬度→像素映射本身可能反了**
（南北或东西弄反）。如果不先证明映射是对的，后面所有方向判断都不可信。

证法：OSM 里挑**面积最大**的轮廓（鼓楼校区必然是田径场，卫星影像上极好认），
把它连同"外框四角"一起描出来，再看图：
  * 四角标注的 NE/NW/SE/SW 是否与地理常识一致；
  * 田径场轮廓是否正好压在影像里那个红色跑道椭圆上。

用法：python tools/_probe_mapping_sanity.py
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import static_basemap as sb  # noqa: E402
from frame_util import frame_of  # noqa: E402

KEY = "gulou"
ZOOM = 16


def polygon_area_deg(ring):
    """鞋带公式算面积（度^2，只用于排序）。"""
    a = 0.0
    for i in range(len(ring)):
        x1, y1 = ring[i]
        x2, y2 = ring[(i + 1) % len(ring)]
        a += x1 * y2 - x2 * y1
    return abs(a) / 2


def main() -> int:
    from PIL import ImageDraw
    from submission_data import load_buildings
    from map_build import gcj_offset_meters

    s, w, n, e = frame_of(KEY)
    blds = load_buildings(KEY)
    ranked = sorted(blds, key=lambda b: polygon_area_deg(b["rings"][0]), reverse=True)
    print("面积最大的 5 个轮廓：")
    for b in ranked[:5]:
        print(f"   {b['name'] or '(无名)':16s} area={polygon_area_deg(b['rings'][0]):.2e} "
              f"center={b['center']}")

    # 只用最大的那个，突出显示
    big = ranked[0]
    off = gcj_offset_meters((s + n) / 2, (w + e) / 2)
    world = sb.world_px(ZOOM)
    dx = off[0] / (2 * math.pi * 6378137.0) * world / 256
    dy = -off[1] / (2 * math.pi * 6378137.0) * world / 256

    for sign, tag in ((0, "zeroshift"), (-1, "negshift")):
        img = sb.build_frame_image(
            s, w, n, e, target_width=900, zoom=ZOOM,
            tile_url="https://webst0{s}.is.autonavi.com/appmaptile?style=6&x={x}&y={y}&z={z}",
            tile_key="amap_sat",
            tile_shift=(sign * dx, sign * dy),
            subdomains="1234",
            extra_headers={"Referer": "https://www.amap.com/"},
            buildings=[big],
            building_style={"outline": (255, 0, 0), "width": 4},
        )
        # 在四角写 NE/NW/SE/SW，人工核对映射方向
        from PIL import Image
        im = Image.open(__import__("io").BytesIO(img.png)).convert("RGB")
        d = ImageDraw.Draw(im)
        labels = [("NW", 6, 6), ("NE", img.width - 60, 6),
                  ("SW", 6, img.height - 28), ("SE", img.width - 60, img.height - 28)]
        for text, x, y in labels:
            d.rectangle([x, y, x + 52, y + 22], fill=(255, 255, 255))
            d.text((x + 6, y + 4), text, fill=(200, 0, 0))
        out = HERE / "data" / "tile_check" / f"mapcheck_{tag}.png"
        im.save(out)
        print(f"   {tag:10s} -> {out.name}")
    print(f"\n最大的轮廓 = {(big['name'] or '(无名)')}，面积 {polygon_area_deg(big['rings'][0]):.2e}")
    print("看图：红色大轮廓是否压住影像里的田径场？四角标注方向是否合乎常理？")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
