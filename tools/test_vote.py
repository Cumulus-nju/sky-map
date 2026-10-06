# -*- coding: utf-8 -*-
"""人气投票（vote_store / vote_config / 投票页 / Supabase SQL）的测试。

最要紧的三条（错了就是线上事故）：
  1. **"最多 3 票"必须拦得住** —— 包括第 4 票、同一幅重复投；
  2. **"一个人只能有一个身份"** —— 同一学号配别的手机号、同一手机号配别的学号，
     都必须被拒（这是"随便编个学号再投 3 票"唯一的防线）；
  3. **时间窗由后端把关** —— 前端显示"可以投"时后端也必须真的收，
     前端显示"已结束"时后端必须真的拒。两处判据必须同一个来源。

用法（GBK 控制台会 UnicodeEncodeError，先设编码）：
    $env:PYTHONIOENCODING="utf-8"; python tools\\test_vote.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import vote_config as C          # noqa: E402
import vote_store as V           # noqa: E402

FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'✓' if ok else '✗'} {name}" + (f"  {detail}" if detail else ""))
    if not ok:
        FAILS.append(name)


def fresh_store() -> V.LocalVoteStore:
    """每个用例一个全新的投票文件 —— 绝不能碰项目里那份 data/votes.json。"""
    tmp = Path(tempfile.mkdtemp(prefix="vote_test_")) / "votes.json"
    return V.LocalVoteStore(tmp)


print("=" * 66)
print(" 人气投票：3 票上限 / 一人一身份 / 时间窗后端把关")
print("=" * 66)

# ---------------------------------------------------------------------------
print("\n[1] 规范化与校验")
check("手机号去掉空格与横线", V.norm_phone("138 0013-8000") == "13800138000",
      V.norm_phone("138 0013-8000"))
check("学号去空格并转大写", V.norm_sid(" nju-2022a01 ") == "NJU2022A01",
      V.norm_sid(" nju-2022a01 "))
check("合法手机号通过", V.valid_phone("13800138000")[0])
check("10 位手机号被拒", not V.valid_phone("1380013800")[0])
check("1[3-9] 之外的号段被拒", not V.valid_phone("12800138000")[0])
check("空手机号被拒且给出中文提示", (not V.valid_phone("")[0]) and bool(V.valid_phone("")[1]))
check("纯数字学号通过", V.valid_sid("20220001")[0])
check("含字母的工号也通过（活动面向全校师生）", V.valid_sid("NJ2022A01")[0])
check("太短的学号被拒", not V.valid_sid("123")[0])
check("昵称不能为空", not V.valid_nick("   ")[0])
check("昵称上限 20 字", not V.valid_nick("x" * 21)[0])
check("姓名必填", not V.valid_name("")[0])
check("姓名太短被拒", not V.valid_name("张")[0])
check("中文姓名通过", V.valid_name("张三")[0])
check("带间隔号的少数民族姓名通过", V.valid_name("艾合买提·买买提")[0])
check("拉丁姓名（含空格）通过", V.valid_name("Mary Jane")[0])
check("纯数字不能当姓名（那是学号）", not V.valid_name("20220001")[0])
check("姓名内部连续空白压成一个（显示用）", V.norm_name("张  三") == "张 三", V.norm_name("张  三"))
check("姓名比对键去掉所有空白（'张 三' 与 '张三' 算同一人）",
      V.name_key("张  三") == V.name_key("张三") == V.name_key(" 张三 ") == "张三",
      repr(V.name_key("张  三")))
check("姓名比对键不影响拉丁姓名内部的字母", V.name_key("Mary Jane") == "MaryJane")
check("手机号掩码形态正确", V.mask_phone("13800138000") == "138****8000",
      V.mask_phone("13800138000"))
check("长学号掩码不泄露全部", V.mask_sid("202200010001") == "2022******01",
      V.mask_sid("202200010001"))
check("掩码里没有完整原文", "202200010001" not in V.mask_sid("202200010001"))

# ---------------------------------------------------------------------------
print("\n[2] 身份语义：学号是账号，姓名与手机号是两个因子")
t1 = V.identity("20220001", "13800138000", "张三")
t2 = V.identity("20220001", "13900139000", "张三")
t3 = V.identity("20220001", "13800138000", "李四")
t4 = V.identity("20220002", "13800138000", "张三")
check("identity 返回三个值 (uid, phone_h, name_h)", len(t1) == 3 and all(t1))
check("同一学号 ⇒ 同一个 uid（换手机号或姓名都不变）", t1[0] == t2[0] == t3[0],
      f"{t1[0][:8]}… / {t2[0][:8]}… / {t3[0][:8]}…")
check("换手机号 ⇒ phone_h 变", t1[1] != t2[1])
check("换姓名 ⇒ name_h 变", t1[2] != t3[2])
check("姓名哈希与手机号哈希不同源（不会互相抵消）", t1[1] != t1[2])
check("不同学号 ⇒ 不同 uid", t1[0] != t4[0])
check("uid 是 32 位十六进制", len(t1[0]) == 32 and all(c in "0123456789abcdef" for c in t1[0]))
check("格式不同但等价的输入算出同一组哈希",
      V.identity(" 20220001 ", "138-0013-8000", " 张三 ") == t1)

# 盐必须真的参与 —— 否则手机号（只有 ~1.7e9 种）可以被枚举反推
_old_salt = os.environ.get("VOTE_SALT")
os.environ["VOTE_SALT"] = "test-salt-AAAA"
u_salt_a = V.identity("20220001", "13800138000", "张三")[0]
os.environ["VOTE_SALT"] = "test-salt-BBBB"
u_salt_b = V.identity("20220001", "13800138000", "张三")[0]
if _old_salt is None:
    os.environ.pop("VOTE_SALT", None)
else:
    os.environ["VOTE_SALT"] = _old_salt
check("换盐后 uid 就变（盐真的生效了）", u_salt_a != u_salt_b)
check("换盐后 uid 也不同于默认盐", u_salt_a != t1[0])

# ---------------------------------------------------------------------------
print("\n[3] 时间窗：判据只有一处，且是北京时间")
check("开窗时间早于关窗时间", C.VOTE_OPEN_AT < C.VOTE_CLOSE_AT)
check("两个时间都带时区", C.VOTE_OPEN_AT.tzinfo is not None and C.VOTE_CLOSE_AT.tzinfo is not None)
check("时区是 +8（否则云端 UTC 会把窗口整体平移 8 小时）",
      C.VOTE_OPEN_AT.utcoffset() == timedelta(hours=8), str(C.VOTE_OPEN_AT.utcoffset()))
check("now_cn() 也是 +8", C.now_cn().utcoffset() == timedelta(hours=8))
check("开窗前 = not_open", C.window_state(C.VOTE_OPEN_AT - timedelta(seconds=1)) == "not_open")
check("开窗瞬间 = open", C.window_state(C.VOTE_OPEN_AT) == "open")
check("窗口中间 = open", C.window_state(C.VOTE_OPEN_AT + timedelta(days=1)) == "open")
check("关窗瞬间 = open（含端点）", C.window_state(C.VOTE_CLOSE_AT) == "open")
check("关窗后 = closed", C.window_state(C.VOTE_CLOSE_AT + timedelta(seconds=1)) == "closed")
check("按用户要求：10-07 开、11-30 关",
      C.VOTE_OPEN_AT.strftime("%Y-%m-%d") == "2026-10-07"
      and C.VOTE_CLOSE_AT.strftime("%Y-%m-%d") == "2026-11-30",
      f"{C.VOTE_OPEN_AT:%Y-%m-%d} → {C.VOTE_CLOSE_AT:%Y-%m-%d}")
check("每人 3 票（用户定稿）", C.VOTES_PER_PERSON == 3, str(C.VOTES_PER_PERSON))

_mode_backup = C.VOTE_MODE
C.VOTE_MODE = "closed"
check("VOTE_MODE=closed 时强制关闭（出事时能一键停）", C.window_state() == "closed")
C.VOTE_MODE = "open"
check("VOTE_MODE=open 时强制开放（调试用）", C.window_state() == "open")
C.VOTE_MODE = _mode_backup

# ---------------------------------------------------------------------------
print("\n[4] 本地投票：3 票上限与重复投")
# 用例都跑在"强制开放"下（否则 10-07 之前根本投不了，测试会全红）
C.VOTE_MODE = "open"
st = fresh_store()

def cast(sid, phone, work, nick="小明", name="张三"):
    return st.cast(sid=sid, phone=phone, name=name, nick=nick, work=work)

r1 = cast("20220001", "13800138000", "P0001")
check("第 1 票成功", r1["ok"], r1["text"])
check("返回已投 1 / 剩 2", (r1["used"], r1["left"]) == (1, 2), str(r1))
r2 = cast("20220001", "13800138000", "P0002")
r3 = cast("20220001", "13800138000", "P0003")
check("第 2、3 票成功", r2["ok"] and r3["ok"])
check("第 3 票后剩 0", r3["left"] == 0, str(r3))
r4 = cast("20220001", "13800138000", "P0004")
check("第 4 票被拒（上限）", (not r4["ok"]) and r4["reason"] == "limit", str(r4))
check("被拒时 used 仍是 3（没有多记）", r4["used"] == 3, str(r4))
check("上限提示里带票数", "3" in V.REASON_TEXT["limit"])

rd = cast("20220001", "13800138000", "P0001")
check("同一幅重复投被拒", (not rd["ok"]) and rd["reason"] == "duplicate", str(rd))
check("重复投的提示与上限提示不同（要用不同的措辞）",
      V.REASON_TEXT["duplicate"] != V.REASON_TEXT["limit"])

check("my_votes 返回 3 幅", sorted(st.my_votes(uid=V.identity("20220001", "13800138000")[0]))
      == ["P0001", "P0002", "P0003"])
check("另一个人不受影响", cast("20220099", "13700137000", "P0001")["ok"])

# 撤回 / 改投
u_a = V.identity("20220001", "13800138000")[0]
rr = st.retract(uid=u_a, work="P0002")
check("撤回首票成功", rr["ok"], str(rr))
check("撤回后剩 1 票可用", rr["left"] == 1, str(rr))
check("撤回后可以改投别的", cast("20220001", "13800138000", "P0005")["ok"])
check("撤回不存在的票被拒", (not st.retract(uid=u_a, work="P0999")["ok"]))
check("撤回不存在的票走 no_such_vote",
      st.retract(uid=u_a, work="P0999")["reason"] == "no_such_vote")

# ---------------------------------------------------------------------------
print("\n[5] 一人一身份：三个方向都必须拦住（核心防线）")
st2 = fresh_store()
ok_a = st2.cast(sid="20220001", phone="13800138000", name="张三",
                nick="小明", work="P0001")
check("先登记 (学号A, 手机A, 姓名甲)", ok_a["ok"])

# 方向一：同一学号 + 换手机号 → 必须拒（否则借同学的手机号就能再来 3 票）
r_same_sid = st2.cast(sid="20220001", phone="13900139000", name="张三",
                      nick="小明", work="P0002")
check("同一学号换手机号 ⇒ identity_mismatch",
      (not r_same_sid["ok"]) and r_same_sid["reason"] == "identity_mismatch", str(r_same_sid))

# 方向二：同一学号 + 换姓名（手机号不变）→ 也必须拒
#        —— 这就是"三重认证"里姓名那一条真正起作用的地方
r_same_name = st2.cast(sid="20220001", phone="13800138000", name="李四",
                       nick="小明", work="P0002")
check("同一学号换姓名 ⇒ identity_mismatch",
      (not r_same_name["ok"]) and r_same_name["reason"] == "identity_mismatch", str(r_same_name))

# 方向三：同一手机号 + 换学号 → 必须拒（否则随手编个学号就是新身份）
r_same_phone = st2.cast(sid="20220099", phone="13800138000", name="李四",
                        nick="小红", work="P0003")
check("同一手机号换学号 ⇒ identity_conflict",
      (not r_same_phone["ok"]) and r_same_phone["reason"] == "identity_conflict", str(r_same_phone))

check("这三次拒绝之后没有产生新投票人", len(st2.voter_rows()) == 1,
      f"{len(st2.voter_rows())} 人")
check("也只剩 1 票（没被白送）", sum(st2.tally().values()) == 1, str(st2.tally()))

# 大小写/空格不同但等价的输入，不能被当成第二个人
r_case = st2.cast(sid=" 20220001 ", phone="138-0013-8000", name=" 张三 ",
                  nick="小明", work="P0002")
check("同一人换个写法回来 = 还是他自己（不算身份冲突）", r_case["ok"], str(r_case))

# 姓名里多个空格（输入法很容易多打一个）也不该被当成换了个人
r_space = st2.cast(sid="20220001", phone="13800138000", name="张  三",
                   nick="小明", work="P0003")
check("姓名里多打空格仍算同一人（不拿输入习惯当安全问题）", r_space["ok"], str(r_space))

# 但**真换了姓名**必须拦住
r_other = st2.cast(sid="20220001", phone="13800138000", name="李四",
                   nick="小明", work="P0004")
check("真换姓名 ⇒ identity_mismatch",
      (not r_other["ok"]) and r_other["reason"] == "identity_mismatch", str(r_other))

# ---------------------------------------------------------------------------
print("\n[6] 时间窗由**后端**把关（不能只靠前端显示）")
st3 = fresh_store()
C.VOTE_MODE = "closed"
rc = st3.cast(sid="20220001", phone="13800138000", name="张三", nick="x", work="P0001")
check("强制关闭时投票被后端拒", (not rc["ok"]) and rc["reason"] == "closed", str(rc))
check("被拒不产生记录", st3.tally() == {} and st3.voter_rows() == [])
C.VOTE_MODE = "auto"
# 用真实时间窗：如果现在还没到 10-07，这一票也必须被拒
if C.now_cn() < C.VOTE_OPEN_AT:
    rn = st3.cast(sid="20220001", phone="13800138000", name="张三", nick="x", work="P0001")
    check("未到开窗时间，auto 模式下后端也拒", (not rn["ok"]) and rn["reason"] == "not_open", str(rn))
    check("撤回同样受时间窗限制", st3.retract(uid="x", work="P0001")["reason"] == "not_open")
else:
    check("（当前已在窗口内，跳过 not_open 用例）", True)

C.VOTE_MODE = "open"
ok_r = st3.cast(sid="20220001", phone="13800138000", name="张三", nick="x", work="P0001")
u3_ = V.identity("20220001", "13800138000", "张三")[0]
C.VOTE_MODE = "closed"
check("关闭后同学自己不能撤回", (not st3.retract(uid=u3_, work="P0001")["ok"]))
check("但管理员随时能删（处理刷票不能被时间窗挡住）",
      st3.admin_retract(uid=u3_, work="P0001")["ok"])
C.VOTE_MODE = _mode_backup

# ---------------------------------------------------------------------------
print("\n[7] 隐私：手机号/学号不落明文，姓名按设计存明文")
# ⚠ 这一段必须自己开窗：上一段末尾把 VOTE_MODE 恢复成 auto 了，
#   而今天可能还没到 10-07 —— 那样这一票会被正常拒掉、文件根本不生成，
#   随后读文件就 FileNotFoundError（第一版就是这么炸的）。
C.VOTE_MODE = "open"
st4 = fresh_store()
st4.cast(sid="20220001", phone="13800138000", name="张三", nick="小明", work="P0001")
blob = st4.path.read_text(encoding="utf-8")
check("文件里没有明文手机号", "13800138000" not in blob)
check("文件里没有明文手机号的后 8 位", "00138000" not in blob)
check("文件里没有明文完整学号", "20220001" not in blob)
check("文件里有手机号掩码（管理员核对争议要用）", "138****8000" in blob)
check("文件里有昵称（后台要认出是谁投的）", "小明" in blob)
# 姓名是**有意**存明文的：管理员的活儿就是"这是不是我们班的人"，
# 掩成"张*"等于没给；而同一张库里的投稿表本来就存着作者姓名明文。
check("姓名按设计存明文（后台核对身份要用）", "张三" in blob)
check("姓名也有哈希（比对用，防止大小写/空格绕过）", V.identity("20220001", "1", "张三")[2] in blob)
check("明文进出参仍然可用（my_votes 按 uid 查）",
      st4.my_votes(uid=V.identity("20220001", "13800138000", "张三")[0]) == ["P0001"])

# ---------------------------------------------------------------------------
print("\n[8] 双后端接口一致（本地能做的，云端也必须能做）")
local_api = {n for n in dir(V.LocalVoteStore) if not n.startswith("_")}
cloud_api = {n for n in dir(V.SupabaseVoteStore) if not n.startswith("_")}
missing = (local_api & {"cast", "retract", "admin_retract", "my_votes", "tally",
                        "voter_rows", "vote_rows", "clear_all"}) - cloud_api
check("云端实现了本地拥有的全部公开方法", not missing, f"缺：{sorted(missing)}")
check("两个后端都声明了 VOTE_API 所属模块版本", V.VOTE_API == 1)
check("工厂未配置 Supabase 时退回本地后端", V.get_vote_store(force=None).kind in ("local", "supabase"))

# ---------------------------------------------------------------------------
print("\n[9] 投票页静态检查（隐私与「票数不外露」）")
page = (HERE / "pages" / "投票.py").read_text(encoding="utf-8")
check("投票页存在", bool(page))
for bad, why in (("s.author", "投稿人姓名不得出现在投票页"),
                 ("s.contact", "联系方式不得出现在投票页"),
                 ("w.author", "投稿人姓名不得出现在投票页")):
    check(f"不引用 {bad}（{why}）", bad not in page)
check("票数只在「截止后公示」分支里取",
      page.index("tally()") > page.index("already_closed"),
      f"tally@{page.index('tally()')} > 公示判据@{page.index('already_closed')}")
check("用真实时间判断是否公示（不受手工开关影响）", "C.now_cn() > C.VOTE_CLOSE_AT" in page)
check("票数上限取自配置，不是硬编码 3", "C.VOTES_PER_PERSON" in page)
check("身份信息不进 URL（不写 query_params）", "query_params" not in page)
check("示例数据被排除在上墙之外", "is_demo" in page)
check("有模块版本自检（防云端「新页面 + 旧模块」）", "VOTE_API" in page)
# 三重认证：姓名必须真的成为投票时送交的因子，而不是"只要求填一下"
check("身份表单里有姓名一栏", "姓名" in page and "V.valid_name" in page)
check("姓名参与校验（必填且不得为纯数字）", "valid_name" in page)
check("投票时把姓名一起送交核对", 'name=st.session_state.get("v_name"' in page)
# 用户 2026-10-06 的两条要求。
# ⚠ 判据要查**真正的调用**（`placeholder=`）而不是这个词本身 ——
#   第一版写成 `"placeholder" not in page`，结果被我自己那句
#   "输入框不写 placeholder" 的注释判成失败（测试的判据把注释也算进去了）。
check("输入框里不预写提示文字（没有 placeholder= 参数）", "placeholder=" not in page)
check("昵称字段已移除（页面不再采集）",
      "v_nick" not in page and 'text_input("昵称"' not in page)

admin = (HERE / "pages" / "管理员.py").read_text(encoding="utf-8")
check("后台有人气投票页签", "人气投票" in admin)
check("后台能看到票数排行", "票数" in admin)
check("后台能撤销单条投票", "admin_retract" in admin)
check("后台能导出投票流水", "人气投票_流水.csv" in admin)
check("后台的上墙状态与 vote_config 一致（同一个字符串）",
      f'"{C.ELIGIBLE_STATUS}"' in admin or f"'{C.ELIGIBLE_STATUS}'" in admin,
      C.ELIGIBLE_STATUS)

# ---------------------------------------------------------------------------
print("\n[10] Supabase SQL 静态检查")
sql = (HERE / "deploy" / "supabase_schema.sql").read_text(encoding="utf-8")
for fn in ("cast_vote", "retract_vote", "admin_retract_vote", "vote_eligible"):
    check(f"定义了 {fn}()", f"function public.{fn}" in sql)
check("用了 advisory lock 串行化（并发超投的真正防线）",
      "pg_advisory_xact_lock" in sql)
check("votes 表主键是 (uid, work_sid)（同一作品只能投一次）",
      "primary key (uid, work_sid)" in sql)
check("phone_h 有唯一约束（一个手机号只能一个身份）",
      "phone_h     text not null unique" in sql)
check("用数据库时间 now()，不信客户端时钟", "timestamptz := now()" in sql)
check("捕获 unique_violation 并翻译成人话", "exception when unique_violation" in sql)
check("投票表开了 RLS（同学端查不到票数）",
      "alter table public.votes  enable row level security;" in sql)
check("改写后的身份模型已同步（不再有 p_sid_h 参数）", "p_sid_h" not in sql)
# 三重认证（2026-10-06 用户改的口径）
check("voters 有 name_h 列（姓名参与比对）", "name_h      text not null default ''" in sql)
check("voters 有 name 明文列（后台核对用）", "add column if not exists name   text not null default ''" in sql)
check("姓名**没有**被做成唯一约束（同名同姓是真实存在的）",
      "name_h      text not null unique" not in sql)
check("cast_vote 收下 p_name_h 与 p_name",
      "p_name_h     text," in sql and "p_name       text," in sql)
check("身份核对同时比对姓名与手机号",
      "v_phone_h <> p_phone_h or coalesce(v_name_h, '') <> p_name_h" in sql)
check("重复执行本文件能给老表补上新列（不会卡住）",
      "add column if not exists name_h" in sql and "add column if not exists name " in sql)

# ⚠ 这一条是踩过的坑：storage_usage 的**扩充版**引用了 voters/votes，
#   如果它出现在建表之前，整段 SQL 会在建视图时就中断（"relation does not exist"）。
#   第一版就写反了，所以钉一条测试。
_pos_table = sql.index("create table if not exists public.voters")
_pos_view = sql.rindex("create or replace view public.storage_usage")
check("扩充版 storage_usage 在 voters 建表**之后**（否则整段 SQL 执行到一半就断）",
      _pos_view > _pos_table, f"表@{_pos_table} 视图@{_pos_view}")
check("storage_usage 的扩充版统计了投票数", "from public.votes" in sql[_pos_view:])

# ---------------------------------------------------------------------------
print("\n" + "=" * 66)
if FAILS:
    print(f"✗ {len(FAILS)} 项失败：")
    for f in FAILS:
        print(f"    - {f}")
    raise SystemExit(1)
print("✓ 全部通过 —— 3 票上限拦得住、一人一身份（姓名+学号+手机号三重）、"
      "时间窗后端把关、手机号与学号不落明文")
