# -*- coding: utf-8 -*-
"""苏州校区「局部米制 → WGS84」配准参数的**唯一入口**。

为什么要收成一个模块
--------------------
这个项目里配准参数被三处用到（立体渲染 `glb_relief`、外框计算 `tools/compute_frames`、
校验图 `tools/register_suzhou`），而且仓库里还有一个**人工校准台**
（`tools/苏州配准校准台.py`）。如果各自解析各自的字段，迟早出现
"校准台存了一套、渲染器读的是另一套"这种最恶心的问题。

字段口径（**与校准台一致**）
---------------------------
    anchor_lat / anchor_lon —— 模型局部坐标 **(0, 0)** 对应的 WGS84 经纬度
    rotation_deg            —— 模型相对正北的旋转角（逆时针为正）
    scale                   —— 缩放（建模数据是"近似米"，正常就是 1.0）

换算（与校准台逐字一致）：
    lx, ly = X·scale, Y·scale
    rx = lx·cosθ − ly·sinθ        # 东向米
    ry = lx·sinθ + ly·cosθ        # 北向米
    lon = anchor_lon + rx / (111320·cos(anchor_lat))
    lat = anchor_lat + ry / 110540

兼容旧字段：`anchor_local_xy` + `anchor_lonlat` + `theta_deg`（2026-10-03 早先的一版）。
"""
from __future__ import annotations

import json
import math
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_PATH = HERE / "data" / "suzhou_georef.json"

M_PER_DEG_LAT = 110540.0


def m_per_deg_lon(lat: float) -> float:
    return 111320.0 * math.cos(math.radians(lat))


def load(path: Path | str | None = None) -> dict:
    """读参数并**规范化**成统一口径（兼容旧的 anchor_local_xy 写法）。"""
    p = Path(path) if path else DEFAULT_PATH
    d = json.loads(Path(p).read_text(encoding="utf-8"))
    theta = d.get("rotation_deg", d.get("theta_deg", 0.0))
    scale = float(d.get("scale", 1.0))
    if "anchor_lat" in d and "anchor_lon" in d:
        ax, ay = 0.0, 0.0
        lon0, lat0 = float(d["anchor_lon"]), float(d["anchor_lat"])
    else:
        ax, ay = d["anchor_local_xy"]
        lon0, lat0 = d["anchor_lonlat"]
    return {"ax": float(ax), "ay": float(ay), "lon0": float(lon0), "lat0": float(lat0),
            "theta_deg": float(theta), "scale": scale, "raw": d}


class Geo:
    """局部米制 (X 东, Y 北) <-> WGS84。"""

    def __init__(self, params: dict | None = None):
        p = params or load()
        if "ax" not in p:                     # 允许直接传原始 json
            p = load_dict(p)
        self.ax, self.ay = p["ax"], p["ay"]
        self.lon0, self.lat0 = p["lon0"], p["lat0"]
        th = math.radians(p.get("theta_deg", 0.0))
        self.ct, self.st = math.cos(th), math.sin(th)
        self.scale = float(p.get("scale", 1.0))
        self.mx = m_per_deg_lon(self.lat0)

    def to_lonlat(self, X, Y):
        import numpy as np
        dx = (np.asarray(X, dtype=float) - self.ax) * self.scale
        dy = (np.asarray(Y, dtype=float) - self.ay) * self.scale
        e = dx * self.ct - dy * self.st
        n = dx * self.st + dy * self.ct
        return self.lon0 + e / self.mx, self.lat0 + n / M_PER_DEG_LAT

    def extent_of(self, features):
        """一批「局部米制 Polygon」的经纬度外接范围 (S, W, N, E)。"""
        ext = None
        for feat in features:
            geom = feat.get("geometry") or {}
            rings = geom.get("coordinates") or []
            for ring in rings:
                for X, Y in ring:
                    lon, lat = self.to_lonlat(X, Y)
                    lon, lat = float(lon), float(lat)
                    if ext is None:
                        ext = (lat, lon, lat, lon)
                    else:
                        s, w, n, e = ext
                        ext = (min(s, lat), min(w, lon), max(n, lat), max(e, lon))
        return ext


def load_dict(d: dict) -> dict:
    """把已经读进来的 dict 规范化（不碰文件）。"""
    theta = d.get("rotation_deg", d.get("theta_deg", 0.0))
    scale = float(d.get("scale", 1.0))
    if "anchor_lat" in d and "anchor_lon" in d:
        return {"ax": 0.0, "ay": 0.0, "lon0": float(d["anchor_lon"]),
                "lat0": float(d["anchor_lat"]), "theta_deg": float(theta),
                "scale": scale, "raw": d}
    ax, ay = d["anchor_local_xy"]
    lon0, lat0 = d["anchor_lonlat"]
    return {"ax": float(ax), "ay": float(ay), "lon0": float(lon0), "lat0": float(lat0),
            "theta_deg": float(theta), "scale": scale, "raw": d}
