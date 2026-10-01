# -*- coding: utf-8 -*-
r"""把苏州底图按外框裁出放大图，便于人工核对校园边界。

输出 data/tile_check/suzhou_frame_zoom_{r}{c}.png
用法: python tools\_zoom_suzhou_frame.py
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
from tools._check_suzhou_frame import w2g  # noqa: E402

TILE = HERE / "data" / "tile_check"


def main() -> int:
    meta = json.loads((TILE / "suzhou_base_meta.json").read_text(encoding="utf-8"))
    z = meta["zoom"]
    n = 2.0 ** z
    img = Image.open(TILE / "suzhou_base_z16.jpg").convert("RGB")

    def px(lat, lon_gcj):
        x = (lon_gcj + 180.0) / 360.0 * n
        y = (1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2.0 * n
        return ((x - meta["tile_x0"]) * 256, (y - meta["tile_y0"]) * 256)

    s, w, nn, e = CAMPUSES["suzhou"].frame
    cs, cw = w2g(s, w)
    cn, ce = w2g(nn, e)
    p_sw, p_ne = px(cs, cw), px(cn, ce)
    x0, x1 = sorted([p_sw[0], p_ne[0]])
    y0, y1 = sorted([p_sw[1], p_ne[1]])

    pad = 130  # 往框外多取一点，看清框外是什么
    x0, y0 = max(0, x0 - pad), max(0, y0 - pad)
    x1, y1 = min(img.width, x1 + pad), min(img.height, y1 + pad)
    box = img.crop((x0, y0, x1, y1))
    dr = ImageDraw.Draw(box)
    dr.rectangle([pad, pad, box.width - pad, box.height - pad], outline=(255, 0, 200), width=3)

    # 叠地标
    for name, (la, lo) in CAMPUSES["suzhou"].landmarks.items():
        gla, glo = w2g(la, lo)
        x, y = px(gla, glo)
        x, y = x - x0, y - y0
        if 0 <= x < box.width and 0 <= y < box.height:
            dr.ellipse([x - 6, y - 6, x + 6, y + 6], fill=(230, 60, 30), outline=(255, 255, 255), width=2)
            dr.text((x + 9, y - 7), name, fill=(255, 255, 0), stroke_width=2, stroke_fill=(0, 0, 0))

    out = TILE / "suzhou_frame_crop.png"
    box.save(out, quality=92)
    print(f"输出 {out}  裁剪区 {box.size}  原图坐标 x{x0}-{x1} y{y0}-{y1}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
