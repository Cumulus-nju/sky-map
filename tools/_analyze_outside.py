# -*- coding: utf-8 -*-
"""列出**校园范围之外**的建筑（按高度降序），用于挑出"遮住校园内部的高楼"。

背景（用户 2026-10-04 反馈）：鼓楼底图南边有两三栋**非学校**的高楼，
在轴测图里向上挤出，正好压住校园内部的建筑。要把它们去掉。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import relief_basemap as rb                       # noqa: E402
from vector_basemap import _load_json, _rings_of  # noqa: E402


def main(campus="gulou", top=14):
    from shapely.geometry import Point, shape

    b = json.loads((HERE / "data" / "raw" / "campus_boundaries.json")
                   .read_text(encoding="utf-8"))
    poly = shape(b[campus]["geojson"])
    print(f"校园范围 bounds = {[round(v, 6) for v in poly.bounds]}")
    print(f"纬度 {poly.bounds[1]:.6f} ~ {poly.bounds[3]:.6f}（南 ~ 北）")
    print(f"经度 {poly.bounds[0]:.6f} ~ {poly.bounds[2]:.6f}（西 ~ 东）\n")

    blds = _load_json(HERE / "data" / "raw" / f"{campus}_buildings.geojson")
    rows = []
    for i, f in enumerate(blds["features"]):
        props = f.get("properties") or {}
        rings = _rings_of(f.get("geometry") or {})
        if not rings:
            continue
        ring = rings[0]
        cx = sum(p[0] for p in ring) / len(ring)
        cy = sum(p[1] for p in ring) / len(ring)
        rows.append({
            "idx": i, "h": rb._height_m(props, i), "lat": cy, "lon": cx,
            "name": props.get("name"), "type": props.get("building"),
            "inside": poly.contains(Point(cx, cy)), "nv": len(ring),
            "id": props.get("id"),
        })
    ins = [r for r in rows if r["inside"]]
    out = [r for r in rows if not r["inside"]]
    print(f"总建筑 {len(rows)}  校园内 {len(ins)}  校园外 {len(out)}\n")

    print(f"=== 校园外建筑（按高度降序，前 {top}）===")
    print(f"{'高度m':>7} {'纬度':>9} {'经度':>10} {'顶点':>4}  {'南北':>4}  名称/类型")
    for r in sorted(out, key=lambda r: -r["h"])[:top]:
        side = "南" if r["lat"] < min(p[1] for p in poly.exterior.coords) else " "
        print(f"{r['h']:7.1f} {r['lat']:9.5f} {r['lon']:10.5f} {r['nv']:4d}  {side:>4}  "
              f"{r['name'] or r['type'] or '-'}   [{r['id']}]")

    s_lat = min(p[1] for p in poly.exterior.coords)
    south = [r for r in out if r["lat"] < s_lat]
    print(f"\n=== 校园**南侧**（lat < {s_lat:.6f}）的建筑，共 {len(south)} 栋 ===")
    for r in sorted(south, key=lambda r: -r["h"])[:12]:
        print(f"{r['h']:7.1f} m  {r['lat']:.6f},{r['lon']:.6f}  "
              f"{r['name'] or r['type'] or '-'}  [{r['id']}]")
    return 0


if __name__ == "__main__":
    a = sys.argv[1:]
    sys.exit(main(a[0] if a else "gulou"))
