"""清空示例数据，准备正式征稿。

用法: python reset_data.py            # 交互确认
      python reset_data.py --yes      # 直接清空
保留 data/raw/ 里的 OSM 底图（那是校区建模资产，不用重抓）。
"""
from __future__ import annotations

import argparse
import shutil
import sys

from submission_data import DATA, THUMBS, SUBMISSIONS


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--yes", action="store_true", help="跳过确认")
    args = ap.parse_args()

    targets = []
    if SUBMISSIONS.exists():
        targets.append(SUBMISSIONS)
    if THUMBS.exists():
        targets.append(THUMBS)
    photos = DATA / "photos"
    if photos.exists():
        targets.append(photos)
    exports = DATA / "exports"
    if exports.exists():
        targets.append(exports)

    if not targets:
        print("已经是干净的，无需清空。")
        return 0

    print("将删除：")
    for t in targets:
        n = len(list(t.rglob("*"))) if t.is_dir() else 1
        print(f"  {t}  ({n} 个文件)")
    print("保留：data/raw/（三校区 OSM 底图）")

    if not args.yes:
        if input("确认清空？输入 yes 继续：").strip().lower() != "yes":
            print("已取消。")
            return 1

    for t in targets:
        if t.is_dir():
            shutil.rmtree(t, ignore_errors=True)
        else:
            t.unlink(missing_ok=True)
    print("已清空，可以开始正式征稿了。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
