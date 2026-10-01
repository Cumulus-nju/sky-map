# -*- coding: utf-8 -*-
r"""裁出苏州外框东边界附近的一条竖带，用来判断"校园到哪结束、工业区从哪开始"。

输出 data/tile_check/suzhou_east_edge.png
用法: python tools\_edge_suzhou.py
"""
from __future__ import annotations

import json
import math
from pathlib import Path

from PIL import Image, ImageDraw

HERE = Path(__file__).resolve().parent.parent
TILE = HERE / "data" / "tile_check"


def main() -> int:
    meta = json.loads((TILE / "suzhou_base_meta.json").read_text(encoding="utf-8"))
    z = meta["zoom"]
    n = 2.0 ** z
    img = Image.open(TILE / "suzhou_base_z16.jpg").convert("RGB")

    def px(lat, lon):
        x = (lon + 180.0) / 360.0 * n
        y = (1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2.0 * n
        return ((x - meta["tile_x0"]) * 256, (y - meta["tile_y0"]) * 256)

    # 经度刻度线：120.3800 ~ 120.3930，每 0.001° (约 95 m) 一条
    la_top, la_bot = 31.3640, 31.3420
    lon0, lon1 = 120.3800, 120.3935
    p_tl = px(la_top, lon0)
    p_br = px(la_bot, lon1)
    x0, y0, x1, y1 = int(p_tl[0]), int(p_tl[1]), int(p_br[0]), int(p_br[1])
    box = img.crop((x0, y0, x1, y1))
    scale = 2
    box = box.resize((box.width * scale, box.height * scale), Image.LANCZOS)
    dr = ImageDraw.Draw(box)

    lon = 120.3800
    step = 0
    while lon <= lon1 + 1e-9:
        cx = (px(la_top, lon)[0] - x0) * scale
        # 每 0.005°（约 475 m）一条主线，其余是 0.001° 细线
        main_line = step % 5 == 0
        dr.line([cx, 0, cx, box.height],
                fill=(255, 0, 200) if main_line else (0, 220, 255),
                width=2 if main_line else 1)
        if main_line:
            dr.text((cx + 3, 6), f"{lon:.4f}", fill=(255, 255, 0), stroke_width=2, stroke_fill=(0, 0, 0))
        lon = round(lon + 0.001, 4)
        step += 1

    # 当前外框东边界（WGS84 120.393623 -> GCJ-02）
    import sys
    sys.path.insert(0, str(HERE))
    from tools._check_suzhou_frame import w2g  # noqa: E402

    g_lon = w2g(31.3532, 120.393623)[1]
    ex = (px(31.3532, g_lon)[0] - x0) * scale
    dr.line([ex, 0, ex, box.height], fill=(0, 255, 0), width=4)
    dr.text((ex - 130, 30), "当前框东界", fill=(0, 255, 0), stroke_width=2, stroke_fill=(0, 0, 0))

    out = TILE / "suzhou_east_edge.png"
    box.save(out, quality=92)
    print(f"输出 {out}  {box.size}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
