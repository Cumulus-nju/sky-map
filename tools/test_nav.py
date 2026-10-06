# -*- coding: utf-8 -*-
"""页面导航条（`site_common.top_nav`）的测试。

为什么值得单独一套：手机端 Streamlit 的侧边栏是**收起**的
（2026-10-05 为了不让它压住地图、把触屏事件全接走而改成 `"auto"`），
于是同学打开只看到「投稿」页，**找不到**「打卡点地图」和「作品投票」——
2026-10-06 用户提的正是这个问题，解法是在正文最上方放一排显眼的入口。

这套测试钉住三件事：
  1. 配置：三个同学端页面都在导航里、**管理后台不在**（登录后才该出现的入口）；
  2. 接线：三个页面都真的调了 `top_nav`（漏一个 = 那个页面又没入口了），
     并且都带 `hasattr` 兜底（云端"新页面 + 旧 site_common"会 AttributeError，
     2026-10-06 投票页就这么整页崩过一次）；
  3. ⭐ 端到端：**用生产入口 `app.py`** 起一次 AppTest，确认三个链接真的渲染出来、
     当前页被标成「（当前）」、且整个过程不抛异常。
     （为什么不拿 `pages/投票.py` 当入口：`st.page_link` 只认「入口脚本 + `pages/`」
     这个页面注册表，那样 `app.py` 根本不在表里 —— 测出来的是测试环境的假象。
     这一点踩过：第一版就是这样报 StreamlitPageNotFoundError 的。）

用法：$env:PYTHONIOENCODING="utf-8"; python tools\\test_nav.py
"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import site_common as S  # noqa: E402

FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'✓' if ok else '✗'} {name}" + (f"  {detail}" if detail else ""))
    if not ok:
        FAILS.append(name)


print("=" * 66)
print(" 页面导航条：三个同学端页面都要有显眼入口（手机端侧边栏是收起的）")
print("=" * 66)

# ---------------------------------------------------------------------------
print("\n[1] 导航配置")
names = [n for _p, _i, n in S.NAV_ITEMS]
paths = [p for p, _i, _n in S.NAV_ITEMS]
check("导航里是 3 个页面", len(S.NAV_ITEMS) == 3, str(names))
check("顺序：投稿 / 打卡点地图 / 作品投票",
      names == [S.SUBMIT_PAGE, S.MAP_PAGE, S.VOTE_PAGE], str(names))
check("**不含管理后台**（那是登录后才该出现的入口）",
      S.ADMIN_PAGE not in names and S.ADMIN_FILE not in paths, str(paths))
check("每一项都有图标", all(i for _p, i, _n in S.NAV_ITEMS),
      str([i for _p, i, _n in S.NAV_ITEMS]))
for p in paths:
    check(f"页面文件存在：{p}", (HERE / p).exists())

src = (HERE / "site_common.py").read_text(encoding="utf-8")
check("定义了 top_nav", "def top_nav(" in src)
check("用 st.page_link（SPA 跳转，保留 session_state）", "st.page_link(" in src)
check("**没用 st.link_button**（那会整页跳转、丢掉刚确认的投票身份）",
      # ⚠ 判据要查**调用**（`st.link_button(`），不能查这个词本身 ——
      #   site_common 的注释里就写着"用 page_link 而不是 link_button"，
      #   查词会被自己的注释判成失败（这已经是本项目第三次踩这个坑了：
      #   placeholder、_nav_*、link_button）。
      "st.link_button(" not in src and "link_button(" not in src)
check("逐项兜底：page_link 失败时退回普通链接", "except Exception" in src and "_page_url" in src)
check("老版本 Streamlit 没有 page_link 时静默跳过",
      'hasattr(st, "page_link")' in src)

# ---------------------------------------------------------------------------
print("\n[2] 三个同学端页面都接了导航")
CALLS = {
    "app.py": "S.top_nav(S.SUBMIT_PAGE)",
    "pages/打卡点地图.py": "S.top_nav(S.MAP_PAGE)",
    "pages/投票.py": "S.top_nav(_NAV_LABEL)",
}
for rel, call in CALLS.items():
    text = (HERE / rel).read_text(encoding="utf-8")
    check(f"{rel} 调了 top_nav", call in text)
    check(f"{rel} 带 hasattr 兜底（防旧 site_common）",
          'hasattr(S, "top_nav")' in text)
check("管理后台**不**放公开导航（避免同学误入）",
      "top_nav" not in (HERE / "pages/管理员.py").read_text(encoding="utf-8"))

# ---------------------------------------------------------------------------
print("\n[3] 端到端：用生产入口 app.py 跑一次（这是真实页面注册表）")
import streamlit as st  # noqa: E402
from streamlit.testing.v1 import AppTest  # noqa: E402

st.cache_data.clear()
at = AppTest.from_file(str(HERE / "app.py"), default_timeout=180)
at.run()
check("页面无异常", not at.exception, str(at.exception)[:200] if at.exception else "")
links = at.get("page_link")
check("渲染出 3 个页面链接", len(links) == 3, f"{len(links)} 个")
labels = [str(getattr(x, "label", "")) for x in links]
check("三个页面都在导航里",
      all(any(n in lb for lb in labels) for n in (S.SUBMIT_PAGE, S.MAP_PAGE, S.VOTE_PAGE)),
      str(labels))
check("当前页被标出来（投稿（当前））",
      any(S.SUBMIT_PAGE in lb and "当前" in lb for lb in labels), str(labels))
pages = [str(getattr(x, "page", "")) for x in links]
check("链接指向真实页面（空 = 根页面 app.py）", "" in pages and "投票" in pages, str(pages))

print("\n" + "=" * 66)
if FAILS:
    print(f"✗ {len(FAILS)} 项失败：")
    for f in FAILS:
        print(f"    - {f}")
    raise SystemExit(1)
print("✓ 全部通过 —— 三个页面的导航都在，且生产入口下真的渲染得出来")
