# -*- coding: utf-8 -*-
"""局部细化配准：在初解附近细扫 旋转 / **缩放** / 平移。

为什么要扫缩放：建模数据来自《苏州校区平面图》（**设计图**，不是实测），
制图比例可能差 1%~2% —— 1300 m 的校园上，1% 就是 13 m 的系统偏差。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "tools"))

from register_suzhou import (GEOREF, RAW, color_masks, load_sat,  # noqa: E402
                             model_masks, score_at)
from vector_basemap import _load_json                              # noqa: E402


def main(tag="google_z17", ds=2, span_px=40):
    base = json.loads(GEOREF.read_text(encoding="utf-8"))
    axy = tuple(base["anchor_local_xy"])
    rgb, meta = load_sat(tag)
    H, W = rgb.shape[:2]
    sat_full = color_masks(rgb)

    def block(x):
        h, w = x.shape
        return x[:h // ds * ds, :w // ds * ds].reshape(h // ds, ds, w // ds, ds).mean(axis=(1, 3))
    sat = {k: block(v) for k, v in sat_full.items()}
    Hs, Ws = sat["built"].shape
    meta_s = dict(meta)
    meta_s["m_per_px"] = meta["m_per_px"] * ds

    # 初解的锚点像素 = 由 anchor_lonlat 反算
    ax = (base["anchor_lonlat"][0] - meta["west"]) / (meta["east"] - meta["west"]) * Ws
    ay = (meta["north"] - base["anchor_lonlat"][1]) / (meta["north"] - meta["south"]) * Hs
    meta_s["_anchor_px"] = (ax, ay)
    yy, xx = np.mgrid[0:Hs, 0:Ws]
    win = ((np.abs(xx - ax) <= span_px) & (np.abs(yy - ay) <= span_px)).astype(np.float32)

    weights = {"built@built": 1.0, "edge@built": 2.0, "edge@hill": 1.0}
    best = None
    for scale in (0.980, 0.985, 0.990, 0.995, 1.000, 1.005, 1.010, 1.015, 1.020):
        for dth in np.arange(-1.0, 1.01, 0.25):
            theta = base["theta_deg"] + dth
            mm = model_masks(theta, scale, axy, meta_s, (Hs, Ws))
            tot, det = score_at(sat, mm, win, weights)
            if tot is None:
                continue
            m = np.where(win > 0, tot, -1e9)
            iy, ix = np.unravel_index(np.argmax(m), m.shape)
            sc = float(m[iy, ix])
            if best is None or sc > best[0]:
                best = (sc, theta, scale, ix, iy,
                        {k: (float(v[iy, ix]) if v is not None else None) for k, v in det.items()})
            print(f"  scale={scale:.3f} θ={theta:+.2f}°  平移({ix-ax:+5.0f},{iy-ay:+5.0f})px  "
                  f"分数 {sc:.4f}", flush=True)
    sc, theta, scale, ix, iy, det = best
    print(f"\n细化最优：scale={scale:.3f} θ={theta:+.2f}°  "
          f"平移=({(ix-ax)*meta_s['m_per_px']:+.1f},{(iy-ay)*meta_s['m_per_px']:+.1f}) m  分数 {sc:.4f}")
    print(f"  （初解 {base['theta_deg']:+.2f}° scale {base.get('scale',1.0):.3f} 分数 {base['score']:.4f}）")
    return 0


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:]))
