# -*- coding: utf-8 -*-
"""列出"校园外的高楼"以及它们在**图上**的像素位置。

用途：用户说"鼓楼南侧还是有一个高楼挡住视野" —— 需要精确定位是哪一栋，
而不是凭肉眼在缩略图上猜。把 (高度, 图上 x/y, 是否已排除, 名称) 打出来，
和底图对照即可锁定。
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


def main(campus="gulou", min_h=55.0, width=2000):
    s, w, n, e = frame_of(campus)
    P = rb._project(s, w, n, e, width)
    proj = P["proj"]
    blds = _load_json(HERE / "data" / "raw" / f"{campus}_buildings.geojson")
    bounds = json.loads((HERE / "data" / "raw" / "campus_boundaries.json")
                        .read_text(encoding="utf-8"))[campus]
    skip = rb._skipped_indexes(blds, bounds)

    outer = bounds["geojson"]["coordinates"][0]
    s_lat = min(pt[1] for pt in outer)
    print(f"{campus}  底图 {P['W']}x{P['H']}   校园南界 lat={s_lat:.6f}")
    print(f"{'高度':>6} {'图上x':>6} {'图上y':>6} {'已删':>4} {'南侧外':>6}  名称")
    rows = []
    for i, f in enumerate(blds["features"]):
        p = f.get("properties") or {}
        h = rb._height_m(p, i)
        if h < min_h:
            continue
        rings = _rings_of(f.get("geometry") or {})
        if not rings:
            continue
        ring = rings[0]
        cx = sum(q[0] for q in ring) / len(ring)
        cy = sum(q[1] for q in ring) / len(ring)
        x, y = proj.pt(cx, cy)
        rows.append((h, x, y, i in skip, cy < s_lat,
                     p.get("name") or p.get("building") or "-"))
    rows.sort(key=lambda r: -r[0])
    for h, x, y, sk, south, nm in rows:
        print(f"{h:6.0f} {x:6.0f} {y:6.0f} {'是' if sk else '否':>4} "
              f"{'是' if south else '':>6}  {nm}")
    print(f"\n（共 {len(rows)} 栋 ≥{min_h:.0f} m；其中已排除 "
          f"{sum(1 for r in rows if r[3])} 栋）")
    return 0


if __name__ == "__main__":
    a = sys.argv[1:]
    sys.exit(main(a[0] if a else "gulou"))
