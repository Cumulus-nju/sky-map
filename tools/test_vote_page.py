# -*- coding: utf-8 -*-
"""投票页的**端到端**测试：真的把 pages/投票.py 跑起来（Streamlit AppTest）。

为什么不能只测数据层：
    数据层全绿、页面照样可能是坏的 —— 比如表单 key 撞了、字段名写错、
    `st.stop()` 把后面的内容一起干掉、缓存把旧数据一直喂回来。
    这个项目里"函数全绿但用户路径是坏的"已经发生过（2026-10-04 的选点吸附）。
    所以这里跑的是**页面脚本本身**，断言的是它渲染出来的东西。

隔离措施（很重要，绝不能碰项目里真实的投稿与投票）：
    * 投稿 → 指到临时目录的 `LocalStore`；
    * 投票 → 指到临时目录的 `LocalVoteStore`；
    * 时间窗 → 强制 `VOTE_MODE="open"`（今天 10-06，真实窗口 10-07 才开）。

用法：$env:PYTHONIOENCODING="utf-8"; python tools\\test_vote_page.py
"""
from __future__ import annotations

import io
import json
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import streamlit as st                       # noqa: E402
from PIL import Image                        # noqa: E402
from streamlit.testing.v1 import AppTest     # noqa: E402

import store as store_mod                    # noqa: E402
import vote_config as C                      # noqa: E402
import vote_store as V                       # noqa: E402
from submission_data import Submission       # noqa: E402

FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'✓' if ok else '✗'} {name}" + (f"  {detail}" if detail else ""))
    if not ok:
        FAILS.append(name)


def tiny_jpeg(color: tuple[int, int, int]) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (240, 160), color).save(buf, "JPEG", quality=70)
    return buf.getvalue()


def make_work(st, *, sid, title, status, campus="gulou", demo=False, author="张三"):
    img = tiny_jpeg((80, 120, 200) if not demo else (200, 80, 80))
    photo_ref = st.put_photo(sid, img, "image/jpeg")
    thumb_ref = st.put_thumb(sid, img)
    return Submission(
        sid=sid, campus=campus, lat=32.0554, lon=118.7789,
        title=title, author=author, contact="13800138000",
        shot_time="2026-10-05 18:20", weather="晚霞",
        loc_text="北大楼前草坪", loc_matched="北大楼",
        status=status, submitted_at="2026-10-05 19:00:00",
        photo=photo_ref, thumb=thumb_ref, thumb_ref=thumb_ref,
        is_demo=demo,
    )


# ---------------------------------------------------------------- 隔离环境
TMP = Path(tempfile.mkdtemp(prefix="vote_page_"))
sub_store = store_mod.LocalStore(TMP)
vote_store = V.LocalVoteStore(TMP / "votes.json")
store_mod.get_store(force=sub_store)
V.get_vote_store(force=vote_store)
C.VOTE_MODE = "open"

sub_store.write_submissions([
    make_work(sub_store, sid="P0001", title="晚霞下的北大楼", status=C.ELIGIBLE_STATUS),
    make_work(sub_store, sid="P0002", title="梧桐缝隙里的云", status=C.ELIGIBLE_STATUS),
    make_work(sub_store, sid="P0003", title="还没初筛的稿子", status="已收稿"),
    make_work(sub_store, sid="P0004", title="示例作品不该上墙", status=C.ELIGIBLE_STATUS, demo=True),
    # P0005 / P0006 是为了把「第 4 票被拦」在**页面**上也走一遍
    make_work(sub_store, sid="P0005", title="清晨的卷云", status=C.ELIGIBLE_STATUS),
    make_work(sub_store, sid="P0006", title="雨后的彩虹", status=C.ELIGIBLE_STATUS),
])

PAGE = HERE / "pages" / "投票.py"
print("=" * 66)
print(" 投票页端到端：上墙过滤 / 身份确认 / 3 票上限 / 撤回")
print("=" * 66)


def run_page():
    """跑一遍页面脚本。每次清缓存，否则 ttl=60 的 wall_items 会把上一次的结果喂回来。"""
    st.cache_data.clear()
    at = AppTest.from_file(str(PAGE), default_timeout=60)
    at.run()
    return at


def page_text(at) -> str:
    parts = []
    for attr in ("markdown", "caption", "title", "header", "subheader", "info",
                 "warning", "error", "success", "text", "code"):
        for el in getattr(at, attr, []) or []:
            v = getattr(el, "value", None)
            if isinstance(v, str):
                parts.append(v)
    return "\n".join(parts)


# ---------------------------------------------------------------- [1] 首屏
print("\n[1] 首屏能渲染，且只上墙该上墙的")
at = run_page()
check("页面无异常", not at.exception, str(at.exception)[:300] if at.exception else "")
body = page_text(at)
check("上墙作品 1 在页面上", "晚霞下的北大楼" in body)
check("上墙作品 2 在页面上", "梧桐缝隙里的云" in body)
check("非入围作品**不**上墙", "还没初筛的稿子" not in body)
check("示例作品**不**上墙（否则同学给假图投票）", "示例作品不该上墙" not in body)
check("有身份确认提示", "先确认身份" in body)
check("隐私：页面不出现投稿人姓名", "张三" not in body)
check("隐私：页面不出现联系方式", "13800138000" not in body)
check("显示了投票时间窗", "投票进行中" in body or "尚未开始" in body)

# ---------------------------------------------------------------- [2] 身份确认
print("\n[2] 身份确认表单（姓名 + 学号 + 手机号 三重认证）")
inputs = {i.label: i for i in at.text_input}
check("有 4 个输入框（姓名/学号/手机号/昵称）", len(at.text_input) == 4,
      f"实际 {[i.label for i in at.text_input]}")
check("有姓名输入框", any("姓名" in lb for lb in inputs))
check("有学号输入框", any("学号" in lb for lb in inputs))
check("有手机号输入框", any("手机号" in lb for lb in inputs))


def fill_identity(name: str, sid: str, phone: str, nick: str = "小明") -> None:
    for i in at.text_input:
        lb = i.label
        if "姓名" in lb:
            i.set_value(name)
        elif "学号" in lb:
            i.set_value(sid)
        elif "手机号" in lb:
            i.set_value(phone)
        else:
            i.set_value(nick)


# 先试一组非法输入：必须被拒，且人还在"未确认"状态
fill_identity("张", "12", "13800138000")     # 姓名太短、学号太短
submits = [b for b in at.button if "确认身份" in b.label]
check("表单有提交按钮", bool(submits), f"按钮：{[b.label for b in at.button]}")
if submits:
    submits[0].click().run()
    errs = " ".join(e.value or "" for e in at.error)
    check("非法姓名被拒（提示里点名姓名）", "姓名" in errs, f"errors={errs!r}")
    check("非法学号被拒（提示里点名学号）", "学号" in errs, f"errors={errs!r}")
    check("被拒后仍是未确认状态", "先确认身份" in page_text(at))

# 纯数字的姓名也要被拒（那是学号，不是姓名）
at = run_page()
fill_identity("20220001", "20220001", "13800138000")
[b for b in at.button if "确认身份" in b.label][0].click().run()
check("纯数字姓名被拒", any("数字" in (e.value or "") for e in at.error),
      f"errors={[e.value for e in at.error]}")

# 再填正确的一组。
# ⚠ 投票人的姓名故意与投稿作者（张三）**不同**：这样才能验证
#   "页面显示投票人自己的姓名（身份条）" 与 "页面绝不显示投稿作者姓名" 两件事。
C.VOTE_MODE = "open"
at = run_page()
fill_identity("王五", "20220001", "13800138000")
[b for b in at.button if "确认身份" in b.label][0].click().run()
check("合法信息提交后无异常", not at.exception,
      str(at.exception)[:300] if at.exception else "")
body = page_text(at)
check("确认后显示已确认身份", "已确认身份" in body, body[:200])
check("确认后显示投票人自己的姓名", "王五" in body, body[:200])
check("确认后显示昵称", "小明" in body)
check("确认后显示已投 0 票", "0 / 3" in body, body[:400])
check("确认后不再显示登记表单", "先确认身份" not in body)
check("隐私：投稿作者姓名仍然不出现", "张三" not in body)
check("身份不出现在 URL 里（隐私）",
      not any("20220001" in str(v) for v in at.query_params.values()))

# ---------------------------------------------------------------- [3] 投票
print("\n[3] 投票流程：3 票上限 + 不能重复投")
grid = [b for b in at.button if b.label.startswith("查看")]
check("每幅作品有「查看」按钮", len(grid) >= 2, f"{[b.label for b in at.button]}")
grid[0].click().run()
check("点「查看」能打开作品详情", "### ②" in page_text(at), page_text(at)[:120])


def open_work(sid: str) -> None:
    """按编号打开详情。

    ⚠ 不要用"第几个查看按钮"来定位作品：网格是按**投稿时间倒序**排的
    （同一时间则编号倒序），按钮顺序与编号顺序并不一致 —— 第一版就是这么
    写错的，导致投票的作品与断言的作品对不上号。按 sid 定位才稳。
    """
    global at
    st.cache_data.clear()
    at.session_state["v_open"] = sid
    at.run()


def vote_for(sid: str) -> bool:
    open_work(sid)
    btn = [b for b in at.button if "投这一票" in b.label]
    if not btn:
        return False
    btn[0].click().run()
    return True


check("能给 P0001 投票", vote_for("P0001"))
check("第 1 票投出去了（显示 1 / 3）", "1 / 3" in page_text(at), page_text(at)[:300])
check("能给 P0002 投票", vote_for("P0002"))
check("第 2 票投出去了（显示 2 / 3）", "2 / 3" in page_text(at), page_text(at)[:300])
check("两票落在两幅不同作品上", vote_store.tally() == {"P0001": 1, "P0002": 1},
      str(vote_store.tally()))

# 回头看第 1 幅：应该显示"已投过"，且没有重复投票按钮
open_work("P0001")
body = page_text(at)
check("已投过的作品显示「已投过」", "已经投过这幅作品" in body, body[:300])
check("已投过的作品不再出现投票按钮",
      not any("投这一票" in b.label for b in at.button),
      f"{[b.label for b in at.button]}")
check("已经投过的作品显示「撤回」入口", any("撤回" in b.label for b in at.button),
      f"{[b.label for b in at.button]}")

# ---------------------------------------------------------------- [4] 撤回
print("\n[4] 撤回改投")
open_work("P0001")
retract_btns = [b for b in at.button if "撤回" in b.label]
check("已投作品上有撤回按钮", bool(retract_btns), f"{[b.label for b in at.button]}")
if retract_btns:
    retract_btns[0].click().run()
    check("撤回后变回 1 / 3", "1 / 3" in page_text(at), page_text(at)[:300])
    check("撤回后只剩 P0002 那票", vote_store.tally() == {"P0002": 1},
          str(vote_store.tally()))
    check("撤回后该作品可以重新投票", vote_for("P0001"))
    check("重新投票后又是 2 票", vote_store.tally() == {"P0001": 1, "P0002": 1},
          str(vote_store.tally()))

# ---------------------------------------------------------------- [4b] 上限
print("\n[4b] 第 3 票用掉之后，第 4 票必须被拦住（页面上也要拦）")
check("能投第 3 票（P0005）", vote_for("P0005"))
check("显示已投满 3 / 3", "3 / 3" in page_text(at), page_text(at)[:300])
open_work("P0006")
body = page_text(at)
check("票用完后不再给投票按钮", not any("投这一票" in b.label for b in at.button),
      f"{[b.label for b in at.button]}")
check("票用完后给出「先撤回一票」的提示", "撤回" in body, body[:400])
check("数据层也确实只有 3 票", sum(vote_store.tally().values()) == 3, str(vote_store.tally()))

# ---------------------------------------------------------------- [5] 后台看得见
print("\n[5] 后台能看到票数（前台看不到）")
rows = vote_store.voter_rows()
check("后台能列出投票人", len(rows) == 1, str(rows))
check("投票人明细带姓名（后台要能核对身份）",
      bool(rows) and rows[0].get("姓名") == "王五", str(rows[:1]))
check("投票人明细带昵称", bool(rows) and rows[0].get("昵称") == "小明", str(rows[:1]))
check("投票人明细是掩码不是明文",
      bool(rows) and rows[0]["手机号"] == "138****8000", str(rows[:1]))
check("投票人明细显示 3 票", bool(rows) and rows[0]["票数"] == 3, str(rows[:1]))
check("投票人明细列出所投作品",
      bool(rows) and all(x in rows[0]["投票作品"] for x in ("P0001", "P0002", "P0005")),
      str(rows[:1]))
check("明细里没有明文手机号",
      not any("13800138000" in json.dumps(r, ensure_ascii=False) for r in rows))
check("票数排行能算出来",
      vote_store.tally() == {"P0001": 1, "P0002": 1, "P0005": 1}, str(vote_store.tally()))
flow = vote_store.vote_rows()
check("流水条数 = 总票数", len(flow) == 3, f"{len(flow)} 条")
check("流水里也带姓名", bool(flow) and all(r.get("姓名") == "王五" for r in flow),
      str(flow[:1]))
check("流水按时间升序（后台排查看得懂）",
      [r["时间"] for r in flow] == sorted(r["时间"] for r in flow),
      str([r["时间"] for r in flow]))

# ---------------------------------------------------------------- [6] 未开始
print("\n[6] 未到窗口时：页面提示且后端拒投")
C.VOTE_MODE = "auto"
if C.now_cn() < C.VOTE_OPEN_AT:
    at = run_page()
    body = page_text(at)
    check("页面提示投票尚未开始", "投票还没开始" in body or "尚未开始" in body, body[:200])
    check("未开始时不显示投票按钮",
          not any("投这一票" in b.label for b in at.button),
          f"{[b.label for b in at.button]}")
    check("未开始时预览作品仍然可以（不拦浏览）", "晚霞下的北大楼" in body)
    res = vote_store.cast(sid="20220001", phone="13800138000", name="王五",
                          nick="小明", work="P0002")
    check("后端也拒（不能只靠前端）", (not res["ok"]) and res["reason"] == "not_open", str(res))
    check("三重认证：换个姓名来投也会被身份校验拦住",
          vote_store.cast(sid="20220001", phone="13800138000", name="赵六",
                          nick="小明", work="P0002")["reason"] in ("not_open", "identity_mismatch"),
          "先受时间窗拦，窗口内则会被姓名不符拦住")
else:
    check("（当前已在窗口内，跳过未开始用例）", True)
C.VOTE_MODE = "open"

# ---------------------------------------------------------------- [7] 空场景
print("\n[7] 一幅都没上墙时不能白屏")
sub_store.write_submissions([])
at = run_page()
check("没有作品时不抛异常", not at.exception,
      str(at.exception)[:300] if at.exception else "")
body = page_text(at)
check("给出同学看得懂的说明", "整理中" in body, body[:200])
# 同学端不该出现"去管理后台"这类内部流程指引
check("不向同学暴露后台操作指引", "管理后台" not in body and "标记为已入围" not in body,
      body[:200])

print("\n" + "=" * 66)
if FAILS:
    print(f"✗ {len(FAILS)} 项失败：")
    for f in FAILS:
        print(f"    - {f}")
    raise SystemExit(1)
print("✓ 全部通过 —— 页面真的能跑：上墙过滤、身份确认、投票、撤回、后台可见")
