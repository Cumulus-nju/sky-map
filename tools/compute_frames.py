# -*- coding: utf-8 -*-
"""校区外框（frame）计算与校验工具。

「外框」是投稿页和成品地图上那个方框 = **校园全部建筑 + 周边一圈缓冲**。
它和 campus_config.py 里的 `bbox` 不是一回事：

    bbox  —— 手校的"校园大致范围"，偏紧，只用于判断落点是否可疑
    frame —— 建筑实际外接范围 + 缓冲，长宽比按各校区实际形状自适应，
             地图视野锁定在此框内（只可往里放大，不能缩出去）

为什么要有这个工具：鼓楼/仙林的老 bbox 是手估的，东西两侧各把
133~149 m 的建筑切在框外，同学正常在校园里点选会被误标「待复核」。
照着建筑实测范围算外框，这个坑不会再犯。

用法：
    python tools/compute_frames.py              # 打印建议的 frame 配置
    python tools/compute_frames.py --verify     # 只校验（CI / 改完代码后跑）
    python tools/compute_frames.py --pad 200    # 换个缓冲宽度（米）

注意：OSM 没有建筑数据的校区（如苏州，2023 年启用的新校区）**算不出来**，
必须人工标定（卫星影像 / 校区总平面图），脚本会明确提示。
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

# Windows 控制台默认 GBK，打不出 ✓/✗ 这类字符会直接抛 UnicodeEncodeError。
# 强制 UTF-8 输出，保证这个脚本在 PowerShell 里直接跑就正常。
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:
    pass

from campus_config import CAMPUSES, frame_of, frame_size_m  # noqa: E402

RAW = HERE / "data" / "raw"

# 每度对应的米数（纬度方向固定，经度方向随纬度收缩）
M_PER_DEG_LAT = 111320.0


def m_per_deg_lon(lat: float) -> float:
    return M_PER_DEG_LAT * math.cos(math.radians(lat))


def _ring_extent(coords, acc):
    """递归遍历 GeoJSON 坐标，累积 (S, W, N, E)。"""
    if not coords:
        return acc
    if isinstance(coords[0], (int, float)):
        lon, lat = coords[0], coords[1]
        if acc is None:
            return (lat, lon, lat, lon)
        s, w, n, e = acc
        return (min(s, lat), min(w, lon), max(n, lat), max(e, lon))
    for part in coords:
        acc = _ring_extent(part, acc)
    return acc


def buildings_extent(campus_key: str):
    """校区 OSM 建筑的外接范围 (S, W, N, E)；没有数据返回 None。"""
    path = RAW / f"{campus_key}_buildings.geojson"
    if not path.exists():
        return None, 0
    data = json.loads(path.read_text(encoding="utf-8"))
    ext = None
    n = 0
    for feat in data.get("features", []):
        geom = feat.get("geometry")
        if not geom:
            continue
        ext = _ring_extent(geom.get("coordinates"), ext)
        n += 1
    return ext, n


def pad_extent(ext, pad_m: float):
    """把外接范围四周各外扩 pad_m 米。"""
    s, w, n, e = ext
    dlat = pad_m / M_PER_DEG_LAT
    dlon = pad_m / m_per_deg_lon((s + n) / 2)
    return (
        round(s - dlat, 6),
        round(w - dlon, 6),
        round(n + dlat, 6),
        round(e + dlon, 6),
    )


def dims(ext) -> tuple[float, float]:
    s, w, n, e = ext
    return (
        (e - w) * m_per_deg_lon((s + n) / 2),
        (n - s) * M_PER_DEG_LAT,
    )


def contains(outer, inner, tol: float = 1e-9) -> bool:
    return (
        outer[0] <= inner[0] + tol
        and outer[1] <= inner[1] + tol
        and outer[2] >= inner[2] - tol
        and outer[3] >= inner[3] - tol
    )


def fmt(ext) -> str:
    s, w, n, e = ext
    return f"({s:.6f}, {w:.6f}, {n:.6f}, {e:.6f})"


def report(pad_m: float) -> int:
    print(f"外框缓冲宽度：四周各 {pad_m:.0f} m\n")
    for key, cfg in CAMPUSES.items():
        ext, n = buildings_extent(key)
        print(f"===== {cfg.name} ({key}) =====")
        if not ext:
            print(f"  ⚠ OSM 无建筑数据（{n} 栋）→ 外框只能人工标定")
            cur = frame_of(key)
            cw, ch = frame_size_m(key)
            print(f"  当前 frame = {fmt(cur)}")
            print(f"  尺寸 {cw:.0f} × {ch:.0f} m  长宽比 {cw / ch:.3f}")
            print("  （苏州校区：依卫星影像 data/tile_check/suzhou_landmark_check.png 标定）")
            print()
            continue
        bw, bh = dims(ext)
        print(f"  建筑 {n} 栋，外接范围 {fmt(ext)}")
        print(f"  建筑跨度 {bw:.0f} × {bh:.0f} m  长宽比 {bw / bh:.3f}")
        sug = pad_extent(ext, pad_m)
        sw, sh = dims(sug)
        print(f"  建议 frame = {fmt(sug)}")
        print(f"  建议尺寸 {sw:.0f} × {sh:.0f} m  长宽比 {sw / sh:.3f}")
        cur = frame_of(key)
        ok = contains(cur, ext)
        print(f"  当前 frame 是否包住全部建筑：{'✓ 是' if ok else '✗ 否 —— 有建筑被切在框外！'}")
        if not ok:
            s0, w0, n0, e0 = ext
            s1, w1, n1, e1 = cur
            print(
                f"    缺口：南 {(s0 - s1) * M_PER_DEG_LAT:+.0f} m  "
                f"西 {(w0 - w1) * m_per_deg_lon((s0 + n0) / 2):+.0f} m  "
                f"北 {(n1 - n0) * M_PER_DEG_LAT:+.0f} m  "
                f"东 {(e1 - e0) * m_per_deg_lon((s0 + n0) / 2):+.0f} m  "
                f"（负数=被切掉）"
            )
        print()
    return 0


def verify(pad_ratio_tol: float = 0.35) -> int:
    """校验当前配置：外框必须包住全部建筑，且长宽比不能太离谱。"""
    fails: list[str] = []
    print("校验各校区外框配置\n")
    for key, cfg in CAMPUSES.items():
        ext, n = buildings_extent(key)
        cur = frame_of(key)
        cw, ch = frame_size_m(key)
        ratio = cw / ch
        print(f"  {cfg.short}: frame 尺寸 {cw:.0f} × {ch:.0f} m  长宽比 {ratio:.3f}")

        if cur != cfg.frame:
            print(f"    ⚠ 未配置 frame，当前退回 bbox")
            fails.append(f"{key}: 未配置 frame")

        if not (0.4 < ratio < 2.6):
            print(f"    ✗ 长宽比 {ratio:.2f} 过于极端，地图视野会很难看")
            fails.append(f"{key}: 长宽比 {ratio:.2f}")

        if ext:
            if not contains(cur, ext):
                print(f"    ✗ 有建筑落在框外 —— 同学在校园里点选会被误标「待复核」")
                fails.append(f"{key}: 框切掉建筑")
            else:
                # 建筑跨度与外框跨度之比，太接近 1 说明几乎没留缓冲
                bw, bh = dims(ext)
                if cw / bw < 1.02 or ch / bh < 1.02:
                    print(f"    ⚠ 缓冲过小（框 {cw:.0f}×{ch:.0f} vs 建筑 {bw:.0f}×{bh:.0f}）")
                    fails.append(f"{key}: 缓冲过小")
                else:
                    print(f"    ✓ 包住全部 {n} 栋建筑，四周留有余量")
        else:
            print(f"    ⚠ OSM 无建筑数据，无法自动校验（依赖人工标定）")
    print()
    if fails:
        print(f"✗ {len(fails)} 项需要处理：" + "、".join(fails))
        return 1
    print("✓ 外框配置全部通过")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="校区外框计算与校验")
    ap.add_argument("--pad", type=float, default=150.0, help="缓冲宽度（米），默认 150")
    ap.add_argument("--verify", action="store_true", help="只校验当前配置，不打印建议值")
    args = ap.parse_args()
    if args.verify:
        return verify()
    return report(args.pad)


if __name__ == "__main__":
    raise SystemExit(main())
