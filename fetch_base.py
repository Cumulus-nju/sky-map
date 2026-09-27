"""抓取南京大学三校区 OSM 建筑轮廓、步行路网与候选机位 POI，作为打卡点地图底图。

用法: python -u fetch_base.py
输出: data/raw/<campus>_buildings.geojson
      data/raw/<campus>_walk.graphml   (可选，失败不致命)
      data/raw/<campus>_poi.csv
      data/raw/fetch_summary.json
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import osmnx as ox
import shapely
from shapely.geometry import box

HERE = Path(__file__).resolve().parent
RAW = HERE / "data" / "raw"
RAW.mkdir(parents=True, exist_ok=True)

ox.settings.log_console = False
ox.settings.use_cache = True
ox.settings.requests_timeout = 240
# 注：不要用 ox.features_from_bbox —— 它在内部投影时会对 (S,W,N,E)
# 元组算出 NaN 面积并抛 "cannot convert float NaN to integer"。
# 直接用 WGS84 polygon 传入 features_from_polygon / graph_from_polygon 即可绕开。

QUERIES = {
    "gulou": "南京大学鼓楼校区, 南京",
    "xianlin": "南京大学仙林校区, 南京",
    "suzhou": "南京大学苏州校区, 苏州",
}

# 所有写出到 GeoJSON 的列都转成字符串，避免 pandas 整型列里的 NaN
# 在 fiona/GDAL 序列化时报 "cannot convert float NaN to integer"。
STR_COLS = ("name", "building", "building:levels", "height", "leisure", "tourism", "amenity", "historic")


def bbox_of(gdf) -> tuple[float, float, float, float]:
    """返回 (S, W, N, E)。"""
    minx, miny, maxx, maxy = gdf.geometry.iloc[0].bounds
    pad = 0.0012
    return (miny - pad, minx - pad, maxy + pad, maxx + pad)


def repair(gdf):
    """清理几何：去空、多部件拆平、修复无效环、丢弃退化面。"""
    gdf = gdf[gdf.geometry.notna()].copy()
    gdf = gdf[~gdf.geometry.is_empty]
    gdf = gdf[gdf.geometry.geom_type.isin(["Polygon", "MultiPolygon"])]
    if gdf.empty:
        return gdf
    gdf["geometry"] = shapely.make_valid(gdf.geometry.values)
    gdf = gdf[gdf.geometry.notna() & ~gdf.geometry.is_empty]
    gdf = gdf[gdf.geometry.geom_type.isin(["Polygon", "MultiPolygon"])]
    if gdf.empty:
        return gdf
    gdf["geometry"] = shapely.buffer(gdf.geometry.values, 0)
    gdf = gdf[gdf.geometry.notna() & ~gdf.geometry.is_empty]
    gdf["geometry"] = shapely.force_2d(gdf.geometry.values)
    gdf = gdf[shapely.area(gdf.geometry.values) > 1e-10]
    return gdf.copy()


def stringify(gdf):
    gdf = gdf.copy()
    for col in gdf.columns:
        if col == "geometry":
            continue
        gdf[col] = gdf[col].apply(lambda v: "" if v is None or v != v else str(v))
    return gdf


def main() -> int:
    summary: dict[str, dict] = {}
    for key, query in QUERIES.items():
        print(f"\n=== {key} :: {query} ===", flush=True)
        summary[key] = {}
        try:
            b = bbox_of(ox.geocode_to_gdf(query))
        except Exception as exc:
            print(f"  geocode FAIL {type(exc).__name__}: {exc}", flush=True)
            continue
        print(f"  bbox(S,W,N,E) = {tuple(round(v, 6) for v in b)}", flush=True)
        poly = box(b[1], b[0], b[3], b[2])  # (W, S, E, N)

        # ---------- buildings ----------
        try:
            bld = ox.features_from_polygon(poly, tags={"building": True})
            keep = [c for c in STR_COLS if c in bld.columns]
            bld = bld[keep + ["geometry"]]
            bld = repair(bld)
            bld = stringify(bld)
            bld["campus"] = key
            out = RAW / f"{key}_buildings.geojson"
            bld.to_file(out, driver="GeoJSON", encoding="utf-8")
            named = int((bld["name"] != "").sum()) if "name" in bld.columns else 0
            n = len(bld)
            print(f"  buildings: {n} (named {named}) -> {out.name}", flush=True)
            summary[key]["buildings"] = n
            summary[key]["named_buildings"] = named
        except Exception as exc:
            print(f"  buildings FAIL {type(exc).__name__}: {exc}", flush=True)

        # ---------- walk network (best effort) ----------
        try:
            graph = ox.graph_from_polygon(poly, network_type="walk", simplify=True)
            path = RAW / f"{key}_walk.graphml"
            ox.save_graphml(graph, path)
            print(f"  walk: {len(graph.edges)} edges -> {path.name}", flush=True)
            summary[key]["walk_edges"] = int(len(graph.edges))
        except Exception as exc:
            print(f"  walk SKIP {type(exc).__name__}: {str(exc)[:100]}", flush=True)
            summary[key]["walk_edges"] = 0

        # ---------- candidate viewpoint POI ----------
        try:
            tags = {
                "leisure": ["park", "garden", "pitch", "common"],
                "tourism": ["viewpoint", "attraction", "museum"],
                "amenity": ["library", "theatre", "arts_centre", "place_of_worship"],
                "historic": True,
                "natural": ["water", "peak", "tree_row"],
            }
            poi = ox.features_from_polygon(poly, tags=tags)
            poi = poi[poi.geometry.notna()].copy()
            if not poi.empty:
                poi["lon"] = poi.geometry.representative_point().x
                poi["lat"] = poi.geometry.representative_point().y
                keep = [c for c in ("name", "leisure", "tourism", "amenity", "historic", "natural") if c in poi.columns]
                poi = poi[keep + ["lon", "lat"]].reset_index(drop=True)
                for col in keep:
                    poi[col] = poi[col].apply(lambda v: "" if v is None or v != v else str(v))
                path = RAW / f"{key}_poi.csv"
                poi.to_csv(path, index=False, encoding="utf-8-sig")
                print(f"  poi: {len(poi)} -> {path.name}", flush=True)
                summary[key]["poi"] = len(poi)
            else:
                print("  poi: 0", flush=True)
                summary[key]["poi"] = 0
        except Exception as exc:
            print(f"  poi FAIL {type(exc).__name__}: {str(exc)[:100]}", flush=True)
            summary[key]["poi"] = 0

    (RAW / "fetch_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print("\n=== SUMMARY ===")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
