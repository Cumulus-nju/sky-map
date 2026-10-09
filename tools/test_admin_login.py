# -*- coding: utf-8 -*-
"""管理后台的登录闸门：**绝不自动登录**。

为什么值得单独一套（2026-10-09 用户报的 bug）：
    线上配了 `ADMIN_PASSWORD` 后，新用户打开 `/管理员` **不用输口令就进去了**。
    根因在 `ensure_admin_from_secrets()` —— 它为了"云端重启后口令不丢"，会按
    Secrets 里的口令调 `set_admin_password()` 把口令落盘；而那个函数顺手置了
    `session_state["admin_ok"] = True`。于是**任何新会话**，只要它触发了这次
    "从 Secrets 同步"（云端每次 Reboot / 容器文件系统被清之后的第一个访问者，
    正好就是"新用户"），就被当成已登录 ⇒ 直通后台。

这套测试钉住四件事：
  1. 静态：`set_admin_password` 的 `login` 参数**默认 False**（默认必须是安全的那个），
     且 `ensure_admin_from_secrets` 明确传 `login=False`；
  2. 动态：**全新会话 + 只有 Secrets 口令**（没有 `data/admin.json`，模拟云端冷启动）
     ⇒ 必须看到登录框，`admin_ok` 不能为真；
  3. 动态：文件已经落盘之后开的**第二个新会话**同样要输口令；口令错了不进；
     **口令对了才进**；
  4. 兜底：`data/admin.json` 读不到（云端只读/刚 Reboot）时，用 Secrets 里的口令
     仍然能登录 —— 否则会出现"怎么输都进不去"的死局。

用法：$env:PYTHONIOENCODING="utf-8"; python tools\\test_admin_login.py
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import site_common as S  # noqa: E402

TEST_PW = "test-pw-c0ffee-2026"
WRONG_PW = "definitely-not-the-password"

FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'✓' if ok else '✗'} {name}" + (f"  {detail}" if detail else ""))
    if not ok:
        FAILS.append(name)


def _labels(at) -> list[str]:
    return [str(getattr(w, "label", "")) for w in at.text_input]


def _is_login_form(at) -> bool:
    return "管理员口令" in _labels(at)


def _rendered_text(at) -> str:
    chunks = [str(getattr(t, "value", "")) for t in at.markdown]
    chunks += [str(getattr(t, "value", "")) for t in at.caption]
    return "\n".join(chunks)


print("=" * 66)
print(" 管理后台登录闸门：不输口令进不去（2026-10-09 修）")
print("=" * 66)

# ---------------------------------------------------------------------------
print("\n[1] 静态：默认必须是「不登录」那一侧")
src = (HERE / "site_common.py").read_text(encoding="utf-8")
page = (HERE / "pages/管理员.py").read_text(encoding="utf-8")

check("set_admin_password 有 login 参数", "def set_admin_password(pw: str, *, login: bool = False)" in src)
check("⚠ login **默认 False**（默认不安全 = 又会免密登录）",
      "login: bool = False" in src)
check("ensure_admin_from_secrets 明确传 login=False",
      "set_admin_password(pw, login=False)" in src)
check("「首次设置」表单自己设口令时传 login=True（设完直接进）",
      "S.set_admin_password(pw1, login=True)" in page)
check("置 admin_ok 只有两处：设口令时按 login 开关 + 校验通过时",
      src.count('st.session_state["admin_ok"] = True') == 2,
      f"{src.count('st.session_state[\"admin_ok\"] = True')} 处")
check("设口令那处被 `if login:` 管住（默认不登录）",
      'if login:\n        st.session_state["admin_ok"] = True' in src)
check("登录状态只写 session_state（不写文件/缓存 ⇒ 换会话就失效）",
      "st.session_state[\"admin_ok\"] = True" in src and "admin_ok" not in (HERE / "store.py").read_text(encoding="utf-8"))

# ---------------------------------------------------------------------------
print("\n[2] 动态：模拟云端冷启动（有 Secrets 口令、没有 admin.json）")
admin_meta = S.ADMIN_META
backup = admin_meta.read_bytes() if admin_meta.exists() else None

os.environ["ADMIN_PASSWORD"] = TEST_PW
from streamlit.testing.v1 import AppTest  # noqa: E402

try:
    if admin_meta.exists():
        admin_meta.unlink()
    check("前置条件：data/admin.json 已删掉", not admin_meta.exists())

    # ---- 会话①：正是"新用户"触及的那一刻 ----
    at1 = AppTest.from_file(str(HERE / "pages" / "管理员.py"), default_timeout=300)
    at1.run()
    check("会话① 无异常", not at1.exception, str(at1.exception)[:200] if at1.exception else "")
    check("会话① **没有**被自动登录（admin_ok 不为真）",
          not at1.session_state.get("admin_ok"),
          f"admin_ok={at1.session_state.get('admin_ok')!r}")
    check("会话① 看到的是登录框", _is_login_form(at1), str(_labels(at1)))
    check("会话① 没有渲染出后台正文（投稿管理）",
          "投稿管理" not in _rendered_text(at1))
    check("口令已按 Secrets 落盘（云端重启不用手动设置）", admin_meta.exists())
    if admin_meta.exists():
        raw = admin_meta.read_text(encoding="utf-8")
        check("落盘文件里**没有明文口令**", TEST_PW not in raw)
        check("落盘的是加盐哈希", "salt" in json.loads(raw) and "hash" in json.loads(raw))

    # ---- 会话②：文件已经在了，第二个新会话仍然要输口令 ----
    at2 = AppTest.from_file(str(HERE / "pages" / "管理员.py"), default_timeout=300)
    at2.run()
    check("会话②（文件已存在）同样要求输口令",
          not at2.session_state.get("admin_ok") and _is_login_form(at2),
          f"admin_ok={at2.session_state.get('admin_ok')!r}")

    # ---- 会话③：口令输错 ⇒ 还是进不去 ----
    at3 = AppTest.from_file(str(HERE / "pages" / "管理员.py"), default_timeout=300)
    at3.run()
    at3.text_input[0].set_value(WRONG_PW)
    at3.button[0].click().run()
    check("会话③ 错口令不登录", not at3.session_state.get("admin_ok"))
    check("会话③ 给出「口令不对」提示",
          "口令不对" in "".join(str(e.value) for e in at3.error))

    # ---- 会话④：口令对了 ⇒ 才进得去 ----
    # ⚠ 入口必须用生产入口 app.py 再 switch_page：直接拿 pages/管理员.py 当入口时
    #   页面注册表里没有它自己，登录成功后 `S.nav()` 的 `st.page_link` 会抛
    #   StreamlitPageNotFoundError（那是测试环境的假象，不是线上行为）——
    #   tools/test_nav.py 里记着同一个坑。
    at4 = AppTest.from_file(str(HERE / "app.py"), default_timeout=300)
    at4.run()
    at4.switch_page("pages/管理员.py").run()
    check("会话④ 切到后台先见到登录框", _is_login_form(at4), str(_labels(at4)))
    at4.text_input[0].set_value(TEST_PW)
    at4.button[0].click().run()
    check("会话④ 正确口令 ⇒ 已登录", bool(at4.session_state.get("admin_ok")))

    # ---- 会话⑤：兜底 —— admin.json 读不到时，Secrets 口令照样能登录 ----
    fallback = AppTest.from_string(f'''
import sys
from pathlib import Path
sys.path.insert(0, r"{HERE}")
import site_common as S
S.ADMIN_META = Path(r"{HERE}") / "data" / "_no_such_dir_" / "admin.json"
import streamlit as st
st.write("LOGIN_OK" if S.check_admin_password("{TEST_PW}") else "LOGIN_FAIL")
''', default_timeout=120)
    fallback.run()
    body = "".join(str(m.value) for m in fallback.markdown)
    check("会话⑤ 文件读不到时，Secrets 里的口令仍可登录（不会死锁）",
          "LOGIN_OK" in body, body[:80])
finally:
    if backup is not None:
        admin_meta.write_bytes(backup)
    elif admin_meta.exists():
        admin_meta.unlink()
    os.environ.pop("ADMIN_PASSWORD", None)

print("\n" + "=" * 66)
if FAILS:
    print(f"✗ {len(FAILS)} 项失败：")
    for f in FAILS:
        print(f"    - {f}")
    raise SystemExit(1)
print("✓ 全部通过 —— 后台每次都要求输口令，且不会把管理员锁在外面")
