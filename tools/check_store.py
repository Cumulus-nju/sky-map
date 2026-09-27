"""存储层自检：本地模式与 Supabase 模式都能测。

用法：
    python tools/check_store.py              # 测当前配置（本地或云端）
    python tools/check_store.py --local      # 强制测本地文件存储
    python tools/check_store.py --supabase   # 强制测 Supabase（需环境变量）
"""
from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  {'✓' if cond else '✗'} {name}{('  ' + detail) if detail else ''}")
    if not cond:
        FAILS.append(name)


def test_store(store, label: str) -> None:
    from submission_data import Submission

    print(f"\n[{label}] {store.describe()}")

    # 用一个明显不会撞车的编号
    sid = "ZZTEST001"
    pid = "zztest001"

    try:
        # 1) 写照片
        payload = b"\xff\xd8\xff\xe0" + b"x" * 2048          # 假 JPEG 头
        ref_o = store.put_photo(pid, payload, "image/jpeg")
        check("put_photo 返回引用", bool(ref_o), str(ref_o))

        thumb = b"\xff\xd8\xff\xe0" + b"t" * 1024
        ref_t = store.put_thumb(pid, thumb)
        check("put_thumb 返回引用", bool(ref_t), str(ref_t))

        # 2) 读回并比对（base64 往返）
        got_o = store.get_photo(ref_o)
        got_t = store.get_thumb(ref_t)
        check("原图往返一致", got_o == payload, f"{len(got_o or b'')} bytes")
        check("缩略图往返一致", got_t == thumb, f"{len(got_t or b'')} bytes")

        # 3) 投稿读写
        before = store.read_submissions()
        rec = Submission(sid=sid, campus="gulou", lat=32.0566, lon=118.7736,
                         title="存储层自检", author="__test__",
                         photo=str(ref_o), thumb_ref=str(ref_t), thumb=f"{pid}.jpg")
        store.write_submissions(list(before) + [rec])
        mid = store.read_submissions()
        check("写入后能读回", any(s.sid == sid for s in mid), f"{len(mid)} 条")

        # 4) 删除并确认清干净
        store.write_submissions(before)
        after = store.read_submissions()
        check("删除后记录消失", not any(s.sid == sid for s in after))
        store.delete_photo(ref_o)
        store.delete_photo(ref_t)
        check("删除后照片取不到", store.get_photo(ref_o) is None)
        check("条数与测试前一致", len(after) == len(before),
              f"{len(before)} → {len(after)}")
    except Exception as exc:
        check(f"{label} 全流程", False, f"{type(exc).__name__}: {exc}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--local", action="store_true")
    ap.add_argument("--supabase", action="store_true")
    args = ap.parse_args()

    from store import LocalStore, SupabaseStore, get_store, supabase_config

    print("=" * 60)
    print(" 存储层自检")
    print("=" * 60)

    cfg = supabase_config()
    print(f"\n环境变量：SUPABASE_URL/KEY {'已配置' if cfg else '未配置'}")

    if args.local:
        test_store(LocalStore(Path(tempfile.mkdtemp())), "本地（临时目录）")
    elif args.supabase:
        if not cfg:
            print("✗ 未配置 SUPABASE_URL / SUPABASE_KEY")
            return 1
        test_store(SupabaseStore(*cfg), "Supabase")
    else:
        st = get_store()
        print(f"当前生效后端：{st.kind}")
        test_store(st, "当前后端")

    print("\n" + "=" * 60)
    if FAILS:
        print(f"✗ {len(FAILS)} 项失败：" + "、".join(FAILS))
        return 1
    print("✓ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
