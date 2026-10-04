# -*- coding: utf-8 -*-
"""配准**放大校验**：在 1:1 像素下看模型轮廓是否严丝合缝套住影像里的屋顶。

整体小图看不出 10 m 的偏差（z17 上 10 m ≈ 12 px），必须切局部放大看。
输出 data/tile_check/georef_zoom_*.jpg
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent.parent
OUT = HERE / "data" / "tile_check"
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "tools"))

from register_suzhou import Geo, load_sat      # noqa: E402
from vector_basemap import _load_json          # noqa: E402

# 放大窗（全分辨率像素，中心 ± 半径）
WINDOWS = {
    "西区建筑群": (470, 800, 300),
    "东区建筑群": (1180, 500, 300),
    "体育场跑道": (1060, 760, 260),
    "山体与书院": (900, 620, 320),
}


def main(tag="google_z17", zoom=2):
    from PIL import Image, ImageDraw
    rgb, meta = load_sat(tag)
    H, W = rgb.shape[:2]
    geo_d = json.loads((HERE / "data" / "suzhou_georef.json").read_text(encoding="utf-8"))
    g = Geo(geo_d)
    plan = _load_json(HERE / "data" / "raw" / "suzhou_plan_buildings.geojson")
    npz = np.load(HERE / "data" / "raw" / "suzhou_glb.npz")

    def to_px(X, Y):
        lon, lat = g.to_lonlat(np.array([X]), np.array([Y]))
        return ((lon[0] - meta["west"]) / (meta["east"] - meta["west"]) * W,
                (meta["north"] - lat[0]) / (meta["north"] - meta["south"]) * H)

    full = Image.fromarray(rgb.astype(np.uint8)).convert("RGB")
    d = ImageDraw.Draw(full, "RGBA")
    for f in plan["features"]:
        for ring in f["geometry"]["coordinates"]:
            pts = [to_px(x, y) for x, y in ring]
            if len(pts) >= 3:
                d.polygon(pts, outline=(255, 0, 200, 255))
    # OSM 校园边界（独立的第三方真值，用来交叉检查）
    vec = _load_json(HERE / "data" / "raw" / "vector_suzhou.geojson")
    for f in vec["features"]:
        pr = f.get("properties") or {}
        if pr.get("amenity") == "university":
            gj = f["geometry"]
            rings = gj["coordinates"] if gj["type"] == "Polygon" else [r for p in gj["coordinates"] for r in p]
            for ring in rings:
                pts = [((lo - meta["west"]) / (meta["east"] - meta["west"]) * W,
                        (meta["north"] - la) / (meta["north"] - meta["south"]) * H) for lo, la in ring]
                d.line(pts + [pts[0]], fill=(255, 255, 0, 255), width=3)
    for nm in ("Landscape / water",):
        V, F = npz[f"{nm}_v"], npz[f"{nm}_f"]
        for tri in F[::6]:
            pts = [to_px(float(V[i, 0]), float(-V[i, 2])) for i in tri]
            d.polygon(pts, outline=(0, 220, 255, 220))

    for name, (cx, cy, r) in WINDOWS.items():
        box = (max(0, cx - r), max(0, cy - r), min(W, cx + r), min(H, cy + r))
        crop = full.crop(box)
        crop = crop.resize((crop.width * zoom, crop.height * zoom), Image.LANCZOS)
        p = OUT / f"georef_zoom_{name}.jpg"
        crop.save(p, quality=90)
        print("->", p, crop.size)


if __name__ == "__main__":
    main(*sys.argv[1:])
