# -*- coding: utf-8 -*-
"""取校园周边的**矢量地图要素**（路网/水体/绿地/校园边界），供自绘底图使用。

为什么要自己取数据、自己画（2026-10-02 用户提议）
------------------------------------------------
底图是**静态图**，用别人的瓦片等于被它的分辨率锁死（1400px 就 1400px，放大就糊）。
而 OSM 的原始数据是**矢量**的：想要 3000px 就画 3000px，字和线永远清晰；
配色、层级、留白也能完全按审美来。这是"截图"和"自己画"的本质区别。

取哪些层（够画一张干净地图即可，不求全）：
    highway    道路（按等级画不同粗细：主干道 / 次干道 / 支路 / 步行道）
    waterway   河流、沟渠
    natural/water、waterway=riverbank  水面
    landuse    绿地（park/grass/forest/meadow/pitch）、学校、商业区
    place      地名点位（画注记用）
    boundary   校园边界（用于画校园轮廓）

⚠️ 只**本地取一次并存成 GeoJSON**（`data/raw/vector_<campus>.geojson`）。
   运行时不联网 —— 云端容器重启也不会因此变慢或失败。
   OSM 数据 © OpenStreetMap contributors，ODbL 授权（署名会画在图上）。

用法：python tools/fetch_vector_layers.py [campus ...]
"""
from __future__ import annotations

import json
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

OUT_DIR = HERE / "data" / "raw"
UA = {"User-Agent": "sky-map-campus-contest/1.0 (Nanjing University campus sky photo contest; contact: organizer)"}

# 多个 Overpass 镜像，逐个试（主站经常排队或 429）
ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.osm.jp/api/interpreter",
]

# 外框四周再多取一点，避免边缘出现"断头路"
PAD_DEG = 0.004


def build_query(s: float, w: float, n: float, e: float) -> str:
    bbox = f"{s},{w},{n},{e}"
    return f"""
[out:json][timeout:120];
(
  way["highway"]({bbox});
  way["waterway"]({bbox});
  way["natural"="water"]({bbox});
  way["waterway"="riverbank"]({bbox});
  way["landuse"]({bbox});
  way["leisure"]({bbox});
  way["place"]({bbox});
  node["place"]({bbox});
  way["boundary"="administrative"]["amenity"="university"]({bbox});
  way["amenity"="university"]({bbox});
);
out geom;
"""


def fetch(query: str) -> dict | None:
    data = urllib.parse.urlencode({"data": query}).encode("utf-8")
    for ep in ENDPOINTS:
        for attempt in range(2):
            try:
                req = urllib.request.Request(ep, data=data, headers=UA)
                raw = urllib.request.urlopen(req, timeout=180).read()
                return json.loads(raw.decode("utf-8"))
            except Exception as exc:
                print(f"    {ep.split('/')[2]} 第{attempt+1}次失败：{type(exc).__name__}: {str(exc)[:70]}")
                time.sleep(2 + attempt * 3)
    return None


def to_geojson(elements: list) -> dict:
    """Overpass 的 out geom 结果 -> GeoJSON FeatureCollection。"""
    feats = []
    for el in elements:
        tags = el.get("tags") or {}
        if el["type"] == "node":
            geom = {"type": "Point", "coordinates": [el["lon"], el["lat"]]}
        elif el["type"] == "way" and el.get("geometry"):
            coords = [[p["lon"], p["lat"]] for p in el["geometry"]]
            if len(coords) < 2:
                continue
            geom = {"type": "LineString", "coordinates": coords}
        else:
            continue
        feats.append({"type": "Feature", "properties": tags, "geometry": geom})
    return {"type": "FeatureCollection", "features": feats}


def main() -> int:
    from frame_util import frame_of

    keys = sys.argv[1:] or ["gulou", "xianlin", "suzhou"]
    for key in keys:
        s, w, n, e = frame_of(key)
        q = build_query(s - PAD_DEG, w - PAD_DEG, n + PAD_DEG, e + PAD_DEG)
        print(f"== {key}  bbox=({s - PAD_DEG:.5f},{w - PAD_DEG:.5f},{n + PAD_DEG:.5f},{e + PAD_DEG:.5f})")
        res = fetch(q)
        if not res:
            print(f"   ✗ {key} 取数失败（稍后重试）")
            continue
        gj = to_geojson(res.get("elements", []))
        # 按图层统计一下，便于判断数据是否够用
        from collections import Counter
        c = Counter()
        for f in gj["features"]:
            p = f["properties"]
            if "highway" in p:
                c["highway"] += 1
            elif "waterway" in p or p.get("natural") == "water" or "water" in p:
                c["water"] += 1
            elif "landuse" in p or "leisure" in p:
                c["landuse"] += 1
            elif "place" in p:
                c["place"] += 1
            elif "amenity" in p or "boundary" in p:
                c["campus"] += 1
            else:
                c["other"] += 1
        out = OUT_DIR / f"vector_{key}.geojson"
        out.write_text(json.dumps(gj, ensure_ascii=False), encoding="utf-8")
        print(f"   ✓ {len(gj['features'])} 个要素  {dict(c)}")
        print(f"     -> {out.name}  ({out.stat().st_size // 1024} KB)")
        time.sleep(1.5)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
