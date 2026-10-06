"""「人气投票」规则配置 —— 所有可调规则集中在这一个文件里。

为什么要单独一个文件：规则是**会变**的（时间窗、票数、哪一档状态上墙），
而它们散在页面、数据层、SQL 函数里就一定会漏改一处。这里定义、别处引用。

⚠ 时间口径：**一律按北京时间（UTC+8）**。
Streamlit Cloud 的容器跑在 UTC，`datetime.now()` 会差 8 小时 ——
如果拿它跟"10月7日～11月30日"比，会把窗口整体平移 8 小时
（症状：明明还没到 10/7，投票却开着；或者 11/30 晚上 16:00 就提前关了）。
中国不实行夏令时，固定 +8 即可，不需要 tzdata。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

# 北京时间（中国无夏令时 ⇒ 固定偏移就够，不依赖 tzdata 库）
CN = timezone(timedelta(hours=8), name="Asia/Shanghai")


# ---------------------------------------------------------------- 时间窗

# 用户 2026-10-06 定的：**10.6 0 点**开、11 月底关。
# ⚠ 原来写的是 10-07，用户当天要求提前到 10-06 —— 也就是说**改完即开放**
#   （当天 00:00 已过）。要临时停掉投票用下面的 VOTE_MODE="closed"。
VOTE_OPEN_AT = datetime(2026, 10, 6, 0, 0, 0, tzinfo=CN)
VOTE_CLOSE_AT = datetime(2026, 11, 30, 23, 59, 59, tzinfo=CN)

# 手工开关，给两种场景用：
#   "auto"   —— 正常运行，按上面的时间窗自动开关（默认）
#   "open"   —— 强制开放：**调试用**。时间窗还没到也想试投票流程时改成它。
#   "closed" —— 强制关闭：出事了（比如发现有组织刷票）想立刻停掉投票。
# 改完记得重启本地 streamlit（起服务时带了 --server.fileWatcherType none，
# 旧进程不会自动加载新模块）。
VOTE_MODE = "auto"


# ---------------------------------------------------------------- 票数规则

# 每人最多几票。用户 2026-10-06 定：3 票。
VOTES_PER_PERSON = 3

# 「3 票必须投给不同作品」——靠 votes 表主键 (uid, work_sid) 强制，
# 不是靠前端灰掉按钮。前端拦截刷新页面就绕过去了。
UNIQUE_PER_WORK = True


# ---------------------------------------------------------------- 上墙范围

# 只有这个状态的投稿才进入投票墙（用户 2026-10-06 选的"只展示已过初筛的稿件"）。
# 取值必须与 pages/管理员.py 的 STATUS_OPTIONS 一致。
ELIGIBLE_STATUS = "已入围"

# 示例数据（is_demo=True）**永远不上墙** —— 否则同学们会给假作品投票。
# 这条不做成开关：踩一次（线上投票墙混进示例图）就是事故。
INCLUDE_DEMO = False


# ---------------------------------------------------------------- 辅助


def now_cn() -> datetime:
    """当前北京时间（带时区，可直接与上面的常量比较）。"""
    return datetime.now(CN)


def window_state(now: datetime | None = None) -> str:
    """当前时间窗状态：`not_open` / `open` / `closed`。

    这是**前端显示与后端校验共用**的同一个判据 —— 两处各写一份必然会漂移，
    漂移的症状是"页面说可以投，点了说已结束"（或反过来），且两边都不报错。
    """
    if VOTE_MODE == "open":
        return "open"
    if VOTE_MODE == "closed":
        return "closed"
    cur = now or now_cn()
    if cur < VOTE_OPEN_AT:
        return "not_open"
    if cur > VOTE_CLOSE_AT:
        return "closed"
    return "open"


def window_text(state: str | None = None) -> str:
    """给人看的一句话状态（页面顶部、后台都用它，保证口径一致）。"""
    st = state or window_state()
    open_s = VOTE_OPEN_AT.strftime("%Y-%m-%d %H:%M")
    close_s = VOTE_CLOSE_AT.strftime("%Y-%m-%d %H:%M")
    if st == "not_open":
        return f"投票尚未开始（{open_s} 开放）"
    if st == "closed":
        return f"投票已结束（{close_s} 截止）"
    return f"投票进行中（{open_s} — {close_s}）"


def open_iso() -> str:
    return VOTE_OPEN_AT.isoformat()


def close_iso() -> str:
    return VOTE_CLOSE_AT.isoformat()
