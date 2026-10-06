"""投票数据层：本地文件 / Supabase 双后端，接口一致。

为什么不复用 store.py 的 `Store`：
    投稿是"整条记录覆盖写"，冲突了也无所谓（管理员最后写赢）；
    投票是**并发下的计数与唯一性约束** —— "最多 3 票""同一作品只投一次"
    必须靠数据库的原子操作保证。前端把按钮灰掉是拦不住的：刷新一下、
    或者两条请求同时发出去，就能投出第 4 票。

    所以投票有两套实现、**同一套语义**：
      * `LocalVoteStore`  —— 本地开发用，json 文件 + 进程内锁；
      * `SupabaseVoteStore` —— 云端用，全部判断放在 Postgres 函数
        `cast_vote()` 里（一个事务内完成"查身份 → 查重 → 计数 → 写入"，
        并用 `pg_advisory_xact_lock` 按 uid 串行化，杜绝并发超投）。

⚠ 两者必须行为一致，改动一处就要改另一处 —— 由 `tools/test_vote.py` 钉住。

身份口径（用户 2026-10-06 定）：**姓名 + 手机号 + 学号，三者都必填**。
    语义是"**学号 = 账号，姓名与手机号 = 每次投票都要核对的因子**"：
    同一个学号永远只能配同一个人（姓名与手机号都要一致），
    同一个手机号也只能登记一个学号。
    ⇒ 想多投票就得**再拿出一张真实手机卡 + 再编一个自洽的姓名学号组合**，
      "随手编个学号再投 3 票"走不通。

    存储上的区别对待（想清楚过，不是偷懒）：
      * **手机号 / 学号 → 只存加盐哈希 + 掩码**。它们是"能被拿去骚扰人"的信息，
        库一泄露就是实打实的损失；掩码（138****8000）够管理员核对争议。
      * **姓名 → 存明文（同时也存哈希用于严格比对）**。理由：管理员的活儿就是
        "这是不是我们班的人""他是不是给自己投的"，掩码成"张*"等于没给；
        而且**同一张库里的投稿表本来就存着作者姓名明文**（`submissions.data.author`），
        权限模型完全一样（只有 service_role 能读）。
        网页上仍然**绝不出现**姓名 —— 那是"不公开"，与"后台可见"是两回事。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from abc import ABC, abstractmethod
from collections import Counter
from datetime import datetime
from pathlib import Path

import vote_config as C

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
VOTES_FILE = DATA / "votes.json"

# 本模块的**接口版本**（自检用，别删）。
# 为什么要有它：Streamlit Cloud 只改 .py 时可能只做**模块级热重载** ——
# 入口脚本（app.py / pages/*.py）每次重跑都是最新的，但 import 进来的模块
# 可能是旧进程里那份。2026-10-05 线上就因此白屏过一次（新 app.py + 旧
# site_common ⇒ IndexError）。页面靠这个常量判断"我拿到的 vote_store 是不是
# 我以为的那一版"，不是就在页面上明说，而不是抛一个看不懂的 AttributeError。
VOTE_API = 1

# 加盐哈希用的盐。**部署时请在 Secrets 里配 `VOTE_SALT`**。
# 为什么必须有盐：手机号只有约 1.7e9 种可能，拿到哈希后暴力枚举是分钟级的事。
# 有盐且盐不泄露 ⇒ 枚举不可行。这个默认值只保证"本地开发能用"，
# 线上**务必**在 Supabase/Streamlit Secrets 里换成自己的随机串。
_DEFAULT_SALT = "tian-guang-yun-ying-vote-2026"


# ---------------------------------------------------------------- 规范化与校验

_PHONE_RE = re.compile(r"^1[3-9]\d{9}$")
# 学号：6~20 位字母数字。放宽到字母是因为**教职工工号可能含字母**，
# 而这次活动是"面向全校师生"的 —— 只收纯数字会把老师挡在门外。
# 因此校验只做"长度 + 字符集"，不猜南京大学学号的位数规则。
_SID_RE = re.compile(r"^[0-9A-Za-z]{6,20}$")


def norm_phone(raw) -> str:
    """去掉空格、横线、+86 等，只留数字。"""
    return re.sub(r"[^\d]", "", str(raw or ""))


def norm_sid(raw) -> str:
    """去掉空格/横线并转大写（大小写不一致会被当成两个人）。"""
    return re.sub(r"[\s\-_]", "", str(raw or "")).upper()


def valid_phone(raw) -> tuple[bool, str]:
    p = norm_phone(raw)
    if not p:
        return False, "请填写手机号。"
    if not _PHONE_RE.match(p):
        return False, "手机号看起来不对（应为 11 位，1 开头）。"
    return True, ""


def valid_sid(raw) -> tuple[bool, str]:
    s = norm_sid(raw)
    if not s:
        return False, "请填写学号/工号。"
    if not _SID_RE.match(s):
        return False, "学号/工号看起来不对（应为 6~20 位字母或数字）。"
    return True, ""


def valid_nick(raw) -> tuple[bool, str]:
    n = str(raw or "").strip()
    if len(n) < 1:
        return False, "请填一个昵称（会显示在投票记录里）。"
    if len(n) > 20:
        return False, "昵称请控制在 20 个字以内。"
    return True, ""


# 姓名：2~20 个字符。允许中文、拉丁字母、空格与间隔号（"艾合买提·买买提"、
# "Mary Jane"都要能填），但**不允许纯数字** —— 那是学号，不是姓名。
_NAME_OK = re.compile(r"^[^\d]+$")


def norm_name(raw) -> str:
    """姓名的**显示**形式：去首尾空格、内部连续空白压成一个（"Mary  Jane" → "Mary Jane"）。

    ⚠ 这里**不做大小写折叠**：中文姓名没有大小写，而拉丁姓名
    （"Li Ming" vs "li ming"）折叠成小写会让管理员看不懂是谁。
    """
    return re.sub(r"\s+", " ", str(raw or "").strip())


def name_key(raw) -> str:
    """姓名用于**比对**的键：去掉**所有**空白后比较（比对用，不用于显示）。

    为什么与 `norm_name` 分开：同学第一次填「张 三」、第二次填「张三」
    是很可能发生的（输入法空格、复制粘贴），按字符严格比对就会判成
    "身份不符" —— 那是拿用户的输入习惯当安全问题。空白对姓名没有信息量。
    """
    return re.sub(r"\s+", "", str(raw or ""))


def valid_name(raw) -> tuple[bool, str]:
    n = norm_name(raw)
    if not n:
        return False, "请填写姓名（用于主办方核对，不会公开）。"
    if len(n) < 2:
        return False, "姓名至少 2 个字符。"
    if len(n) > 20:
        return False, "姓名请控制在 20 个字符以内。"
    if not _NAME_OK.match(n):
        return False, "姓名里不能只有数字（数字请填在学号一栏）。"
    return True, ""


def mask_phone(p: str) -> str:
    p = norm_phone(p)
    if len(p) != 11:
        return "*" * len(p)
    return f"{p[:3]}****{p[-4:]}"


def mask_sid(s: str) -> str:
    s = norm_sid(s)
    n = len(s)
    if n <= 4:
        return "*" * n
    if n <= 8:
        return s[:2] + "*" * (n - 4) + s[-2:]
    return s[:4] + "*" * (n - 6) + s[-2:]


# ---------------------------------------------------------------- 身份哈希


def vote_salt() -> str:
    """取投票用盐：环境变量 / Streamlit secrets 优先，否则用内置默认值。"""
    v = ""
    try:
        v = os.environ.get("VOTE_SALT", "") or ""
    except Exception:
        pass
    if not v:
        try:
            import streamlit as st

            v = str(st.secrets.get("VOTE_SALT", "") or "")
        except Exception:
            v = ""
    return v.strip() or _DEFAULT_SALT


def _h(*parts: str) -> str:
    raw = "\x1f".join([vote_salt(), *parts])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def identity(sid: str, phone: str, name: str = "") -> tuple[str, str, str]:
    """由 (学号, 手机号, 姓名) 算出 `(uid, phone_h, name_h)`。

    **uid 就是学号/工号的哈希** —— 语义上"一个人 = 一个学号"；
    姓名与手机号是**每次投票都要核对**的两个因子（存在 voters 表里）。

    为什么不让 uid 把三者一起哈希（第一版就是这么写的，已改）：
      那样 uid 与输入一一对应，`库里的因子 <> 本次传入` 这个分支
      **永远不可能成立** —— 成了走不到的死代码，看着像防护其实没有防护。
      而"同一学号换个手机号/换个名字再投一次"恰恰是最该拦的情况。
      现在 uid 只由学号决定，那些校验就真的会生效。

    ⚠ `VOTE_SALT` **一旦开始投票就不能再改**：改了 uid 与各因子哈希全部变样，
      已登记的投票人对不上号，同学会看到「与登记信息不一致」。
      要改就先把 votes/voters 清空（后台有"清空全部投票"）。
    """
    s, p, n = norm_sid(sid), norm_phone(phone), name_key(name)
    return _h("S", s), _h("P", p), _h("N", n)


# ---------------------------------------------------------------- 结果与文案

# 失败原因 → 给同学看的话。**放在这里而不是页面里**：
# 页面、后台、测试三处都要用到同一套口径，各写一份必然出现
# "页面说票数用完了、实际是身份对不上"这种自相矛盾的提示。
REASON_TEXT = {
    "ok": "投票成功",
    "not_open": "投票还没开始，请稍后再来。",
    "closed": "投票已经结束了。",
    "limit": f"你已经投满 {C.VOTES_PER_PERSON} 票了。想看别的作品可以先撤回一票。",
    "duplicate": "这幅作品你已经投过了 —— 3 票需要投给不同的作品。",
    "identity_mismatch": "姓名 / 学号 / 手机号 与首次登记的不一致。请用**同一组信息**投票"
                         "（三项都要和第一次填的一模一样）。",
    "identity_conflict": "该手机号已被另一个学号登记过。若确实填错了，请联系主办方处理。",
    "no_such_work": "这幅作品不在投票名单里（可能已被下架）。",
    "no_such_vote": "这条投票记录不存在（可能已经撤回了）。",
    "bad_input": "填写的信息不完整或格式不对。",
    "error": "服务器开了个小差，请稍后重试。",
}


def result(ok: bool, reason: str, used: int = 0, left: int = 0, **extra) -> dict:
    out = {"ok": ok, "reason": reason, "used": used, "left": left,
           "text": REASON_TEXT.get(reason, reason)}
    out.update(extra)
    return out


REASONS_FOR_LIMIT = ("limit", "duplicate")


# ---------------------------------------------------------------- 抽象接口


class VoteStore(ABC):
    """投票读写接口。两个后端必须给出**完全相同**的结果语义。"""

    kind = "base"

    @abstractmethod
    def cast(self, *, sid: str, phone: str, name: str, nick: str, work: str) -> dict:
        """投票。`sid`/`phone`/`name` 是三因子，`nick` 只用于后台认人。
        返回 `result()` 形状的 dict。"""

    @abstractmethod
    def retract(self, *, uid: str, work: str) -> dict:
        """同学自己撤回一票（受时间窗限制）。"""

    @abstractmethod
    def admin_retract(self, *, uid: str, work: str) -> dict:
        """管理员删某张票（**不受时间窗限制**，用于处理刷票）。"""

    @abstractmethod
    def my_votes(self, *, uid: str) -> list[str]:
        """某人已投的作品编号列表。"""

    @abstractmethod
    def tally(self) -> dict[str, int]:
        """作品编号 → 票数（**只给后台**）。"""

    @abstractmethod
    def voter_rows(self) -> list[dict]:
        """投票人明细（后台用，只含掩码不含明文）。"""

    @abstractmethod
    def vote_rows(self) -> list[dict]:
        """投票流水（后台用）。"""

    def describe(self) -> str:
        return f"{self.kind} 投票存储"

    def used(self, uid: str) -> int:
        return len(self.my_votes(uid=uid))


# ---------------------------------------------------------------- 本地实现


class LocalVoteStore(VoteStore):
    """本地 json 文件存储：`data/votes.json`。

    结构::

        {"voters": {uid: {"phone_h","sid_mask","phone_mask","nick","created"}},
         "votes":  [{"uid","work_sid","created"}]}

    `uid` 就是学号哈希（见 `identity()`），`phone_h` 是手机号哈希。

    ⚠ 这里的"原子性"只是一个**进程内锁** —— 本地开发够用（Streamlit 单进程多线程），
    但它挡不住多进程/多实例。真正的保证在 Supabase 那份（Postgres 事务）。
    所以：**线上必须接 Supabase**，本地这份只是让开发不用起数据库。
    """

    kind = "local"

    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else VOTES_FILE
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    # ---- 文件读写 ----
    def _load(self) -> dict:
        if not self.path.exists():
            return {"voters": {}, "votes": []}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return {"voters": {}, "votes": []}
        data.setdefault("voters", {})
        data.setdefault("votes", [])
        return data

    def _save(self, data: dict) -> None:
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self.path)   # 原子替换：中途崩了也不会留下半截文件

    @staticmethod
    def _now() -> str:
        return C.now_cn().strftime("%Y-%m-%d %H:%M:%S")

    # ---- 核心 ----
    def cast(self, *, sid: str, phone: str, name: str, nick: str, work: str) -> dict:
        state = C.window_state()
        if state != "open":
            return result(False, state if state == "not_open" else "closed")

        uid, phone_h, name_h = identity(sid, phone, name)

        with self._lock:
            data = self._load()
            voters, votes = data["voters"], data["votes"]

            # 1) 同一幅作品不能重复投（先查这条：最常见的误操作就是连点两下，
            #    这时候说"你已经投过了"比说"你票用完了"准确得多）
            if any(v["uid"] == uid and v["work_sid"] == work for v in votes):
                used = sum(1 for v in votes if v["uid"] == uid)
                return result(False, "duplicate", used=used,
                              left=max(0, C.VOTES_PER_PERSON - used))

            # 2) 三重因子核对：同一个学号必须始终配同一个姓名**和**同一个手机号
            #    （挡"换个手机号再投一次""换个名字再投一次"）
            me = voters.get(uid)
            if me and (me.get("phone_h") != phone_h or me.get("name_h") != name_h):
                used = sum(1 for v in votes if v["uid"] == uid)
                return result(False, "identity_mismatch", used=used,
                              left=max(0, C.VOTES_PER_PERSON - used))

            # 3) 反向冲突：同一个手机号不能登记成两个人
            #    （没有这条，(学号A,手机B) 与 (学号C,手机B) 就是两个人 ⇒ 6 票。
            #     姓名不做唯一约束 —— 同名同姓是真实存在的，撞名不能算作弊。）
            if not me and any(o.get("phone_h") == phone_h for o in voters.values()):
                return result(False, "identity_conflict")

            # 4) 票数上限
            used = sum(1 for v in votes if v["uid"] == uid)
            if used >= C.VOTES_PER_PERSON:
                return result(False, "limit", used=used, left=0)

            # 5) 落库
            if not me:
                voters[uid] = {
                    "name": norm_name(name),          # 明文：管理员的活儿要用（见模块注释）
                    "name_h": name_h,
                    "phone_h": phone_h,
                    "sid_mask": mask_sid(sid), "phone_mask": mask_phone(phone),
                    "nick": str(nick).strip()[:20], "created": self._now(),
                }
            else:
                me["nick"] = str(nick).strip()[:20]
            votes.append({"uid": uid, "work_sid": work, "created": self._now()})
            self._save(data)
            new_used = used + 1

        return result(True, "ok", used=new_used,
                      left=max(0, C.VOTES_PER_PERSON - new_used))

    def _remove(self, uid: str, work: str, *, check_window: bool) -> dict:
        if check_window:
            state = C.window_state()
            if state != "open":
                # ⚠ 要如实回报是"还没开始"还是"已结束"。
                # 第一版这里一律返回 "closed"，于是 10-07 之前撤回会显示
                # "投票已经结束了" —— 同学看到的是自相矛盾的提示
                # （页面明明写着"投票尚未开始"）。cast() 一开始就处理对了，
                # 只有这条漏了；两处口径必须一致，测试里钉了一条。
                return result(False, "not_open" if state == "not_open" else "closed")
        with self._lock:
            data = self._load()
            votes = data["votes"]
            keep = [v for v in votes if not (v["uid"] == uid and v["work_sid"] == work)]
            if len(keep) == len(votes):
                return result(False, "no_such_vote")
            data["votes"] = keep
            self._save(data)
            used = sum(1 for v in keep if v["uid"] == uid)
        return result(True, "ok", used=used, left=max(0, C.VOTES_PER_PERSON - used))

    def retract(self, *, uid: str, work: str) -> dict:
        return self._remove(uid, work, check_window=True)

    def admin_retract(self, *, uid: str, work: str) -> dict:
        return self._remove(uid, work, check_window=False)

    def my_votes(self, *, uid: str) -> list[str]:
        with self._lock:
            data = self._load()
        return [v["work_sid"] for v in data["votes"] if v["uid"] == uid]

    def tally(self) -> dict[str, int]:
        with self._lock:
            data = self._load()
        return dict(Counter(v["work_sid"] for v in data["votes"]))

    def voter_rows(self) -> list[dict]:
        with self._lock:
            data = self._load()
        votes = data["votes"]
        out = []
        for uid, v in data["voters"].items():
            mine = [x for x in votes if x["uid"] == uid]
            out.append({
                "uid": uid,
                "昵称": v.get("nick", ""),
                "姓名": v.get("name", ""),
                "学号": v.get("sid_mask", ""),
                "手机号": v.get("phone_mask", ""),
                "票数": len(mine),
                "投票作品": "、".join(x["work_sid"] for x in mine),
                "首次时间": v.get("created", ""),
            })
        out.sort(key=lambda r: (-r["票数"], r["首次时间"]))
        return out

    def vote_rows(self) -> list[dict]:
        with self._lock:
            data = self._load()
        nick = {uid: v.get("nick", "") for uid, v in data["voters"].items()}
        nm = {uid: v.get("name", "") for uid, v in data["voters"].items()}
        smask = {uid: v.get("sid_mask", "") for uid, v in data["voters"].items()}
        rows = [{
            "作品": v["work_sid"],
            "昵称": nick.get(v["uid"], ""),
            "姓名": nm.get(v["uid"], ""),
            "学号": smask.get(v["uid"], ""),
            "uid": v["uid"],
            "时间": v.get("created", ""),
        } for v in data["votes"]]
        rows.sort(key=lambda r: r["时间"])
        return rows

    def clear_all(self) -> int:
        """清空全部投票（后台危险操作，需二次确认）。"""
        with self._lock:
            data = self._load()
            n = len(data["votes"])
            self._save({"voters": {}, "votes": []})
        return n

    def describe(self) -> str:
        return f"本地投票文件（{self.path.name}）"


# ---------------------------------------------------------------- Supabase 实现


class SupabaseVoteStore(VoteStore):
    """云端后端：所有写操作都走 Postgres 函数，读操作直接查表。

    表与函数定义在 `deploy/supabase_schema.sql`。**加表之后必须去 Supabase
    SQL Editor 里执行一遍那段 SQL**，否则会在投票时报 404（表不存在）——
    这正是本项目 2026-10-05 那次"提交投稿报 HTTPError"的同类坑。
    """

    kind = "supabase"

    def __init__(self, url: str, key: str, timeout: int = 30):
        import requests  # 局部导入：本地模式不需要它

        self._requests = requests
        self.url = url.rstrip("/")
        self.key = key
        self.timeout = timeout

    # ---- 底层 HTTP（与 store.SupabaseStore 同一套密钥兼容逻辑）----
    def _headers(self, extra: dict | None = None) -> dict:
        from store import SupabaseStore

        h = {"apikey": self.key, "Content-Type": "application/json"}
        if not SupabaseStore._is_new_key_format(self.key):
            h["Authorization"] = f"Bearer {self.key}"
        if extra:
            h.update(extra)
        return h

    def _check(self, r, path: str) -> None:
        if r.status_code < 400:
            return
        from store import StoreHTTPError

        raise StoreHTTPError(r.status_code, r.text or "", path)

    def _rpc(self, fn: str, payload: dict):
        r = self._requests.post(
            f"{self.url}/rest/v1/rpc/{fn}",
            headers=self._headers(),
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            timeout=self.timeout,
        )
        self._check(r, f"rpc/{fn}")
        return r.json() if r.text else None

    def _select(self, table: str, params: dict) -> list:
        r = self._requests.get(f"{self.url}/rest/v1/{table}",
                               headers=self._headers(), params=params, timeout=self.timeout)
        self._check(r, table)
        return r.json() if r.text else []

    def ping(self) -> tuple[bool, str]:
        try:
            self._select("voters", {"select": "uid", "limit": 1})
            return True, "投票表连接正常"
        except Exception as exc:
            return False, f"{type(exc).__name__}: {exc}"

    # ---- 写 ----
    def cast(self, *, sid: str, phone: str, name: str, nick: str, work: str) -> dict:
        uid, phone_h, name_h = identity(sid, phone, name)
        payload = {
            "p_uid": uid, "p_phone_h": phone_h, "p_name_h": name_h,
            "p_name": norm_name(name),
            "p_sid_mask": mask_sid(sid), "p_phone_mask": mask_phone(phone),
            "p_nick": str(nick).strip()[:20], "p_work": work,
            "p_open": C.open_iso(), "p_close": C.close_iso(),
            "p_limit": C.VOTES_PER_PERSON,
        }
        raw = self._rpc("cast_vote", payload) or {}
        if isinstance(raw, list):        # 有的版本会把单行结果包成数组
            raw = raw[0] if raw else {}
        if not isinstance(raw, dict):
            return result(False, "error")
        reason = str(raw.get("reason", "ok"))
        return result(bool(raw.get("ok")), reason,
                      used=int(raw.get("used") or 0), left=int(raw.get("left") or 0))

    def _retract_rpc(self, uid: str, work: str, fn: str) -> dict:
        raw = self._rpc(fn, {"p_uid": uid, "p_work": work,
                             "p_open": C.open_iso(), "p_close": C.close_iso()}) or {}
        if isinstance(raw, list):
            raw = raw[0] if raw else {}
        if not isinstance(raw, dict):
            return result(False, "error")
        return result(bool(raw.get("ok")), str(raw.get("reason", "ok")),
                      used=int(raw.get("used") or 0), left=int(raw.get("left") or 0))

    def retract(self, *, uid: str, work: str) -> dict:
        return self._retract_rpc(uid, work, "retract_vote")

    def admin_retract(self, *, uid: str, work: str) -> dict:
        return self._retract_rpc(uid, work, "admin_retract_vote")

    # ---- 读 ----
    def my_votes(self, *, uid: str) -> list[str]:
        rows = self._select("votes", {"select": "work_sid", "uid": f"eq.{uid}",
                                     "order": "created_at.asc"})
        return [r["work_sid"] for r in rows]

    def tally(self) -> dict[str, int]:
        rows = self._select("votes", {"select": "work_sid"})
        return dict(Counter(r["work_sid"] for r in rows))

    def voter_rows(self) -> list[dict]:
        voters = self._select("voters", {"select": "*", "order": "created_at.asc"})
        votes = self._select("votes", {"select": "uid,work_sid"})
        by_uid: dict[str, list[str]] = {}
        for v in votes:
            by_uid.setdefault(v["uid"], []).append(v["work_sid"])
        out = []
        for v in voters:
            mine = by_uid.get(v["uid"], [])
            out.append({
                "uid": v["uid"],
                "昵称": v.get("nick", ""),
                "姓名": v.get("name", ""),
                "学号": v.get("sid_mask", ""),
                "手机号": v.get("phone_mask", ""),
                "票数": len(mine),
                "投票作品": "、".join(mine),
                "首次时间": v.get("created_at", ""),
            })
        out.sort(key=lambda r: (-r["票数"], r["首次时间"]))
        return out

    def vote_rows(self) -> list[dict]:
        voters = {v["uid"]: v for v in self._select("voters", {"select": "*"})}
        rows = []
        for v in self._select("votes", {"select": "uid,work_sid,created_at",
                                       "order": "created_at.asc"}):
            who = voters.get(v["uid"], {})
            rows.append({
                "作品": v["work_sid"],
                "昵称": who.get("nick", ""),
                "姓名": who.get("name", ""),
                "学号": who.get("sid_mask", ""),
                "uid": v["uid"],
                "时间": v.get("created_at", ""),
            })
        return rows

    def clear_all(self) -> int:
        """清空全部投票（后台危险操作）。先清票再清人，避免中间态。

        ⚠ PostgREST 的 DELETE **必须带过滤条件**（不带会被拒），
        所以这里用一个恒真的条件 `uid != '__none__'` 来表达"全部"。
        """
        n = len(self._select("votes", {"select": "uid"}))
        for table in ("votes", "voters"):
            r = self._requests.delete(f"{self.url}/rest/v1/{table}",
                                      headers=self._headers(),
                                      params={"uid": "neq.__none__"},
                                      timeout=self.timeout)
            self._check(r, table)
        return n

    def describe(self) -> str:
        return f"Supabase 云端投票（{self.url.split('//')[-1].split('.')[0]}…）"


# ---------------------------------------------------------------- 工厂

_VSTORE: VoteStore | None = None


def get_vote_store(force: VoteStore | None = None) -> VoteStore:
    """取当前生效的投票后端。

    后端选择**跟着投稿存储走**：投稿在 Supabase，投票也必须在那
    （否则云端重启会丢票 —— 免费平台的文件系统是临时的）。
    """
    global _VSTORE
    if force is not None:
        _VSTORE = force
        return _VSTORE
    if _VSTORE is not None:
        return _VSTORE
    from store import supabase_config

    cfg = supabase_config()
    _VSTORE = SupabaseVoteStore(*cfg) if cfg else LocalVoteStore()
    return _VSTORE


def reset_vote_store() -> None:
    global _VSTORE
    _VSTORE = None


def vote_backend_kind() -> str:
    return get_vote_store().kind


def parse_created(text: str) -> datetime | None:
    """把库里存的时间串解析出来（本地是 +08 的字符串，云端是 ISO）。"""
    if not text:
        return None
    t = str(text).strip().replace("Z", "+00:00")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z",
                "%Y-%m-%dT%H:%M:%S.%f+00:00", "%Y-%m-%dT%H:%M:%S+00:00"):
        try:
            return datetime.strptime(t, fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(t)
    except Exception:
        return None
