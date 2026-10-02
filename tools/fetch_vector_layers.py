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
    """构造 Overpass 查询。

    ⚠️ **必须同时取 way 和 relation**（2026-10-03 踩的坑）：
    OSM 里**大面积**的林地/水面/公园常被画成 **multipolygon 关系（relation）**，
    而不是单个闭合 way。只查 way 的话，像仙林南雍山那一整片森林会**完全取不到**，
    自绘出来就是一片空白（对照 OSM 官方渲染图一眼就能看出差在哪）。
    """
    bbox = f"{s},{w},{n},{e}"
    keys = ('["highway"]', '["waterway"]', '["natural"]', '["landuse"]',
            '["leisure"]', '["place"]', '["amenity"="university"]',
            '["boundary"="administrative"]')
    parts = []
    for k in keys:
        parts.append(f'  way{k}({bbox});')
        parts.append(f'  relation{k}({bbox});')
    parts.append(f'  node["place"]({bbox});')
    body = "\n".join(parts)
    return f"""
[out:json][timeout:180];
(
{body}
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


# 哪些标签意味着"这是个面"（闭合 way 要按面存，否则渲染时会被当折线丢掉）
_AREA_KEYS = ("landuse", "leisure", "amenity", "building", "area", "man_made",
              "public_transport")
_AREA_NATURAL = {"water", "wood", "scrub", "grassland", "wetland", "bare_rock",
                 "sand", "beach", "heath", "fell"}
_AREA_WATERWAY = {"riverbank", "dock"}


def _rings_from_relation(el: dict) -> list:
    """把 multipolygon 关系拼成环列表（outer 环，内环暂按外环处理）。

    `out geom` 的关系结果长这样：
        members: [{type:'way', role:'outer', geometry:[{lat,lon},...]}, ...]
    多个 outer 成员要各自成为独立环（不能首尾相接强行串起来）。
    """
    rings = []
    for m in el.get("members") or []:
        if m.get("type") != "way":
            continue
        role = m.get("role") or "outer"
        if role not in ("outer", "exclave", ""):
            continue
        geo = m.get("geometry") or []
        coords = [[p["lon"], p["lat"]] for p in geo]
        if len(coords) >= 4:
            if coords[0] != coords[-1]:
                coords.append(coords[0])
            rings.append(coords)
    return rings


def to_geojson(elements: list) -> dict:
    """Overpass 的 out geom 结果 -> GeoJSON FeatureCollection。

    两个必须处理的坑（都踩过）：
    ① `out geom` 对**闭合的 way**（绿地、水面、球场）同样返回 geometry 点列，
       一律当 LineString 存的话，面状图层全空 ⇒ 只剩建筑。
       ⇒ 闭合 + 带面状标签的 way 要存成 Polygon。
    ② **大面积的林地/水面常是 multipolygon 关系**，只查 way 会整个漏掉
       （仙林南雍山那片森林就是这么丢的）⇒ 关系要拼成 Polygon。
    """
    feats = []
    for el in elements:
        tags = el.get("tags") or {}
        geom = None
        if el["type"] == "node":
            geom = {"type": "Point", "coordinates": [el["lon"], el["lat"]]}
        elif el["type"] == "way" and el.get("geometry"):
            coords = [[p["lon"], p["lat"]] for p in el["geometry"]]
            if len(coords) < 2:
                continue
            closed = len(coords) >= 4 and coords[0] == coords[-1]
            looks_area = (
                any(tags.get(k) for k in _AREA_KEYS)
                or tags.get("natural") in _AREA_NATURAL
                or tags.get("waterway") in _AREA_WATERWAY
            )
            geom = ({"type": "Polygon", "coordinates": [coords]}
                    if (closed and looks_area)
                    else {"type": "LineString", "coordinates": coords})
        elif el["type"] == "relation":
            rings = _rings_from_relation(el)
            if rings:
                geom = ({"type": "Polygon", "coordinates": [rings[0]]}
                        if len(rings) == 1
                        else {"type": "MultiPolygon", "coordinates": [[r] for r in rings]})
        if geom is None:
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
