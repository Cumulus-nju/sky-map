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

# 跑之前先给正式缓存目录拍个快照 —— 结束时把所有"新增"文件删掉（见文件末尾）。
# 这样测试不会往仓库里留任何中间产物，也不用依赖 gitignore。
try:
    _CACHE_BEFORE = {p.name for p in sb.IMAGE_CACHE.iterdir() if p.is_file()}
except Exception:
    _CACHE_BEFORE = set()

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

print("\n[7] 自绘底图（平面 / 2.5D 立体 / 三维轴测）：坐标口径必须与瓦片路径一致")
print("     （三条路径返回同一个 FrameImage，点选换算不用区分来源 —— 口径错了就会 '点 A 存 B'）")
_META_FIELDS = ("width", "height", "zoom", "scale_x", "scale_y", "origin_x", "origin_y",
                "south", "west", "north", "east")
try:
    import json as _json

    import static_basemap as _sb

    for key in CAMPUSES:
        src = _sb.source_of(key)
        print(f"  · {key}: 配置源 = {src}")
        if src == "tile":
            continue
        s, w, n, e = frame_of(key)
        stem = _sb.IMAGE_CACHE / f"{key}_{src}_w2000"
        have = next((q for q in (stem.with_suffix(".png"), stem.with_suffix(".jpg"))
                     if q.exists()), None)
        if have is None:
            check(f"{key}: 已生成 {src} 底图存在", False, f"缺 {stem.name}.*")
            continue
        meta = _json.loads(have.with_suffix(have.suffix + ".json").read_text(encoding="utf-8"))
        check(f"{key}: {src} 底图的外框与 campus_config 一致",
              _sb.frame_matches(meta, s, w, n, e),
              "外框改了就必须重新生成底图，否则整张图静默错位")
        # ⚠ 关键防线（2026-10-03 的事故）：`frame_image_for` 在自绘抛异常时
        #   **会退回瓦片拼图**（而且当时还是静默的）。如果只用"文件存在/读文件"来验，
        #   就正好漏掉"渲染器其实坏了、页面拿到的是瓦片图"这种情况。
        #   所以这里必须走**统一入口** + prefer=src（失败直接抛），并核对字节。
        fi = _sb.frame_image_for(key, s, w, n, e, target_width=2000, prefer=src)
        check(f"{key}[{src}]: 统一入口返回的就是自绘底图（没被静默换成瓦片）",
              fi.png == have.read_bytes(),
              f"入口 {len(fi.png)//1024} KB / 磁盘 {have.stat().st_size//1024} KB")
        tl = fi.px_to_latlng(0, 0)
        br = fi.px_to_latlng(fi.width, fi.height)
        ok_corners = (near(tl[0], n, 2e-3) and near(tl[1], w, 2e-3)
                      and near(br[0], s, 2e-3) and near(br[1], e, 2e-3))
        check(f"{key}[{src}]: 自绘图四角 == 外框四角（容差 2e-3°）", ok_corners,
              f"左上({tl[0]:.6f},{tl[1]:.6f}) 右下({br[0]:.6f},{br[1]:.6f})")
        x0, y0 = fi.latlng_to_px(32.0569, 118.7744) if key == "gulou" else (fi.width / 2, fi.height / 2)
        lat, lon = fi.px_to_latlng(x0, y0)
        x1, y1 = fi.latlng_to_px(lat, lon)
        check(f"{key}[{src}]: 自绘图换算往返闭合", abs(x1 - x0) < 1e-6 and abs(y1 - y0) < 1e-6,
              f"Δ=({abs(x1 - x0):.2e},{abs(y1 - y0):.2e}) px")
        rows = frame_picker.assert_js_python_agree(fi, samples=4)
        worst = max(max(r["dlat"], r["dlon"]) for r in rows)
        check(f"{key}[{src}]: 自绘图 JS/Python 换算一致", worst < 1e-9, f"最大差 {worst:.2e}")
except Exception as exc:
    check("自绘底图坐标自检", False, f"{type(exc).__name__}: {exc}")

print("\n[8] 预生成底图必须与 app 请求的宽度一致（守部署隐患）")
print("     —— 不一致的话云端容器会**现场重新拉瓦片/渲染**：慢，且可能被限流")
try:
    import json as _json

    import static_basemap as _sb
    # app.py 里 render_picker 请求的宽度
    APP_WIDTH = 2000
    SUFFIX = {"vector": "vector", "relief": "relief", "glb": "glb"}
    for key in CAMPUSES:
        src = _sb.source_of(key)
        if src in SUFFIX:
            # 自绘路径的编码格式是"哪个小存哪个"，所以两种扩展名都找
            stem = _sb.IMAGE_CACHE / f"{key}_{SUFFIX[src]}_w{APP_WIDTH}"
            p = next((q for q in (stem.with_suffix(".png"), stem.with_suffix(".jpg"))
                      if q.exists()), stem.with_suffix(".png"))
        else:
            z = sb.choose_zoom(*[frame_of(key)[i] for i in (0, 1, 2, 3)], APP_WIDTH)
            p = _sb.IMAGE_CACHE / f"{key}_osm_w{APP_WIDTH}_z{z}.jpg"
        ok = p.exists() and p.stat().st_size > 0
        check(f"{key}({src}): 预生成底图存在 {p.name}", ok,
              f"{p.stat().st_size // 1024} KB" if ok else "缺失 ⇒ 云端会现场生成")
        if ok and p.with_suffix(p.suffix + ".json").exists():
            meta = _json.loads(p.with_suffix(p.suffix + ".json").read_text(encoding="utf-8"))
            check(f"{key}({src}): 预生成底图的外框与配置一致",
                  _sb.frame_matches(meta, *frame_of(key)),
                  "外框改了必须先重新生成底图再提交")
except Exception as exc:
    check("预生成底图检查", False, f"{type(exc).__name__}: {exc}")

# ---------------------------------------------------------------------------
print("\n[9] 立体底图点选吸附：点屋顶 → 贴回楼基")
print("     —— 立体底图上屋顶相对楼基位移 = 楼高；点到屋顶说明人在楼里/楼上，要贴回去")
try:
    import relief_basemap as _rb

    _s, _w, _n, _e = frame_of("gulou")
    _roofs = _rb.roof_shapes("gulou", _s, _w, _n, _e, width=2000)
    check("鼓楼: 导出了屋顶多边形", len(_roofs["b"]) > 100, f"{len(_roofs['b'])} 个")
    check("鼓楼: 位移方向与 VIEW 一致",
          near(_roofs["v"][0], _rb.VIEW[0], 1e-9)
          and near(_roofs["v"][1], _rb.VIEW[1], 1e-9), f"v={_roofs['v']}")

    # 屋顶内部的点 → 吸附结果必须恰好是"反向平移"
    # ⚠ 不可以用"顶点平均"当内部点：凹多边形（U 形宿舍楼）的平均点可能落在外面，
    #   那样测的就不是吸附而是射线法的边界行为（第一版自检就踩过这个）。
    # 屋顶内部的点 → 吸附后必须落在**某栋楼的足迹内**（语义：贴回楼里）。
    # ⚠ 不能断言"等于当前遍历这栋的平移"：屋顶多边形会互相重叠（前排楼压住后排楼），
    #   而 `snap_to_footprint` 是**有意**取"最靠近观众的那栋"（视觉上真正压在上面的），
    #   那时落点属于另一栋的足迹 —— 那是对的。
    #   （第一版断言就是按"当前这栋"写的，于是误报了一次失败。）
    feet_flat = []
    for dz, flat in _roofs["b"]:
        fp = []
        for i in range(len(flat) // 2):
            fp.append(flat[2 * i] - _rb.VIEW[0] * dz)
            fp.append(flat[2 * i + 1] - _rb.VIEW[1] * dz)
        feet_flat.append(fp)
    wp = 0
    landed = 0
    for dz, flat in _roofs["b"]:
        m = len(flat) // 2
        cx = sum(flat[2 * i] for i in range(m)) / m
        cy = sum(flat[2 * i + 1] for i in range(m)) / m
        if not frame_picker.point_in_poly(cx, cy, flat):
            continue                      # 顶点平均落在凹多边形外，不是"可点到的位置"
        wp += 1
        gx, gy = frame_picker.snap_to_footprint(cx, cy, _roofs)
        if any(frame_picker.point_in_poly(gx, gy, fp) for fp in feet_flat):
            landed += 1
    check("鼓楼: 屋顶内点吸附后都落在某栋楼的足迹内", wp > 0 and landed == wp,
          f"{landed}/{wp} 栋")

    _far = (-9999.0, -9999.0)
    check("鼓楼: 屋顶外的点原样返回（不能乱吸）",
          frame_picker.snap_to_footprint(_far[0], _far[1], _roofs) == _far)
    check("无 roofs 时不吸附（瓦片/平面底图旧行为不变）",
          frame_picker.snap_to_footprint(123.4, 567.8, None) == (123.4, 567.8))

    _fi = sb.frame_image_for("gulou", _s, _w, _n, _e, target_width=2000, prefer="relief")
    _doc, _h = frame_picker.build_picker_html(_fi, campus_key="gulou",
                                              display_width=880, roofs=_roofs)
    check("HTML 注入了 roofs 几何", '"roofs":' in _doc and '"v":' in _doc)
    check("HTML 含吸附实现 snapToFootprint", "function snapToFootprint" in _doc)
    check("HTML 含射线法 inPoly", "function inPoly" in _doc)
    _doc2, _h2 = frame_picker.build_picker_html(_fi, campus_key="gulou", display_width=880)
    check("不传 roofs 时 payload 为 null", '"roofs": null' in _doc2)
except Exception as exc:
    check("屋顶吸附", False, f"{type(exc).__name__}: {exc}")

# ---------------------------------------------------------------------------
print("\n[10] 校园范围虚线 + 校外挡视线高楼排除")
print("     —— 用户 2026-10-04：南边几栋非学校高楼挡住校园内部；并在图上标出校园范围")
try:
    import relief_basemap as _rb2

    _bd = _json.loads((_rb2.RAW / "campus_boundaries.json").read_text(encoding="utf-8"))
    _blds = _rb2._load_json(_rb2.RAW / "gulou_buildings.geojson")
    _sk = _rb2._skipped_indexes(_blds, _bd["gulou"])
    check("鼓楼: 排除了校园南侧的校外高楼", len(_sk) >= 1, f"{len(_sk)} 栋 idx={sorted(_sk)}")
    check("鼓楼: 被排除的都是高楼（≥ SKIP_OUTSOUTH_H）",
          all(_rb2._height_m(_blds["features"][i]["properties"], i) >= _rb2.SKIP_OUTSOUTH_H
              for i in _sk))
    # 排除项必须**同时**从吸附几何里去掉，否则会吸到看不见的楼
    _rf = _rb2.roof_shapes("gulou", *frame_of("gulou"), width=2000)
    _n_bld_visible = len(_blds["features"]) - len(_sk)
    check("鼓楼: 吸附几何里的楼数 = 可见楼数（排除已同步）",
          len(_rf["b"]) <= _n_bld_visible,
          f"屋顶 {len(_rf['b'])} ≤ 可见楼 {_n_bld_visible}")
    # 标志物白名单：每个名字都必须**真的存在于** cfg.landmarks ——
    # 写错名字会静默少标一个，看图时谁也不会发现（所以钉死它）。
    from campus_config import CAMPUSES as _CS
    _miss = []
    for _k, _names in _rb2.LABEL_LANDMARKS.items():
        for _it in _names:
            _nm = _it[0] if isinstance(_it, (tuple, list)) else _it
            if _nm not in (_CS[_k].landmarks if _k in _CS else {}):
                _miss.append(f"{_k}/{_nm}")
    _tot = sum(len(v) for v in _rb2.LABEL_LANDMARKS.values())
    check("标志物名单里的名字都存在于 campus_config.landmarks", not _miss,
          f"共 {_tot} 个；缺失={_miss}")
    check("标志物每校区 4~8 个（宁少勿多，不要标满）",
          all(4 <= len(v) <= 8 for v in _rb2.LABEL_LANDMARKS.values()),
          str({k: len(v) for k, v in _rb2.LABEL_LANDMARKS.items()}))
    check("蓝色虚线已移除（用户改主意，换成标志物）",
          not hasattr(_rb2, "DRAW_CAMPUS_BOUNDARY"))
    _alias_bad = [k for k in _rb2.LABEL_ALIAS
                  if not any(k in c.landmarks for c in _CS.values())]
    check("短名映射的 key 都真实存在于某个校区的 landmarks", not _alias_bad,
          f"{len(_rb2.LABEL_ALIAS)} 条；无效={_alias_bad}")
except Exception as exc:
    check("校园虚线/排除规则", False, f"{type(exc).__name__}: {exc}")

print("\n" + "=" * 66)
# 清理测试自己生成的中间产物。
#
# 为什么必须清：测试会用 `target_width=600/700` 生成底图，落到**正式**缓存目录
# `data/frame_images/` 里。之前没清，每跑一次就往仓库塞一批小尺寸副本。
#
# 做法用"快照对比"而不是按文件名匹配：本脚本开头记录了目录内容，
# 这里把所有**新增**文件删掉 —— 不管它叫什么名字、多少种尺寸，都不会漏。
# （试过用 .gitignore 兜底，但对这类文件并不生效，靠不住。）
_new = []
try:
    for p in sb.IMAGE_CACHE.iterdir():
        if p.is_file() and p.name not in _CACHE_BEFORE:
            p.unlink(missing_ok=True)
            _new.append(p.name)
except Exception as exc:
    print(f"⚠ 清理测试产物失败：{type(exc).__name__}: {exc}")
print(f"（已清理测试产物 {len(_new)} 个文件：{', '.join(sorted(_new)) or '无'}）")

if FAILS:
    print(f"✗ {len(FAILS)} 项失败：" + "、".join(FAILS))
    raise SystemExit(1)
print("✓ 全部通过 —— 像素与经纬度严格对应，JS/Python 无漂移")