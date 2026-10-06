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

S.nav(S.VOTE_PAGE)

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
                st.markdown(f"**✅ 已确认身份：{st.session_state.get('v_name', '')}**"
                            f"（昵称 {st.session_state.get('v_nick', '')}）")
                st.caption(
                    f"学号 `{st.session_state.get('v_sid_mask', '')}` · "
                    f"手机 `{st.session_state.get('v_phone_mask', '')}` · "
                    f"已投 **{used} / {C.VOTES_PER_PERSON}** 票"
                    + ("　🎉 票已用完，可撤回后改投" if used >= C.VOTES_PER_PERSON else "")
                )
            with c2:
                if st.button("切换身份", use_container_width=True, key="v_logout"):
                    for k in ("v_uid", "v_nick", "v_name", "v_sid", "v_phone",
                              "v_sid_mask", "v_phone_mask"):
                        st.session_state.pop(k, None)
                    st.rerun()
        return True, uid, mine

    with st.container(border=True):
        st.markdown(f"**① 先确认身份，再投票**（每人最多 {C.VOTES_PER_PERSON} 票，"
                    f"{C.VOTES_PER_PERSON} 票需投给**不同**作品）")
        st.caption(
            "需要**姓名 + 学号/工号 + 手机号**三项一起登记，缺一不可。"
            "三项都必填是故意的：**学号就是你的账号**，姓名和手机号是每次投票都要"
            "对上的凭证 —— 换个手机号、换个名字都投不出第二份票，"
            "同一个手机号也只能登记一个学号。"
            "信息仅用于防止重复投票，**不在投票墙上公开**（只有主办方后台能看）。"
        )
        st.caption("⚠️ 之后再投票时，请**三项都填得和第一次一模一样**，"
                   "否则会被判定为身份不符。")
        with st.form("vote_identity"):
            f1, f2, f3, f4 = st.columns(4)
            name = f1.text_input("姓名", max_chars=20, placeholder="真实姓名")
            sid = f2.text_input("学号 / 工号", placeholder="如 20220001")
            phone = f3.text_input("手机号", max_chars=20, placeholder="11 位")
            nick = f4.text_input("昵称", max_chars=20, placeholder="投票记录里显示")
            ok = st.form_submit_button("确认身份", type="primary",
                                       use_container_width=True)
        if ok:
            errs = []
            for fn, val in ((V.valid_name, name), (V.valid_sid, sid),
                            (V.valid_phone, phone), (V.valid_nick, nick)):
                good, msg = fn(val)
                if not good:
                    errs.append(msg)
            if errs:
                st.error(" ".join(errs))
            else:
                new_uid, _, _ = V.identity(sid, phone, name)
                st.session_state["v_uid"] = new_uid
                st.session_state["v_nick"] = nick.strip()
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


def do_cast(work) -> None:
    store = V.get_vote_store()
    try:
        res = store.cast(
            sid=st.session_state.get("v_sid", ""),
            phone=st.session_state.get("v_phone", ""),
            name=st.session_state.get("v_name", ""),
            nick=st.session_state.get("v_nick", ""),
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
state = C.window_state()
st.caption(C.window_text(state) + f" · 每人 {C.VOTES_PER_PERSON} 票 · 只展示已入围作品")

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

f1, f2, f3, f4 = st.columns([1.1, 1.1, 1.1, 0.9])
with f1:
    camp_opts = ["全部"] + sorted({w.campus for w in items if w.campus})
    camp = st.selectbox("校区", camp_opts, key="v_camp")
with f2:
    wx_opts = ["全部"] + sorted({(w.weather or "").strip() for w in items if (w.weather or "").strip()})
    wx = st.selectbox("天象", wx_opts, key="v_wx")
with f3:
    order = st.selectbox("排序", ["最新投稿", "编号"], key="v_order")
with f4:
    if st.button("🔄 刷新", use_container_width=True, key="v_reload"):
        wall_items.clear()
        wall_thumb.clear()
        st.rerun()

view = [w for w in items
        if (camp == "全部" or w.campus == camp)
        and (wx == "全部" or (w.weather or "").strip() == wx)]
if order == "编号":
    view.sort(key=lambda w: w.sid)
else:
    view.sort(key=lambda w: (w.submitted_at or "", w.sid), reverse=True)

if not view:
    st.info("当前筛选条件下没有作品。")
    st.stop()

pages = max(1, (len(view) + PAGE_SIZE - 1) // PAGE_SIZE)
page = int(st.session_state.get("v_page", 1) or 1)
page = max(1, min(pages, page))
st.session_state["v_page"] = page

p1, p2, p3 = st.columns([1, 3, 1])
with p1:
    if st.button("← 上一页", disabled=(page <= 1), use_container_width=True, key="v_prev"):
        st.session_state["v_page"] = page - 1
        st.rerun()
with p2:
    st.caption(f"第 {page} / {pages} 页 · 共 {len(view)} 幅作品")
with p3:
    if st.button("下一页 →", disabled=(page >= pages), use_container_width=True, key="v_next"):
        st.session_state["v_page"] = page + 1
        st.rerun()

chunk = view[(page - 1) * PAGE_SIZE: page * PAGE_SIZE]
cols = st.columns(NCOL)
for i, w in enumerate(chunk):
    with cols[i % NCOL]:
        img = work_thumb(w)
        if img is not None:
            st.image(img, use_container_width=True)
        else:
            st.caption("（照片读取失败）")
        voted = "　✅已投" if w.sid in mine else ""
        st.markdown(f"**{work_title(w)}**{voted}")
        st.caption(f"{campus_name(w.campus)} · {work_place(w)}")
        label = "查看" + ("（已投）" if w.sid in mine else "")
        if st.button(label, key=f"open_{w.sid}", use_container_width=True):
            st.session_state["v_open"] = w.sid
            st.rerun()

st.divider()
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
