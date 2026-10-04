# -*- coding: utf-8 -*-
"""局部放大验收：把**卫星影像**和**提取出的树点**并排放大，肉眼看树对不对得上。

为什么要这个：整校区的三联图缩到 1500 px 宽以后，一棵树只有 2~3 个像素，
根本看不出"位置对不对、密度够不够"。验收必须看 1:1 甚至放大的局部。

用法：
    python tools/_peek_trees.py [campus] [zoom] [lat] [lon] [半边长米]
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import static_basemap as sb            # noqa: E402
from relief_basemap import _tree_points_sat  # noqa: E402
from vector_basemap import Projector   # noqa: E402

TILE = HERE / "data" / "tile_check"


def main(campus="gulou", zoom=19, lat=32.0569, lon=118.7744, half_m=260.0):
    from PIL import Image, ImageDraw

    png = TILE / f"campussat_{campus}_z{zoom}.png"
    meta = json.loads((TILE / f"campussat_{campus}_z{zoom}.json").read_text(encoding="utf-8"))
    fi = sb.FrameImage(png=png.read_bytes(), **{k: meta[k] for k in (
        "width", "height", "zoom", "scale_x", "scale_y",
        "origin_x", "origin_y", "south", "west", "north", "east")})

    cx, cy = fi.latlng_to_px(lat, lon)
    mpp = (fi.east - fi.west) * 111320.0 * np.cos(np.radians((fi.south + fi.north) / 2)) / fi.width
    half = int(half_m / mpp)
    box = (int(cx - half), int(cy - half), int(cx + half), int(cy + half))
    print(f"{campus} z{zoom} {mpp:.3f} m/px  中心像素 ({cx:.0f},{cy:.0f})  裁 {box}  "
          f"= {2*half_m:.0f}m 见方")

    img = Image.open(png).convert("RGB").crop(box)
    W, H = img.size
    # 树点：用**正式渲染那套**取点（保证看到的就是底图上会画的东西）。
    # ⚠ 半径与底图同口径：底图的 z = 宽度/1400，高清图宽 6000 ⇒ 这里必须缩放，
    #   否则圆会小成几个像素，看着"树很稀"是自己画错了。
    z_draw = fi.width / 1400.0
    proj = Projector(fi.south, fi.west, fi.north, fi.east, fi.width, fi.height)
    pts = _tree_points_sat(campus, proj, z_draw) or []
    overlay = img.copy()
    d = ImageDraw.Draw(overlay)
    shown = 0
    for (px, py, r) in pts:
        gx, gy = px - box[0], py - box[1]
        if -20 <= gx <= W + 20 and -20 <= gy <= H + 20:
            shown += 1
            d.ellipse([gx - r, gy - r, gx + r, gy + r],
                      fill=(255, 60, 60), outline=(120, 0, 0))
    print(f"该窗口内 {shown} 棵树（全校区 {len(pts)} 棵）")

    row = np.hstack([np.asarray(img), np.asarray(overlay)])
    out = TILE / f"peektrees_{campus}_z{zoom}.jpg"
    Image.fromarray(row).save(out, quality=90)
    print(f"-> {out}  ({row.shape[1]}x{row.shape[0]})")
    return 0


if __name__ == "__main__":
    a = sys.argv[1:]
    sys.exit(main(a[0] if a else "gulou",
                  int(a[1]) if len(a) > 1 else 19,
                  float(a[2]) if len(a) > 2 else 32.0569,
                  float(a[3]) if len(a) > 3 else 118.7744,
                  float(a[4]) if len(a) > 4 else 260.0))
