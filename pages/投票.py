"""作品投票墙 —— 陈列所有入围作品，同学确认身份后投票（每人最多 3 票）。

几条**刻意如此**的设计（改之前先读，每条都是选了另一条会出事的坑）：

  * **只上墙「已入围」的投稿**，示例数据（`is_demo`）永不进墙。
    否则线上投票墙会混进 demo 图，同学给假作品投票。
  * **投票期间不显示票数**（截止后自动公示）。显示实时票数会让
    "谁领先就投谁"滚雪球，人气奖就失真了。
  * **票数上限不由前端决定**。按钮灰掉只是提示 —— 真正的约束在
    `vote_store`（云端是 Postgres 函数 `cast_vote()`，一个事务里完成
    查身份+查重+计数+写入）。前端拦截刷新页面就绕过去了。
  * **URL 里不带任何身份信息**。身份只活在 `session_state`（服务端），
    而 URL 会被分享、被截图、进浏览器历史 —— 塞进去等于公开同学手机号。
    代价：换个浏览器/刷新会话要重新填一次，这是有意换来的隐私。
  * **页面上不出现投稿人姓名**（与打卡点地图同一条规矩）。
"""
from __future__ import annotations

import io
import sys
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import site_common as S  # noqa: E402

S.setup("作品投票", "🗳")

import vote_config as C  # noqa: E402
import vote_store as V  # noqa: E402
from campus_config import CAMPUSES  # noqa: E402
from submission_data import load_submissions  # noqa: E402

# ⚠⚠ **不许**把这行写成 `S.nav(S.VOTE_PAGE)`（第一版就是，线上真的整页炸了）。
# 原因：Streamlit Cloud 只改 .py 时可能只做**模块级热重载** —— 入口脚本
# （app.py / pages/*.py）每次重跑都是最新的，但 `import` 进来的模块可能还是
# 旧进程里那份。于是出现"**新 pages/投票.py + 旧 site_common.py**"，
# 旧模块里没有 `VOTE_PAGE` ⇒ `AttributeError` ⇒ 投票页整页打不开，
# 而错误信息还被平台涂掉了（只剩一句 "original error message is redacted"）。
# 这与 2026-10-05 那次 `take_pick()` 返回三元组导致的 IndexError **是同一个坑**，
# 也与 requirements.txt 里记的两次重建是同一个坑 —— 这是第四次了。
#
# **规矩（写下来别再犯）**：入口脚本里凡是要用被 import 模块的
# 新常量 / 新函数 / 新返回值，一律 `getattr(..., 默认值)` 兜底，
# 并在页面上**明说**"模块是旧的、请 Reboot"，而不是抛一个看不懂的错。
_NAV_LABEL = "作品投票"
if hasattr(S, "VOTE_PAGE"):
    _NAV_LABEL = S.VOTE_PAGE
else:
    st.error(
        "⚠️ `site_common.py` 还是旧版本（本页面是新版）。\n\n"
        "**请到 Streamlit Cloud 的 Manage app → Reboot 容器**，"
        "等 1~2 分钟后刷新本页。\n\n"
        "（这是 Streamlit 的「模块级热重载」行为：只改了 .py 时，"
        "入口脚本会更新，但已加载的模块不会。）"
    )

S.nav(_NAV_LABEL)

WALL_PX = 460          # 投票墙缩略图长边（比投稿页的 900 小很多：一屏要放 18 张）
PAGE_SIZE = 18
NCOL = 3               # 手机上一行 3 张、桌面上也不至于太宽

# ---------------------------------------------------------------- 自检
#
# 云端只改 .py 时可能"新页面 + 旧 vote_store"（模块级热重载，见 site_common.PICK_API
# 那段注释）。少个函数就是 AttributeError，同学看到的是白屏 —— 不如自己说清楚。
if getattr(V, "VOTE_API", 0) != 1:
    st.error(
        "⚠️ 投票模块版本不匹配（页面是新的，`vote_store.py` 还是旧版）。\n\n"
        "**云端**：到 Streamlit Cloud 的 Manage app → **Reboot** 容器；"
        "**本地**：重启 streamlit（起服务时带了 `--server.fileWatcherType none`，"
        "旧进程不会自动加载新模块）。"
    )
    st.stop()


# ---------------------------------------------------------------- 数据


@st.cache_data(show_spinner=False, ttl=60)
def wall_items() -> list:
    """上墙作品。只保留「已入围」且非示例数据，按投稿时间排序。

    `ttl=60`：同学连点几张卡片会触发多次 rerun，每次都去 Supabase 拉一遍
    全部投稿太慢；但也不能一直缓存（管理员刚标记入围的作品要能很快看到），
    所以给一个短 TTL + 手动刷新按钮。
    """
    out = []
    for s in load_submissions():
        if s.is_demo and not C.INCLUDE_DEMO:
            continue
        if (s.status or "") != C.ELIGIBLE_STATUS:
            continue
        out.append(s)
    out.sort(key=lambda s: (s.submitted_at or "", s.sid))
    return out


@st.cache_data(show_spinner=False, ttl=3600, max_entries=600)
def wall_thumb(ref: str, version: str) -> bytes | None:
    """把缩略图压到 `WALL_PX` 供投票墙使用。

    为什么不直接用存好的 900px 缩略图：一屏 18 张、每张 900px 约 150 KB，
    一页就是 2.7 MB（还要 base64 塞进 WebSocket），手机上会明显卡。
    压到 460px 后每张约 40 KB。`version` 参与缓存键，换了图会自动失效。
    """
    if not ref:
        return None
    from store import get_store

    store = get_store()
    raw = store.get_thumb(ref)
    if not raw:
        raw = store.get_photo(ref)      # 兜底：没有缩略图就用原图压
    if not raw:
        return None
    try:
        from PIL import Image

        img = Image.open(io.BytesIO(raw))
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        img.thumbnail((WALL_PX, WALL_PX), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=78, optimize=True, progressive=True)
        return buf.getvalue()
    except Exception:
        return raw      # 压不动就原样给（HEIC 之类），总比空白强


def work_thumb(w) -> bytes | str | None:
    ref = w.thumb_ref or w.thumb
    data = wall_thumb(ref, f"{w.sid}:{w.submitted_at}")
    if data:
        return data
    # 本地模式下缩略图可能是文件路径而 get_thumb 没能直接命中，退回原来的入口
    from submission_data import thumb_display_src

    return thumb_display_src(w, max_side=WALL_PX)


def detail_image(w):
    from submission_data import thumb_display_src

    return thumb_display_src(w, max_side=900)


def campus_name(key: str) -> str:
    cfg = CAMPUSES.get(key)
    return getattr(cfg, "short", None) or (cfg.name if cfg else key)


def work_title(w) -> str:
    return (w.title or "").strip() or f"（未命名 · {w.sid}）"


def work_place(w) -> str:
    return (w.loc_matched or w.loc_text or "").strip() or "未标注机位"


@st.cache_data(show_spinner=False, ttl=60)
def vote_backend_ok() -> tuple[bool, str]:
    """投票用的两张表到底能不能用。

    为什么要预检：云端最常见的部署失误是**忘了到 Supabase 跑一遍
    `deploy/supabase_schema.sql`**（表不存在 ⇒ 404）。不预检的话，同学要一直
    填完身份、点下投票，才看到一个裸的 "HTTP 404"；预检能提前把话说清楚。
    `ttl=60` 是为了配好之后不用重启就能恢复。
    """
    store = V.get_vote_store()
    ping = getattr(store, "ping", None)
    if ping is None:
        return True, ""          # 本地后端没有 ping，也不需要
    try:
        return ping()
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


# ---------------------------------------------------------------- 身份


def identity_bar() -> tuple[bool, str, set[str]]:
    """身份条。返回 `(是否已确认, uid, 已投作品集合)`。"""
    uid = st.session_state.get("v_uid") or ""
    if uid:
        store = V.get_vote_store()
        try:
            mine = set(store.my_votes(uid=uid))
        except Exception as exc:
            st.error(f"读取你的投票记录失败：{type(exc).__name__}: {exc}")
            mine = set()
        used = len(mine)
        with st.container(border=True):
            c1, c2 = st.columns([4, 1])
            with c1:
                st.markdown(f"**✅ 已确认身份：{st.session_state.get('v_name', '')}**")
                st.caption(
                    f"学号 `{st.session_state.get('v_sid_mask', '')}` · "
                    f"手机 `{st.session_state.get('v_phone_mask', '')}` · "
                    f"已投 **{used} / {C.VOTES_PER_PERSON}** 票"
                    + ("　🎉 票已用完，可撤回后改投" if used >= C.VOTES_PER_PERSON else "")
                )
            with c2:
                if st.button("切换身份", use_container_width=True, key="v_logout"):
                    for k in ("v_uid", "v_name", "v_sid", "v_phone",
                              "v_sid_mask", "v_phone_mask"):
                        st.session_state.pop(k, None)
                    st.rerun()
        return True, uid, mine

    with st.container(border=True):
        st.markdown(f"**① 先确认身份，再投票**（每人最多 {C.VOTES_PER_PERSON} 票，"
                    f"{C.VOTES_PER_PERSON} 票需投给**不同**作品）")
        # 这里原来还有两段说明（"三项都必填是故意的…"与"之后再投票要填得一模一样"）。
        # 用户 2026-10-06 要求只留票数规则 ⇒ 两段都删了。
        # 删掉不丢信息：隐私口径与"每次都要填得完全一致"都写在页面底部
        # 收起的「📖 投票规则」里；而真填错时，报错文案本身也会提示
        # "请用同一组信息投票（三项都要和第一次填的一模一样）"。
        #
        # ⚠ 输入框**不写 placeholder**（用户 2026-10-06 要求"框框里面不要预写东西"）：
        #   标签已经写清要填什么，框里再塞示例反而像已经填好了。
        # ⚠ 昵称字段已按用户要求**去掉**：它原来只是给后台认人用的，
        #   而现在姓名字段本身就存了明文、后台看得到 —— 昵称完全冗余。
        with st.form("vote_identity"):
            f1, f2, f3 = st.columns(3)
            name = f1.text_input("姓名", max_chars=20)
            sid = f2.text_input("学号 / 工号")
            phone = f3.text_input("手机号", max_chars=20)
            ok = st.form_submit_button("确认身份", type="primary",
                                       use_container_width=True)
        if ok:
            errs = []
            for fn, val in ((V.valid_name, name), (V.valid_sid, sid),
                            (V.valid_phone, phone)):
                good, msg = fn(val)
                if not good:
                    errs.append(msg)
            if errs:
                st.error(" ".join(errs))
            else:
                new_uid, _, _ = V.identity(sid, phone, name)
                st.session_state["v_uid"] = new_uid
                st.session_state["v_name"] = V.norm_name(name)
                st.session_state["v_sid"] = V.norm_sid(sid)
                st.session_state["v_phone"] = V.norm_phone(phone)
                st.session_state["v_sid_mask"] = V.mask_sid(sid)
                st.session_state["v_phone_mask"] = V.mask_phone(phone)
                st.rerun()
    return False, "", set()


# ---------------------------------------------------------------- 投票动作


def flash(kind: str, text: str) -> None:
    """把一条提示留到**下一次 rerun** 再显示。

    为什么不能直接 st.success：投票后必须 rerun 才能刷新"已投 x/3"，
    而 rerun 会把刚写下的提示一起冲掉 —— 同学会以为没投上。
    """
    st.session_state["v_flash"] = (kind, text)


def show_flash() -> None:
    item = st.session_state.pop("v_flash", None)
    if not item:
        return
    kind, text = item
    (st.success if kind == "ok" else st.warning)(text)
    # 再在屏幕角落弹一下：卡片上的投票按钮在**页面下方**，投完会 rerun，
    # 而顶部这条 success 很可能不在视野里 —— 同学会以为没投上。
    # `st.toast` 固定在角落，和滚动位置无关。
    try:
        st.toast(text, icon="✅" if kind == "ok" else "⚠️")
    except Exception:
        pass   # 老版本 Streamlit 没有 st.toast，忽略（顶部那条仍然在）


def do_cast(work) -> None:
    store = V.get_vote_store()
    try:
        res = store.cast(
            sid=st.session_state.get("v_sid", ""),
            phone=st.session_state.get("v_phone", ""),
            name=st.session_state.get("v_name", ""),
            # 昵称字段已按用户要求去掉（2026-10-06）。这里仍然传空串而不是删掉
            # 这个参数：`cast()` 的 nick 一路通到 Postgres 函数 `cast_vote(...)`
            # 的参数列表，而线上**已经执行过那段 SQL** —— 改签名会让
            # `create or replace function` 变成"新增一个重载"而不是替换，
            # PostgREST 会因函数歧义报错。留着这个参数是无害的（永远是空串）。
            nick="",
            work=work.sid,
        )
    except Exception as exc:
        flash("warn", f"投票失败：{type(exc).__name__}: {exc}")
        st.rerun()
        return
    if res.get("ok"):
        flash("ok", f"投票成功！你已投 {res['used']}/{C.VOTES_PER_PERSON} 票"
                    + ("，票已用完。" if res["left"] == 0 else f"，还剩 {res['left']} 票。"))
    else:
        flash("warn", res.get("text") or "投票没有成功。")
    st.rerun()


def do_retract(work) -> None:
    store = V.get_vote_store()
    try:
        res = store.retract(uid=st.session_state.get("v_uid", ""), work=work.sid)
    except Exception as exc:
        flash("warn", f"撤回失败：{type(exc).__name__}: {exc}")
        st.rerun()
        return
    if res.get("ok"):
        flash("ok", f"已撤回对「{work_title(work)}」的投票，"
                    f"现在已投 {res['used']}/{C.VOTES_PER_PERSON} 票。")
    else:
        flash("warn", res.get("text") or "撤回没有成功。")
    st.rerun()


# ---------------------------------------------------------------- 页面

st.markdown("## 🗳 人气投票")
# 这里原来还有一行状态说明：
#     st.caption(C.window_text(state) + " · 每人 3 票 · 只展示已入围作品")
# 用户 2026-10-06 要求删掉。它本来也是**重复信息**：
#   * "投票尚未开始 / 已结束" 下面那个 st.info 会单独提示（还更详细，带日期）；
#   * "每人 3 票、必须投给不同作品" 写在身份区标题里；
#   * 完整规则在页面底部的「📖 投票规则」里。
# `state` 变量本身还要用（下面 not_open 的分支），所以只删这行、不删上面那句。
state = C.window_state()

show_flash()

already_closed = C.now_cn() > C.VOTE_CLOSE_AT

items = wall_items()

if state == "not_open":
    st.info(f"投票还没开始，{C.VOTE_OPEN_AT.strftime('%Y年%m月%d日')} 开放。"
            "可以先看看下面的作品。")

if not items:
    # ⚠ 这句话是**给同学看的**。不要在这里写"请到管理后台标记入围" ——
    # 同学端没有管理后台，看到只会一头雾水（组织者那边由后台自己提示）。
    st.warning(
        "投票作品正在整理中 —— 只有通过初筛的入围作品才会出现在这里，"
        "请过一会儿再来看看。",
        icon="🗂",
    )
    st.stop()

# 投票后端预检：跑不通就只给一句人话，并且**不显示身份表单与投票按钮**
# （让同学填完一整套信息再撞一个 404 是最糟的体验）。作品照样能浏览。
backend_ok, backend_why = vote_backend_ok()
if not backend_ok:
    st.warning("投票功能暂时不可用，作品仍可正常浏览。请稍后再来，或联系主办方。",
               icon="🛠")
    identified, uid, mine = False, "", set()
else:
    identified, uid, mine = identity_bar()

# ---- 截止后公示结果（用真实时间，不看手工开关）----
if already_closed:
    try:
        tally = V.get_vote_store().tally()
    except Exception:
        tally = {}
    if tally:
        with st.container(border=True):
            st.markdown("### 🏆 投票结果（已截止，票数公示）")
            ranked = sorted(items, key=lambda w: (-tally.get(w.sid, 0), w.sid))
            top = [w for w in ranked if tally.get(w.sid, 0) > 0][:10]
            rows = [{"名次": i, "作品": work_title(w), "编号": w.sid,
                     "票数": tally.get(w.sid, 0)} for i, w in enumerate(top, 1)]
            if rows:
                st.dataframe(rows, use_container_width=True, hide_index=True)
            st.caption(f"共 {sum(tally.values())} 票。")

# ---- ② 作品详情（选中后才出现，放在网格上方：点卡片后它就在眼前，不用往回翻）----
opened = st.session_state.get("v_open") or ""
current = next((w for w in items if w.sid == opened), None)
if current is not None:
    with st.container(border=True):
        st.markdown(f"### ② {work_title(current)}")
        c1, c2 = st.columns([1.4, 1])
        with c1:
            img = detail_image(current)
            if img is not None:
                st.image(img, use_container_width=True)
            else:
                st.warning("这幅作品的照片没能读出来。")
        with c2:
            meta = {
                "编号": current.sid,
                "校区": campus_name(current.campus),
                "拍摄机位": work_place(current),
                "拍摄时间": current.shot_time or "—",
                "天象": current.weather or "—",
            }
            for k, v in meta.items():
                st.markdown(f"**{k}**：{v}")
            if current.note:
                st.caption(f"作者自述：{current.note}")
            st.divider()

            vouched = current.sid in mine
            if already_closed:
                st.info("投票已截止。")
            elif not backend_ok:
                st.info("投票暂时不可用，请稍后再试。")
            elif not identified:
                st.info("请先在上方**确认身份**，然后就能投票了。")
            elif vouched:
                st.success("你已经投过这幅作品。")
                if st.button("↩️ 撤回这一票", use_container_width=True,
                             key=f"ret_{current.sid}"):
                    do_retract(current)
            elif len(mine) >= C.VOTES_PER_PERSON:
                st.warning(f"你的 {C.VOTES_PER_PERSON} 票已经用完了。"
                           "想改投，先撤回一票（在上面或已投的作品里）。")
            else:
                left = C.VOTES_PER_PERSON - len(mine)
                if st.button(f"👍 投这一票（还剩 {left} 票）", type="primary",
                             use_container_width=True, key=f"cast_{current.sid}"):
                    do_cast(current)
        if st.button("收起", key="v_close"):
            st.session_state.pop("v_open", None)
            st.rerun()

# ---- ③ 筛选 + 作品网格 ----
st.markdown("### ③ 作品陈列墙")

f1, f2 = st.columns(2)
with f1:
    # ⚠ 这一行原来有两个坑（2026-10-06 用户报"只显示 suzhou"）：
    #   1. **没转中文**：选项用的是内部英文 key，下拉里就直愣愣显示 "suzhou"。
    #      必须过 `campus_name()`（读 CAMPUSES 的 short = 鼓楼/仙林/苏州）。
    #   2. **选项是现从作品里取的**（`{w.campus for w in items}`）⇒ 当作品全都
    #      标在苏州校区时，下拉里就只剩一个 "suzhou" —— 看着像功能坏了，
    #      其实只是"别的校区还没有入围作品"。
    #   改成：**固定列出三个校区**（顺序跟 CAMPUSES 一致：鼓楼/仙林/苏州），
    #   并在括号里带上作品数 —— 这样"哪个校区还没作品"一眼可见，
    #   不会再有"怎么只有一个选项"的困惑。
    _camp_count = {k: sum(1 for w in items if w.campus == k) for k in CAMPUSES}
    camp_opts = ["全部"] + list(CAMPUSES)

    def _camp_label(k: str) -> str:
        if k == "全部":
            return f"全部（{len(items)}）"
        return f"{campus_name(k)}（{_camp_count.get(k, 0)}）"

    camp = st.selectbox("校区", camp_opts, format_func=_camp_label, key="v_camp")
with f2:
    order = st.selectbox("排序", ["最新投稿", "编号"], key="v_order")
# 「天象」筛选已按用户要求去掉（2026-10-06）：作品墙按天象筛没什么人用，
# 反而占掉筛选行的位置。**天象本身没删** —— 作品详情里照旧显示
# 「天象：晚霞」，只是不再拿它当筛选条件。
# 顺带把上一版留在会话里的 v_wx 清掉，免得它一直挂在 session_state 里。
st.session_state.pop("v_wx", None)

view = [w for w in items if (camp == "全部" or w.campus == camp)]
if order == "编号":
    view.sort(key=lambda w: w.sid)
else:
    view.sort(key=lambda w: (w.submitted_at or "", w.sid), reverse=True)

if not view:
    st.info("当前筛选条件下没有作品。"
            + ("该校区还没有入围作品 —— 换个校区看看。"
               if camp != "全部" else ""))
    st.stop()

pages = max(1, (len(view) + PAGE_SIZE - 1) // PAGE_SIZE)
page = int(st.session_state.get("v_page", 1) or 1)
page = max(1, min(pages, page))
st.session_state["v_page"] = page

chunk = view[(page - 1) * PAGE_SIZE: page * PAGE_SIZE]
cols = st.columns(NCOL)
_votes_left = max(0, C.VOTES_PER_PERSON - len(mine))
for i, w in enumerate(chunk):
    with cols[i % NCOL]:
        img = work_thumb(w)
        if img is not None:
            st.image(img, use_container_width=True)
        else:
            st.caption("（照片读取失败）")
        voted = w.sid in mine
        st.markdown(f"**{work_title(w)}**" + ("　✅已投" if voted else ""))
        st.caption(f"{campus_name(w.campus)} · {work_place(w)}")

        # ---- 直接在卡片上投票（用户 2026-10-06 要求：不用先点进「查看」）----
        # ⚠ key 必须与详情面板里的区分开（那边用 `cast_`/`ret_`）：同一个作品
        #   同时在网格和详情面板里出现时，key 撞了会直接抛
        #   StreamlitDuplicateElementKey 把整页搞崩。
        # 两个按钮**竖着排**而不是并排：手机上一行 3 张卡片，卡片里再分两列
        # 每个按钮只剩 ~55px，字会挤成一条缝。
        if voted:
            if st.button("↩️ 撤回这一票", key=f"card_ret_{w.sid}",
                         use_container_width=True, help="撤回你对这幅作品的投票"):
                do_retract(w)
        elif not backend_ok:
            st.button("👍 投票", key=f"card_cast_{w.sid}", disabled=True,
                      use_container_width=True, help="投票功能暂时不可用")
        elif not identified:
            st.button("👍 投票", key=f"card_cast_{w.sid}", disabled=True,
                      use_container_width=True, help="请先在上方确认身份，然后就能投票")
        elif _votes_left <= 0:
            st.button("👍 投票", key=f"card_cast_{w.sid}", disabled=True,
                      use_container_width=True, help=f"{C.VOTES_PER_PERSON} 票已用完，可先撤回一票")
        else:
            if st.button(f"👍 投票（还剩 {_votes_left} 票）", key=f"card_cast_{w.sid}",
                         use_container_width=True):
                do_cast(w)

        if st.button("查看", key=f"open_{w.sid}", use_container_width=True):
            st.session_state["v_open"] = w.sid
            st.rerun()

# ---- 翻页与刷新：统一放在**作品列表底部**（用户 2026-10-06 要求）
# 以前放在列表上方 —— 但翻页这个动作发生在"看完这一屏之后"，
# 放在下面才是顺着人的动作顺序；顺带把「🔄 刷新」从筛选行里挪出来
# （它本来挤在三个下拉框右边，既不像筛选、手机上又挤）。
# 布局用两行而不是一行四列：手机上 390px 塞四个控件会挤成一条缝。
st.divider()
_b1, _b2 = st.columns(2)
with _b1:
    if st.button("← 上一页", disabled=(page <= 1), use_container_width=True, key="v_prev"):
        st.session_state["v_page"] = page - 1
        st.rerun()
with _b2:
    if st.button("下一页 →", disabled=(page >= pages), use_container_width=True, key="v_next"):
        st.session_state["v_page"] = page + 1
        st.rerun()

_b3, _b4 = st.columns([3, 1])
with _b3:
    st.caption(f"第 {page} / {pages} 页 · 共 {len(view)} 幅作品"
               + ("" if pages > 1 else "（已全部显示）"))
with _b4:
    if st.button("🔄 刷新", use_container_width=True, key="v_reload"):
        wall_items.clear()
        wall_thumb.clear()
        st.rerun()

with st.expander("📖 投票规则"):
    st.markdown(
        f"""
- 投票时间：**{C.VOTE_OPEN_AT.strftime('%Y-%m-%d %H:%M')} —
  {C.VOTE_CLOSE_AT.strftime('%Y-%m-%d %H:%M')}**（北京时间），过点自动关闭。
- 每人 **{C.VOTES_PER_PERSON} 票**，且 **{C.VOTES_PER_PERSON} 票必须投给不同作品**。
- 投票期间**不显示票数**，截止后在本页公示，避免"谁领先投谁"。
- 投票可以**撤回后改投**（截止前）。
- 身份登记需要**姓名 + 学号/工号 + 手机号**三项，且每次都必须填得完全一致；
  仅用于防止重复投票，**不在投票墙上公开**（只有主办方后台能看）。
- 库里对学号与手机号只存加盐哈希与掩码；姓名存明文但**只在后台可见**。
- 发现异常投票请联系主办方，后台可逐条核查并撤销。
"""
    )
