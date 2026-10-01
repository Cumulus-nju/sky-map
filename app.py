"""「天光云影」校园天空摄影大赛 —— 投稿页（网站首页）。

同学打开网站：
  1. 选校区 → 在地图上点一下机位（也可拖动更正）；
  2. 传原图 → 自动读 EXIF 的拍摄时间、器材，若能读到 GPS 也会提示；
  3. 填作品名 / 天象 / 手记 → 提交。
提交即刻落盘到 data/submissions.jsonl 并生成缩略图，右侧可跳到
「🗺 打卡点地图」页看实时汇总，或让组织方批量生成后分享。

启动：双击 打开网站.bat（或 启动投稿页.bat），浏览器访问 http://localhost:8501
"""
from __future__ import annotations

import hashlib
import os
from datetime import datetime
from pathlib import Path

import folium
import streamlit as st
from streamlit_folium import st_folium

import site_common as S

S.setup("投稿", "🌤")

# 构建版本标记：显示在侧边栏，用来确认线上到底跑的是哪一版。
# 教训（2026-10-01）：push 后线上报 ImportError，而本地用同一组文件跑完全正常，
# 且 Git 各引用 / 工作区 / GitHub raw 的哈希完全一致 —— 云端模块状态可能与磁盘不一致。
# 所以：① 下面 campus_config 的导入做兜底，不一致也不至于整站崩；
#       ② 版本号可见（见 site_common.BUILD），一眼看出线上有没有真的更新。
try:
    from campus_config import (  # noqa: E402
        CAMPUSES,
        campus as get_campus,
        frame_of,
        min_zoom_for_frame,
    )
except Exception as _cfg_exc:  # pragma: no cover - 仅云端异常时走到
    import campus_config as _cfg

    CAMPUSES = _cfg.CAMPUSES
    get_campus = _cfg.campus

    def frame_of(key):  # type: ignore[misc]
        c = _cfg.campus(key)
        return c.frame if c.frame and tuple(c.frame) != (0.0, 0.0, 0.0, 0.0) else c.bbox

    def min_zoom_for_frame(key, view_w=1000.0, view_h=460.0):  # type: ignore[misc]
        import math

        s, w, n, e = frame_of(key)
        w_m = (e - w) * 111320.0 * math.cos(math.radians((s + n) / 2)) * 1.15
        h_m = (n - s) * 111320.0 * 1.15
        px0 = 156543.03392 * math.cos(math.radians((s + n) / 2))
        return max(11, min(18, int(math.floor(math.log2(min(view_w / (w_m / px0), view_h / (h_m / px0)))))))

    import streamlit as _st

    _st.sidebar.warning(
        f"campus_config 导入异常，已启用兜底逻辑：{type(_cfg_exc).__name__}: {_cfg_exc}",
        icon="⚠️",
    )

from photos import make_thumb_bytes, parse_shot_time, read_exif  # noqa: E402
from submission_data import (  # noqa: E402
    DATA,
    Landmark,
    Submission,
    THUMBS,
    build_landmark_index,
    load_submissions,
    next_sid,
    resolve_location,
    save_submissions,
)

WEATHER_PRESETS = [
    "晚霞/火烧云", "朝霞/日出", "积云/蓝天白云", "层云/阴天",
    "雨/彩虹", "雷电/强对流", "月亮/星空", "雾/霾/平流雾", "其他",
]

LOCK = DATA / ".submit.lock"


@st.cache_data(show_spinner=False)
def landmark_index() -> dict[str, list[Landmark]]:
    return build_landmark_index()


def load_locked() -> list[Submission]:
    """极简文件锁：避免两位同学同时提交时互相覆盖（同一台机器 / 内网部署）。"""
    for _ in range(50):
        try:
            fd = os.open(LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
            return load_submissions()
        except FileExistsError:
            import time

            time.sleep(0.1)
    return load_submissions()


def release_lock() -> None:
    try:
        LOCK.unlink()
    except FileNotFoundError:
        pass


def save_photo(upload) -> tuple[str, str]:
    """存原图 + 缩略图，返回 (原图引用, 缩略图引用)。

    走存储层：本地模式落文件，云端模式进 Supabase。
    """
    import io

    from store import _safe_id, get_store

    raw = upload.getvalue()
    mime = (getattr(upload, "type", "") or "image/jpeg").split(";")[0].strip()
    pid = hashlib.sha1(raw).hexdigest()[:12]
    st_obj = get_store()

    origin_ref = st_obj.put_photo(pid, raw, mime or "image/jpeg")

    thumb_ref = ""
    try:
        thumb_ref = st_obj.put_thumb(pid, make_thumb_bytes(io.BytesIO(raw)))
    except Exception:
        thumb_ref = ""
    return origin_ref, thumb_ref


def render_map(campus_key: str, picked: tuple[float, float] | None):
    cfg = get_campus(campus_key)
    fs, fw, fn, fe = frame_of(campus_key)
    # 视口宽高参考值：用浏览器实测的 st_folium 嵌入尺寸（高 460 是下面传的）
    min_z = min_zoom_for_frame(campus_key, view_w=914, view_h=460)
    m = folium.Map(
        location=list(cfg.center),
        zoom_start=cfg.zoom,
        tiles=None,
        control_scale=True,
        # 把视野锁进外框：只能往里放大，缩不出去；maxBoundsViscosity=1 让边界变硬
        # （不加的话 Leaflet 在拖到边界时会弹性回弹，看着像"卡住"）
        min_lat=fs, max_lat=fn, min_lon=fw, max_lon=fe,
        max_bounds=True,
        maxBoundsViscosity=1.0,
        # ⚠ 必须用 minZoom 而不是 min_zoom：folium 的 min_zoom 只作用于瓦片图层，
        # 不会写进 L.map 的 options，地图照样能缩到 z=0 看到整个东亚。
        minZoom=min_z,
        zoomControl=True,
    )

    def add_tile(src, name, show):
        kw = dict(name=name, max_zoom=src.max_zoom, attr=src.attr, show=show, control=True)
        if src.subdomains:
            kw["subdomains"] = list(src.subdomains)
        folium.TileLayer(tiles=src.url, **kw).add_to(m)

    # 街道底图（WGS84）设为默认：和照片点位、OSM 建筑同一坐标系，点选最准
    add_tile(cfg.streets, cfg.streets.name, True)
    # 卫星影像仅作参考 —— 高德是 GCJ-02，未纠偏，别用它对准机位
    add_tile(cfg.imagery, cfg.imagery.name + "（仅参考，有坐标偏移）", False)

    # 外框：校园 + 周边缓冲。点击请落在框内，框外会被标为「待复核」
    folium.Rectangle(
        bounds=[[fs, fw], [fn, fe]], color="#2f6fb5", weight=1.6, fill=False, dash_array="6 4",
        tooltip="可拍摄范围：校园及周边一圈。点框外会被标为「待复核」",
    ).add_to(m)

    # 已知地标，方便同学对准
    for name, (la, lo) in cfg.landmarks.items():
        folium.CircleMarker(
            [la, lo], radius=4, color="#e0713c", weight=2, fill=True, fill_color="#fff",
            fill_opacity=1, tooltip=name,
        ).add_to(m)

    if picked and picked[0] is not None:
        folium.Marker(
            list(picked), tooltip="你选的机位", icon=folium.Icon(color="red", icon="camera", prefix="fa")
        ).add_to(m)

    folium.LayerControl(collapsed=True).add_to(m)
    return st_folium(m, height=460, use_container_width=True, returned_objects=["last_clicked"])


def main() -> None:
    S.nav(S.SUBMIT_PAGE)

    st.title("🌤 天光云影 · 校园天空摄影大赛投稿")
    st.caption("南京大学校园天空摄影大赛 · 南赫学院 × 南京大学摄影社 · 征稿 2026-10-08 ~ 2026-11-15")

    idx = landmark_index()
    existing = load_submissions()
    located = sum(1 for s in existing if s.has_point)
    with st.sidebar:
        st.metric("已收稿", f"{len(existing)} 幅")
        st.metric("已自动定点", f"{located} 幅")
        st.page_link(S.MAP_FILE, label="🗺 看打卡点地图", icon=None,
                     help="已收到的作品汇总成的交互地图")
        st.caption("所有投稿落盘在 `data/` 目录；组织方跑一次地图生成即可汇总。")

    # ---------------- 第一步：选校区与机位 ----------------
    st.subheader("① 选择校区并在图上点出机位")
    key_labels = {k: c.name for k, c in CAMPUSES.items()}
    campus_key = st.radio(
        "校区", list(CAMPUSES), format_func=lambda k: key_labels[k], horizontal=True, key="campus_key"
    )
    st.caption(
        "在地图上**点一下**你拍照站的位置（越准越好）；点错了再点一次即可覆盖。"
        "蓝框是**校园及周边**范围，点在里面就对了。"
        "请用默认的**街道底图**对准（卫星影像有坐标偏移，仅供看地形）。"
    )

    state_key = f"picked_{campus_key}"
    picked = st.session_state.get(state_key)
    clicked = render_map(campus_key, picked)
    if clicked and clicked.get("last_clicked"):
        lc = clicked["last_clicked"]
        newpt = (round(lc["lat"], 6), round(lc["lng"], 6))
        if newpt != picked:
            st.session_state[state_key] = newpt
            st.rerun()

    left, right = st.columns([1, 1])
    with left:
        picked = st.session_state.get(state_key)
        if picked:
            st.success(f"已选机位：{picked[0]:.6f}, {picked[1]:.6f}")
        else:
            st.warning("还没点选机位 —— 也可以只在下面手填位置描述，系统会尝试自动匹配")
    with right:
        loc_text = st.text_input(
            "位置描述（可选，但强烈建议填）",
            placeholder="例：北大楼前草坪、图书馆南侧台阶、操场看台…",
        )

    # ---------------- 第二步：上传照片 ----------------
    st.subheader("② 上传作品")
    st.caption("建议直接传**手机原图**：能自动读出拍摄时间与器材，也方便溯源；微信压缩版会丢失这些信息。")
    upload = st.file_uploader("选择照片", type=["jpg", "jpeg", "png", "heic", "webp", "tif", "tiff"])

    exif: dict = {}
    if upload is not None:
        upload.seek(0)
        exif = read_exif(upload)
        upload.seek(0)
        c1, c2 = st.columns([1, 1])
        with c1:
            st.image(upload, caption="投稿预览", use_container_width=True)
        with c2:
            if exif.get("has_gps"):
                st.info(f"📡 从 EXIF 读到定位：{exif['lat']:.6f}, {exif['lon']:.6f}")
            else:
                st.caption("照片里没有可用的 GPS 信息（微信传输过通常会丢失），以上面点选的机位为准。")
            if exif.get("shot_time"):
                st.write(f"🕒 拍摄时间：{exif['shot_time']}")
            if exif.get("camera"):
                st.write(f"📷 器材：{exif['camera']}")

    # ---------------- 第三步：作品信息 ----------------
    st.subheader("③ 作品信息")
    f1, f2 = st.columns(2)
    with f1:
        title = st.text_input("作品名 *", placeholder="例：梧桐缝里的晚霞")
        author = st.text_input("姓名 / 昵称 *")
        shot_time = st.text_input(
            "拍摄时间", value=parse_shot_time(exif.get("shot_time", "")) if exif else "",
            placeholder="2026-10-20 17:40",
        )
    with f2:
        contact = st.text_input("联系方式（学号 / 微信 / 邮箱）", help="仅用于发放奖品与版权确认，不公开")
        weather = st.selectbox("天气现象 *", WEATHER_PRESETS)
        weather_detail = st.text_input("天象细节（可选）", placeholder="例：高积云 + 落日侧光，地平线有层积云")

    note = st.text_area("拍摄手记 / 机位提示（会展示在地图上）", height=90,
                        placeholder="例：站在台阶第三级，广角端贴近地面仰拍，等太阳落到楼后 5 分钟出现火烧云")

    agree = st.checkbox("我确认作品为本人原创，并授权主办方用于打卡点地图与宣传展示 *")

    # ---------------- 提交 ----------------
    if st.button("🚀 提交投稿", type="primary", use_container_width=True):
        problems = []
        if not title.strip():
            problems.append("作品名")
        if not author.strip():
            problems.append("姓名/昵称")
        if upload is None:
            problems.append("照片")
        if not agree:
            problems.append("原创与授权声明")
        if problems:
            st.error("还差：" + "、".join(problems))
            return

        exif_pt = (exif.get("lat"), exif.get("lon")) if exif.get("has_gps") else None
        picked = st.session_state.get(state_key)
        res = resolve_location(
            campus_key=campus_key, picked=picked, exif=exif_pt, text=loc_text, index=idx
        )

        with st.spinner("正在保存并自动定点…"):
            subs = load_locked()
            try:
                origin_ref, thumb_ref = save_photo(upload)
                rec = Submission(
                    sid=next_sid(subs),
                    campus=campus_key,
                    lat=res["lat"], lon=res["lon"],
                    picked_lat=picked[0] if picked else None,
                    picked_lon=picked[1] if picked else None,
                    exif_lat=exif_pt[0] if exif_pt else None,
                    exif_lon=exif_pt[1] if exif_pt else None,
                    loc_text=loc_text,
                    loc_matched=res["matched"],
                    loc_score=res["score"],
                    loc_source=res["source"],
                    loc_verify=bool(res["verify"]),
                    title=title.strip(),
                    author=author.strip(),
                    contact=contact.strip(),
                    shot_time=parse_shot_time(shot_time),
                    weather=(weather + (" · " + weather_detail if weather_detail else "")).strip(" ·"),
                    camera=exif.get("camera", ""),
                    note=note.strip(),
                    photo=origin_ref,
                    thumb=Path(thumb_ref).name if thumb_ref else "",
                    thumb_ref=thumb_ref,
                    submitted_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                )
                subs.append(rec)
                save_submissions(subs)
            finally:
                release_lock()

        st.success(f"投稿成功！编号 **{rec.sid}**")
        if rec.has_point:
            src_label = {"picked": "地图点选", "exif": "照片 EXIF", "text": "文字匹配",
                         "none": "未定点"}.get(rec.loc_source, rec.loc_source)
            st.write(f"自动定点：**{rec.lat:.6f}, {rec.lon:.6f}**"
                     f"（来源：{src_label}；匹配到「{rec.loc_matched}」）")
            if rec.loc_verify:
                st.warning("这个落点可能不准，会在地图上标为「待复核」，组织方会人工核对。")
        else:
            st.warning("没能自动定点：请在地图上点一下机位，或把位置描述写得更具体（如“北大楼前草坪”）。")

        st.page_link(S.MAP_FILE, label="🗺 去「打卡点地图」页看汇总效果", icon=None)
        st.balloons()
        st.caption("可继续提交下一幅：在地图上点新的机位即可。")


main()
