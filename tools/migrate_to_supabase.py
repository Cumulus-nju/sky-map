"""把本地已有的投稿迁移到 Supabase（上云前跑一次）。

会读取本地 data/submissions.jsonl 与照片文件，写入 Supabase，
然后回读校验条数是否一致。

用法（PowerShell）：
    $env:SUPABASE_URL="https://xxx.supabase.co"
    $env:SUPABASE_KEY="eyJhbGciOi..."
    python tools/migrate_to_supabase.py --dry-run     # 先看看要传什么
    python tools/migrate_to_supabase.py               # 真传

先在 Supabase SQL Editor 里执行过 deploy/supabase_schema.sql。
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from store import (  # noqa: E402
    LocalStore,
    SupabaseStore,
    _safe_id,
    supabase_config,
)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只统计，不实际上传")
    args = ap.parse_args()

    cfg = supabase_config()
    if not cfg:
        print("✗ 没有检测到 SUPABASE_URL / SUPABASE_KEY 环境变量。")
        print("  先在 PowerShell 里设置这两个变量，例如：")
        print('    $env:SUPABASE_URL="https://xxxx.supabase.co"')
        print('    $env:SUPABASE_KEY="eyJhbGciOi..."')
        return 1

    local = LocalStore()
    subs = local.read_submissions()
    print(f"本地投稿：{len(subs)} 条")
    if not subs:
        print("没有可迁移的数据。")
        return 0

    # 统计要传的体积
    total = 0
    missing = []
    for s in subs:
        for ref, which in ((s.photo, "原图"), (s.thumb_ref or s.thumb, "缩略图")):
            data = local.get_photo(ref) if which == "原图" else local.get_thumb(ref)
            if data:
                total += len(data)
            elif which == "原图":
                missing.append(f"{s.sid} 缺{which}")
    print(f"照片总体积：{total / 1024 / 1024:.2f} MB（base64 后约 {total * 4 / 3 / 1024 / 1024:.2f} MB）")
    if missing:
        print(f"⚠ 有 {len(missing)} 条缺原图，将只迁移元数据与缩略图：")
        for m in missing[:8]:
            print("   ", m)

    if args.dry_run:
        print("\n（dry-run，未上传）")
        return 0

    cloud = SupabaseStore(*cfg)
    ok, msg = cloud.ping()
    print(f"\n连接 Supabase：{msg}")
    if not ok:
        print("  → 请检查 URL/KEY，以及是否已执行 deploy/supabase_schema.sql")
        return 1

    print("\n开始迁移…")
    done = 0
    for i, s in enumerate(subs, 1):
        pid = _safe_id(s.sid)
        origin = local.get_photo(s.photo)
        thumb = local.get_thumb(s.thumb_ref or s.thumb)
        try:
            if origin:
                cloud.put_photo(pid, origin, "image/jpeg")
            if thumb:
                cloud.put_thumb(pid, thumb)
            s.photo = pid
            s.thumb_ref = pid
            s.thumb = f"{pid}.jpg"
            done += 1
        except Exception as exc:
            print(f"  ✗ {s.sid} 失败：{type(exc).__name__}: {exc}")
        if i % 10 == 0 or i == len(subs):
            print(f"  已处理 {i}/{len(subs)}")

    print("\n写入投稿记录…")
    cloud.write_submissions(subs)

    back = cloud.read_submissions()
    print(f"\n✓ 迁移完成：云端现有 {len(back)} 条（本地 {len(subs)} 条）")
    if len(back) != len(subs):
        print("⚠ 条数不一致，请检查上面的报错。")
        return 1
    print("现在可以把 SUPABASE_URL / SUPABASE_KEY 配到 Streamlit Secrets 里部署了。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
