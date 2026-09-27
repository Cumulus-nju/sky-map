"""验证 Supabase 两代密钥的请求头写法。

背景（2026-09 查官方文档确认）：
    Supabase 正在弃用基于 JWT 的 anon / service_role 密钥，改用
    `sb_publishable_...`（公开）与 `sb_secret_...`（私有）。新格式**不是 JWT**，
    官方明确说明：

        无法在 `Authorization: Bearer ...` 标头中发送公开密钥或私有密钥，
        除非该值完全等于 `apikey` 标头。在这种情况下，请求将被转发到项目
        的数据库，但会被拒绝，因为该值不是 JWT。

    所以 SupabaseStore._headers() 必须分两代处理：
      * 旧版 JWT（eyJ...）→ 同时发 apikey + Authorization: Bearer，JWT 决定角色；
      * 新版非 JWT（sb_...）→ 只发 apikey，不发 Authorization。

    这个测试就是防止以后有人把 Authorization 头改回去而悄悄弄坏云端。

用法：
    python tools/test_key_formats.py
"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from store import SupabaseStore  # noqa: E402

CASES = [
    # (说明, 密钥, 是否应带 Authorization 头)
    ("旧版 JWT service_role", "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.abc.def", True),
    ("旧版 JWT anon", "eyJhbGciOiJIUzI1NiJ9.anon.sig", True),
    ("新版私有密钥", "sb_secret_AbCdEf123456", False),
    ("新版公开密钥", "sb_publishable_XyZ789", False),
]


def main() -> int:
    print("=" * 60)
    print(" Supabase 密钥格式 -> 请求头 兼容性测试")
    print("=" * 60)
    failures = 0
    for name, key, expect_auth in CASES:
        store = SupabaseStore.__new__(SupabaseStore)  # 不触发 requests 导入
        store.key = key
        headers = store._headers()
        has_auth = "Authorization" in headers
        good = (has_auth == expect_auth) and headers["apikey"] == key
        if not good:
            failures += 1
        print(
            f"  {'✓' if good else '✗'} {name:22s} "
            f"apikey=有  Authorization={'有' if has_auth else '无'} "
            f"（期望 {'有' if expect_auth else '无'}）"
        )

    # extra 头不能污染 apikey / 不能误加 Authorization
    store = SupabaseStore.__new__(SupabaseStore)
    store.key = "sb_secret_x"
    headers = store._headers({"Prefer": "return=minimal"})
    extra_ok = (
        headers.get("Prefer") == "return=minimal"
        and headers["apikey"] == "sb_secret_x"
        and "Authorization" not in headers
    )
    if not extra_ok:
        failures += 1
    print(f"  {'✓' if extra_ok else '✗'} extra 请求头合并正确且未污染 apikey")

    print()
    print("=" * 60)
    if failures:
        print(f"✗ 有 {failures} 项失败")
        return 1
    print("✓ 全部通过 —— 新旧两种密钥都能正确发请求")
    return 0


if __name__ == "__main__":
    sys.exit(main())
