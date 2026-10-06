"""管理后台：审核、修改、删除投稿（仅管理员可见）。

入口：侧边栏「🔧 管理后台」（登录后才出现），或直接访问 /管理员
首次使用会让你设置口令，之后用它登录。

和同学端的区别：
  * 同学端（投稿页）只有提交，不能改也不能删别人的稿子；
  * 本页可以改位置/文字/奖项，可以删稿，可以看「待复核」队列并逐个校正。
"""
from __future__ import annotations

import sys
from pathlib import Path

import folium
import streamlit as st
from streamlit_folium import st_folium

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import site_common as S  # noqa: E402

S.setup("管理后台", "🔧")

from campus_config import CAMPUSES  # noqa: E402
from frame_util import frame_of  # noqa: E402  # 统一走带三级降级的兜底，不再自己写 c.frame

from submission_data import (  # noqa: E402
    delete_submission,
    load_submissions,
    regenerate_thumb,
    thumb_display_src,
    update_submission,
)

STATUS_OPTIONS = ["已收稿", "已入围", "已获奖", "已退稿"]
AWARD_OPTIONS = ["", "一等奖", "二等奖", "三等奖", "人气奖", "优秀奖"]
PAGE_SIZE = 12


def _campus_or_first(key: str):
    """按 key 取校区配置；key 异常（空值/脏数据/历史遗留）时退回第一个。

    为什么需要：后台是对着**已存下来的投稿数据**渲染的，而 `CAMPUSES[s.campus]`
    在 s.campus 不是合法 key（手工改过 jsonl、旧版本写入、字段为空）时会 KeyError，
    整个后台直接崩。这里是兜底查询，不是静默吞错 —— 调用处仍会显示原始 key。
    """
    if key in CAMPUSES:
        return CAMPUSES[key]
    return next(iter(CAMPUSES.values()))


def _campus_short(key: str) -> str:
    return _campus_or_first(key).short if key else "未知"


# ---------------------------------------------------------------- 登录 / 初始化


def login_gate() -> bool:
    """未登录则显示登录/初始化界面，返回是否已登录。"""
    S.ensure_admin_from_secrets()   # 部署时 secrets 里配了口令就自动就位
    if S.is_admin():
        return True

    st.markdown("## 🔧 管理后台")
    st.caption("同学端只能投稿，看不到这个页面。")

    if not S.admin_configured():
        st.info(
            "**首次使用**：请设置一个管理员口令。\n\n"
            "设置后写入 `data/admin.json`（只存加盐哈希，不存明文）。"
            "忘记口令就删掉这个文件重设。\n\n"
            "部署到云端时，建议在 Secrets 里配 `ADMIN_PASSWORD`，"
            "否则每次重启都会被当成首次使用。"
        )
        with st.form("setup"):
            pw1 = st.text_input("设置口令", type="password")
            pw2 = st.text_input("再输一次", type="password")
            ok = st.form_submit_button("创建管理员口令", type="primary")
        if ok:
            if len(pw1) < 4:
                st.error("口令至少 4 位。")
            elif pw1 != pw2:
                st.error("两次输入不一致。")
            else:
                S.set_admin_password(pw1)
                st.success("已创建，正在进入后台…")
                st.rerun()
        st.caption(f"（临时口令也可以先用 `{S.DEFAULT_PASSWORD}`，但建议自己设一个）")
        return False

    with st.form("login"):
        pw = st.text_input("管理员口令", type="password")
        ok = st.form_submit_button("登录", type="primary")
    if ok:
        if S.check_admin_password(pw):
            st.rerun()
        else:
            st.error("口令不对。")
    return False


if not login_gate():
    st.stop()


# ---------------------------------------------------------------- 侧边栏


S.nav(S.ADMIN_PAGE)

# 站点状态：构建版本 + 存储后端。
# 原来挂在**同学端侧边栏**上，用户 2026-10-04 要求移到后台 ——
# 这两项是给组织者看的（确认线上跑的是哪一版 / 云端数据库有没有接上），
# 同学不需要看。
st.caption(f"🛠 构建版本 `{S.BUILD}`")
S.storage_status()

# 存储自检：**读得通不代表写得进**（读走 submissions、写照片走 photos，
# 两边的权限/体积/表结构都可能不同）。2026-10-05 线上就是"能读、能写投稿、
# 写照片报 HTTPError"，光看 ping 完全看不出来 —— 所以这里真写一小行再删掉，
# 失败时把**状态码与云端返回**摆出来（平台上原始报错会被涂掉）。
with st.expander("🔌 存储自检（读 + 写探针）"):
    st.caption("排查'能看不能投'这类问题时先点这个。写入探针用的是固定安全 id，跑完会自动删掉。")
    if st.button("运行自检", key="store_probe"):
        try:
            from store import get_store

            obj = get_store()
            ok_r, msg_r = obj.ping() if hasattr(obj, "ping") else (True, "本地存储，跳过")
            st.write(("✅ " if ok_r else "❌ ") + f"读取：{msg_r}")
            probe = getattr(obj, "write_probe", None)
            if probe is None:
                st.write("✅ 写入：本地文件存储，无需探针")
            else:
                ok_w, msg_w = probe()
                st.write(("✅ " if ok_w else "❌ ") + f"写入：{msg_w}")
                if not ok_w:
                    st.warning(
                        "写入失败时看上面的状态码：**401/403** = 密钥不是 service_role 或 RLS "
                        "没放开；**404** = `photos` 表没建（执行 `deploy/supabase_schema.sql`）；"
                        "**413/500 且照片很大** = 单张原图太大；**429/503** = 配额用完或项目被暂停。"
                    )
        except Exception as exc:  # 自检本身绝不能再把页面搞崩
            st.error(f"自检自身出错：{type(exc).__name__}: {exc}")

subs = load_submissions()
pending = [s for s in subs if s.loc_verify or not s.has_point]

# ---- 投票模块（2026-10-06 新增）----
# 单独兜一层错：投票表还没建（没执行最新的 supabase_schema.sql）时会报 404，
# 而**后台其余功能不能因此整个打不开** —— 审核投稿是主功能，投票是附加的。
_vote_err = ""
vote_cfg = None
vote_store = None
_vote_stats = {"voters": 0, "votes": 0}
# 一次取齐，渲染期间不再重取：每多调一次就是一次到 Supabase 的往返，
# 而且分两次取会让侧边栏的数字与页签里的表格来自**不同快照**、看起来自相矛盾。
# 撤销 / 清空之后都会 st.rerun()，那时自然重新取。
_vote_voters: list = []
_vote_tally: dict = {}
_vote_flow: list = []
try:
    import vote_config as vote_cfg          # noqa: F401
    import vote_store as _vs

    vote_store = _vs.get_vote_store()
    _vote_voters = vote_store.voter_rows()
    _vote_tally = vote_store.tally()
    _vote_flow = vote_store.vote_rows()
    _vote_stats["voters"] = len(_vote_voters)
    _vote_stats["votes"] = sum(_vote_tally.values())
except Exception as exc:
    _vote_err = f"{type(exc).__name__}: {exc}"

with st.sidebar:
    st.metric("总投稿", f"{len(subs)} 幅")
    st.metric("待复核", f"{len(pending)} 幅")
    if not _vote_err:
        st.metric("投票人数", f"{_vote_stats['voters']} 人")
        st.metric("总票数", f"{_vote_stats['votes']} 票")
    st.divider()
    if st.button("退出登录", use_container_width=True):
        S.logout()
        st.rerun()
    st.caption("口令存放在 `data/admin.json`；删掉即可重设。")

st.markdown("## 🔧 投稿管理")
st.caption("可以修改位置与文字、删除稿件、校正待复核的点位。所有改动即时写入 "
           "`data/submissions.jsonl`，之后到「🗺 打卡点地图」页刷新即可看到效果。")

tab_review, tab_edit, tab_table, tab_vote = st.tabs(
    [f"⚠️ 待复核（{len(pending)}）", "✏️ 逐条修改", "📋 总表 / 批量删除",
     f"🗳 人气投票（{_vote_stats['votes']}）"]
)


# ---------------------------------------------------------------- 待复核队列
def spot_picker(key_prefix: str, campus_key: str, init: tuple[float, float]):
    """一个小地图，点一下取坐标，用于人工校正。"""
    cfg = _campus_or_first(campus_key)
    center = list(init) if init and init[0] is not None else list(cfg.center)
    m = folium.Map(location=center, zoom_start=cfg.zoom + 1, tiles=None, control_scale=True)
    folium.TileLayer(tiles=cfg.streets.url, attr=cfg.streets.attr,
                     name=cfg.streets.name, max_zoom=cfg.streets.max_zoom).add_to(m)
    # 外框（校园 + 周边）画出来供参考，但不锁视野 ——
    # 管理员校正落点时可能要拖到框外，别把自由度锁死。
    s_, w_, n_, e_ = frame_of(campus_key)
    folium.Rectangle(bounds=[[s_, w_], [n_, e_]], color="#2f6fb5", weight=1,
                     fill=False, dash_array="4 4",
                     tooltip="校园及周边范围（参考）").add_to(m)
    for nm, (la, lo) in cfg.landmarks.items():
        folium.CircleMarker([la, lo], radius=3, color="#e0713c", weight=2,
                            fill=True, fill_color="#fff", fill_opacity=1, tooltip=nm).add_to(m)
    if init and init[0] is not None:
        folium.Marker(list(init), tooltip="当前落点",
                      icon=folium.Icon(color="red", icon="camera", prefix="fa")).add_to(m)
    out = st_folium(m, height=380, use_container_width=True,
                    returned_objects=["last_clicked"], key=key_prefix)
    if out and out.get("last_clicked"):
        lc = out["last_clicked"]
        return round(lc["lat"], 6), round(lc["lng"], 6)
    return None


with tab_review:
    if not pending:
        st.success("没有待复核的投稿，全部点位都可信。")
    else:
        st.caption("这些投稿的落点可能不准（落在校区外或文字匹配置信度低）。"
                   "在地图上点一下正确位置，保存即可。")
        for s in pending[:30]:
            with st.expander(
                f"**{s.sid}** · {s.title or '未命名'} · {CAMPUSES[s.campus].short if s.campus in CAMPUSES else s.campus} · "
                f"{'无坐标' if not s.has_point else f'{s.lat:.5f}, {s.lon:.5f}'}"
                f"{'　(落点在校区外)' if s.has_point and s.loc_verify else ''}",
                expanded=False,
            ):
                c1, c2 = st.columns([1, 1.1])
                with c1:
                    src = thumb_display_src(s)
                    if src:
                        st.image(src, use_container_width=True)
                    else:
                        st.caption("（无缩略图）")
                    st.write(f"**作者**：{s.author or '匿名'}")
                    st.write(f"**拍摄时间**：{s.shot_time or '未填'}")
                    st.write(f"**天象**：{s.weather or '未填'}")
                    st.write(f"**位置描述**：{s.loc_text or '（空）'}")
                    st.write(f"**自动匹配**：{s.loc_matched or '（无）'} "
                             f"（置信度 {s.loc_score:.2f}，来源 {s.loc_source}）")
                    if s.note:
                        st.write(f"**手记**：{s.note}")
                with c2:
                    st.write("**点一下正确位置**（在图上点选会自动填入下面的经纬度）")
                    got = spot_picker(f"pk_{s.sid}", s.campus,
                                      (s.lat, s.lon) if s.has_point else None)
                    kla, klo = f"la_{s.sid}", f"lo_{s.sid}"
                    if got:
                        st.session_state[kla] = float(got[0])
                        st.session_state[klo] = float(got[1])
                    la = st.number_input("纬度", format="%.6f", key=kla,
                                         value=float(s.lat) if s.has_point else 0.0)
                    lo = st.number_input("经度", format="%.6f", key=klo,
                                         value=float(s.lon) if s.has_point else 0.0)
                    if got:
                        st.info(f"已选：{got[0]:.6f}, {got[1]:.6f} —— 点下面保存")
                    b1, b2 = st.columns([1, 1])
                    if b1.button("✅ 保存并解除待复核", key=f"ok_{s.sid}",
                                 type="primary", use_container_width=True):
                        update_submission(s.sid, lat=la, lon=lo, loc_verify=False)
                        st.success(f"{s.sid} 已更新")
                        st.rerun()
                    if b2.button("🗑 删掉这幅", key=f"del_{s.sid}", use_container_width=True):
                        delete_submission(s.sid, purge_files=True)
                        st.warning(f"{s.sid} 已删除")
                        st.rerun()


# ---------------------------------------------------------------- 逐条修改
with tab_edit:
    if not subs:
        st.info("还没有投稿。")
    else:
        # 支持直接输入编号，也支持在总表里选中后跳过来
        default_sid = st.session_state.get("edit_sid") or subs[-1].sid
        ids = [s.sid for s in subs]
        idx = ids.index(default_sid) if default_sid in ids else len(ids) - 1
        sid = st.selectbox("选择投稿编号", ids, index=idx, key="edit_pick")
        st.session_state["edit_sid"] = sid
        s = next(x for x in subs if x.sid == sid)

        c1, c2 = st.columns([1, 1.4])
        with c1:
            src = thumb_display_src(s)
            if src:
                st.image(src, caption=f"{s.sid} 缩略图", use_container_width=True)
            else:
                st.warning("没有缩略图")
            st.caption(f"原图：`{s.photo}`　投稿时间：{s.submitted_at}")
            if st.button("🔁 用原图重新生成缩略图", use_container_width=True):
                ok, msg = regenerate_thumb(sid)
                (st.success if ok else st.error)(f"{'已重新生成：' if ok else ''}{msg}")
                if ok:
                    st.rerun()

        with c2:
            with st.form(f"edit_{sid}"):
                f1, f2 = st.columns(2)
                with f1:
                    title = st.text_input("作品名", value=s.title)
                    author = st.text_input("作者", value=s.author)
                    shot_time = st.text_input("拍摄时间", value=s.shot_time)
                    campus_key = st.selectbox(
                        "校区", list(CAMPUSES),
                        index=list(CAMPUSES).index(s.campus) if s.campus in CAMPUSES else 0,
                        format_func=lambda k: CAMPUSES[k].name,
                    )
                with f2:
                    weather = st.text_input("天象", value=s.weather or "")
                    status = st.selectbox("状态", STATUS_OPTIONS,
                                          index=STATUS_OPTIONS.index(s.status)
                                          if s.status in STATUS_OPTIONS else 0)
                    award = st.selectbox("奖项", AWARD_OPTIONS,
                                         index=AWARD_OPTIONS.index(s.award)
                                         if s.award in AWARD_OPTIONS else 0)
                    contact = st.text_input("联系方式（不公开）", value=s.contact)
                note = st.text_area("拍摄手记", value=s.note, height=70)
                loc_text = st.text_input("位置描述", value=s.loc_text)

                g1, g2, g3 = st.columns([1, 1, 1])
                with g1:
                    lat = st.number_input("纬度", value=float(s.lat) if s.has_point else 0.0,
                                          format="%.6f")
                with g2:
                    lon = st.number_input("经度", value=float(s.lon) if s.has_point else 0.0,
                                          format="%.6f")
                with g3:
                    verify = st.checkbox("标记为待复核", value=bool(s.loc_verify))

                if st.form_submit_button("💾 保存修改", type="primary", use_container_width=True):
                    update_submission(
                        sid, title=title, author=author, shot_time=shot_time, campus=campus_key,
                        weather=weather, status=status, award=award, contact=contact,
                        note=note, loc_text=loc_text,
                        lat=lat or None, lon=lon or None, loc_verify=verify,
                    )
                    st.success(f"{sid} 已保存")
                    st.rerun()

        st.divider()
        st.write("**地图上校正位置**（点一下取坐标，再填到上面的经纬度框里保存）")
        got = spot_picker(f"editmap_{sid}", s.campus, (s.lat, s.lon) if s.has_point else None)
        if got:
            st.info(f"已选：{got[0]:.6f}, {got[1]:.6f} —— 填到上方经纬度框后点保存")


# ---------------------------------------------------------------- 总表 / 批量
with tab_table:
    if not subs:
        st.info("还没有投稿。")
    else:
        rows = []
        for s in subs:
            rows.append({
                "编号": s.sid,
                "校区": _campus_short(s.campus),
                "作品名": s.title,
                "作者": s.author,
                "天象": s.weather,
                "拍摄时间": s.shot_time,
                "纬度": s.lat,
                "经度": s.lon,
                "待复核": "⚠" if s.loc_verify else "",
                "状态": s.status,
                "奖项": s.award,
                "示例": "示例" if s.is_demo else "",
            })
        # 按需导入 pandas：它只在「总表」这一个页签用得上，而云端
        # requirements.txt 刻意不含 pandas（部署更快）。顶层 import 会让
        # 整个管理后台在云端直接 ImportError，所以挪到这里。
        import pandas as pd

        df = pd.DataFrame(rows)
        st.dataframe(df, use_container_width=True, hide_index=True, height=420)

        st.download_button("⬇️ 导出总表 CSV", df.to_csv(index=False).encode("utf-8-sig"),
                           file_name="投稿总表.csv", mime="text/csv")

        st.divider()
        st.subheader("批量删除")
        st.caption("删除不可撤销。勾选后确认即可。")
        options = st.multiselect("选择要删除的投稿", [s.sid for s in subs],
                                 format_func=lambda x: next(
                                     f"{x} · {y.title or '未命名'} · {y.author or '匿名'}"
                                     for y in subs if y.sid == x))
        c1, c2 = st.columns([1, 3])
        with c1:
            purge = st.checkbox("同时删除照片文件", value=False)
        with c2:
            st.write("")
            if st.button(f"🗑 删除选中的 {len(options)} 条", disabled=not options,
                         type="primary"):
                n = sum(1 for sid in options if delete_submission(sid, purge_files=purge))
                st.warning(f"已删除 {n} 条")
                st.rerun()

        st.divider()
        st.subheader("示例数据")
        n_demo = sum(1 for s in subs if s.is_demo)
        st.caption(f"当前有 {n_demo} 条示例数据。正式征稿前清掉它们。")
        if st.button("清空全部示例数据", disabled=n_demo == 0):
            for s in [x for x in subs if x.is_demo]:
                delete_submission(s.sid, purge_files=True)
            st.success("示例数据已清空")
            st.rerun()

        st.divider()
        st.subheader("刷新地图")
        if st.button("🔄 重新生成地图（在线版 + 离线版）", type="primary"):
            import contextlib
            import io

            import map_build
            from submission_data import export_csv

            buf = io.StringIO()
            with st.spinner("生成中…"):
                with contextlib.redirect_stdout(buf):
                    p1 = map_build.build_payload(load_submissions(), embed_photos=False)
                    map_build.write_map(p1, offline=False)
                    p2 = map_build.build_payload(load_submissions(), embed_photos=True)
                    map_build.write_map(p2, offline=True)
                    export_csv(load_submissions())
            st.success("地图已重新生成，去「🗺 打卡点地图」页看效果。")
            st.code(buf.getvalue() or "完成")


# ---------------------------------------------------------------- 人气投票
#
# 这一栏是**唯一能看到票数的地方** —— 同学端的投票墙在截止前只显示
# "已投/未投"，不显示任何票数（用户 2026-10-06 定的规则），
# 否则会出现"谁领先就投谁"的滚雪球。所以这里要做得能查、能导出、能纠错。
with tab_vote:
    st.subheader("🗳 人气投票")

    if _vote_err:
        st.error(
            f"投票数据读不出来：{_vote_err}\n\n"
            "**如果状态码是 404**：`voters` / `votes` 表还没建 —— 去 Supabase 的 "
            "SQL Editor 执行 `deploy/supabase_schema.sql`（文件末尾那段「人气投票」）。\n\n"
            "**如果是 401/403**：密钥不是 `service_role`。"
        )
        st.stop()

    # ---- 状态 ----
    wstate = vote_cfg.window_state()
    c1, c2, c3 = st.columns([2, 1, 1])
    with c1:
        st.markdown(f"**时间窗**：{vote_cfg.window_text(wstate)}")
        if vote_cfg.VOTE_MODE != "auto":
            st.warning(f"⚠️ 规则里的 `VOTE_MODE = \"{vote_cfg.VOTE_MODE}\"` —— "
                       "这是**手工开关**（调试用），记得改回 `\"auto\"`。")
    with c2:
        st.metric("投票人数", f"{_vote_stats['voters']} 人")
    with c3:
        st.metric("总票数", f"{_vote_stats['votes']} 票")
    st.caption(f"投票存储：{vote_store.describe()} · "
               f"每人上限 {vote_cfg.VOTES_PER_PERSON} 票 · "
               f"上墙条件：状态 = 「{vote_cfg.ELIGIBLE_STATUS}」且非示例数据")

    # ---- 上墙管理 ----
    st.divider()
    st.subheader("① 上墙管理")
    eligible = [s for s in subs if (s.status or "") == vote_cfg.ELIGIBLE_STATUS and not s.is_demo]
    st.caption(f"当前上墙 **{len(eligible)}** 幅。只有状态被标成"
               f"「{vote_cfg.ELIGIBLE_STATUS}」的投稿才会出现在投票墙上，"
               "示例数据永不进墙。")
    candidates = [s for s in subs if not s.is_demo and (s.status or "") != vote_cfg.ELIGIBLE_STATUS]
    picks = st.multiselect(
        "把初筛通过的作品标记为已入围（可多选）",
        [s.sid for s in candidates],
        format_func=lambda x: next(f"{x} · {y.title or '未命名'}" for y in candidates if y.sid == x),
        key="vote_eligible_pick",
    )
    b1, b2 = st.columns([1, 3])
    with b1:
        if st.button(f"✅ 标记入围（{len(picks)}）", disabled=not picks, type="primary"):
            from submission_data import save_submissions

            all_subs = load_submissions()
            want = set(picks)
            for s in all_subs:
                if s.sid in want:
                    s.status = vote_cfg.ELIGIBLE_STATUS
            save_submissions(all_subs)
            st.success(f"已把 {len(want)} 幅标记为「{vote_cfg.ELIGIBLE_STATUS}」")
            st.rerun()
    with b2:
        st.write("")
        st.caption("上墙/下架都改「状态」字段；下架把状态改成别的即可。")

    # ---- 票数排行 ----
    st.divider()
    st.subheader("② 票数排行")
    tally = _vote_tally
    by_sid = {s.sid: s for s in subs}
    rank_rows = []
    for i, (work, n) in enumerate(sorted(tally.items(), key=lambda kv: (-kv[1], kv[0])), 1):
        s = by_sid.get(work)
        rank_rows.append({
            "名次": i,
            "作品": (s.title if s else "") or "（作品已删除）",
            "编号": work,
            "票数": n,
            "校区": _campus_short(s.campus) if s else "",
            "状态": s.status if s else "",
        })
    if rank_rows:
        st.dataframe(rank_rows, use_container_width=True, hide_index=True, height=380)
    else:
        st.info("还没有任何投票。")

    # ---- 投票人明细 ----
    st.divider()
    st.subheader("③ 投票人明细")
    st.caption("学号与手机号在库里**只存加盐哈希**，这里显示掩码；"
               "**姓名存明文**（核对身份要用），但只在后台可见、投票墙上从不显示。")
    voters_rows = _vote_voters
    if voters_rows:
        st.dataframe([{k: v for k, v in r.items() if k != "uid"} for r in voters_rows],
                     use_container_width=True, hide_index=True, height=320)
    else:
        st.info("还没有人投票。")

    # ---- 流水 + 导出 ----
    st.divider()
    st.subheader("④ 投票流水 / 导出")
    flow = _vote_flow
    if flow:
        st.dataframe([{k: v for k, v in r.items() if k != "uid"} for r in flow],
                     use_container_width=True, hide_index=True, height=320)
        import csv as _csv
        import io as _io

        buf = _io.StringIO()
        cols = ["作品", "昵称", "姓名", "学号", "时间"]
        wr = _csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
        wr.writeheader()
        wr.writerows(flow)
        e1, e2 = st.columns(2)
        with e1:
            st.download_button("⬇️ 导出投票流水 CSV",
                               buf.getvalue().encode("utf-8-sig"),
                               file_name="人气投票_流水.csv", mime="text/csv")
        with e2:
            buf2 = _io.StringIO()
            cols2 = ["昵称", "姓名", "学号", "手机号", "票数", "投票作品", "首次时间"]
            wr2 = _csv.DictWriter(buf2, fieldnames=cols2, extrasaction="ignore")
            wr2.writeheader()
            wr2.writerows(voters_rows)
            st.download_button("⬇️ 导出投票人 CSV",
                               buf2.getvalue().encode("utf-8-sig"),
                               file_name="人气投票_投票人.csv", mime="text/csv")
    else:
        st.info("还没有投票流水。")

    # ---- 纠错 ----
    st.divider()
    st.subheader("⑤ 撤销异常投票")
    st.caption("投票截止后同学自己不能改票，但**管理员随时可以**"
               "（发现组织刷票、身份填错时用）。")
    if flow:
        labels = [f"{r['昵称'] or '（无昵称）'} · {r['学号']} → {r['作品']} · {r['时间']}"
                  for r in flow]
        idx = st.selectbox("选择要撤销的一条投票", range(len(labels)),
                           format_func=lambda i: labels[i], key="vote_revoke_pick")
        if st.button("🗑 撤销这一票", type="primary"):
            target = flow[idx]
            res = vote_store.admin_retract(uid=target["uid"], work=target["作品"])
            if res.get("ok"):
                st.success(f"已撤销：{target['昵称']} → {target['作品']}")
            else:
                st.error(res.get("text") or "撤销失败")
            st.rerun()

    with st.expander("☢️ 危险操作：清空全部投票"):
        st.caption("把所有投票与投票人记录一并删除，**不可撤销**。"
                   "正式征稿前的测试投票用这个清。")
        sure = st.checkbox("我确认要清空全部投票记录", key="vote_clear_confirm")
        if st.button("清空全部投票", disabled=not sure, type="primary"):
            n = vote_store.clear_all()
            st.warning(f"已清空（原有 {n} 条投票记录）")
            st.rerun()
