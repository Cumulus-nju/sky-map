# -*- coding: utf-8 -*-
"""把桌面「南苏建模数据」里的 site_plan_rect.geojson 导入仓库（局部米制）。

为什么要导入：后续渲染/配准都该在**仓库内**可复现，不能依赖桌面路径。
保留 `height` / `num_floors` / `building_type` / `roof_type`，因为立体渲染要用真高度。

输出：data/raw/suzhou_plan_buildings.geojson
    properties: height(米) / num_floors / building_type / roof_type / name
    coordinates: **局部米制**（X 向东、Y 向北），原点同 GLB
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

SRC = (Path.home() / "Desktop" / "南苏建模数据_备用" / "南苏CFD建模文件" /
       "建模数据" / "site_plan_rect.geojson")
DST = Path(__file__).resolve().parent.parent / "data" / "raw" / "suzhou_plan_buildings.geojson"


def main():
    src = json.loads(SRC.read_text(encoding="utf-8"))
    feats = []
    for f in src["features"]:
        p = f.get("properties") or {}
        feats.append({
            "type": "Feature",
            "geometry": f["geometry"],
            "properties": {
                "name": p.get("name"),
                "height": p.get("height"),
                "num_floors": p.get("num_floors"),
                "building_type": p.get("building_type"),
                "roof_type": p.get("roof_type"),
            },
        })
    out = {
        "type": "FeatureCollection",
        "name": "nju-suzhou-campus-buildings-localmetres",
        "crs": {"type": "name",
                "properties": {"name": "local metric: X east, Y north (origin = model origin)"}},
        "source": "南京大学资产管理处《苏州校区平面图》2024-03，经精确矩形分解（面积误差 -5.2%）",
        "features": feats,
    }
    DST.parent.mkdir(parents=True, exist_ok=True)
    DST.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    hs = [f["properties"]["height"] for f in feats if f["properties"].get("height")]
    print(f"导入 {len(feats)} 个建筑足迹 -> {DST} ({DST.stat().st_size//1024} KB)")
    print(f"  高度：有 {len(hs)} 个，{min(hs):.1f} ~ {max(hs):.1f} m，中位 {sorted(hs)[len(hs)//2]:.1f} m")
    return 0


if __name__ == "__main__":
    sys.exit(main())
