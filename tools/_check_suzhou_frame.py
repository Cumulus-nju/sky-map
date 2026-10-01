# -*- coding: utf-8 -*-
r"""校验苏州校区地标与外框在真实卫星影像上的落位。

底图 data/tile_check/suzhou_base_z16.jpg 是高德瓦片拼的（**GCJ-02**），
所以 WGS84 坐标要先走 w2g 偏移才能叠上去。

输出 data/tile_check/suzhou_frame_check.png
用法: python tools\_check_suzhou_frame.py
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

from PIL import Image, ImageDraw

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
from campus_config import CAMPUSES  # noqa: E402

TILE = HERE / "data" / "tile_check"


def w2g(lat, lon):
    """WGS84 -> GCJ-02（高德瓦片用的坐标系）。"""
    a, ee = 6378245.0, 0.00669342162296594323
    dlat = -100.0 + 2.0 * lon + 3.0 * lat + 0.2 * lat * lat + 0.1 * lon * lat + 0.2 * math.sqrt(abs(lon))
    dlat += (20.0 * math.sin(6.0 * lon * math.pi) + 20.0 * math.sin(2.0 * lon * math.pi)) * 2.0 / 3.0
    dlat += (20.0 * math.sin(lat * math.pi) + 40.0 * math.sin(lat / 3.0 * math.pi)) * 2.0 / 3.0
    dlat += (160.0 * math.sin(lat / 12.0 * math.pi) + 320 * math.sin(lat * math.pi / 30.0)) * 2.0 / 3.0
    dlon = 300.0 + lon + 2.0 * lat + 0.1 * lon * lon + 0.1 * lon * lat + 0.1 * math.sqrt(abs(lon))
    dlon += (20.0 * math.sin(6.0 * lon * math.pi) + 20.0 * math.sin(2.0 * lon * math.pi)) * 2.0 / 3.0
    dlon += (20.0 * math.sin(lon * math.pi) + 40.0 * math.sin(lon / 3.0 * math.pi)) * 2.0 / 3.0
    dlon += (150.0 * math.sin(lon / 12.0 * math.pi) + 300.0 * math.sin(lon / 30.0)) * 2.0 / 3.0
    rl = math.radians(lat)
    m = 1 - ee * math.sin(rl) ** 2
    sq = math.sqrt(m)
    return (
        lat + (dlat * 180.0) / ((a * (1 - ee)) / (m * sq) * math.pi),
        lon + (dlon * 180.0) / (a / sq * math.cos(rl) * math.pi),
    )


def main() -> int:
    meta = json.loads((TILE / "suzhou_base_meta.json").read_text(encoding="utf-8"))
    z = meta["zoom"]
    n = 2.0 ** z
    img = Image.open(TILE / "suzhou_base_z16.jpg").convert("RGB")
    dr = ImageDraw.Draw(img)

    def px(lat, lon_gcj):
        x = (lon_gcj + 180.0) / 360.0 * n
        y = (1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2.0 * n
        return ((x - meta["tile_x0"]) * 256, (y - meta["tile_y0"]) * 256)

    # 外框（WGS84 -> GCJ-02）。注意像素 y 轴朝下，北边的 y 更小
    s, w, nn, e = CAMPUSES["suzhou"].frame
    cs, cw = w2g(s, w)
    cn, ce = w2g(nn, e)
    p_sw, p_ne = px(cs, cw), px(cn, ce)
    x0, x1 = sorted([p_sw[0], p_ne[0]])
    y0, y1 = sorted([p_sw[1], p_ne[1]])
    dr.rectangle([x0, y0, x1, y1], outline=(255, 0, 200), width=4)

    # 地标
    for name, (la, lo) in CAMPUSES["suzhou"].landmarks.items():
        gla, glo = w2g(la, lo)
        x, y = px(gla, glo)
        dr.ellipse([x - 7, y - 7, x + 7, y + 7], fill=(230, 60, 30), outline=(255, 255, 255), width=2)
        dr.text((x + 10, y - 8), name, fill=(255, 255, 0), stroke_width=2, stroke_fill=(0, 0, 0))

    out = TILE / "suzhou_frame_check.png"
    img.save(out)
    print(f"输出: {out}  尺寸 {img.size}")

    # 同时打印每个地标与框的位置关系
    print("\n地标是否在外框内：")
    for name, (la, lo) in CAMPUSES["suzhou"].landmarks.items():
        ok = s <= la <= nn and w <= lo <= e
        print(f"  {'在内' if ok else '框外'}  {name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
