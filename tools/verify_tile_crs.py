# -*- coding: utf-8 -*-
"""判定瓦片源的坐标系：底图到底需不需要 GCJ-02 平移。

结论（2026-10-02，本案已定）
----------------------------
**谷歌卫星瓦片零偏移即与 WGS84/OSM 对齐**，不需要 GCJ-02 平移。

这与"高德需纠偏 500 m"并不矛盾：高德**街道矢量图**确实是 GCJ-02、必须纠偏；
但**卫星影像**和矢量图是两套东西。⚠ 换瓦片源时**必须重跑本脚本**，
不要凭"上一家需要"就默认这一家也需要。

判据（客观，不靠肉眼）
----------------------
把 OSM 建筑轮廓画到两种候选底图上，量**轮廓处的影像边缘强度提升**：
真实建筑轮廓必然压在屋顶边缘上，所以对齐的那一版提升更高。
统计量用 90 分位 —— 均值会被大片平坦区稀释、中位数会因过半采样落在平坦处
直接为 0（这两个坑都踩过，导致三版底图"看起来没区别"）。

用法：python tools/verify_tile_crs.py [campus]
"""
from __future__ import annotations

import io
import math
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import static_basemap as sb  # noqa: E402
from frame_util import frame_of  # noqa: E402

W = 1200
GOOGLE_SAT = "https://mt{s}.google.com/vt/lyrs=s&x={x}&y={y}&z={z}"


def main() -> int:
    import numpy as np
    from PIL import Image, ImageDraw, ImageFilter
    from map_build import gcj_offset_meters
    from submission_data import load_buildings

    campus = sys.argv[1] if len(sys.argv) > 1 else "gulou"
    s, w, n, e = frame_of(campus)
    blds = load_buildings(campus)
    print(f"校区={campus} 建筑轮廓 {len(blds)} 栋")
    if len(blds) < 30:
        print("⚠ 该校区 OSM 建筑太少（如苏州），本判据不适用 —— 请换鼓楼/仙林跑。")

    off = gcj_offset_meters((s + n) / 2, (w + e) / 2)
    z = sb.choose_zoom(s, w, n, e, W)
    world = sb.world_px(z)
    dx = off[0] / (2 * math.pi * 6378137.0) * world / 256
    dy = -off[1] / (2 * math.pi * 6378137.0) * world / 256
    print(f"zoom={z} GCJ 米偏移=({off[0]:+.1f},{off[1]:+.1f}) 瓦片平移=({dx:+.2f},{dy:+.2f})")

    results = {}
    for tag, shift in (("零偏移(WGS)", (0.0, 0.0)), ("GCJ平移", (dx, dy))):
        fi = sb.build_frame_image(
            s, w, n, e, target_width=W, zoom=z,
            tile_url=GOOGLE_SAT, tile_key="google_sat", subdomains="0123",
            tile_shift=shift,
        )
        img = Image.open(io.BytesIO(fi.png)).convert("RGB")
        # 轮廓掩膜（画到与图同尺寸）
        mask = Image.new("L", img.size, 0)
        dm = ImageDraw.Draw(mask)
        for b in blds:
            for ring in b.get("rings", []):
                pts = [fi.latlng_to_px(lat, lon) for lon, lat in ring]
                if len(pts) >= 3:
                    dm.line(pts + [pts[0]], fill=255, width=2)
        mask = mask.filter(ImageFilter.MaxFilter(3))

        edge = np.asarray(img.convert("L").filter(ImageFilter.GaussianBlur(0.8))
                          .filter(ImageFilter.FIND_EDGES)).astype(float)
        m = np.asarray(mask) > 127
        lift = float(np.percentile(edge[m], 90) / max(np.percentile(edge, 90), 1e-9))
        results[tag] = lift
        print(f"  {tag:12s} 轮廓处边缘强度 P90 提升 = {lift:.3f}")

        # 存一张带轮廓的对照图，备查
        comp = img.copy()
        comp.paste(Image.new("RGB", img.size, (255, 0, 0)),
                   mask=mask.point(lambda v: 90 if v else 0))
        comp.save(HERE / "data" / "tile_check" / f"gcheck_{'wgs' if shift == (0.0, 0.0) else 'gcj'}.png")

    if len(results) == 2:
        best = max(results, key=results.get)
        second = min(results.values())
        ratio = results[best] / max(second, 1e-9)
        print(f"\n→ 对齐更好的是 **{best}**（{results[best]:.3f} vs {second:.3f}，{ratio:.3f}×）")
        print("   " + ("✅ 差异明显，可定案" if ratio > 1.10 else
                       "⚠ 差异不明显 —— 两者差别不大，选哪个都不会难看"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
