# -*- coding: utf-8 -*-
"""抓**任意校区**外框内的卫星影像（当作"地面真值"用）。

用途（2026-10-03）：
  · 补树 —— OSM 只标了很少的树（鼓楼仅 13 条行道树 + 7 片林地），
    但卫星影像上**哪块真有树冠**看得一清二楚。用户要求"效果按照卫星图为准"，
    所以先取影像，再从影像里提取树冠掩膜，最后只在掩膜内生成树。
  · 校验底图/地标位置。

输出（data/tile_check/，已 gitignore）：
    campusat_<campus>_z<z>.png / .json
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import static_basemap as sb            # noqa: E402
from frame_util import frame_of        # noqa: E402

OUT = HERE / "data" / "tile_check"


def main(campus: str = "gulou", z: int = 17, width: int = 2400):
    s, w, n, e = frame_of(campus)

    def prog(done, total):
        if done % 20 == 0 or done == total:
            print(f"   瓦片 {done}/{total}", flush=True)

    fi = sb.build_frame_image(s, w, n, e, target_width=width, zoom=z,
                              tile_url=sb.GOOGLE_SAT, tile_key="google_sat",
                              subdomains=sb.GOOGLE_SUBDOMAINS, progress=prog)
    OUT.mkdir(parents=True, exist_ok=True)
    png = OUT / f"campussat_{campus}_z{z}.png"
    png.write_bytes(fi.png)
    meta = {k: getattr(fi, k) for k in (
        "width", "height", "zoom", "scale_x", "scale_y", "origin_x", "origin_y",
        "south", "west", "north", "east")}
    png.with_suffix(".json").write_text(json.dumps(meta, indent=1), encoding="utf-8")
    print(f"-> {png}  ({fi.width}x{fi.height}, {len(fi.png)//1024} KB)")
    print(f"   外框 {s:.6f},{w:.6f} ~ {n:.6f},{e:.6f}")
    return 0


if __name__ == "__main__":
    a = sys.argv[1:]
    sys.exit(main(a[0] if a else "gulou",
                  int(a[1]) if len(a) > 1 else 17,
                  int(a[2]) if len(a) > 2 else 2400))
