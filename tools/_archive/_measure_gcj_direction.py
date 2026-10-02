# -*- coding: utf-8 -*-
"""判断 GCJ-02 平移方向——最终判据：轮廓掩膜 vs 影像边缘图 的相关系数。

判据演进（记下来，免得下次再走弯路）
------------------------------------
1. 「影像边缘点到最近轮廓的平均距离」→ 失败：轮廓太密（298 栋楼），
   任何像素附近都有轮廓线，三个方向都是 ~52 px，最优只领先 1.01×（噪声）。
2. 「轮廓处边缘强度的中位数」→ 失败：过半采样点落在屋顶内部等平坦处，
   中位数 = 0，三方向无区分度。
3. ✅ **皮尔逊相关系数**：把"轮廓掩膜"和"影像边缘图"当两幅空间信号，
   方向对时两者在**同一批像素**上同时为高 ⇒ 相关系数最大。
   这个判据对"轮廓密"不敏感（它比的是空间重合模式，不是绝对距离），
   也有明确物理含义。

用法：python tools/_measure_gcj_direction.py
"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from PIL import Image, ImageFilter  # noqa: E402

CHECK = HERE / "data" / "tile_check"


def red_mask_np(img: Image.Image):
    import numpy as np

    a = np.asarray(img.convert("RGB")).astype(int)
    r, g, b = a[:, :, 0], a[:, :, 1], a[:, :, 2]
    return ((r > 140) & (g < 110) & (b < 110) & ((r - g) > 60) & ((r - b) > 60))


def edge_np(img: Image.Image):
    import numpy as np

    # 先轻微模糊，避免单像素噪声主导相关
    g = img.convert("L").filter(ImageFilter.GaussianBlur(1.0))
    return np.asarray(g.filter(ImageFilter.FIND_EDGES)).astype(float)


def score(name: str):
    import numpy as np

    p = CHECK / f"{name}.png"
    if not p.exists():
        return None
    img = Image.open(p)
    red = red_mask_np(img)
    if red.sum() < 500:
        return None
    # 轮廓加粗一点（真实屋顶边缘有宽度，1px 线会低估重合）
    red_d = np.asarray(
        Image.fromarray((red * 255).astype("uint8")).filter(ImageFilter.MaxFilter(3))
    ).astype(float) / 255.0
    edge = edge_np(img)

    # 皮尔逊相关
    a = red_d.ravel() - red_d.mean()
    b = edge.ravel() - edge.mean()
    denom = (np.sqrt((a * a).sum()) * np.sqrt((b * b).sum()))
    corr = float((a * b).sum() / denom) if denom else 0.0

    # 另一个直观指标：轮廓处的边缘均值 / 全图边缘均值（>1 说明轮廓偏向边缘）
    lift = float(edge[red_d > 0.5].mean() / max(edge.mean(), 1e-9))
    return {"corr": corr, "lift": lift, "n": int((red_d > 0.5).sum())}


def main() -> int:
    print(f"{'方向':>16s} {'相关系数':>10s} {'轮廓处边缘提升':>16s} {'采样点':>9s}")
    res = {}
    for name in ("gcj_dir_plus", "gcj_dir_zero", "gcj_dir_minus"):
        s = score(name)
        if not s:
            print(f"{name}: 缺图或掩膜太少，先跑 tools/_probe_gcj_direction.py")
            continue
        res[name] = s
        print(f"{name:>16s} {s['corr']:10.4f} {s['lift']:16.3f} {s['n']:9d}")

    if len(res) >= 2:
        for metric, label in (("corr", "相关系数"), ("lift", "边缘提升")):
            best = max(res, key=lambda k: res[k][metric])
            vals = sorted((res[k][metric] for k in res), reverse=True)
            ratio = vals[0] / max(vals[1], 1e-9) if vals[1] else float("inf")
            print(f"\n按{label}：**{best}** 最优（{res[best][metric]:.4f}），"
                  f"领先次优 {ratio:.3f}×")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
