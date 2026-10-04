# -*- coding: utf-8 -*-
"""把 OSM 路网/水面叠到卫星影像上，验证影像的坐标系（WGS84 vs GCJ-02）。

为什么必须先验：高德是 GCJ-02，与 WGS84 差 **490~640 m**（约 5 个街区）。
如果弄反了，后面所有配准都会在一个错的位置上"看起来很合理"。
OSM 是 WGS84；**路网能严丝合缝压在影像的道路上**，才说明影像也是 WGS84。
"""
from __future__ import annotations

import io
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import static_basemap as sb          # noqa: E402
from vector_basemap import Projector, _load_json, _lines_of, _area_rings  # noqa: E402

OUT = HERE / "data" / "tile_check"


def load_frame(tag: str):
    meta = json.loads((OUT / f"suzhousat_{tag}.json").read_text(encoding="utf-8"))
    png = (OUT / f"suzhousat_{tag}.png").read_bytes()
    keep = ("width", "height", "zoom", "scale_x", "scale_y", "origin_x", "origin_y",
            "south", "west", "north", "east")
    return sb.FrameImage(png=png, **{k: meta[k] for k in keep})


def main(tag="google_z17", shift_m=(0.0, 0.0)):
    from PIL import Image, ImageDraw
    fi = load_frame(tag)
    img = Image.open(io.BytesIO(fi.png)).convert("RGB")
    # 用 FrameImage 自带的换算（与点选完全同一套），保证叠加位置可信
    proj = Projector(fi.south, fi.west, fi.north, fi.east, fi.width, fi.height)
    d = ImageDraw.Draw(img, "RGBA")

    vec = _load_json(HERE / "data" / "raw" / "vector_suzhou.geojson")
    n_road = n_water = 0
    for f in vec["features"]:
        pr = f.get("properties") or {}
        if pr.get("highway"):
            for line in _lines_of(f.get("geometry") or {}):
                pts = [proj.pt(lo, la) for lo, la in line]
                if len(pts) >= 2:
                    d.line(pts, fill=(255, 0, 200, 210), width=4, joint="curve")
                    n_road += 1
        if pr.get("waterway") or pr.get("natural") == "water":
            for ring in _area_rings(f.get("geometry") or {}, pr):
                pts = [proj.pt(lo, la) for lo, la in ring]
                if len(pts) >= 3:
                    d.polygon(pts, outline=(0, 220, 255, 230))
                    n_water += 1
            for line in _lines_of(f.get("geometry") or {}):
                pts = [proj.pt(lo, la) for lo, la in line]
                if len(pts) >= 2:
                    d.line(pts, fill=(0, 220, 255, 230), width=3, joint="curve")
                    n_water += 1
    print(f"叠加：道路 {n_road} 条 / 水 {n_water} 处")
    img.resize((img.width // 2, img.height // 2), Image.LANCZOS).save(
        OUT / f"crscheck_{tag}.jpg", quality=86)
    print("->", OUT / f"crscheck_{tag}.jpg")


if __name__ == "__main__":
    main(*sys.argv[1:])
