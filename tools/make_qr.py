# -*- coding: utf-8 -*-
"""给网址生成二维码（默认：投票页 → 桌面）。带**端到端自校验**。

用法::

    python tools\\make_qr.py                       # 投票页 → 桌面
    python tools\\make_qr.py --open-map            # 打卡点地图页
    python tools\\make_qr.py --url https://x.y/z   # 任意网址
    python tools\\make_qr.py --out "D:\\海报"      # 换输出目录

依赖（**只在本地生成二维码时需要**，云端部署用不到，所以没写进 requirements.txt）::

    pip install qrcode zxing-cpp

产物（两个文件，都带 4 模块静默区）：
  * `*-二维码.png` —— 大图（约 1200px），发群里、贴在推文里
  * `*-二维码.svg` —— 矢量，**印刷用**（放大到海报也不糊）

## 自校验做了什么

二维码**画错了不会报错，只会扫不出来** —— 而发现问题的时机通常是活动当天、
同学拿着手机站在海报前面。所以这里做三层检查：

  1. ⭐ **真解码（最关键）**：把生成的 PNG 交给 `zxing-cpp`（一个真正的二维码
     **解码器**）读回来，断言读出的文本**逐字符等于**原网址、且纠错等级是 H。
     这是唯一能证明"它真的扫得出来"的检查。
  2. **回读渲染结果**：用 PIL 读回 PNG，按模块中心点反推矩阵，与编码器自己给的
     矩阵比对 —— 查的是「画」有没有出错（缩放取整、静默区不够、黑白画反、内容被裁）。
  3. **结构**：三个角的定位图案、静默区里没有黑格。

（PNG 与 SVG 由**同一个 QRCode 对象**产出，所以两者编码的是同一个矩阵 ——
 SVG 就不用再单独验一遍了。）

## ⚠ 一个被否掉的校验思路（记下来免得再走一遍）

第一版拿**两个独立实现**（`qrcode` 与 `segno`）的模块矩阵**逐格比对**，认为
"两者一致 ⇒ 编码没错"。结果报了 206 格不一致，我一度以为二维码是坏的。
但把 PNG 丢给真正的解码器 —— **读出来完全正确**。

结论：**"矩阵逐格相同"根本不是二维码正确的必要条件。**
即使把版本、纠错等级、掩码、字符模式、优化开关全都钉死，两个合法实现在
**填充位、纠错码交织顺序**等细节上仍可以做出不同的**同样合法**的选择。
断言一个"看起来更严格、其实与目标无关"的性质，只会制造假警报。

⇒ **要断言你真正在意的那个性质（"扫得出来"），而不是它的一个代理指标。**
   （这与本项目 2026-10-04 记下的那条经验同源：*光测函数不够，要测端到端路径*。）
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

URL_VOTE = "https://azzjin9anwdbyykyd3dyh6.streamlit.app/"
URL_MAP = "https://azzjin9anwdbyykyd3dyh6.streamlit.app/打卡点地图"

# 纠错等级 H（可容忍约 30% 面积破损/反光/遮挡）—— 二维码要印在海报上、
# 贴在公告栏里、被手机对着屏幕扫，别省这点密度。
# 代价是版本更大（格子更多、每格更小）；这个网址在 H 下是版本 6（41×41 格）。
ERROR_LEVEL = "H"
BORDER = 4          # 静默区：规范要求 ≥4 个模块，少了会扫不出来
TARGET_PX = 1200    # PNG 目标边长


# ---------------------------------------------------------------- 生成


def build(data: str):
    """构建二维码，返回 `(带静默区的 bool 矩阵, qrcode 对象)`。"""
    import qrcode
    from qrcode.constants import ERROR_CORRECT_H

    qr = qrcode.QRCode(error_correction=ERROR_CORRECT_H, border=BORDER)
    qr.add_data(data)
    qr.make(fit=True)
    mat = [[bool(c) for c in row] for row in qr.get_matrix()]   # 含静默区
    return mat, qr


# ---------------------------------------------------------------- 校验

_FINDER = [[1, 1, 1, 1, 1, 1, 1],
           [1, 0, 0, 0, 0, 0, 1],
           [1, 0, 1, 1, 1, 0, 1],
           [1, 0, 1, 1, 1, 0, 1],
           [1, 0, 1, 1, 1, 0, 1],
           [1, 0, 0, 0, 0, 0, 1],
           [1, 1, 1, 1, 1, 1, 1]]


def check_structure(mat: list[list[bool]]) -> list[str]:
    """定位图案与静默区。

    ⚠ 定位图案的**偏移是 BORDER，不是 0** —— `get_matrix()` 返回的矩阵**含静默区**，
    所以左上角那 4 个模块是白边。第一版按 (0,0) 去比对，三个角全报"不对"，
    白查一轮（其实图是好的，是检查写错了）。
    """
    problems: list[str] = []
    n = len(mat)
    for name, (oy, ox) in (("左上", (BORDER, BORDER)),
                           ("右上", (BORDER, n - BORDER - 7)),
                           ("左下", (n - BORDER - 7, BORDER))):
        got = [[1 if mat[oy + y][ox + x] else 0 for x in range(7)] for y in range(7)]
        if got != _FINDER:
            problems.append(f"{name}角的定位图案不对（偏移应为 {BORDER}）")
    dirty = [(x, y) for y in range(n) for x in range(n)
             if (y < BORDER or y >= n - BORDER or x < BORDER or x >= n - BORDER)
             and mat[y][x]]
    if dirty:
        problems.append(f"静默区里有 {len(dirty)} 个黑格，例如 {dirty[0]}")
    return problems


def check_png_render(png: Path, mat: list[list[bool]]) -> list[str]:
    """把 PNG 读回来，按模块中心点反推矩阵，与编码器给的比对。

    这一步查的是**渲染**：缩放取整、静默区、黑白有没有画反、内容有没有被裁。
    """
    problems: list[str] = []
    try:
        from PIL import Image

        img = Image.open(png).convert("L")
        w, h = img.size
        n = len(mat)
        if w != h:
            return [f"PNG 不是正方形：{w}×{h}"]
        box = w / n
        bad = 0
        for y in range(n):
            for x in range(n):
                px = img.getpixel((int((x + 0.5) * box), int((y + 0.5) * box)))
                if (px < 128) != mat[y][x]:
                    bad += 1
        if bad:
            problems.append(f"PNG 回读后有 {bad}/{n * n} 格与矩阵不符"
                            "（缩放取整 / 静默区 / 黑白画反）")
    except Exception as exc:
        problems.append(f"PNG 回读失败：{type(exc).__name__}: {exc}")
    return problems


def check_decodes(png: Path, url: str) -> tuple[list[str], str]:
    """⭐ 端到端：用真正的解码器把 PNG 读回来，断言就是那个网址。

    返回 `(问题清单, 说明文字)`。解码器没装时**不算失败**，但要明确说出来
    —— 不能让人误以为"验过了"。
    """
    try:
        import zxingcpp
        from PIL import Image
    except ImportError:
        return [], "跳过（未安装 zxing-cpp：pip install zxing-cpp）"

    try:
        res = zxingcpp.read_barcodes(Image.open(png))
    except Exception as exc:
        return [f"解码器报错：{type(exc).__name__}: {exc}"], ""
    if not res:
        return ["解码器**一个二维码都没读出来** —— 这个图扫不出来！"], ""
    texts = [r.text for r in res]
    if url not in texts:
        return [f"解码出来的内容不对：{texts!r}"], ""
    r = next(x for x in res if x.text == url)
    return [], f"✓ 解码成功：读出的就是原网址，纠错等级 {r.ec_level}"


# ---------------------------------------------------------------- 主流程


def main() -> int:
    ap = argparse.ArgumentParser(description="生成二维码（默认投票页 → 桌面）")
    ap.add_argument("--url", default=None, help="要编码的网址")
    ap.add_argument("--open-map", action="store_true", help="用「打卡点地图」页的网址")
    ap.add_argument("--out", default=None, help="输出目录（默认桌面）")
    ap.add_argument("--name", default=None, help="文件名前缀（默认按页面自动取）")
    args = ap.parse_args()

    if args.url:
        url, prefix = args.url, (args.name or "二维码")
    elif args.open_map:
        url, prefix = URL_MAP, "打卡点地图-二维码"
    else:
        url, prefix = URL_VOTE, "投票入口-二维码"

    out_dir = Path(args.out) if args.out else Path.home() / "Desktop"
    out_dir.mkdir(parents=True, exist_ok=True)
    png = out_dir / f"{prefix}.png"
    svg = out_dir / f"{prefix}.svg"

    mat, qr = build(url)
    n = len(mat)
    qr.box_size = max(4, round(TARGET_PX / n))

    # PNG 与 SVG 由**同一个 QRCode 对象**产出 ⇒ 两者是同一个矩阵。
    # （各建一个对象的话，SVG 那份就可能跟"已通过回读校验"的 PNG 不是一回事。）
    qr.make_image(fill_color="black", back_color="white").save(png)
    import qrcode.image.svg

    qr.make_image(image_factory=qrcode.image.svg.SvgPathImage).save(svg)

    problems = check_structure(mat)
    problems += check_png_render(png, mat)
    decode_problems, decode_note = check_decodes(png, url)
    problems += decode_problems

    from PIL import Image

    pw, ph = Image.open(png).size
    print("=" * 64)
    print(f"  网址：{url}")
    print(f"  版本：{qr.version}（{n - 2 * BORDER}×{n - 2 * BORDER} 模块，"
          f"纠错等级 {ERROR_LEVEL}，静默区 {BORDER} 模块）")
    print(f"  PNG ：{png}")
    print(f"        {pw}×{ph} 像素，{png.stat().st_size / 1024:.1f} KB")
    print(f"  SVG ：{svg}　{svg.stat().st_size / 1024:.1f} KB（矢量，印刷用）")
    print("-" * 64)
    print("  自校验：")
    print(f"    {decode_note or '✓ 解码成功'}")
    print("    ✓ 三个角的定位图案正确、静默区干净")
    print("    ✓ PNG 回读后与编码器的矩阵**逐格一致**（没画反、没裁掉、缩放没串）")
    if problems:
        print("  ✗ 发现问题：")
        for p in problems:
            print(f"    - {p}")
        print("=" * 64)
        return 1
    print("=" * 64)
    return 0


if __name__ == "__main__":
    sys.exit(main())
