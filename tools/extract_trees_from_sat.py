# -*- coding: utf-8 -*-
"""从**卫星影像**里提取树冠，生成树点（用户要求"效果按照卫星图为准"）。

为什么要这样做
--------------
OSM 在鼓楼只标了 13 条 `tree_row` + 7 片 `wood`，而卫星影像上校园里
**树冠成片**（道路两侧、院落、北园/南园）。只按 OSM 画 ⇒ 整个校区看着荒。
用户原话"效果按照卫星图为准"，那就别凭感觉撒点。

2026-10-04 改版：换更好的数据源（用户拍板）
-------------------------------------------
问题：鼓楼的树"位置对、成团，但**达不到影像的浓密程度**"（掩膜 6.5% vs 影像约
15~20%），而**加密种树网格没有用**（10px→8px 观感只多 1%、数据却涨 55%）。
原因：瓶颈不是间距 —— 间距 6.5 m 时，r≈4 m 的树冠圆早就互相重叠了；
真正的瓶颈是**掩膜覆盖率**（树只长在 6.5% 的地方，其余地方本来就没树）。

根因（量出来的）：z17 在南京 ≈1.01 m/px，而 `fetch_campus_sat.py` 的
`target_width` 写死 **2400**，对应 0.65 m/px —— **源比目标还粗，等于上采样**，
有效分辨率就是 1.01 m/px。一棵 6 m 树冠只有 **6 个像素**，边缘全和屋顶/阴影混色，
ExG 被拉低 ⇒ 判据只能抓到最"浓绿"的那一小部分。

改法（两条缺一不可）：
  1. **真提高有效分辨率**：抓 z19 并且把 `width` 提到源分辨率
     （`6000` ≈ 0.26 m/px，一棵树冠从 6 px 变成 24 px）。只提 zoom 不提 width 是白提。
  2. **高清判据 → 降采样 → 低分辨率做形态学**：
     · 逐像素判据在 4000 万像素的高清图上算（受益于清晰边界）；
     · 用**块均值**降到 ~1 m/px 的工作分辨率再做开闭/连通域 ——
       "绿色占比"比"单像素是不是绿"稳得多，等价于超采样抗锯齿；
     · 形态学在 4000 万像素上做大结构元素会慢到不可用，降采样后成本与 z17 相当。
     副产品：**工作分辨率固定 WORK_MPP ⇒ 下面所有形态学参数可以直接按"米"写**，
     以后换 zoom / 换校区都不用重调参数。

⚠ 更早踩过的坑（别重犯）
------------------------
1. 先试了"绿通道占优"（`g>r+6 & g>b+6`）⇒ 只覆盖 1.3%，几乎什么都没抓到。
   这期影像**偏暗、偏冷**，真正的树冠在 RGB 上并不"绿"。
2. 换成"暗 + 略绿"（ExG，excess green）⇒ 覆盖 8%，但**把塑胶跑道/球场也抓进来了**
   （跑道又暗又绿）。所以必须用 OSM 的球场多边形把它们**显式排除**。
3. 逐像素判据在这期影像上**本来就不稳** ⇒ 所以只保留**成片的大块**（BLOB_MIN），
   并在块内**种密**让树冠连成片；小块一律丢掉（"不撒芝麻"的关键）。

输出：data/raw/<campus>_trees.geojson（点 + size 档位）
      data/tile_check/treecheck_<campus>_z<zoom>.jpg（影像 | 树冠掩膜 | 树点）
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import static_basemap as sb     # noqa: E402
from vector_basemap import Projector, _area_rings, _load_json  # noqa: E402

TILE = HERE / "data" / "tile_check"
RAW = HERE / "data" / "raw"

# ---------------------------------------------------------------- 颜色判据
# 与分辨率无关（作用在原始像素上），所以不随 zoom 变。
EXG_MIN = 2             # ExG = 2G - R - B，>0 表示偏绿
BRIGHT_MAX = 175        # 树冠比铺装/亮屋顶暗（150 → 175：受光的树冠也放进来，实测 +0.2pp）

# ------------------------------------------------------- 以下全部按**米**定义
WORK_MPP = 1.0          # 工作分辨率（米/像素）—— 形态学都在这个尺度上做
SHARE_MIN = 0.35        # 工作像素内"绿像素占比"超过它才算树冠
OPEN_M = 1.9            # 开运算：去掉约 1 px 宽的噪声（细碎混色）
CLOSE_M = 4.5           # 闭运算：把邻近树冠斑块**连成片**（不然是一颗颗孤立的点）
MIN_BLOB_M2 = 34.0      # 第一轮去碎斑的最小面积
BLOB_MIN_M2 = 150.0     # 成片树冠块的最小面积 —— **"不撒芝麻"的关键**
# ↑ 2026-10-04：原来是 380（照抄 z17 的 900px）。实测这一条**才是覆盖率的真正开关**：
#   380 → 150 让掩膜 8.71% → 11.14%（+2.4pp），而"只留成片大块"原本把大量真实的
#   中小树冠（院落树、行道树）连带砍掉了。降到 150 后看图仍成团、没有撒芝麻。
GRID_M = 5.5            # 块内种树网格间距（米）
COVER_WIN_M = 16.0      # 算"邻域树冠覆盖率"的窗口（决定 size 档位）
RNG_SEED = 20261003     # 固定种子：树点必须可复现（底图要能缓存）


def _mpp(fi) -> float:
    """影像的**米/像素**（按外框中点纬度算，经度方向要乘 cos(lat)）。"""
    lat_mid = (fi.south + fi.north) / 2.0
    w_m = (fi.east - fi.west) * 111320.0 * math.cos(math.radians(lat_mid))
    return w_m / fi.width


def _px(metres: float, mpp: float, *, odd: bool = False, least: int = 1) -> int:
    """米 → 工作图像素（可选强制奇数，居中用）。"""
    n = max(least, int(round(metres / mpp)))
    if odd and n % 2 == 0:
        n += 1
    return n


def _downsample(mask: np.ndarray, f: int) -> np.ndarray:
    """按 f×f 块**均值**降采样（返回 float32 占比图；f<=1 时原样转 float）。"""
    if f <= 1:
        return mask.astype(np.float32)
    h, w = mask.shape
    hc, wc = (h // f) * f, (w // f) * f
    a = mask[:hc, :wc].astype(np.float32)
    return a.reshape(hc // f, f, wc // f, f).mean(axis=(1, 3))


def _pitch_mask(campus: str, shape, fi, f: int):
    """OSM 里的球场/运动场 → 工作分辨率掩膜（跑道又暗又绿，必须排除）。

    直接画在**工作分辨率**上（1 m/px 对多边形来说完全够），省一次降采样。
    """
    from PIL import Image, ImageDraw

    hw, ww = shape
    # 工作像素 (i,j) 的中心 ≈ 高清像素 (i+0.5)*f → 用外框直接建工作图投影
    proj = Projector(fi.south, fi.west, fi.north, fi.east, ww, hw)
    vec = _load_json(RAW / f"vector_{campus}.geojson")
    m = Image.new("1", (ww, hw), 0)
    d = ImageDraw.Draw(m)
    n = 0
    for feat in vec.get("features", []):
        pr = feat.get("properties") or {}
        # ⚠ 2026-10-04：**`sports_centre` 必须排除在外**（曾经把它写进排除列表，是个真错误）。
        #   它是"体育设施**区域**"多边形，会把整个地块（含建筑/道路/绿化）圈进去 ——
        #   鼓楼外框里的"五台山体育中心"一块就 **12.1 万 m²**（外框的 4.5%），
        #   整块排除等于把那片的树全抹掉：实测掩膜因此从 13.4% 掉到 8.5%。
        #   真正又暗又绿、会和树冠混淆的是**场地本身**（pitch / track / stadium）。
        if pr.get("leisure") not in ("pitch", "stadium", "track") \
                and pr.get("landuse") != "recreation_ground":
            continue
        for ring in _area_rings(feat.get("geometry") or {}, pr):
            pts = [proj.pt(lo, la) for lo, la in ring]
            if len(pts) >= 3:
                d.polygon(pts, fill=1)
                n += 1
    return np.asarray(m, dtype=bool), n


def main(campus: str = "gulou", zoom: int = 19):
    from PIL import Image
    from scipy import ndimage

    png = TILE / f"campussat_{campus}_z{zoom}.png"
    meta = json.loads((TILE / f"campussat_{campus}_z{zoom}.json").read_text(encoding="utf-8"))
    fi = sb.FrameImage(png=png.read_bytes(),
                       **{k: meta[k] for k in (
                           "width", "height", "zoom", "scale_x", "scale_y",
                           "origin_x", "origin_y", "south", "west", "north", "east")})

    # ---- ① 高清逐像素判据（用 int16 算，别把 4000 万像素先转 float32）----
    arr = np.asarray(Image.open(png).convert("RGB"), dtype=np.int16)
    h, w = arr.shape[:2]
    mpp_hi = _mpp(fi)
    veg_hi = ((2 * arr[..., 1] - arr[..., 0] - arr[..., 2] > EXG_MIN)
              & (arr.sum(axis=2) < BRIGHT_MAX * 3))
    del arr
    print(f"{campus} z{zoom}: {w}x{h}  高清 {mpp_hi:.3f} m/px  "
          f"逐像素暗绿 {veg_hi.mean()*100:5.2f}%")

    # ---- ② 降到工作分辨率：占比图 → 二值 ----
    f = max(1, int(round(WORK_MPP / mpp_hi)))
    hw, ww = h // f, w // f
    share = _downsample(veg_hi, f)
    del veg_hi
    veg = share >= SHARE_MIN
    print(f"      工作 {ww}x{hw} @{WORK_MPP:.2f} m/px（块 {f}x{f}）  "
          f"占比≥{SHARE_MIN} 的格 {veg.mean()*100:5.2f}%")

    # ---- ③ 工作分辨率上的形态学（参数按米写，这里换成像素）----
    veg = ndimage.binary_opening(veg, structure=np.ones((_px(OPEN_M, WORK_MPP),) * 2))
    veg = ndimage.binary_closing(veg, structure=np.ones((_px(CLOSE_M, WORK_MPP),) * 2))
    lab, n = ndimage.label(veg)
    if n:
        sizes = ndimage.sum(veg, lab, range(1, n + 1))
        keep = np.zeros(n + 1, bool)
        keep[1:][sizes >= MIN_BLOB_M2 / WORK_MPP ** 2] = True
        veg = keep[lab]
    print(f"      形态学后 {veg.mean()*100:5.2f}%  （{n} 个连通块）")

    pitch, npitch = _pitch_mask(campus, (hw, ww), fi, f)
    canopy = veg & ~pitch
    lab, n = ndimage.label(canopy)
    nblob = 0
    if n:
        sizes = ndimage.sum(canopy, lab, range(1, n + 1))
        big = np.zeros(n + 1, bool)
        big[1:][sizes >= BLOB_MIN_M2 / WORK_MPP ** 2] = True
        canopy = big[lab]
        nblob = int(sum(1 for s in sizes if s >= BLOB_MIN_M2 / WORK_MPP ** 2))
    print(f"      排除球场 {npitch} 块（{pitch.mean()*100:.2f}%）"
          f" ⇒ **成片树冠 {canopy.mean()*100:5.2f}%**（{nblob} 块）")

    # ---- ④ 块内种树：邻域覆盖率向量化（别在 Python 里逐点算 mean）----
    win = _px(COVER_WIN_M, WORK_MPP, odd=True)
    fill_map = ndimage.uniform_filter(canopy.astype(np.float32), size=win, mode="constant")
    rng = np.random.default_rng(RNG_SEED)
    grid = _px(GRID_M, WORK_MPP)
    feats = []
    for gy in range(hw // grid):
        for gx in range(ww // grid):
            x = (gx + rng.random()) * grid
            y = (gy + rng.random()) * grid
            xi, yi = int(min(ww - 1, x)), int(min(hw - 1, y))
            if not canopy[yi, xi]:
                continue
            fill = float(fill_map[yi, xi])
            size = 0 if fill < 0.5 else (1 if fill < 0.8 else 2)
            # 工作像素 → 高清像素中心 → 经纬度
            lat, lon = fi.px_to_latlng((xi + 0.5) * f, (yi + 0.5) * f)
            feats.append({"type": "Feature",
                          "geometry": {"type": "Point",
                                       "coordinates": [round(lon, 7), round(lat, 7)]},
                          "properties": {"size": size}})

    out = {"type": "FeatureCollection",
           "name": f"{campus}_trees_from_satellite",
           "source": (f"从 {png.name} 提取树冠（ExG>{EXG_MIN} 且亮度<{BRIGHT_MAX}，"
                      f"高清 {mpp_hi:.3f} m/px 判据 → {WORK_MPP:.1f} m/px 占比图 → "
                      f"开闭+只留 ≥{BLOB_MIN_M2:.0f}m² 的成片块 → 块内按 {GRID_M}m 网格种密；"
                      f"排除 OSM 球场多边形）"),
           "params": {"exg_min": EXG_MIN, "bright_max": BRIGHT_MAX,
                      "work_mpp": WORK_MPP, "share_min": SHARE_MIN,
                      "open_m": OPEN_M, "close_m": CLOSE_M,
                      "blob_min_m2": BLOB_MIN_M2, "grid_m": GRID_M,
                      "mpp_hi": round(mpp_hi, 4)},
           "features": feats}
    dst = RAW / f"{campus}_trees.geojson"
    dst.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    print(f"-> {dst}  ({len(feats)} 棵树, {dst.stat().st_size//1024} KB)")

    # ---- ⑤ 诊断图：影像 | 树冠掩膜 | 树点（同一工作分辨率，便于和影像对比）----
    base = np.asarray(Image.open(png).convert("RGB").resize((ww, hw), Image.BILINEAR))
    ov = base.copy()
    ov[canopy] = (255, 40, 40)
    mask_panel = (base * 0.5 + ov * 0.5).astype(np.uint8)
    dots = base.copy()
    for feat in feats:
        lon, lat = feat["geometry"]["coordinates"]
        hx, hy = fi.latlng_to_px(lat, lon)
        xi, yi = int(hx / f), int(hy / f)
        if 0 <= xi < ww and 0 <= yi < hw:
            dots[max(0, yi - 1):yi + 2, max(0, xi - 1):xi + 2] = (255, 40, 40)
    row = np.hstack([base, mask_panel, dots])
    Image.fromarray(row).resize((row.shape[1] // 3, row.shape[0] // 3), Image.LANCZOS)\
        .save(TILE / f"treecheck_{campus}_z{zoom}.jpg", quality=86)
    print(f"-> {TILE / f'treecheck_{campus}_z{zoom}.jpg'}")
    return 0


if __name__ == "__main__":
    a = sys.argv[1:]
    sys.exit(main(a[0] if a else "gulou", int(a[1]) if len(a) > 1 else 19))
