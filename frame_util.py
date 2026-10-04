"""外框（frame）取值的**唯一兜底入口** —— 让页面在云端模块状态不一致时也不崩。

为什么要有这个文件
------------------
2026-10-01 线上白屏的**真实原因**（拿到完整 traceback 后确认，不是先前猜的"容器状态
不一致"这么含糊）：

    云端容器里 `campus_config.py` 是**旧版**（Campus 数据类没有 `frame` 字段，
    也没有 `frame_of` / `min_zoom_for_frame`），而 `app.py` 是**新版**。

于是执行链变成：

    from campus_config import ..., frame_of, min_zoom_for_frame   # ImportError（旧模块没这俩名字）
      → 走 except 兜底
      → 兜底里写的是 `c.frame`                                     # AttributeError（旧数据类没这个字段）
      → 整站崩，白屏。

教训：**兜底代码比正式代码更容易写错，因为它平时跑不到。**
所以这里把兜底收敛成一份、带**三级降级**，并且 `tools/test_frame_fallback.py`
会真的造一个"旧 campus_config"（既缺函数又缺字段）来跑它 —— 上一次的测试只模拟了
"缺函数"，把字段当成一定存在，所以测试全绿而线上照崩。

三级降级
--------
1. 模块里有 `frame_of`   → 直接用（正常情况）
2. 校区对象有 `frame` 字段 → 用 frame，空值退回 bbox
3. 都没有                → 用本文件内置的 `_FALLBACK` 表（校园 + 150 m 缓冲实测值）

第 3 级的数据与 `campus_config.py` 保持一致，由测试固化；改了配置忘了改这里会报错。
"""
from __future__ import annotations

import math

# ---------------------------------------------------------------- 第 3 级兜底数据
#
# (south, west, north, east)，即 campus_config.Campus.frame 的实测值。
# 生成方法见 tools/compute_frames.py。**不要手改**，改完跑 tools/test_frame_fallback.py。
_FALLBACK: dict[str, tuple[float, float, float, float]] = {
    "gulou": (32.049223, 118.766207, 32.064661, 118.782631),
    "xianlin": (32.108661, 118.944348, 32.131039, 118.964916),
    # 2026-10-03：苏州外框由"卫星影像人工标定"改为按**模型实测建筑范围 + 150 m 缓冲**
    # 计算（总平面 935 个精确足迹 + data/suzhou_georef.json），旧框比校园大 50%。
    "suzhou": (31.346885, 120.370471, 31.359385, 120.387431),
}

# 第 2/3 级算 minZoom 时用到的中心纬度（与 campus_config.Campus.center 一致）
_CENTER_LAT: dict[str, float] = {
    "gulou": 32.056942,
    "xianlin": 32.119850,
    "suzhou": 31.3532,
}

# 实在查不到时用的默认值（南京一带，越小越安全 —— 视野会稍宽，不会把校园切掉）
_DEFAULT_FRAME = (32.04, 118.76, 32.14, 118.98)
_DEFAULT_LAT = 32.09


def _module():
    """取当前进程里的 campus_config 模块（拿不到就返回 None）。"""
    try:
        import campus_config as cfg
    except Exception:
        return None
    return cfg


def _campus_obj(key: str):
    """取校区配置对象；模块里没有 campus() 时退回 CAMPUSES 字典。"""
    cfg = _module()
    if cfg is None:
        return None
    try:
        fn = getattr(cfg, "campus", None)
        if callable(fn):
            return fn(key)
        return (getattr(cfg, "CAMPUSES", None) or {}).get(key)
    except Exception:
        return None


def frame_of(key: str) -> tuple[float, float, float, float]:
    """取校区外框 (S, W, N, E)，三级降级，**任何情况下都不抛异常**。"""
    # 1) 模块提供 frame_of 就用它
    cfg = _module()
    fn = getattr(cfg, "frame_of", None) if cfg is not None else None
    if callable(fn):
        try:
            got = fn(key)
            if got and tuple(got) != (0.0, 0.0, 0.0, 0.0):
                return tuple(got)  # type: ignore[return-value]
        except Exception:
            pass  # 继续降级，绝不因为这个把页面搞崩

    # 2) 校区对象上有 frame / bbox 字段
    obj = _campus_obj(key)
    if obj is not None:
        for attr in ("frame", "bbox"):
            val = getattr(obj, attr, None)  # ⚠ 必须用 getattr：旧版 Campus 没有 frame
            if val and tuple(val) != (0.0, 0.0, 0.0, 0.0):
                return tuple(val)  # type: ignore[return-value]

    # 3) 本文件内置的兜底表
    if key in _FALLBACK:
        return _FALLBACK[key]
    return _DEFAULT_FRAME


def min_zoom_for_frame(key: str, view_w: float = 1000.0, view_h: float = 460.0) -> int:
    """算"刚好把外框装进视口"的缩放级（地图可缩到的最小级）。

    优先用模块里的实现（它是权威的）；拿不到就用下面的兜底公式，两者一致。
    """
    cfg = _module()
    fn = getattr(cfg, "min_zoom_for_frame", None) if cfg is not None else None
    if callable(fn):
        try:
            return int(fn(key, view_w, view_h))
        except Exception:
            pass

    return _compute_min_zoom(frame_of(key), _center_lat(key), view_w, view_h)


def _center_lat(key: str) -> float:
    obj = _campus_obj(key)
    center = getattr(obj, "center", None) if obj is not None else None
    if center:
        try:
            return float(center[0])
        except Exception:
            pass
    return _CENTER_LAT.get(key, _DEFAULT_LAT)


def _compute_min_zoom(frame: tuple[float, float, float, float], lat: float,
                      view_w: float, view_h: float, pad: float = 1.15) -> int:
    """Web Mercator：缩放 0 时 1 像素 = 156543.03392·cos(lat) 米。

    外框在缩放 z 下占的像素 = 框米数 · 2^z / (156543.03392·cos(lat))，
    令它等于视口尺寸解出 z，宽高取较小者（两个方向都得装得下）。
    `pad` 是允许"比框再往外缩一点"的余量。
    """
    s, w, n, e = frame
    w_m = (e - w) * 111320.0 * math.cos(math.radians(lat)) * pad
    h_m = (n - s) * 111320.0 * pad
    if w_m <= 0 or h_m <= 0:
        return 12
    px0 = 156543.03392 * math.cos(math.radians(lat))
    zoom = math.log2(min(view_w / (w_m / px0), view_h / (h_m / px0)))
    return max(11, min(18, int(math.floor(zoom))))


def zoom_to_fit(key: str, view_w: float, view_h: float) -> int:
    """把外框**正好装进视口**所需的缩放级（不留余量）。

    这是地图的初始视野：进去就先看见整个蓝框。实际生效的视野由浏览器端的
    `fitBounds` 决定，这里主要用于服务端估算与容器高度换算。
    """
    return _compute_min_zoom(frame_of(key), _center_lat(key), view_w, view_h, pad=1.0)


def frame_center(key: str) -> tuple[float, float]:
    """外框的几何中心 (lat, lon)。

    ⚠️ 不要用 `campus().center` 当初始视野中心：那是校园中心，与外框中心不重合，
    框会偏到一边（2026-10-02 的"长条"问题就有一部分是这个原因）。
    """
    s, w, n, e = frame_of(key)
    return ((s + n) / 2, (w + e) / 2)


def frame_aspect(key: str) -> float:
    """外框的 高/宽 比（用于按框的比例决定地图容器高度，避免框被压成一条）。"""
    s, w, n, e = frame_of(key)
    lat = _center_lat(key)
    w_m = (e - w) * 111320.0 * math.cos(math.radians(lat))
    h_m = (n - s) * 111320.0
    if w_m <= 0:
        return 0.5
    return h_m / w_m


def map_height_for_frame(key: str, view_w: float = 914.0,
                         min_h: float = 420.0, max_h: float = 800.0) -> int:
    """按外框比例算出地图容器高度，让框尽量填满、不浪费大片空白。

    高瘦的框（如仙林）给高一点的容器，宽扁的框（如苏州）给矮一点的。
    限制在 [min_h, max_h] 内，免得极端比例把页面撑爆或压扁。
    """
    h = view_w * frame_aspect(key) + 40.0   # +40 给比例尺/图例留一点余量
    return int(max(min_h, min(max_h, round(h))))


def module_state_ok() -> bool:
    """当前 campus_config 是否"完整新版"（有 frame 字段与两个函数）。

    侧边栏据此提示"已启用兜底"，让"云端模块旧"这件事**可见**，
    而不是等某个页面崩了才发现。
    """
    cfg = _module()
    if cfg is None:
        return False
    if not callable(getattr(cfg, "frame_of", None)):
        return False
    if not callable(getattr(cfg, "min_zoom_for_frame", None)):
        return False
    obj = _campus_obj("gulou")
    if obj is None:
        return False
    return hasattr(obj, "frame")


def module_version() -> str:
    """campus_config 自报的版本；旧模块没有这个属性 -> 返回空串。

    这是判断"线上到底加载了哪一版模块"的**直接证据**（比读文件 sha 可靠：
    文件对不代表进程里加载的模块对）。
    """
    cfg = _module()
    return str(getattr(cfg, "CONFIG_VERSION", "") or "") if cfg is not None else ""
