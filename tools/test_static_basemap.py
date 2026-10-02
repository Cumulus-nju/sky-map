# -*- coding: utf-8 -*-
"""静态底图方案的正确性测试。

为什么必须有这个测试
--------------------
静态底图方案把"点选"变成了**纯数学换算**，好处是没有视图尺寸参与、永远对得齐；
代价是**一旦算错，不会报错，只会把坐标悄悄存错**（同学以为点的是 A 楼，存的是 B 楼）。
所以这里把三件事钉死：

  1. 像素 ↔ 经纬度 往返必须闭合（浮点误差级）；
  2. 图像四角必须等于外框四角（容差几百米级以内 —— 裁剪取整会带来亚像素偏移）；
  3. **浏览器里那套 JS 公式与 Python 的必须等价** —— 两边各写一遍，最容易悄悄漂移。

用法：python tools\\test_static_basemap.py
"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import frame_picker  # noqa: E402
import static_basemap as sb  # noqa: E402
from frame_util import frame_of  # noqa: E402

FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'✓' if ok else '✗'} {name}" + (f"  {detail}" if detail else ""))
    if not ok:
        FAILS.append(name)


def near(a: float, b: float, tol: float) -> bool:
    return abs(a - b) <= tol


print("=" * 66)
print(" 静态底图：换算正确性 / 四角对齐 / JS-Python 一致性")
print("=" * 66)

CAMPUSES = ("gulou", "xianlin", "suzhou")

# ---------------------------------------------------------------------------
print("\n[1] 纯数学：像素 ↔ 经纬度 往返闭合")
for key in CAMPUSES:
    s, w, n, e = frame_of(key)
    z = sb.choose_zoom(s, w, n, e, 1400)
    px0, py0 = sb._lon_to_px(w, z), sb._lat_to_px(n, z)
    px1, py1 = sb._lon_to_px(e, z), sb._lat_to_px(s, z)
    scale = 1400 / (px1 - px0)
    hh = (py1 - py0) * scale
    worst = 0.0
    for x, y in ((0, 0), (1400, hh), (700, hh / 2), (123, 456), (1399, hh - 1)):
        lat, lon = sb.latlng_from_image_px(x, y, zoom=z, scale_x=scale, scale_y=scale,
                                           origin_x=px0, origin_y=py0)
        x2, y2 = sb.image_px_from_latlng(lat, lon, zoom=z, scale_x=scale, scale_y=scale,
                                         origin_x=px0, origin_y=py0)
        worst = max(worst, abs(x2 - x), abs(y2 - y))
    check(f"{key}: 往返误差 < 1e-6 px", worst < 1e-6, f"最大 {worst:.3e} px")

# ---------------------------------------------------------------------------
print("\n[2] 图像四角 == 外框四角（含裁剪取整的亚像素偏移）")
for key in CAMPUSES:
    s, w, n, e = frame_of(key)
    # 用一张"假图"跑换算，不需要联网拉瓦片
    fi = sb.FrameImage(
        png=b"", width=1400, height=1553, zoom=16,
        scale_x=1.0, scale_y=1.0, origin_x=0.0, origin_y=0.0,
        south=s, west=w, north=n, east=e,
    )
    check(f"{key}: 元数据自洽（south<north, west<east）",
          fi.south < fi.north and fi.west < fi.east)

    # 真图（有缓存就用缓存，没有就跳过联网部分，不让测试依赖网络）
    try:
        img = sb.cached_frame_image(key, s, w, n, e, target_width=600)
        tl = img.px_to_latlng(0, 0)
        br = img.px_to_latlng(img.width, img.height)
        # 容差：裁剪取整可能带来约 1 像素偏移，1 px ≈ 2.4 m ≈ 2.2e-5 度，取 0.002 度(≈220m) 很宽松
        check(f"{key}: 左上角≈(north,west)", near(tl[0], n, 2e-3) and near(tl[1], w, 2e-3),
              f"({tl[0]:.6f},{tl[1]:.6f}) vs ({n:.6f},{w:.6f})")
        check(f"{key}: 右下角≈(south,east)", near(br[0], s, 2e-3) and near(br[1], e, 2e-3),
              f"({br[0]:.6f},{br[1]:.6f}) vs ({s:.6f},{e:.6f})")
    except Exception as exc:
        print(f"  … {key}: 跳过真图检查（{type(exc).__name__}: {str(exc)[:60]}）")

# ---------------------------------------------------------------------------
print("\n[3] ★ 浏览器 JS 公式 vs Python 公式 必须等价")
print("     （两边各写一遍，最容易静默漂移 —— 漂移了就是'点 A 存 B'）")
for key in CAMPUSES:
    s, w, n, e = frame_of(key)
    try:
        img = sb.cached_frame_image(key, s, w, n, e, target_width=600)
    except Exception as exc:
        print(f"  … {key}: 无图，用合成 FrameImage（{type(exc).__name__}）")
        img = sb.FrameImage(png=b"", width=1000, height=1000, zoom=16,
                            scale_x=1.0, scale_y=1.0, origin_x=0.0, origin_y=0.0,
                            south=s, west=w, north=n, east=e)
    rows = frame_picker.assert_js_python_agree(img, samples=5)
    worst_lat = max(r["dlat"] for r in rows)
    worst_lon = max(r["dlon"] for r in rows)
    check(f"{key}: JS 与 Python 一致（<1e-9 度）",
          worst_lat < 1e-9 and worst_lon < 1e-9,
          f"最大差 lat={worst_lat:.2e} lon={worst_lon:.2e}")

# ---------------------------------------------------------------------------
print("\n[4] 选择缩放级：宽度应接近目标，且不超框")
for key in CAMPUSES:
    s, w, n, e = frame_of(key)
    z = sb.choose_zoom(s, w, n, e, 1400)
    span = sb._lon_to_px(e, z) - sb._lon_to_px(w, z)
    check(f"{key}: zoom={z} 世界宽度 {span:.0f}px ≤ 1400（不超框）", span <= 1400)

# ---------------------------------------------------------------------------
print("\n[5] 组件 HTML 生成不报错，且含关键要素")
try:
    s, w, n, e = frame_of("gulou")
    img = sb.FrameImage(png=b"\x89PNG\r\n\x1a\n" + b"0" * 64, width=1400, height=1553,
                        zoom=16, scale_x=1.1, scale_y=1.1, origin_x=100.0, origin_y=200.0,
                        south=s, west=w, north=n, east=e)
    doc, h = frame_picker.build_picker_html(img, campus_key="gulou", display_width=880,
                                            picked=(32.05714, 118.77421),
                                            landmarks={"北大楼": (32.05714, 118.77421)})
    check("HTML 生成成功且有高度", isinstance(doc, str) and h > 0, f"高度 {h:.0f}px")
    check("含 data URL 底图", "data:image/png;base64," in doc)
    check("含点击处理", "addEventListener('click'" in doc)
    check("含查询参数回传", "searchParams.set('pick'" in doc)
    check("经纬度非空", "nwLat" in doc and "seLat" in doc)
except Exception as exc:
    check("组件 HTML 生成", False, f"{type(exc).__name__}: {exc}")

print("\n" + "=" * 66)
if FAILS:
    print(f"✗ {len(FAILS)} 项失败：" + "、".join(FAILS))
    raise SystemExit(1)
print("✓ 全部通过 —— 像素与经纬度严格对应，JS/Python 无漂移")
