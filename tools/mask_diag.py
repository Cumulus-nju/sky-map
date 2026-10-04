# -*- coding: utf-8 -*-
"""诊断：卫星影像的颜色分离阈值到底对不对（水/植被/建成区）。

自动配准最怕"判据本身是错的" —— 先看 mask 长什么样，再谈优化。
输出 data/tile_check/maskdiag.jpg（原图 | 水 | 植被 | 建成区 | 边缘强度 五联图）。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent.parent
OUT = HERE / "data" / "tile_check"
sys.path.insert(0, str(HERE))

PROBES = {                     # 预览图(1200宽)坐标 -> 全分辨率坐标要 *2
    "西区建筑群": (200, 400),
    "庄里山林地": (350, 350),
    "东侧大水面": (760, 300),
    "农田":       (100, 80),
    "跑道":       (530, 375),
    "东区建筑":   (620, 500),
}


def main(tag="google_z17"):
    from PIL import Image
    im = Image.open(OUT / f"suzhousat_{tag}.png").convert("RGB")
    a = np.asarray(im, dtype=np.float32)
    H, W = a.shape[:2]
    r, g, b = a[..., 0], a[..., 1], a[..., 2]
    mx, mn = a.max(axis=2), a.min(axis=2)
    sat = mx - mn

    print("采样点（全分辨率坐标 = 预览坐标 ×2）：")
    for name, (px, py) in PROBES.items():
        x, y = px * 2, py * 2
        box = a[max(0, y - 6):y + 6, max(0, x - 6):x + 6]
        m = box.reshape(-1, 3).mean(axis=0)
        print(f"  {name:8s} RGB=({m[0]:6.1f},{m[1]:6.1f},{m[2]:6.1f})  "
              f"max-min={m.max()-m.min():5.1f} 亮度={m.mean():6.1f}")

    # 候选阈值
    water = ((b > r + 10) & (g > r + 4) & (mx < 170)).astype(np.float32)
    green = ((g > r + 8) & (g >= b + 8)).astype(np.float32)
    built = ((mx > 150) & (sat < 28)).astype(np.float32)
    # 边缘强度（建筑/道路密的地方高，农田低）
    gray = a.mean(axis=2)
    gx = np.zeros_like(gray); gy = np.zeros_like(gray)
    gx[:, 1:-1] = gray[:, 2:] - gray[:, :-2]
    gy[1:-1, :] = gray[2:, :] - gray[:-2, :]
    edge = np.sqrt(gx ** 2 + gy ** 2)
    edge = edge / (edge.max() or 1.0)

    for n, m in (("water", water), ("green", green), ("built", built), ("edge>0.05", (edge > 0.05).astype(np.float32))):
        print(f"  {n:10s} 占比 {m.mean()*100:5.2f}%")

    tiles = [a.astype(np.uint8)]
    for m in (water, green, built, edge * 3):
        tiles.append((np.clip(m, 0, 1)[..., None] * 255).repeat(3, axis=2).astype(np.uint8))
    row = np.hstack(tiles)
    Image.fromarray(row).resize((row.shape[1] // 4, row.shape[0] // 4), Image.LANCZOS)\
        .save(OUT / "maskdiag.jpg", quality=88)
    print("->", OUT / "maskdiag.jpg")
    return 0


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:]))
