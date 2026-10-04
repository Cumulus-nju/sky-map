# -*- coding: utf-8 -*-
"""屋顶吸附几何自检。

检查三件事：
  1. 重构后 `_project()` 算出的投影参数，与**当初生成缓存底图时写进 meta 的**一致
     （证明把投影抽成公共函数没有改变任何数值）；
  2. `roof_shapes()` 的多边形数 == 建筑足迹数（顺序、筛选都对得上）；
  3. **顶点级**：屋顶多边形 == 足迹逐顶点平移 `VIEW×dz`（误差只该来自 round(0.1)）；
     **内部点级**：足迹内部的点平移后仍在该足迹内。

⚠ 为什么不用"顶点平均值"当质心：凹多边形（U 形/L 形宿舍楼很常见）的顶点平均
   **本来就可能落在多边形外** —— 拿它当判据会报一堆假警（第一版就是这么被骗的）。
   内部点必须用 shapely 的 `representative_point()`（保证在多边形内）。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import relief_basemap as rb                       # noqa: E402
from frame_util import frame_of                   # noqa: E402
from vector_basemap import _load_json, _rings_of  # noqa: E402


def inpoly(x, y, poly) -> bool:
    """射线法（与 JS `snapToFootprint` 里那份必须等价）。"""
    inside = False
    cnt = len(poly) // 2
    j = cnt - 1
    for i in range(cnt):
        xi, yi = poly[2 * i], poly[2 * i + 1]
        xj, yj = poly[2 * j], poly[2 * j + 1]
        if ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / (yj - yi) + xi):
            inside = not inside
        j = i
    return inside


def main(campus="gulou", width=2000):
    s, w, n, e = frame_of(campus)
    P = rb._project(s, w, n, e, width)
    fails = []

    print(f"=== [1] {campus} 重构等价性：_project() vs 缓存底图 ===")
    fi = rb.cached_relief_frame(campus, s, w, n, e, width=width)
    meta = {"width": fi.width, "height": fi.height, "origin_x": fi.origin_x,
            "origin_y": fi.origin_y, "scale_x": fi.scale_x, "scale_y": fi.scale_y}
    for k, v in [("width", P["W"]), ("height", P["H"]), ("origin_x", P["ox"]),
                 ("origin_y", P["oy"]), ("scale_x", P["scale_x"]),
                 ("scale_y", P["scale_y"])]:
        ok = abs(meta[k] - v) < 1e-9
        if not ok:
            fails.append(f"{k} meta={meta[k]} calc={v}")
        print(f"  {k:9s} meta={meta[k]!r:22s} calc={v!r:22s} {'OK' if ok else 'DIFF'}")

    print(f"\n=== [2] roof_shapes({campus}) ===")
    R = rb.roof_shapes(campus, s, w, n, e, width=width)
    print(f"  屋顶多边形数 {len(R['b'])}   "
          f"JSON 体积 {len(json.dumps(R, ensure_ascii=False))/1024:.1f} KB   view={R['v']}")

    blds = _load_json(rb.RAW / f"{campus}_buildings.geojson")
    items = []
    for idx, feat in enumerate(blds["features"]):
        props = feat.get("properties") or {}
        dz = rb._height_m(props, idx) * P["ppm"] * rb.EXAG
        if dz < 1.0:
            continue
        for ring in _rings_of(feat.get("geometry") or {}):
            pts = [P["proj"].pt(lo, la) for lo, la in ring]
            if len(pts) >= 3:
                items.append((max(q[1] for q in pts), dz, pts))
    items.sort(key=lambda t: t[0])
    if len(items) != len(R["b"]):
        fails.append(f"足迹数 {len(items)} != 多边形数 {len(R['b'])}")
    print(f"  足迹数 {len(items)}  {'与多边形数一致 OK' if len(items) == len(R['b']) else 'DIFF'}")

    print(f"\n=== [3a] 顶点级：屋顶 == 足迹平移 VIEW×dz ===")
    worst = 0.0
    for (_, _dz, pts), entry in zip(items, R["b"]):
        dz2, flat = entry
        for i, (x, y) in enumerate(pts):
            worst = max(worst,
                        abs(flat[2 * i] - (x + R["v"][0] * dz2)),
                        abs(flat[2 * i + 1] - (y + R["v"][1] * dz2)))
    ok = worst <= 0.101          # round(x,1) 与 round(dz,1) **各**贡献 0.05，合计 0.1
    if not ok:
        fails.append(f"顶点最大偏差 {worst:.4f} px")
    print(f"  最大偏差 {worst:.4f} px（round(x,1)+round(dz,1) 允许 ≤0.101）  {'OK' if ok else 'DIFF'}")

    print(f"\n=== [3b] 内部点级：足迹内部的点平移后仍在该足迹内 ===")
    try:
        from shapely.geometry import Point, Polygon
    except Exception as exc:
        print(f"  ⚠ 没有 shapely（{exc}），跳过这一项")
    else:
        # ⚠ 方向别搞反：图上画出来、用户点到的是**屋顶**，所以要从**屋顶多边形**
        #   内部取点，再减去位移，验证它落回**足迹**内。
        #   （前一版从"足迹内部点"减位移，当然会跑到足迹外面 —— 那是判据写反了，
        #     不是几何错了；差点因此去改没问题的实现。）
        outside, checked = 0, 0
        ring_outside = 0
        for (_, _dz, pts), entry in zip(items, R["b"]):
            dz2, flat = entry
            foot = Polygon([(x, y) for x, y in pts])
            roof = Polygon([(flat[2 * i], flat[2 * i + 1])
                            for i in range(len(flat) // 2)])
            if (not foot.is_valid) or foot.area <= 0 \
                    or (not roof.is_valid) or roof.area <= 0:
                continue
            checked += 1
            c = roof.representative_point()      # 屋顶内部（= 用户可能点到的位置）
            gx, gy = c.x - R["v"][0] * dz2, c.y - R["v"][1] * dz2
            if not foot.buffer(0.3).contains(Point(gx, gy)):
                outside += 1
            # 顺带用射线法复核（这份才是 JS 实际用的算法）
            fp = []
            for (px, py) in pts:
                fp += [px, py]
            if not inpoly(gx, gy, fp):
                ring_outside += 1
        print(f"  {checked} 栋有效多边形中：shapely 判外在 {outside} 栋，"
              f"射线法判外在 {ring_outside} 栋")
        # 射线法**没有容差**，而 round(x,1)/round(dz,1) 各有 0.05 px 误差：
        # 恰好压在边界上的代表点会被翻转判定。顶点级已经证明"屋顶 = 足迹平移"
        # （偏差 ≤0.098 px），所以这里允许极少数边界歧义，超过 0.5% 才当真。
        tol = max(1, int(round(checked * 0.005)))
        if outside > 0 or ring_outside > tol:
            fails.append(f"内部点吸附落在外：shapely {outside} / 射线法 {ring_outside}"
                         f"（射线法容差 {tol}）")
        elif ring_outside:
            print(f"  （射线法多出的 {ring_outside} 栋是边界数值歧义，在容差 {tol} 内）")

    print("\n" + ("✓ 全部通过" if not fails else "✗ 失败：" + "；".join(fails)))
    return 0 if not fails else 1


if __name__ == "__main__":
    a = sys.argv[1:]
    sys.exit(main(a[0] if a else "gulou"))
