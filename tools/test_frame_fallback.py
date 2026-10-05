# -*- coding: utf-8 -*-
"""验证外框(frame)取值的兜底链 —— 专门模拟 2026-10-01 线上白屏那个真实场景。

背景（重要，别把这个测试改弱了）
--------------------------------
线上白屏的真实原因：**云端 campus_config 是旧版，app.py 是新版**。

    旧 Campus 数据类：没有 `frame` 字段，也没有 `frame_of` / `min_zoom_for_frame`
    新 app.py       ：`from campus_config import ..., frame_of` 失败 -> 走 except 兜底
                      -> 兜底里读 `c.frame` -> AttributeError -> 整站白屏

**上一次的测试 `test_import_fallback.py` 只模拟了"缺函数"，把 `frame` 字段当成
一定存在**，所以测试全绿而线上照崩。这个文件把两种缺失都模拟到，并且额外验证
"就算模块的 frame_of 直接抛异常"也不会把页面搞崩。

用法：python tools\\test_frame_fallback.py
"""
from __future__ import annotations

import contextlib
import sys
import types
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import campus_config as real  # noqa: E402
import frame_util  # noqa: E402

FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'✓' if ok else '✗'} {name}" + (f"  {detail}" if detail else ""))
    if not ok:
        FAILS.append(name)


class _OldCampus:
    """模拟**旧版** Campus：只有 bbox / center，没有 frame 字段。"""

    def __init__(self, center, bbox):
        self.center = center
        self.bbox = bbox


class _NewCampus:
    """模拟**新版** Campus：有 frame 字段。"""

    def __init__(self, center, bbox, frame):
        self.center = center
        self.bbox = bbox
        self.frame = frame


def _make_module(*, campus_map=None, with_frame_of=True, with_min_zoom=True,
                 frame_of_raises=False) -> types.ModuleType:
    """造一个假的 campus_config 模块。"""
    mod = types.ModuleType("campus_config")
    mod.CAMPUSES = campus_map if campus_map is not None else real.CAMPUSES

    def _campus(key):
        try:
            return mod.CAMPUSES[key]
        except KeyError:
            raise KeyError(f"未知校区 {key!r}") from None

    mod.campus = _campus
    if with_frame_of:
        if frame_of_raises:
            def _boom(key):
                raise RuntimeError("模拟模块里的 frame_of 自身炸了")
            mod.frame_of = _boom
        else:
            mod.frame_of = real.frame_of
    if with_min_zoom:
        mod.min_zoom_for_frame = real.min_zoom_for_frame
    return mod


@contextlib.contextmanager
def fake_config(mod):
    """在 with 块内让 `import campus_config` 拿到假模块。"""
    saved = sys.modules.get("campus_config")
    sys.modules["campus_config"] = mod
    try:
        yield
    finally:
        if saved is not None:
            sys.modules["campus_config"] = saved
        else:
            sys.modules.pop("campus_config", None)


print("=" * 64)
print(" 外框兜底链测试（复现 2026-10-01 线上白屏场景）")
print("=" * 64)

# ---------------------------------------------------------------------------
print("\n[1] 场景A：旧模块（缺 frame 字段 + 缺 frame_of / min_zoom_for_frame）")
print("     —— 这正是线上白屏的组合，旧代码在这里 AttributeError")
old_map = {k: _OldCampus(c.center, c.bbox) for k, c in real.CAMPUSES.items()}
old_mod = _make_module(campus_map=old_map, with_frame_of=False, with_min_zoom=False)
with fake_config(old_mod):
    check("模块状态自检判定为「不完整」", frame_util.module_state_ok() is False)
    check("模块版本读不到（旧版没有）", frame_util.module_version() == "")
    try:
        for key in real.CAMPUSES:
            got = frame_util.frame_of(key)
            # 关键断言：**不抛异常**且给出一个合法的框。
            # ⚠ 旧模块下拿到的是 bbox（旧 Campus 有 bbox 字段），不是内置 frame 表 ——
            #   这是**预期行为**（bbox 也是"覆盖校园"的合法外框），内置表只在连 bbox
            #   都没有时才用到（见场景C）。别把这里写成"必须等于 frame"，否则会误导。
            check(f"{real.CAMPUSES[key].short}: 不抛异常且给出合法外框",
                  len(got) == 4 and got[2] > got[0] and got[3] > got[1]
                  and tuple(got) == tuple(real.CAMPUSES[key].bbox),
                  f"{tuple(round(v, 6) for v in got)}（旧模块下退回 bbox）")
        for key in real.CAMPUSES:
            mz = frame_util.min_zoom_for_frame(key, 914, 460)
            check(f"{real.CAMPUSES[key].short}: minZoom 是合法级别", 11 <= mz <= 18, f"{mz}")
    except Exception as exc:  # 兜底代码最怕的就是这里抛异常
        check("旧模块下整条链路不抛异常", False, f"{type(exc).__name__}: {exc}")

# ---------------------------------------------------------------------------
print("\n[2] 场景B：模块里 frame_of 自己抛异常（兜底不能跟着崩）")
boom_map = {k: _NewCampus(c.center, c.bbox, c.frame) for k, c in real.CAMPUSES.items()}
boom_mod = _make_module(campus_map=boom_map, with_frame_of=True, frame_of_raises=True)
with fake_config(boom_mod):
    try:
        got = frame_util.frame_of("gulou")
        check("frame_of 抛异常时降级到对象上的 frame 字段",
              tuple(got) == tuple(real.frame_of("gulou")),
              f"{tuple(round(v, 6) for v in got)}")
        mz = frame_util.min_zoom_for_frame("gulou", 914, 460)
        check("minZoom 也随之降级且与正式一致",
              mz == real.min_zoom_for_frame("gulou", 914, 460), f"{mz}")
    except Exception as exc:
        check("frame_of 抛异常时不冒泡", False, f"{type(exc).__name__}: {exc}")

# ---------------------------------------------------------------------------
print("\n[3] 场景C：模块只有 CAMPUSES，校区对象连 bbox 都没有（最惨情况）")
naked_map = {k: object() for k in real.CAMPUSES}  # 既无 frame 也无 bbox
bare_mod = types.ModuleType("campus_config")
bare_mod.CAMPUSES = naked_map  # 故意不提供 campus()
with fake_config(bare_mod):
    try:
        got = frame_util.frame_of("gulou")
        # 这才该落到**内置 frame 表**
        check("退到内置兜底表且不抛异常", tuple(got) == tuple(real.frame_of("gulou")),
              f"{tuple(round(v, 6) for v in got)}")
        mz = frame_util.min_zoom_for_frame("gulou", 914, 460)
        check("minZoom 用内置表算且与正式一致",
              mz == real.min_zoom_for_frame("gulou", 914, 460), f"{mz}")
    except Exception as exc:
        check("只有 CAMPUSES 时不抛异常", False, f"{type(exc).__name__}: {exc}")

# ---------------------------------------------------------------------------
print("\n[4] 场景D：完全正常的模块（降级不该改变正常行为）")
new_map = {k: _NewCampus(c.center, c.bbox, c.frame) for k, c in real.CAMPUSES.items()}
new_mod = _make_module(campus_map=new_map)
new_mod.CONFIG_VERSION = "test"
with fake_config(new_mod):
    check("模块状态自检判定为「完整」", frame_util.module_state_ok() is True)
    check("模块版本可读", frame_util.module_version() == "test")
    for key in real.CAMPUSES:
        a = frame_util.frame_of(key)
        b = frame_util.min_zoom_for_frame(key, 914, 460)
        check(f"{real.CAMPUSES[key].short}: 正常路径与正式实现一致",
              tuple(a) == tuple(real.frame_of(key))
              and b == real.min_zoom_for_frame(key, 914, 460))

# ---------------------------------------------------------------------------
print("\n[5] 内置兜底表 vs campus_config 正式配置")
print("     （防止改了配置忘了改 frame_util._FALLBACK 造成静默漂移）")
from frame_util import _FALLBACK  # noqa: E402

check("校区集合一致", set(_FALLBACK) == set(real.CAMPUSES),
      f"兜底={sorted(_FALLBACK)} 配置={sorted(real.CAMPUSES)}")
for key in real.CAMPUSES:
    if key in _FALLBACK:
        check(f"{real.CAMPUSES[key].short}: 兜底 frame 与配置一致",
              tuple(_FALLBACK[key]) == tuple(real.frame_of(key)),
              f"{tuple(round(v, 6) for v in _FALLBACK[key])}")

# ---------------------------------------------------------------------------
print("\n[6] 未知校区：不能抛异常（后台/地图会拿它兜脏数据）")
try:
    got = frame_util.frame_of("__不存在的校区__")
    check("未知 key 返回默认框而不是 KeyError", len(got) == 4, f"{tuple(round(v, 4) for v in got)}")
except Exception as exc:
    check("未知 key 不抛异常", False, f"{type(exc).__name__}: {exc}")

# ---------------------------------------------------------------------------
print("\n[7] 入口脚本必须扛得住「旧 site_common」（2026-10-05 线上 IndexError 的教训）")
# 规律：Streamlit 只对 **import 进来的模块**做模块级热重载，而**入口脚本每次重跑都是新的**
# ⇒ 出现"新 app.py + 旧 site_common.py"。当时 `take_pick()` 的返回值从 2 个改成 3 个，
#   app.py 直接写 `from_pick[2]` ⇒ **IndexError 整页崩，而且只在点选之后才炸**
#   （那行只在收到回传时才执行，所以页面看着好好的）。
# 结论：**跨模块的兼容逻辑必须自包含写在入口脚本里，且不得依赖任何新函数** ——
#       旧模块里没有新函数，`S.merge_pick(...)` 这种写法会 AttributeError。
import site_common as _sc  # noqa: E402

_src = (HERE / "app.py").read_text(encoding="utf-8")
check("app.py 不再裸取 take_pick() 的第三个值（旧模块只有两个值）",
      "from_pick[2]" not in _src)
check("app.py 先 len() 判长度再取校区（两种形状都吃）",
      "len(raw) > 2" in _src and "str(raw[2])" in _src)
check("app.py 不调用 site_common 的新函数（旧模块里不存在 ⇒ AttributeError）",
      "S.merge_pick(" not in _src and "S.pick_api_ok(" not in _src)
check("site_common 自报接口形状版本（app.py 据此给可见告警）",
      getattr(_sc, "PICK_API", None) == 2, f"PICK_API={getattr(_sc, 'PICK_API', None)}")
check("app.py 的告警读取也做了 getattr 兜底（旧模块没有该常量）",
      'getattr(S, "PICK_API", 1)' in _src)

print("\n" + "=" * 64)
if FAILS:
    print(f"✗ {len(FAILS)} 项失败：" + "、".join(FAILS))
    raise SystemExit(1)
print("✓ 全部通过 —— 旧模块/异常/未知 key 都不会再让页面崩")
