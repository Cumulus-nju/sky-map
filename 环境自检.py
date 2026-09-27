"""环境自检：一键确认这台机器能不能跑起来。

用法:
    python 环境自检.py
    python tools/preflight.py
"""
from __future__ import annotations

import importlib
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if HERE.name == "tools":
    HERE = HERE.parent

NEED = [
    ("streamlit", "投稿页", True),
    ("streamlit_folium", "投稿页上的地图", True),
    ("folium", "投稿页上的地图", True),
    ("PIL", "读照片 EXIF / 生成缩略图", True),
    ("numpy", "基础计算", True),
    ("pandas", "数据汇总", True),
    ("geopandas", "矢量底图（可后补）", False),
    ("shapely", "矢量底图（可后补）", False),
    ("osmnx", "抓取校区底图（可后补）", False),
]


def main() -> int:
    ok = True
    print("=" * 62)
    print(" 天光云影 · 打卡点地图 —— 环境自检")
    print("=" * 62)

    # --- Python 版本 ---
    v = sys.version_info
    print(f"\n[Python] {sys.version.split()[0]}   {sys.executable}")
    if v < (3, 10):
        print("  ✗ 版本过低，需要 3.10 以上")
        ok = False
    else:
        print("  ✓ 版本可用")

    # --- 依赖 ---
    print("\n[依赖模块]")
    missing_required = []
    for mod, why, required in NEED:
        try:
            importlib.import_module(mod)
            print(f"  ✓ {mod:<18} {why}")
        except Exception:
            if required:
                print(f"  ✗ {mod:<18} {why}   ← 缺少，必须装")
                missing_required.append(mod)
                ok = False
            else:
                print(f"  – {mod:<18} {why}（缺了也能用，只是不能重抓底图）")

    if missing_required:
        print("\n  修复办法：在本文件夹打开 cmd，运行")
        print("      pip install -r requirements.txt")

    # --- 数据与产物 ---
    print("\n[文件检查]")
    sys.path.insert(0, str(HERE))
    try:
        from campus_config import CAMPUSES

        want = list(CAMPUSES)
    except Exception:
        want = ["gulou", "xianlin", "suzhou"]

    data = HERE / "data"
    raw = data / "raw"
    # 苏州校区在 OSM 上没有建筑数据，属正常；这里按"每个校区是否有底图文件"判断，
    # 只要覆盖率和 POI 之类能支撑地图即可。
    have = []
    for k in want:
        files = list(raw.glob(f"{k}_*")) if raw.exists() else []
        if files:
            have.append(k)
    print(f"  {'✓' if len(have) == len(want) else '✗'} 校区底图 {len(have)}/{len(want)} 个"
          f"（{'、'.join(have)}）")
    if len(have) < len(want):
        print("      缺文件请从 zip 完整解压，或双击 抓取校区底图.bat 重新下载")
        ok = False
    n_bld = len(list(raw.glob("*_buildings.geojson"))) if raw.exists() else 0
    print(f"  ✓ 建筑轮廓 {n_bld} 个校区（苏州校区 OSM 无数据，靠卫星影像，属正常）")

    subs = data / "submissions.jsonl"
    n_sub = 0
    if subs.exists():
        n_sub = sum(1 for line in subs.read_text(encoding="utf-8").splitlines() if line.strip())
        demo = "（是示例数据，正式征稿前跑 清空示例数据.bat）" if n_sub and "demo_" in subs.read_text(encoding="utf-8") else ""
        print(f"  ✓ 投稿记录 {n_sub} 条 {demo}")
    else:
        print("  – 还没有投稿记录（正常，同学提交后就有了）")

    for f in ("submit_app.py", "map_build.py", "campus_config.py"):
        print(f"  {'✓' if (HERE / f).exists() else '✗'} {f}")

    # --- 结论 ---
    print("\n" + "=" * 62)
    if ok:
        print(" ✓ 可以开始用了。接下来：")
        print("     1) 双击  生成打卡点地图.bat      看看地图效果")
        print("     2) 双击  启动投稿页.bat          开始收稿")
    else:
        print(" ✗ 还有问题，按上面提示处理后重跑本脚本")
    print("=" * 62)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
