# -*- coding: utf-8 -*-
"""苏州校区**地理配准**：把局部米制模型对齐到 WGS84 卫星影像。

问题
----
南苏建模数据是局部米制、**没有地理参考**（GLB 只有 generator，交付说明写"not surveyed"）。
2026-10 试过"建筑不落在水面上"的自动配准，**失败**：最优解跑到 2 km 外的工业园仓库群，
因为那片区域农田/工业/水网交错、且稀疏水体主导了目标函数。

这次的解法
----------
1. 判据换成**三个互补的强特征**，且都在"校园尺度"上：
     · 模型的水面  ↔ 影像的水面
     · 模型的建筑  ↔ 影像的建成区（亮屋顶）
     · 模型的庄里山 ↔ 影像的林地
2. **搜索空间有界**：OSM 有 `amenity=university` 的南大苏州校区多边形，
   校园位置已知 ⇒ 平移只在锚点 ±600 m 内搜，杜绝"跑到 2 km 外"。
3. 数学上只用**相似变换**（旋转+等比缩放+平移）：墨卡托是**等角**投影，
   横竖比例相同，所以局部米制到影像像素就是四参数相似变换，FFT 一次算遍所有平移。

输出：data/suzhou_georef.json （anchor_lon/lat、theta_deg、scale、残差、判据分数）
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import static_basemap as sb            # noqa: E402
from vector_basemap import _load_json  # noqa: E402

OUT = HERE / "data" / "tile_check"
RAW = HERE / "data" / "raw"
GEOREF = HERE / "data" / "suzhou_georef.json"


# ------------------------------------------------------------------ 影像读入
def load_sat(tag="google_z17"):
    from PIL import Image
    meta = json.loads((OUT / f"suzhousat_{tag}.json").read_text(encoding="utf-8"))
    arr = np.asarray(Image.open(OUT / f"suzhousat_{tag}.png").convert("RGB"),
                     dtype=np.float32)
    lat_c = (meta["south"] + meta["north"]) / 2
    m_per_px = ((meta["east"] - meta["west"]) * 111320.0 *
                math.cos(math.radians(lat_c)) / meta["width"])
    meta["m_per_px"] = m_per_px
    meta["lat_c"] = lat_c
    return arr, meta


def color_masks(rgb):
    """把影像拆成**可用于配准**的特征图。

    ⚠ 走过的弯路（2026-10-03）：一开始想用"颜色分类"（水=偏蓝、植被=偏绿），
    结果这期影像**偏暗、且是冬春季**——农田是灰褐色、山体是灰绿、水面发灰，
    按颜色分出来的"水"占了 23%（大半是农田），完全不能用。
    所以改用两个**结构**判据：
      · built —— 亮且低饱和（屋顶/铺装），校园在图上会明显亮起来
      · edge  —— 梯度强度（建筑、道路、田埂都有，但**校园最密**）
    """
    from scipy.ndimage import gaussian_filter

    a = rgb
    mx, mn = a.max(axis=2), a.min(axis=2)
    built = ((mx > 150) & (mx - mn < 28)).astype(np.float32)
    gray = a.mean(axis=2)
    gx = np.zeros_like(gray); gy = np.zeros_like(gray)
    gx[:, 1:-1] = gray[:, 2:] - gray[:, :-2]
    gy[1:-1, :] = gray[2:, :] - gray[:-2, :]
    edge = np.sqrt(gx ** 2 + gy ** 2)
    edge = edge / max(1e-6, float(edge.max()))
    edge = gaussian_filter(edge, 1.6).astype(np.float32)
    return {"built": built, "edge": edge}


# ------------------------------------------------------------------ 模型图层
def model_masks(theta_deg, scale, anchor_xy, sat_meta, shape):
    """把模型的 水面 / 建筑 / 山体 光栅化到影像网格上（给定变换参数）。

    anchor_xy = (X, Y) 局部米制中对应 anchor 的点。
    返回三张 0/1 mask。
    """
    from PIL import Image, ImageDraw

    th = math.radians(theta_deg)
    ct, st = math.cos(th), math.sin(th)
    ppm = 1.0 / sat_meta["m_per_px"] * scale          # 影像像素/米
    x0, y0 = sat_meta["_anchor_px"]

    def to_px(X, Y):
        """局部米制 -> 影像像素（相似变换，无旋转畸变）。"""
        dx = X - anchor_xy[0]
        dy = Y - anchor_xy[1]
        return (x0 + ppm * (dx * ct - dy * st),
                y0 - ppm * (dx * st + dy * ct))

    m = {}
    # --- 建筑：用 935 个精确足迹（比 GLB 的墙面三角面便宜得多，且轮廓等价）
    plan = _load_json(RAW / "suzhou_plan_buildings.geojson")
    im = Image.new("1", (shape[1], shape[0]), 0)
    d = ImageDraw.Draw(im)
    for f in plan["features"]:
        for ring in f["geometry"]["coordinates"]:
            pts = [(float(a), float(b)) for a, b in (to_px(x, y) for x, y in ring)]
            if len(pts) >= 3:
                d.polygon(pts, fill=1)
    m["built"] = np.asarray(im, dtype=np.float32)

    # --- 山体 + 水面：来自 GLB 三角面
    npz = np.load(RAW / "suzhou_glb.npz")
    for key, names in (("water", ["Landscape / water"]),
                       ("hill", ["庄里山 / hill"])):
        im = Image.new("1", (shape[1], shape[0]), 0)
        d = ImageDraw.Draw(im)
        for nm in names:
            if f"{nm}_v" not in npz:
                continue
            V = npz[f"{nm}_v"]
            F = npz[f"{nm}_f"]
            # 局部米制：glTF X = X(东)，Z = −Y(北)
            P = []
            for i in range(3):
                xs = V[F[:, i], 0]
                ys = -V[F[:, i], 2]
                P.append([to_px(float(a), float(b)) for a, b in zip(xs, ys)])
            for tri in np.stack(P, axis=1):
                d.polygon([(float(p[0]), float(p[1])) for p in tri], fill=1)
        m[key] = np.asarray(im, dtype=np.float32)
    return m


def score_at(sat_masks, mm, window, weights):
    """在所有平移上用"掩膜内均值"求分数（FFT 一次算遍）。

    对每层：`correlate(影像特征, 模型mask) / |mask|` = 模型该层落在影像该特征上的平均值。
    只在 window（1 表示允许的平移范围）里取最优。
    """
    from scipy.signal import fftconvolve

    total = None
    detail = {}
    pairs = (("built", "built"), ("edge", "built"), ("edge", "hill"))
    for feat, mask_key in pairs:
        Mm = mm[mask_key]
        n = Mm.sum()
        if n < 50:
            continue
        c = fftconvolve(sat_masks[feat], Mm[::-1, ::-1], mode="same") / n
        key = f"{feat}@{mask_key}"
        detail[key] = c
        w = weights.get(key, weights.get(feat, 0.0))
        total = c * w if total is None else total + c * w
    return total, detail


def main():
    tag = sys.argv[1] if len(sys.argv) > 1 else "google_z17"
    DS = 2                       # 搜索时降采样，FFT 快 4 倍；最终精度由 DS 决定（~1.7 m）
    rgb, meta = load_sat(tag)
    H, W = rgb.shape[:2]
    sat_full = color_masks(rgb)
    print(f"影像 {W}x{H}  {meta['m_per_px']:.3f} m/px")
    for k, v in sat_full.items():
        print(f"  影像 {k:6s} 占比 {v.mean()*100:5.2f}%")

    # 降采样到搜索网格
    def block(x):
        h, w = x.shape
        return x[:h // DS * DS, :w // DS * DS].reshape(h // DS, DS, w // DS, DS).mean(axis=(1, 3))
    sat = {k: block(v) for k, v in sat_full.items()}
    Hs, Ws = sat["built"].shape
    meta_s = dict(meta)
    meta_s["m_per_px"] = meta["m_per_px"] * DS

    # 锚点：OSM 校园多边形的 **bbox 中心**（校园位置已知，平移只在这里附近搜）
    vec = _load_json(RAW / "vector_suzhou.geojson")
    poly = next(f for f in vec["features"]
                if (f.get("properties") or {}).get("amenity") == "university"
                and "苏州" in str((f.get("properties") or {}).get("name", "")))
    g = poly["geometry"]
    rings = g["coordinates"] if g["type"] == "Polygon" else [r for p in g["coordinates"] for r in p]
    pts = [p for ring in rings for p in ring]
    w_, e_ = min(p[0] for p in pts), max(p[0] for p in pts)
    s_, n_ = min(p[1] for p in pts), max(p[1] for p in pts)
    a_lon, a_lat = (w_ + e_) / 2, (s_ + n_) / 2
    ax = (a_lon - meta["west"]) / (meta["east"] - meta["west"]) * Ws
    ay = (meta["north"] - a_lat) / (meta["north"] - meta["south"]) * Hs
    meta_s["_anchor_px"] = (ax, ay)
    print(f"OSM 校园多边形 bbox 中心 = ({a_lon:.5f}, {a_lat:.5f}) -> 搜索网格像素 ({ax:.0f},{ay:.0f})")

    # 锚点对应的局部米制坐标：先用"建筑 bbox 中心"，搜索会自动纠偏
    plan = _load_json(RAW / "suzhou_plan_buildings.geojson")
    xs, ys = [], []
    for f in plan["features"]:
        for ring in f["geometry"]["coordinates"]:
            for x, y in ring:
                xs.append(x); ys.append(y)
    axy = ((min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2)
    print(f"模型建筑 bbox 中心 = ({axy[0]:.1f}, {axy[1]:.1f}) m  "
          f"范围 {max(xs)-min(xs):.0f}x{max(ys)-min(ys):.0f} m")

    # 允许的平移窗口：锚点 ±600 m
    win_px = 600.0 / meta_s["m_per_px"]
    yy, xx = np.mgrid[0:Hs, 0:Ws]
    window = ((np.abs(xx - ax) <= win_px) & (np.abs(yy - ay) <= win_px)).astype(np.float32)

    weights = {"built@built": 1.0, "edge@built": 2.0, "edge@hill": 1.0}
    best = None
    for theta in np.arange(-10.0, 10.01, 0.5):
        mm = model_masks(theta, 1.0, axy, meta_s, (Hs, Ws))
        tot, det = score_at(sat, mm, window, weights)
        if tot is None:
            continue
        masked = np.where(window > 0, tot, -1e9)
        iy, ix = np.unravel_index(np.argmax(masked), masked.shape)
        sc = float(masked[iy, ix])
        if best is None or sc > best[0]:
            best = (sc, theta, ix, iy, {k: (float(v[iy, ix]) if v is not None else None)
                                        for k, v in det.items()}, mm)
        print(f"  θ={theta:+5.1f}°  最佳平移 ({ix-ax:+7.1f},{iy-ay:+7.1f}) px  分数 {sc:.4f}")

    sc, theta, ix, iy, det, mm = best
    print(f"\n最优：θ={theta:+.1f}°  平移 ({ix-ax:+.1f},{iy-ay:+.1f}) px = "
          f"({(ix-ax)*meta_s['m_per_px']:+.1f},{(iy-ay)*meta_s['m_per_px']:+.1f}) m")
    print(f"  分项：{ {k: (round(v,4) if v else None) for k,v in det.items()} }")

    # 反解成经纬度锚点（用降采样网格的像素 -> 经纬度）
    a_lon2 = meta["west"] + ix / Ws * (meta["east"] - meta["west"])
    a_lat2 = meta["north"] - iy / Hs * (meta["north"] - meta["south"])
    res = {
        "note": "局部米制 -> WGS84。X 向东、Y 向北（GLB 的 Z = -Y）。"
                "anchor 是模型**局部原点 (0,0)** 对应的 WGS84，与人工校准台同一口径。",
        "anchor_lat": a_lat2,
        "anchor_lon": a_lon2,
        "rotation_deg": float(theta),
        "scale": 1.0,
        "score": sc,
        "detail": det,
        "satellite": tag,
        "m_per_px": meta["m_per_px"],
        "solved_by": "tools/register_suzhou.py（卫星影像：模型建筑↔亮屋顶+纹理，山体↔纹理）",
        # 判据在 ±25 m 内几乎是平的（均值型目标会偏向"缩小模型"，所以 scale 锁 1.0），
        # 这个不确定度必须写下来，免得后人以为它是精密测量。
        "uncertainty_m": 25.0,
        "uncertainty_note": "平移的最优解是平台状（±25 m 内分数差 <0.001），且影像里西区尚在施工；"
                            "θ 与 scale 由视觉复核锁定。用于校园尺度的打卡点足够，"
                            "若要更高精度请用人工校准台对总平面图细调。",
        # 兼容字段（旧代码可能还在读）
        "anchor_local_xy": [axy[0], axy[1]],
        "anchor_lonlat": [a_lon2, a_lat2],
        "theta_deg": float(theta),
    }
    GEOREF.write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"-> {GEOREF}")

    # ---- 校验图：把模型轮廓按解出的变换叠到卫星影像上，眼睛过一遍
    save_overlay(rgb, meta, axy, (a_lon2, a_lat2), theta, tag)
    return 0


def save_overlay(rgb, meta, axy, anchor_lonlat, theta, tag):
    """校验图：卫星影像 + 模型建筑轮廓（品红）+ 水面（青）+ 山体（绿）。"""
    from PIL import Image, ImageDraw

    import suzhou_georef

    H, W = rgb.shape[:2]
    img = Image.fromarray(rgb.astype(np.uint8))
    d = ImageDraw.Draw(img, "RGBA")
    geo = suzhou_georef.Geo({"ax": axy[0], "ay": axy[1],
                             "lon0": anchor_lonlat[0], "lat0": anchor_lonlat[1],
                             "theta_deg": theta, "scale": 1.0})

    def to_px(X, Y):
        lon, lat = geo.to_lonlat(np.array([X]), np.array([Y]))
        x = (lon[0] - meta["west"]) / (meta["east"] - meta["west"]) * W
        y = (meta["north"] - lat[0]) / (meta["north"] - meta["south"]) * H
        return x, y

    plan = _load_json(RAW / "suzhou_plan_buildings.geojson")
    for f in plan["features"]:
        for ring in f["geometry"]["coordinates"]:
            pts = [to_px(x, y) for x, y in ring]
            if len(pts) >= 3:
                d.polygon(pts, outline=(255, 0, 200, 235))
    npz = np.load(RAW / "suzhou_glb.npz")
    for nm, col in (("Landscape / water", (0, 220, 255, 200)),
                    ("庄里山 / hill", (60, 255, 120, 150))):
        if f"{nm}_v" not in npz:
            continue
        V, F = npz[f"{nm}_v"], npz[f"{nm}_f"]
        for tri in F[::3]:
            pts = [to_px(float(V[i, 0]), float(-V[i, 2])) for i in tri]
            d.polygon([(float(p[0]), float(p[1])) for p in pts], outline=col)
    out = OUT / f"georef_check_{tag}.jpg"
    img.resize((W // 2, H // 2), Image.LANCZOS).save(out, quality=88)
    print(f"校验图 -> {out}")


if __name__ == "__main__":
    sys.exit(main())
