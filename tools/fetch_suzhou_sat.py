# -*- coding: utf-8 -*-
"""抓苏州校区一带的**卫星影像**（配准用的"标准答案"）。

为什么配准要用卫星影像：南苏建模数据是**局部米制、没有地理参考**
（GLB 元信息只有 generator，交付说明也写明"not surveyed"）。
而卫星影像上，**水面/河道/庄里山/道路**是真实地物，可以拿来对齐 ——
比之前试过的"建筑不落在水面上"强得多（那个判据被稀疏水体主导，跑飞了）。

输出（都在 data/tile_check/，已 gitignore）：
    suzhusat_<src>_z<z>.png / .json   影像 + 坐标参数（FrameImage 的 meta）
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import static_basemap as sb            # noqa: E402
from vector_basemap import _load_json  # noqa: E402

OUT = HERE / "data" / "tile_check"

SOURCES = {
    # 谷歌卫星（WGS84，项目里 tools/verify_tile_crs.py 验过）
    "google": dict(tile_url=sb.GOOGLE_SAT, tile_key="google_sat",
                   subdomains="0123", tile_shift=(0.0, 0.0)),
    # 高德卫星（GCJ-02，需要平移；shift 由 --shift 传入）
    "amap": dict(tile_url="https://webst0{s}.is.autonavi.com/appmaptile"
                          "?style=6&x={x}&y={y}&z={z}",
                 tile_key="amap_sat", subdomains="1234", tile_shift=(0.0, 0.0)),
}


def campus_bbox(pad_m: float = 350.0):
    """以南苏校园多边形为中心，外扩 pad_m 米。"""
    v = _load_json(HERE / "data" / "raw" / "vector_suzhou.geojson")
    poly = None
    for f in v["features"]:
        pr = f.get("properties") or {}
        if pr.get("amenity") == "university" and "苏州" in str(pr.get("name", "")):
            poly = f
            break
    if poly is None:
        raise SystemExit("没找到 OSM 的南大苏州校区多边形")
    g = poly["geometry"]
    rings = g["coordinates"] if g["type"] == "Polygon" else [r for p in g["coordinates"] for r in p]
    pts = [p for ring in rings for p in ring]
    lons = [p[0] for p in pts]
    lats = [p[1] for p in pts]
    w, e, s, n = min(lons), max(lons), min(lats), max(lats)
    dlat = pad_m / 110540.0
    dlon = pad_m / (111320.0 * __import__("math").cos(__import__("math").radians((s + n) / 2)))
    return s - dlat, w - dlon, n + dlat, e + dlon


def main():
    src = sys.argv[1] if len(sys.argv) > 1 else "google"
    z = int(sys.argv[2]) if len(sys.argv) > 2 else 17
    shift = (0.0, 0.0)
    for a in sys.argv:
        if a.startswith("--shift="):
            dx, dy = a.split("=", 1)[1].split(",")
            shift = (float(dx), float(dy))
    cfg = dict(SOURCES[src])
    cfg["tile_shift"] = shift

    s, w, n, e = campus_bbox()
    print(f"外框: S{s:.5f} W{w:.5f} N{n:.5f} E{e:.5f}  "
          f"({(e-w)*111320*0.854/1000:.2f} x {(n-s)*110540/1000:.2f} km)")

    def prog(done, total):
        if done % 20 == 0 or done == total:
            print(f"   瓦片 {done}/{total}", flush=True)

    fi = sb.build_frame_image(s, w, n, e, target_width=2400, zoom=z,
                              subdomains=cfg["subdomains"],
                              tile_url=cfg["tile_url"], tile_key=cfg["tile_key"],
                              tile_shift=cfg["tile_shift"], progress=prog)
    tag = f"{src}_z{z}" + (f"_sh{shift[0]:.1f}_{shift[1]:.1f}" if shift != (0, 0) else "")
    png = OUT / f"suzhousat_{tag}.png"
    OUT.mkdir(parents=True, exist_ok=True)
    png.write_bytes(fi.png)
    meta = {k: getattr(fi, k) for k in (
        "width", "height", "zoom", "scale_x", "scale_y", "origin_x", "origin_y",
        "south", "west", "north", "east")}
    meta["source"] = src
    meta["tile_shift"] = list(shift)
    png.with_suffix(".json").write_text(json.dumps(meta, indent=1), encoding="utf-8")
    print(f"-> {png}  ({fi.width}x{fi.height}, {len(fi.png)//1024} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
