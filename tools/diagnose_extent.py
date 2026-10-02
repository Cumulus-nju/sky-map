# -*- coding: utf-8 -*-
"""诊断"初始地图范围偏了"：把几个可能的"中心"标出来对比。

用户反馈初始显示的范围偏了。可能的原因有三类，必须分清：
  A. 跑的是**旧框**（旧版 campus_config 的 bbox，1105×1252m），比新框小一圈；
  B. 新框（frame）相对**校园中心** cfg.center 偏了；
  C. 新框相对**建筑范围中心**偏了（数据上已证明是均匀 150m 缓冲，不该偏）。

本脚本把 cfg.center / 框中心 / 建筑范围中心 / 旧 bbox 范围都画在一张图上，
并打印各自相差多少米，一眼看出是哪一类。

用法：python tools/diagnose_extent.py [campus]
"""
from __future__ import annotations

import io
import json
import math
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


def building_extent(campus: str):
    p = HERE / "data" / "raw" / f"{campus}_buildings.geojson"
    d = json.loads(p.read_text(encoding="utf-8"))
    lons, lats = [], []
    for f in d.get("features", []):
        g = f.get("geometry")
        if not g:
            continue
        c = g["coordinates"]
        rings = [c[0]] if g["type"] == "Polygon" else [q[0] for q in c]
        for r in rings:
            for lon, lat in r:
                lons.append(lon)
                lats.append(lat)
    return min(lons), max(lons), min(lats), max(lats)


def main() -> int:
    campus = sys.argv[1] if len(sys.argv) > 1 else "gulou"
    from PIL import Image, ImageDraw
    from campus_config import campus as get_campus

    cfg = get_campus(campus)
    fs, fw, fn, fe = frame_of(campus)              # frame: (south, west, north, east)
    bs, bw, bn, be = cfg.bbox                      # bbox 同顺序
    # ⚠ building_extent 返回的是 (min_lon, max_lon, min_lat, max_lat)，
    #   顺序和 bbox 的 (S, W, N, E) **不同**，必须分开命名，否则经纬度会串位
    #   （第一版就是这么写错的，导致算出 -800 万米）。
    b_min_lon, b_max_lon, b_min_lat, b_max_lat = building_extent(campus)
    gs, gw, gn, ge = b_min_lat, b_min_lon, b_max_lat, b_max_lon

    lat = (fs + fn) / 2
    mLat = 111320.0
    mLon = 111320.0 * math.cos(math.radians(lat))

    def m(a, b, is_lat=False):
        return (a - b) * (mLat if is_lat else mLon)

    print(f"=== {cfg.short} ===")
    print(f"  框 frame      {fw:.6f},{fs:.6f} ~ {fe:.6f},{fn:.6f}  "
          f"({(fe-fw)*mLon:.0f} x {(fn-fs)*mLat:.0f} m)")
    print(f"  旧 bbox       {bw:.6f},{bs:.6f} ~ {be:.6f},{bn:.6f}  "
          f"({(be-bw)*mLon:.0f} x {(bn-bs)*mLat:.0f} m)")
    print(f"  建筑实际范围   {gw:.6f},{gs:.6f} ~ {ge:.6f},{gn:.6f}  "
          f"({(ge-gw)*mLon:.0f} x {(gn-gs)*mLat:.0f} m)")
    fc = ((fw + fe) / 2, (fs + fn) / 2)
    bc = ((bw + be) / 2, (bs + bn) / 2)
    gc = ((gw + ge) / 2, (gs + gn) / 2)
    print("\n  中心对比（相对框中心，正=偏东/偏北）：")
    print(f"    校园配置中心 cfg.center  东 {m(cfg.center[1], fc[0]):+7.0f} m  "
          f"北 {m(cfg.center[0], fc[1], True):+7.0f} m")
    print(f"    旧 bbox 中心             东 {m(bc[0], fc[0]):+7.0f} m  "
          f"北 {m(bc[1], fc[1], True):+7.0f} m")
    print(f"    建筑范围中心             东 {m(gc[0], fc[0]):+7.0f} m  "
          f"北 {m(gc[1], fc[1], True):+7.0f} m")
    print(f"    旧 bbox 相对建筑范围      东 {m(bc[0], gc[0]):+7.0f} m  "
          f"北 {m(bc[1], gc[1], True):+7.0f} m  ← 若这里明显不为 0，说明旧框偏")

    # 画图：框内底图 + 各种中心标记
    fi = sb.cached_frame_image(campus, fs, fw, fn, fe, target_width=1400)
    im = Image.open(io.BytesIO(fi.png)).convert("RGB")
    d = ImageDraw.Draw(im, "RGBA")

    def mark(lat_, lon_, label, color, arm=26):
        if not (fs <= lat_ <= fn and fw <= lon_ <= fe):
            return False
        x, y = fi.latlng_to_px(lat_, lon_)
        d.line([x - arm, y, x + arm, y], fill=color, width=4)
        d.line([x, y - arm, x, y + arm], fill=color, width=4)
        f = font(22)
        for dx in (-2, -1, 0, 1, 2):
            for dy in (-2, -1, 0, 1, 2):
                d.text((x + 16 + dx, y - 14 + dy), label, font=f, fill=(0, 0, 0, 210))
        d.text((x + 16, y - 14), label, font=f, fill=color)
        return True

    # 旧 bbox 边界用虚线矩形标出（用来判断"是不是在跑旧框"）
    p1 = fi.latlng_to_px(bn, bw)
    p2 = fi.latlng_to_px(bs, be)
    for i in range(0, int(max(abs(p2[0] - p1[0]), abs(p2[1] - p1[1]))), 22):
        pass
    d.rectangle([p1[0], p1[1], p2[0], p2[1]], outline=(255, 0, 255, 255), width=3)

    mark(*cfg.center, "cfg.center 校园中心", (255, 40, 40, 255))
    mark((fs + fn) / 2, (fw + fe) / 2, "框几何中心", (40, 90, 255, 255))
    mark((gs + gn) / 2, (gw + ge) / 2, "建筑范围中心", (0, 160, 60, 255))

    out = HERE / "data" / "tile_check" / f"extent_check_{campus}.png"
    im.save(out)
    print(f"\n  已出图 {out}")
    print("  洋红实线框 = 旧 bbox 的范围（若初始显示就是它，说明跑的是旧模块）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
