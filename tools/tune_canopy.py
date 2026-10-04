# -*- coding: utf-8 -*-
"""树冠掩膜阈值调参：把几组候选规则**叠到影像上**，肉眼挑最像树的那一组。

（手工取像素点太容易取偏，直接用叠加图对比最靠谱。）
输出 data/tile_check/canopy_tune.jpg
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
TILE = HERE / "data" / "tile_check"

CANDIDATES = [
    ("ExG>6 & 亮<150", lambda r, g, b, br: (2 * g - r - b > 6) & (br < 150)),
    ("ExG>12 & 亮<140", lambda r, g, b, br: (2 * g - r - b > 12) & (br < 140)),
    ("ExG>18 & 亮<135", lambda r, g, b, br: (2 * g - r - b > 18) & (br < 135)),
    ("G>R+2 & G>B+2 & 亮<125", lambda r, g, b, br: (g > r + 2) & (g > b + 2) & (br < 125)),
]


def main(campus="gulou", zoom=17):
    from PIL import Image
    a = np.asarray(Image.open(TILE / f"campussat_{campus}_z{zoom}.png").convert("RGB"),
                   dtype=np.float32)
    r, g, b = a[..., 0], a[..., 1], a[..., 2]
    br = a.mean(axis=2)
    base = a.astype(np.uint8)
    panels = [base]
    for name, fn in CANDIDATES:
        m = fn(r, g, b, br)
        print(f"{name:26s} 覆盖 {m.mean()*100:5.2f}%")
        ov = base.copy()
        ov[m] = (255, 40, 40)
        panels.append((base * 0.5 + ov * 0.5).astype(np.uint8))
    row = np.hstack(panels)
    Image.fromarray(row).resize((row.shape[1] // 5, row.shape[0] // 5), Image.LANCZOS)\
        .save(TILE / f"canopy_tune_{campus}.jpg", quality=88)
    print("->", TILE / f"canopy_tune_{campus}.jpg")
    return 0


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:]))
