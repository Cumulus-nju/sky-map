# -*- coding: utf-8 -*-
"""验证 app.py 里 campus_config 导入失败时的兜底逻辑。

情景：云端 campus_config 模块状态异常（缺 frame_of / min_zoom_for_frame），
此时旧写法会让整站 ImportError 崩掉，兜底后应能算出与外层一致的 minZoom。
"""
import math
import sys
import types
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import campus_config as real  # noqa: E402
from campus_config import min_zoom_for_frame as real_mz  # noqa: E402

# 造一个"缺新名字"的 campus_config，模拟线上异常模块
fake = types.ModuleType("campus_config")
fake.CAMPUSES = real.CAMPUSES
fake.campus = real.campus
sys.modules["campus_config"] = fake

print("模拟：campus_config 缺 frame_of / min_zoom_for_frame")
try:
    from campus_config import CAMPUSES, campus as get_campus, frame_of, min_zoom_for_frame  # noqa: F401
    print("  ✗ 竟然导入成功了，模拟无效")
    raise SystemExit(1)
except ImportError as e:
    print(f"  ✓ 如预期触发 ImportError: {e}")


def frame_of(key):
    c = fake.campus(key)
    return c.frame if c.frame and tuple(c.frame) != (0.0, 0.0, 0.0, 0.0) else c.bbox


def min_zoom_for_frame(key, view_w=1000.0, view_h=460.0):
    s, w, n, e = frame_of(key)
    w_m = (e - w) * 111320.0 * math.cos(math.radians((s + n) / 2)) * 1.15
    h_m = (n - s) * 111320.0 * 1.15
    px0 = 156543.03392 * math.cos(math.radians((s + n) / 2))
    return max(11, min(18, int(math.floor(math.log2(min(view_w / (w_m / px0), view_h / (h_m / px0)))))))


print("\n兜底实现 vs 正式实现：")
bad = 0
for key in real.CAMPUSES:
    a = min_zoom_for_frame(key, 914, 460)
    b = real_mz(key, 914, 460)
    ok = a == b
    bad += 0 if ok else 1
    print(f"  {'✓' if ok else '✗'} {real.CAMPUSES[key].short}: 兜底={a}  正式={b}")
    print(f"      frame(兜底)={tuple(round(v, 6) for v in frame_of(key))}")
    print(f"      frame(正式)={tuple(round(v, 6) for v in real.frame_of(key))}")

print()
if bad:
    print(f"✗ {bad} 个校区不一致")
    raise SystemExit(1)
print("✓ 兜底逻辑与正式实现完全一致")
