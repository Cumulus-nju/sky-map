# -*- coding: utf-8 -*-
"""照片入库瘦身（photo_shrink）的单元测试。

为什么值得有：这是 2026-10-05 线上"提交投稿报 HTTPError"的直接对策 ——
同学传了 20 MB 的手机原图，base64 后 ~27 MB 进不了云端、也吃不掉 500 MB 免费库。
纯函数、不依赖 Streamlit 与网络，跑起来很快。

用法：python tools\\test_photo_shrink.py
"""
from __future__ import annotations

import io
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import photo_shrink  # noqa: E402

FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'✓' if ok else '✗'} {name}" + (f"  {detail}" if detail else ""))
    if not ok:
        FAILS.append(name)


def noisy_jpeg(w: int, h: int, quality: int = 95) -> bytes:
    """造一张"压不动"的图：随机噪声最难压，正好用来模拟 20 MB 的手机原图。"""
    from PIL import Image

    raw = os.urandom(w * h * 3)
    im = Image.frombytes("RGB", (w, h), raw)
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=quality)
    return buf.getvalue()


print("=" * 62)
print(" 照片入库瘦身：大图必须压到能进库，小图不能被动")
print("=" * 62)

# ---------------------------------------------------------------------------
print("\n[1] 大图（模拟手机原图）：必须压到 ~2 MB 以内")
big = noisy_jpeg(4000, 3000)
print(f"    造出的原图：{len(big) / 1048576:.1f} MB（4000×3000 噪声 JPEG —— 最难压的情况）")
check("原图确实超过阈值（否则这个用例没意义）", len(big) > photo_shrink.TARGET_BYTES)
t0 = time.time()
out, mime, note = photo_shrink.shrink_for_store(big, "image/heic")
dt = time.time() - t0
print(f"    压缩后：{len(out) / 1048576:.2f} MB，耗时 {dt:.2f}s\n    说明：{note}")
check("压到目标体积以内（≤ 2 MB）", len(out) <= photo_shrink.TARGET_BYTES,
      f"{len(big) / 1048576:.1f} → {len(out) / 1048576:.2f} MB")
check("压完不超过硬上限", len(out) <= photo_shrink.HARD_LIMIT_BYTES)
check("统一转成 JPEG（HEIC 等浏览器/地图都认不了）", mime == "image/jpeg")
check("给了说明（要在页面上告诉同学）", bool(note) and "压缩" in note)
check("说明里带上了质量与尺寸（便于事后追查）", "质量" in note and "长边" in note)
check("压缩耗时可接受（< 10s，别把提交卡死）", dt < 10.0, f"{dt:.2f}s")

from PIL import Image  # noqa: E402

w, h = Image.open(io.BytesIO(out)).size
check("长边 ≤ MAX_EDGE", max(w, h) <= photo_shrink.MAX_EDGE, f"{w}×{h}")

# ---------------------------------------------------------------------------
print("\n[1b] 算法必须选「能装下的**最高**质量」（省下的空间换画质，不无脑压小）")
import re as _re  # noqa: E402

_q = int(_re.search(r"质量 (\d+)", note).group(1))
_ladder = list(photo_shrink.QUALITY_LADDER)
check("选中的质量来自质量阶梯", _q in _ladder, f"q{_q} ∈ {_ladder}")
if _q in _ladder:
    _i = _ladder.index(_q)
    if _i > 0:
        # 再高一档就应该装不下 —— 否则说明压过头了。
        # ⚠ 必须用**同一份输入**比：拿"已压过一遍的输出"当基准是不成立的
        #   （二次编码会更小，第一版测试就是这么误判的）。
        from PIL import Image as _I
        from PIL import ImageOps as _IO

        _src = _IO.exif_transpose(_I.open(io.BytesIO(big)))
        if _src.mode not in ("RGB", "L"):
            _src = _src.convert("RGB")
        _src.thumbnail((photo_shrink.MAX_EDGE, photo_shrink.MAX_EDGE))
        _higher = photo_shrink._encode_jpeg(_src, _ladder[_i - 1])
        check("再高一档就超 2 MB（说明没有压过头）", len(_higher) > photo_shrink.TARGET_BYTES,
              f"q{_ladder[_i - 1]} → {len(_higher) / 1048576:.2f} MB")
    else:
        check("已经是阶梯最高档（无可再升）", True, f"q{_q}")

# ---------------------------------------------------------------------------
print("\n[2] 小图：原样返回（不重编码、不掉画质、不留说明）")
small = noisy_jpeg(640, 480, quality=70)
out2, mime2, note2 = photo_shrink.shrink_for_store(small, "image/png")
check("小图原样不动", out2 is small or out2 == small)
check("mime 保持原样", mime2 == "image/png")
check("没有多余的说明", note2 == "")

# ---------------------------------------------------------------------------
print("\n[3] 压不动的东西：不能抛异常（压缩只是优化，不该让投稿失败）")
junk = b"\xff\xd8\xff\xe0" + os.urandom(photo_shrink.TARGET_BYTES + 1024)
try:
    out3, mime3, note3 = photo_shrink.shrink_for_store(junk, "image/jpeg")
    check("坏数据不抛异常", True, f"退回原图，说明={note3!r}")
    check("退回的是原始字节", out3 == junk)
except Exception as exc:
    check("坏数据不抛异常", False, f"{type(exc).__name__}: {exc}")

# ---------------------------------------------------------------------------
print("\n[4] 硬上限：压完还是太大就明确拒绝（带能给同学看的中文）")
try:
    photo_shrink.ensure_storable(b"x" * (photo_shrink.HARD_LIMIT_BYTES + 1))
    check("超硬上限应抛 PhotoTooLarge", False, "居然没抛")
except photo_shrink.PhotoTooLarge as exc:
    check("超硬上限抛 PhotoTooLarge", True, f"消息：{str(exc)[:40]}…")
    check("消息里有可执行的建议", "换一张" in str(exc))
except Exception as exc:
    check("超硬上限抛 PhotoTooLarge", False, f"抛错类型不对：{type(exc).__name__}")
try:
    photo_shrink.ensure_storable(b"x" * 1024)
    check("不超过硬上限时不抛", True)
except Exception as exc:
    check("不超过硬上限时不抛", False, f"{type(exc).__name__}: {exc}")

print("\n" + "=" * 62)
if FAILS:
    print(f"✗ {len(FAILS)} 项失败：" + "、".join(FAILS))
    raise SystemExit(1)
print("✓ 全部通过 —— 大图能进库、小图不动、坏图不炸")
