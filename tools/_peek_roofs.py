# -*- coding: utf-8 -*-
"""屋顶吸附几何的**图像验收**：把 `roof_shapes()` 导出的轮廓叠在真实底图上。

为什么必须看图
--------------
数字自检（tools/_check_roof_shapes.py）只能证明"屋顶 = 足迹平移"——那本来就是
导出时的定义，**属于同义反复**。它证明不了**足迹本身落在正确的建筑上**。

尤其苏州：足迹是**局部米制坐标**经 `geo.to_lonlat()` 换过来的，坐标约定一旦搞错
（比如把北当成南、或者忘了取负），数字自检照样全绿，但轮廓会整体飞出图外或错位。
只有把它叠在底图上看一眼才能立刻发现。

输出：data/tile_check/peekroofs_<campus>.jpg
    红 = 屋顶多边形（`roof_shapes` 导出，应贴合**画出来的屋顶**）
    蓝 = 吸附目标（屋顶反向平移后的足迹，应贴合**楼基**）

用法: python tools/_peek_roofs.py [campus ...]
"""
from __future__ import annotations

import io
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import static_basemap as sb                 # noqa: E402
from frame_util import frame_of             # noqa: E402

TILE = HERE / "data" / "tile_check"


def main(*campuses):
    from PIL import Image, ImageDraw

    for campus in campuses:
        src = sb.source_of(campus)
        if src == "relief":
            import relief_basemap as R
        elif src == "glb":
            import glb_relief as R
        else:
            print(f"{campus}: 底图源是 {src}，没有屋顶吸附，跳过")
            continue

        s, w, n, e = frame_of(campus)
        fi = sb.frame_image_for(campus, s, w, n, e, target_width=2000)
        roofs = R.roof_shapes(campus, s, w, n, e, width=2000)

        img = Image.open(io.BytesIO(fi.png)).convert("RGB")
        ov = img.copy()
        d = ImageDraw.Draw(ov)
        vx, vy = roofs["v"]
        for dz, flat in roofs["b"]:
            pts = [(flat[2 * i], flat[2 * i + 1]) for i in range(len(flat) // 2)]
            d.polygon(pts, outline=(230, 30, 30))                        # 屋顶
            d.polygon([(x - vx * dz, y - vy * dz) for x, y in pts],
                      outline=(30, 110, 235))                            # 足迹 = 吸附目标
        out = TILE / f"peekroofs_{campus}.jpg"
        ov.save(out, quality=88)
        print(f"{campus} ({src}): {len(roofs['b'])} 个屋顶多边形 -> {out}")
    return 0


if __name__ == "__main__":
    a = sys.argv[1:]
    sys.exit(main(*(a or ["gulou", "xianlin", "suzhou"])))
