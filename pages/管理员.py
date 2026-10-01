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

try:
    from campus_config import CAMPUSES, frame_of  # noqa: E402
except Exception:  # pragma: no cover - 仅云端模块状态异常时走到
    import campus_config as _cfg

    CAMPUSES = _cfg.CAMPUSES

    def frame_of(key):  # type: ignore[misc]
        c = _cfg.campus(key)
        return c.frame if c.frame and tuple(c.frame) != (0.0, 0.0, 0.0, 0.0) else c.bbox
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

subs = load_submissions()
pending = [s for s in subs if s.loc_verify or not s.has_point]

with st.sidebar:
    st.metric("总投稿", f"{len(subs)} 幅")
    st.metric("待复核", f"{len(pending)} 幅")
    st.divider()
    if st.button("退出登录", use_container_width=True):
        S.logout()
        st.rerun()
    st.caption("口令存放在 `data/admin.json`；删掉即可重设。")

st.markdown("## 🔧 投稿管理")
st.caption("可以修改位置与文字、删除稿件、校正待复核的点位。所有改动即时写入 "
           "`data/submissions.jsonl`，之后到「🗺 打卡点地图」页刷新即可看到效果。")

tab_review, tab_edit, tab_table = st.tabs(
    [f"⚠️ 待复核（{len(pending)}）", "✏️ 逐条修改", "📋 总表 / 批量删除"]
)


# ---------------------------------------------------------------- 待复核队列
def spot_picker(key_prefix: str, campus_key: str, init: tuple[float, float]):
    """一个小地图，点一下取坐标，用于人工校正。"""
    cfg = CAMPUSES[campus_key]
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
                f"**{s.sid}** · {s.title or '未命名'} · {CAMPUSES[s.campus].short} · "
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
                        "校区", list(CAMPUSES), index=list(CAMPUSES).index(s.campus),
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
                "校区": CAMPUSES[s.campus].short if s.campus in CAMPUSES else s.campus,
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
