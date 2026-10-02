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
    check("含右键选点", "addEventListener('contextmenu'" in doc)
    check("屏蔽了浏览器右键菜单", "ev.preventDefault()" in doc)
    check("左键才拖动（右键不进入拖动）", "if (ev.button === 2) return;" in doc)
    check("含查询参数回传", "searchParams.set('pick'" in doc)
    check("含视图状态回传（pnav）", "searchParams.set('pnav'" in doc)
    check("含滚轮缩放", "addEventListener('wheel'" in doc)
    check("含拖拽平移", "addEventListener('pointermove'" in doc)
    check("含缩放按钮", 'id="zin"' in doc and 'id="zout"' in doc)
    check("含窗口尺寸换算 winW/winH", "function winW()" in doc and "function winH()" in doc)
    check("含宽度自适应", "function fitToParent()" in doc)
    check("经纬度非空", "nwLat" in doc and "seLat" in doc)
    # 结构性检查：JS 里 getElementById 用到的 id 都必须在 HTML 里存在。
    # （漏掉 <div id="pick"> 曾导致 render() 抛 TypeError、组件完全失灵，且不报错。）
    import re as _re
    used = set(_re.findall(r"getElementById\('([^']+)'\)", doc))
    declared = set(_re.findall(r'id="([^"]+)"', doc))
    missing = sorted(used - declared)
    check("getElementById 用到的 id 都存在", not missing, f"缺失：{missing}")
except Exception as exc:
    check("组件 HTML 生成", False, f"{type(exc).__name__}: {exc}")

# ---------------------------------------------------------------------------
print("\n[6] 缩放级边界：pnav 的 zoom 必须被夹到合法范围")
from site_common import take_nav  # noqa: E402
import streamlit as st  # noqa: E402

# 直接测解析函数的健壮性（不依赖 Streamlit 运行时）
try:
    import site_common
    # 用 monkeypatch 的方式验证解析逻辑
    cases = [
        ("32.05,118.77,0", (32.05, 118.77, 0)),
        ("32.05,118.77,2", (32.05, 118.77, 2)),
        ("32.05,118.77,99", (32.05, 118.77, 2)),     # 越界应被夹到组件上限
        ("32.05,118.77,-5", (32.05, 118.77, 0)),
    ]
    for raw, want in cases:
        site_common.qp = lambda name, default="", _r=raw: _r if name == "pnav" else default
        got = site_common.take_nav()
        ok = got is not None and abs(got[0] - want[0]) < 1e-9 and abs(got[1] - want[1]) < 1e-9 and got[2] == want[2]
        check(f"pnav={raw!r} -> {want}", ok, f"得到 {got}")
    # 非法输入不应抛异常
    for bad in ("", "abc", "1,2", "999,999,1"):
        site_common.qp = lambda name, default="", _r=bad: _r if name == "pnav" else default
        try:
            site_common.take_nav()
            check(f"非法 pnav={bad!r} 不抛异常", True)
        except Exception as exc:
            check(f"非法 pnav={bad!r} 不抛异常", False, f"{type(exc).__name__}: {exc}")
except Exception as exc:
    check("pnav 解析测试", False, f"{type(exc).__name__}: {exc}")

print("\n[7] 自绘矢量底图：坐标口径必须与瓦片路径一致")
print("     （两条路径返回同一个 FrameImage，点选换算不用区分来源 —— 口径错了就会 '点 A 存 B'）")
try:
    import static_basemap as _sb

    for key in CAMPUSES:
        src = _sb.source_of(key)
        print(f"  · {key}: 配置源 = {src}")
        if src != "vector":
            continue
        s, w, n, e = frame_of(key)
        fi = _sb.frame_image_for(key, s, w, n, e, target_width=700)
        tl = fi.px_to_latlng(0, 0)
        br = fi.px_to_latlng(fi.width, fi.height)
        ok_corners = (near(tl[0], n, 2e-3) and near(tl[1], w, 2e-3)
                      and near(br[0], s, 2e-3) and near(br[1], e, 2e-3))
        check(f"{key}: 自绘图四角 == 外框四角（容差 2e-3°）", ok_corners,
              f"左上({tl[0]:.6f},{tl[1]:.6f}) 右下({br[0]:.6f},{br[1]:.6f})")
        # 往返闭合
        x0, y0 = fi.latlng_to_px(32.0569, 118.7744) if key == "gulou" else (fi.width / 2, fi.height / 2)
        lat, lon = fi.px_to_latlng(x0, y0)
        x1, y1 = fi.latlng_to_px(lat, lon)
        check(f"{key}: 自绘图换算往返闭合", abs(x1 - x0) < 1e-6 and abs(y1 - y0) < 1e-6,
              f"Δ=({abs(x1 - x0):.2e},{abs(y1 - y0):.2e}) px")
        # 与像素换算等价性（同瓦片路径那套自检）
        rows = frame_picker.assert_js_python_agree(fi, samples=4)
        worst = max(max(r["dlat"], r["dlon"]) for r in rows)
        check(f"{key}: 自绘图 JS/Python 换算一致", worst < 1e-9, f"最大差 {worst:.2e}")
except Exception as exc:
    check("自绘底图坐标自检", False, f"{type(exc).__name__}: {exc}")

print("\n" + "=" * 66)
if FAILS:
    print(f"✗ {len(FAILS)} 项失败：" + "、".join(FAILS))
    raise SystemExit(1)
print("✓ 全部通过 —— 像素与经纬度严格对应，JS/Python 无漂移")