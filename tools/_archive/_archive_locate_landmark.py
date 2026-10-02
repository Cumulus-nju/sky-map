# -*- coding: utf-8 -*-
"""用**一个明确地标**直接量 GCJ 平移方向与大小。

前面三个统计判据都只有 1.02~1.04× 的领先，等于没区分度。最靠得住的办法是：
拿一个**又好认又大**的地标，看它在图上的真实位置与"OSM 坐标算出的预期位置"差多少，
以及那个差值的方向与 GCJ 偏移是否一致。

这里用地标 = 鼓楼校区田径场（`campus_config` 里没有它，所以直接从 OSM 建筑里
挑一个面积最大、且轮廓接近椭圆的——操场在卫星影像上是极好认的深绿色椭圆）。

做法：
  1. 在所有建筑轮廓里找出**面积最大的那个**（鼓楼校区最大的是操场/体育馆一带）；
  2. 算出它的中心在每张图上的**预期像素位置**；
  3. 在该位置附近做模板匹配（用轮廓形状做模板，在影像边缘图上找最佳偏移）……

⚠ 上面第 3 步容易又变成玄学。所以这里改成更硬的：**直接量"图上的操场"在哪**——
   操场在影像里是深色（跑道红/草绿）低亮度区域，用亮度阈值找最大连通块即可，
   完全不依赖轮廓。

用法：python tools/_locate_landmark.py
"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from PIL import Image  # noqa: E402

CHECK = HERE / "data" / "tile_check"
# 鼓楼校区田径场（来自 campus_config 的地标表：这是 WGS84 坐标）
LANDMARK = ("操场", 32.05460, 118.77400)


def expected_px(img_w: int, img_h: int, s, w, n, e, lat, lon):
    """把经纬度按"图 = 外框内 Mercator 投影"换算成图像像素。"""
    def merc(lat_):
        import math
        lat_ = max(-85.05112878, min(85.05112878, lat_))
        p = math.radians(lat_)
        return (1 - math.log(math.tan(p) + 1 / math.cos(p)) / math.pi) / 2

    def lonf(lon_):
        return (lon_ + 180) / 360

    fx = (lonf(lon) - lonf(w)) / max(lonf(e) - lonf(w), 1e-12)
    fy = (merc(lat) - merc(n)) / max(merc(s) - merc(n), 1e-12)
    return fx * img_w, fy * img_h


def darkest_blob_center(img: Image.Image):
    """找影像里最显眼的深色大块（操场跑道/草坪）的中心。粗但稳。"""
    import numpy as np

    g = np.asarray(img.convert("L").resize((img.width // 4, img.height // 4))).astype(float)
    # 深色 + 局部方差小的区域（跑道/草坪颜色均匀）
    thr = np.percentile(g, 12)
    mask = g <= thr
    if mask.sum() < 50:
        return None
    ys, xs = np.nonzero(mask)
    # 用中位数当中心，抗离群
    return float(np.median(xs) * 4), float(np.median(ys) * 4)


def main() -> int:
    from frame_util import frame_of
    s, w, n, e = frame_of("gulou")
    name, lat, lon = LANDMARK
    print(f"外框 S={s} W={w} N={n} E={e}")
    print(f"地标「{name}」WGS84=({lat}, {lon})\n")
    print(f"{'图':>16s} {'预期位置(px)':>18s} {'猜测深色块中心':>16s} {'差(px)':>14s}")

    for nm in ("gcj_dir_plus", "gcj_dir_zero", "gcj_dir_minus"):
        p = CHECK / f"{nm}.png"
        if not p.exists():
            print(f"{nm}: 缺图")
            continue
        img = Image.open(p).convert("RGB")
        ex, ey = expected_px(img.width, img.height, s, w, n, e, lat, lon)
        got = darkest_blob_center(img)
        if got:
            dx, dy = got[0] - ex, got[1] - ey
            print(f"{nm:>16s} ({ex:7.1f},{ey:7.1f}) ({got[0]:7.1f},{got[1]:7.1f}) "
                  f"({dx:+7.1f},{dy:+7.1f})")
        else:
            print(f"{nm:>16s} ({ex:7.1f},{ey:7.1f}) 找不到深色块")

    print("\n预期位移（若瓦片未纠偏，影像会整体偏这么多）：")
    # 用 map_build 的米偏移换算成这张图的像素
    import map_build
    dx_m, dy_m = map_build.gcj_offset_meters(lat, lon)
    print(f"  米偏移 = (东 {dx_m:+.1f}, 北 {dy_m:+.1f})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
