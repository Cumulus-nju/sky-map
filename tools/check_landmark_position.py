# -*- coding: utf-8 -*-
"""检查底图上的**地标定位**是否瞄对了楼（用户反馈"定位错了"）。

做法：把底图 + 每个地标的标记点（cfg.landmarks 的坐标）与它的名字
一起画出来。地标坐标若瞄错了楼，一眼可见：名字标的点和实际楼对不上。

同时把 OSM 建筑轮廓也画上（细线），这样能判断"点位是否落在它该在的楼上"。

用法：python tools/check_landmark_position.py [campus]
输出：data/tile_check/landmark_check_<campus>.png
"""
from __future__ import annotations

import io
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import static_basemap as sb  # noqa: E402
from frame_util import frame_of  # noqa: E402

FONT = [r"C:\Windows\Fonts\msyhbd.ttc", r"C:\Windows\Fonts\msyh.ttc"]


def font(size):
    from PIL import ImageFont
    for p in FONT:
        if Path(p).exists():
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                pass
    return ImageFont.load_default()


def main() -> int:
    campus = sys.argv[1] if len(sys.argv) > 1 else "gulou"
    from PIL import Image, ImageDraw
    from campus_config import campus as get_campus
    from submission_data import load_buildings

    fi = sb.cached_frame_image(campus, *frame_of(campus), target_width=1500)
    im = Image.open(io.BytesIO(fi.png)).convert("RGB")
    d = ImageDraw.Draw(im, "RGBA")

    # 1) OSM 建筑轮廓（细白线，帮助判断点位该落在哪栋）
    for b in load_buildings(campus):
        for ring in b.get("rings", []):
            pts = [fi.latlng_to_px(lat, lon) for lon, lat in ring]
            if len(pts) >= 3:
                d.line(pts + [pts[0]], fill=(255, 255, 255, 90), width=1)

    # 2) 地标：坐标 -> 像素，画十字 + 名字
    cfg = get_campus(campus)
    f = font(19)
    n_in = 0
    for name, (la, lo) in cfg.landmarks.items():
        x, y = fi.latlng_to_px(la, lo)
        if not (0 <= x <= fi.width and 0 <= y <= fi.height):
            continue
        n_in += 1
        # 十字
        d.line([x - 11, y, x + 11, y], fill=(220, 20, 20, 255), width=3)
        d.line([x, y - 11, x, y + 11], fill=(220, 20, 20, 255), width=3)
        # 名字带描边
        tx, ty = x + 14, y - 11
        for dx in (-2, -1, 0, 1, 2):
            for dy in (-2, -1, 0, 1, 2):
                d.text((tx + dx, ty + dy), name, font=f, fill=(0, 0, 0, 200))
        d.text((tx, ty), name, font=f, fill=(255, 235, 120, 255))

    out = HERE / "data" / "tile_check" / f"landmark_check_{campus}.png"
    im.save(out)
    print(f"{campus}: 框内地标 {n_in}/{len(cfg.landmarks)} 个 -> {out}")
    print(f"   底图 {fi.width}x{fi.height}")
    print("\n看图：每个红色十字应落在它名字所指的是那栋楼上；若整体偏移 => 定位有系统误差。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
