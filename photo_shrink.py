# -*- coding: utf-8 -*-
"""投稿照片「入库前瘦身」：把要进云端数据库的那一份压到 **约 2 MB** 以内。

为什么需要它（2026-10-05 线上真实故障）
--------------------------------------
同学传了一张 **20 MB** 的手机原图，点提交时报 `HTTPError`。原因有两层：

1. 照片是**以 base64 存进数据库 text 字段**的 —— 20 MB 原图 base64 后约 **27 MB**，
   塞进一个 JSON 请求体，云端网关直接拒收；
2. 就算收得下也不行：免费层数据库只有 **500 MB**，20 MB 一张的话**存 18 张就满**，
   一场征稿根本办不下来。

策略（用户 2026-10-05 拍板："按 QQ 那种压缩办法，压到 2 MB 左右"）
--------------------------------------------------------------------
**质量阶梯凑目标**，而不是固定质量：
1. 长边先缩到 `MAX_EDGE`（2560 ≈ A4 300dpi，比微信/QQ 更宽，评审够用）；
2. 质量从 90 起逐档降到 58，**取第一个 ≤ `TARGET_BYTES`(2 MB) 的档** ——
   也就是说：**只要不超 2 MB，就尽量用高质量**（省下来的空间换画质）；
3. 连 58 都超 2 MB，就把长边再退到 1920 重试一轮；
4. 极端情况仍超 `HARD_LIMIT_BYTES`(6 MB) → 抛 `PhotoTooLarge`，
   带一句能直接给同学看的话（总比让云端白跑一趟再报 500 强）。

其他约定：
- **≤ 2 MB 的小图原样入库**（不重编码、不掉画质、不浪费时间）。
- **只在云端生效**：本地文件存储照旧存原图（不占数据库，组织者留档/印刷更好）。
- 压不动（HEIC 等 Pillow 解不了的格式）就原样返回并附说明 ——
  压缩只是优化，**不该成为投稿失败的原因**。
- 垂直方向按 EXIF 摆正（手机竖拍不转正的话入库版会躺倒）。

⚠ 为什么单独放一个**新模块**（而不是塞进 `app.py` 或现有的 `photos.py`）：
   云端模块级热重载会让「新 app.py + 旧 photos.py」共存，此时调用新函数会
   `AttributeError` 把整页打崩；而**新模块名不在旧进程的 `sys.modules` 里**，
   一定从磁盘新加载 ⇒ 天然绕开这个坑（2026-10-05 已经因此崩过两次）。
"""
from __future__ import annotations

import io

# 小图不动、大图压到这个数以内（用户拍板：2 MB 左右）
TARGET_BYTES = 2 * 1024 * 1024
# 入库版的长边上限：2560 ≈ A4 300dpi（比微信/QQ 更宽，评审/印刷都够）
MAX_EDGE = 2560
# 长边退让档：质量降到底还超标时才用
FALLBACK_EDGE = 1920
# 质量阶梯（从高到低找第一个不超目标体积的）
QUALITY_LADDER = (90, 82, 74, 66, 58)
# 压完还超这个数就明确拒绝（云端单请求体的实际容忍度未知，留足余量）
HARD_LIMIT_BYTES = 6 * 1024 * 1024


class PhotoTooLarge(RuntimeError):
    """照片压不动 / 压完仍然太大 —— 消息是可以直接给同学看的中文。"""


def _encode_jpeg(im, quality: int) -> bytes:
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=quality, optimize=True, progressive=True)
    return buf.getvalue()


def _resample():
    """缩放滤镜常量：新 Pillow 在 `Image.Resampling` 下，老版本在 `Image` 下。"""
    from PIL import Image

    return getattr(getattr(Image, "Resampling", Image), "LANCZOS")


def _fit(im, edge: int):
    out = im.copy()
    out.thumbnail((edge, edge), _resample())
    return out


def shrink_for_store(raw: bytes, mime: str) -> tuple[bytes, str, str]:
    """把要进库的那份压到 `TARGET_BYTES` 以内，返回 `(数据, mime, 说明)`。

    说明为空串表示"原样使用"。**任何异常都不抛**（压不动就按原图走）。
    """
    if len(raw) <= TARGET_BYTES:
        return raw, mime, ""

    before_mb = len(raw) / 1048576
    try:
        from PIL import Image, ImageOps

        src = Image.open(io.BytesIO(raw))
        # 手机竖拍照片靠 EXIF 的 orientation 才是正的 —— 不转正入库版会躺倒
        src = ImageOps.exif_transpose(src)
        if src.mode not in ("RGB", "L"):
            src = src.convert("RGB")
        before_size = src.size

        chosen: tuple[bytes, int, tuple[int, int]] | None = None
        for edge in (MAX_EDGE, FALLBACK_EDGE):
            cand = _fit(src, edge)
            for q in QUALITY_LADDER:
                data = _encode_jpeg(cand, q)
                chosen = (data, q, cand.size)
                if len(data) <= TARGET_BYTES:
                    break
            if chosen and len(chosen[0]) <= TARGET_BYTES:
                break

        if chosen is None:              # 理论上到不了（阶梯至少跑一次）
            return raw, mime, ""
        out, quality, size = chosen
        if len(out) >= len(raw):        # 极小概率：原图本来就已经压得很狠
            return raw, mime, ""
        note = (f"照片已压缩入库：{before_mb:.1f} MB → {len(out) / 1048576:.2f} MB"
                f"（长边 {before_size[0]}×{before_size[1]} → {size[0]}×{size[1]}"
                f"，JPEG 质量 {quality}）")
        return out, "image/jpeg", note
    except Exception as exc:  # noqa: BLE001 —— 压缩失败不能连累投稿
        return raw, mime, f"（压缩失败，按原图入库：{type(exc).__name__}）"


def ensure_storable(data: bytes) -> None:
    """压完还太大就抛出**能给同学看**的错误，别让云端白跑一趟。"""
    if len(data) > HARD_LIMIT_BYTES:
        raise PhotoTooLarge(
            f"这张照片 {len(data) / 1048576:.1f} MB，压缩后仍然太大，云端存不下。"
            "请换一张小一点的，或先用手机/电脑把它转成 JPG（HEIC 等格式可能压不动）再传。"
        )
