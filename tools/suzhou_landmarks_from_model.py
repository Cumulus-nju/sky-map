# -*- coding: utf-8 -*-
"""从三维模型里**算出**苏州校区的实名地标坐标（替换那批估算值）。

背景（旧记录的已知缺陷）
-----------------------
`campus_config.py` 里苏州的 9 个地标是当年人工估算的，在校准图上
"操场/体育馆/南门/北门都落在农田或空地上"，会让同学用**文字描述**
（"我在图书馆拍的"）匹配到错误坐标。

现在模型里每个建筑组团都带中文名（西区·图书馆 / 东区·科创大厦 / 苏式园林 / 校门…），
直接按图层算**底面重心**就是真实坐标，不需要再估。

输出：data/tile_check/suzhou_landmarks_suggested.json + 控制台可直接粘贴的片段
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import suzhou_georef                       # noqa: E402
from glb_relief import PAL                 # noqa: E402

RAW = HERE / "data" / "raw"
OUT = HERE / "data" / "tile_check"

# 哪些图层当"地标"（要有名字、且是同学会提的地方）
WANTED = [
    ("西区 · 图书馆", "图书馆"),
    ("西区 · 大礼堂", "大礼堂"),
    ("西区 · 北大楼建筑风貌群", "北大楼"),
    ("西区 · 公共教学楼", "公共教学楼"),
    ("西区 · 学生生活综合体", "学生生活综合体"),
    ("西区 · 文体中心", "文体中心"),
    ("西区 · 科研综合体 1", "科研综合体1"),
    ("西区 · 科研综合体 2", "科研综合体2"),
    ("西区 · 科研综合体 3", "科研综合体3"),
    ("西区 · 科研综合体 4", "科研综合体4"),
    ("西区 · 公共科研与服务", "公共科研与服务"),
    ("东区 · 教学组团", "教学组团"),
    ("东区 · 科创大厦", "科创大厦"),
    ("东区 · 学生书院", "学生书院"),
    ("东区 · 天枢楼", "天枢楼"),
    ("东区 · 环形食堂", "环形食堂"),
    ("苏式园林", "苏式园林"),
    ("庄里山 / hill", "庄里山"),
]
# 运动场要单独处理：模型里 "运动场" 这一层的**东西两块场地是并成一层的**，
# 直接算重心会落在两块场地中间的空地上（一个"没有操场"的操场坐标）。
# 所以按 X 正负拆成西区/东区两个点。
SPORT_LAYERS = ["运动场 / pitch", "运动场 / sport", "运动场 / line"]


def main():
    geo = suzhou_georef.Geo()
    npz = np.load(RAW / "suzhou_glb.npz")
    # 图层名形如 "西区 · 图书馆 / glass"（对象 + 材质）。按 " / " 前缀归并成一个对象，
    # 这样才能拿到"整栋楼"的底面轮廓（只用某一种材质会偏）。
    agg: dict[str, list] = {}
    for n in npz.files:
        if not n.endswith("_v"):
            continue
        full = n[:-2]
        prefix = full.split(" / ")[0].strip()
        agg.setdefault(prefix, []).append(full)
    res = {}
    print("从三维模型算出的苏州地标（局部米制底面重心 -> WGS84）：\n")
    print("        landmarks={")
    for wanted, label in WANTED:
        key = wanted.split(" / ")[0].strip()
        if key not in agg:
            print(f"  # ⚠ 模型里没有图层 {wanted}")
            continue
        V = np.vstack([npz[f"{g}_v"].astype(np.float64) for g in agg[key]])
        y = V[:, 1]
        base = V[y <= (y.min() + 2.0)]         # 只用**贴地**那批顶点，避免幕墙顶点带偏重心
        if len(base) < 3:
            base = V
        X = float(base[:, 0].mean())
        Y = float(-base[:, 2].mean())          # glTF Z 指向南 ⇒ 北 = -Z
        lon, lat = geo.to_lonlat(X, Y)
        res[label] = [round(float(lat), 6), round(float(lon), 6)]
        print(f'            "{label}": ({lat:.6f}, {lon:.6f}),'
              f'   # 图层 {key}  局部({X:.0f},{Y:.0f}) m  底面 {len(base)} 点')
    print("        },")

    # 运动场：按东西两块分别取重心
    parts = [g for layer in SPORT_LAYERS
             for g in agg.get(layer.split(" / ")[0].strip(), [])
             if f"{g}_v" in npz.files]
    if parts:
        V = np.vstack([npz[f"{g}_v"].astype(np.float64) for g in parts])
        for side, sel in (("西区", V[:, 0] < 0), ("东区", V[:, 0] >= 0)):
            sub = V[sel]
            if len(sub) < 3:
                continue
            y = sub[:, 1]
            base = sub[y <= y.min() + 2.0]
            if len(base) < 3:
                base = sub
            X, Y = float(base[:, 0].mean()), float(-base[:, 2].mean())
            lon, lat = geo.to_lonlat(X, Y)
            res[f"运动场（{side}）"] = [round(float(lat), 6), round(float(lon), 6)]
            print(f'            "运动场（{side}）": ({lat:.6f}, {lon:.6f}),'
                  f'   # 局部({X:.0f},{Y:.0f}) m')
    OUT.mkdir(parents=True, exist_ok=True)
    p = OUT / "suzhou_landmarks_suggested.json"
    p.write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n-> {p}")
    # 顺带报一下和旧估算值的距离，好知道以前差多少
    return 0


if __name__ == "__main__":
    sys.exit(main())
