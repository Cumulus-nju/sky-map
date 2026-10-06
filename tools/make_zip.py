"""打包成可分享的 zip。

只打必要文件，排除临时产物与缓存；默认带上示例数据（方便先看效果），
用 --no-demo 可打成"干净版"，直接用于正式征稿。

用法:
    python tools/make_zip.py                 # 含示例数据
    python tools/make_zip.py --no-demo       # 干净版
    python tools/make_zip.py --clean-build   # 顺带把已有的 exports 清掉重建
"""
from __future__ import annotations

import argparse
import shutil
import sys
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
DIST = HERE / "dist"

# 顶层的 .py **用通配全部打进去**，不手写清单。
# 为什么改（2026-10-06）：原来的手写清单漏了 frame_picker.py、relief_basemap.py、
# photo_shrink.py、vote_store.py 等等 —— 漏文件时打包脚本**自己不报错**
# （只是少打一个），但解压出来的包一运行就 ImportError，属于"发出去才发现"的那种坑。
TOP_PY_GLOB = "*.py"

# 顶层要打的非 .py 文件（不存在则跳过）
TOP_FILES = (
    "使用指南.md", "README.md", "requirements.txt", "requirements-local.txt", ".gitignore",
    "打开网站.bat", "生成打卡点地图.bat", "抓取校区底图.bat", "清空示例数据.bat",
)
# 顶层要一并打进去的整目录（保证 Streamlit 不会卡在首次邮箱提示、带上部署资料）
TOP_DIRS = (".streamlit", "pages", "deploy")
TOOLS = (
    "test_submit.py", "test_e2e.py", "test_backend_agnostic.py",
    "check_store.py", "migrate_to_supabase.py",
    "check_tiles.py", "verify_align.py", "gcj_offset.py",
    "test_photo_shrink.py", "test_static_basemap.py", "test_frame_fallback.py",
    "test_key_formats.py", "test_vote.py", "test_vote_page.py", "test_nav.py",
)
# 运行时不需要，但有助于排查/复验，按需带
TOOLS_OPTIONAL = (
    "cdp_check.mjs", "cdp_basemap.mjs", "cdp_markers.mjs", "cdp_measure.mjs", "check_gcj.mjs",
    # 按手机尺寸给几个页面各截一张图 —— 移动端排版**只有看图才知道**对不对
    "cdp_phone_shots.mjs",
)

SKIP_DIRS = {"__pycache__", ".ipynb_checkpoints", "photos", "tile_check", ".streamlit"}
SKIP_SUFFIX = (".pyc", ".tmp", ".lock")


def should_skip(path: Path) -> bool:
    if any(part in SKIP_DIRS for part in path.parts):
        return True
    if path.suffix in SKIP_SUFFIX:
        return True
    return False


def add_tree(zf: zipfile.ZipFile, root: Path, arc_dir: str, arc_prefix: str,
             skip_demo: bool = False) -> int:
    """把 root 下所有文件打进 arc_prefix/arc_dir/，保持 root 内的相对结构。

    root 必须已经是目标目录本身（如 data/raw），arc_dir 是它在包内的相对路径
    （如 data/raw）——不能用 data/ 当 base 再 relative_to，那样会算不出来。
    """
    n = 0
    for p in sorted(root.rglob("*")):
        if p.is_dir():
            continue
        rel = p.relative_to(root)
        if should_skip(rel):
            continue
        if skip_demo and p.name.startswith("demo_"):
            continue
        zf.write(p, f"{arc_prefix}/{arc_dir}/{rel.as_posix()}")
        n += 1
    return n


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-demo", action="store_true", help="打干净版（不含示例投稿）")
    ap.add_argument("--name", default=None, help="输出 zip 名（不含扩展名）")
    args = ap.parse_args()

    stamp = "clean" if args.no_demo else "demo"
    name = args.name or f"天光云影-打卡点地图-{stamp}"
    DIST.mkdir(parents=True, exist_ok=True)
    out = DIST / f"{name}.zip"
    if out.exists():
        out.unlink()

    prefix = name  # zip 内的顶层文件夹

    added = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for src in sorted(HERE.glob(TOP_PY_GLOB)):
            if src.is_file():
                zf.write(src, f"{prefix}/{src.name}")
                added += 1
        for f in TOP_FILES:
            src = HERE / f
            if src.exists():
                zf.write(src, f"{prefix}/{f}")
                added += 1
        for d in TOP_DIRS:
            src = HERE / d
            if src.is_dir():
                for p in sorted(src.rglob("*")):
                    if p.is_file() and not should_skip(p.relative_to(src)):
                        zf.write(p, f"{prefix}/{d}/{p.relative_to(src).as_posix()}")
                        added += 1
        for f in TOOLS + TOOLS_OPTIONAL:
            src = HERE / "tools" / f
            if src.exists():
                zf.write(src, f"{prefix}/tools/{f}")
                added += 1

        # data/raw：校区底图（必须带，否则矢量底图是空的）
        data = HERE / "data"
        raw = data / "raw"
        if raw.exists():
            added += add_tree(zf, raw, "data/raw", prefix)

        # 示例数据（可选）
        if not args.no_demo:
            subs = data / "submissions.jsonl"
            if subs.exists():
                zf.write(subs, f"{prefix}/data/submissions.jsonl")
                added += 1
            thumbs = data / "thumbs"
            if thumbs.exists():
                added += add_tree(zf, thumbs, "data/thumbs", prefix)
            exports = data / "exports"
            if exports.exists():
                # exports 里含 thumbs 副本（在线版地图要用），一并带上
                added += add_tree(zf, exports, "data/exports", prefix)

        # 空目录占位，保证首次运行不会因为缺目录报错
        for d in ("data/photos", "data/thumbs", "data/exports"):
            zf.writestr(f"{prefix}/{d}/.keep", "")

    size_mb = out.stat().st_size / 1024 / 1024
    print(f"✓ 打包完成：{out}")
    print(f"  共 {added} 个文件，{size_mb:.2f} MB")
    print(f"  解压后顶层目录：{prefix}/")
    if args.no_demo:
        print("  （干净版：不含示例投稿，可直接用于正式征稿）")
    else:
        print("  （含 20 条示例投稿，先看效果用；正式征稿前请运行 清空示例数据.bat）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
