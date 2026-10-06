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
import streamlit.components.v1 as components
from streamlit_folium import st_folium

import site_common as S

S.setup("投稿", "🌤")

# 外框与缩放级统一走 frame_util（带三级降级，任何模块状态都不会抛异常）。
#
# 教训（2026-10-01 线上白屏，完整 traceback 已确认）：云端容器里 campus_config 是
# **旧版**（Campus 没有 frame 字段、也没有 frame_of / min_zoom_for_frame），而 app.py
# 是新版。原来的写法在这里 `import frame_of` 失败 -> 走 except 兜底 -> 兜底里读
# `c.frame` -> AttributeError -> 整站白屏。所以：
#   ① 只 import 一定存在的名字（CAMPUSES / campus），不 import 可能缺的新函数；
#   ② frame_of / min_zoom_for_frame 一律用 frame_util 的降级实现。
from campus_config import CAMPUSES, campus as get_campus  # noqa: E402
from frame_util import (  # noqa: E402
    frame_center,
    frame_of,
    map_height_for_frame,
    min_zoom_for_frame,
    module_state_ok,
    module_version,
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

    ⚠ **云端必须先瘦身**（2026-10-05 线上故障）：照片是 base64 存进数据库 text 字段的，
    20 MB 的手机原图 base64 后约 27 MB ⇒ ① 单个请求体太大、云端直接拒收（报 HTTPError）；
    ② 免费层数据库只有 500 MB，20 MB 一张存 18 张就满。
    所以云端超过 2 MB 就重编码成「长边 ≤ 2560、JPEG q88」（≈A4 300dpi，约 0.6~1.2 MB）；
    **本地文件存储照旧存原图**（不占数据库，组织者自己留档/印刷更好）。
    """
    import io

    from photo_shrink import ensure_storable, shrink_for_store
    from store import _safe_id, get_store, storage_kind

    raw = upload.getvalue()
    mime = (getattr(upload, "type", "") or "image/jpeg").split(";")[0].strip()
    pid = hashlib.sha1(raw).hexdigest()[:12]
    st_obj = get_store()

    if storage_kind() != "local":
        raw, mime, note = shrink_for_store(raw, mime)
        ensure_storable(raw)
        if note:
            st.session_state["_photo_note"] = note      # 提交成功后再展示给同学

    origin_ref = st_obj.put_photo(pid, raw, mime or "image/jpeg")

    thumb_ref = ""
    try:
        thumb_ref = st_obj.put_thumb(pid, make_thumb_bytes(io.BytesIO(raw)))
    except Exception:
        thumb_ref = ""
    return origin_ref, thumb_ref


def roof_shapes_for(campus_key: str, fs, fw, fn, fe):
    """取该校区立体底图的**屋顶多边形**（点选吸附用）；取不到就返回 None。

    只有**自绘立体底图**才有"屋顶相对楼基位移"这回事：瓦片拼图是正射影像、
    旧平面自绘没有高度 —— 都不需要吸附。

    ⚠ 必须容错：吸附是锦上添花，**绝不能因为它把投稿页搞挂**
    （老坑：兜底路径平时跑不到，所以最容易写错；这里一出错就退回"不吸附"）。
    """
    import sys

    import static_basemap
    try:
        src = static_basemap.source_of(campus_key)
        if src == "relief":
            import relief_basemap
            return relief_basemap.roof_shapes(campus_key, fs, fw, fn, fe, width=2000)
        if src == "glb":
            import glb_relief
            return glb_relief.roof_shapes(campus_key, fs, fw, fn, fe, width=2000)
    except Exception as exc:
        print(f"[pick] 屋顶吸附几何不可用（{type(exc).__name__}: {exc}），本次不吸附",
              file=sys.stderr)
    return None


def render_picker(campus_key: str, picked: tuple[float, float] | None):
    """渲染选点组件：**外框内的静态底图** + 点击取坐标。

    为什么换成静态图（2026-10-02，用户提的方案，替代实时 Leaflet 地图）：
      * 实时地图的"视野贴合"依赖容器实际尺寸，服务端算不准 —— 就是之前那个
        "蓝框塞进扁容器变成一条"的老问题，怎么调都有余量；
      * 静态图里"图上的每个像素 ↔ 固定的经纬度"是纯数学关系，**没有视图尺寸参与**，
        点选永远对得齐；
      * 美术可控（图可以先做统一调色/压暗/加标注），且内嵌 data URL，弱网也能看。

    换算的一致性由 `frame_picker.assert_js_python_agree` 守着（测试里会跑）。
    """
    import frame_picker
    import static_basemap

    cfg = get_campus(campus_key)
    fs, fw, fn, fe = frame_of(campus_key)

    try:
        # 统一入口：有矢量数据的校区用**自绘底图**（更清晰、配色可控、无瓦片条款问题），
        # 没有的（如苏州）自动退回瓦片拼接。两条路径坐标口径一致。
        fi = static_basemap.frame_image_for(
            campus_key, fs, fw, fn, fe, target_width=2000)
    except Exception as exc:
        # 拼图失败（断网 / 瓦片源限流）不能让投稿页崩掉 —— 退回实时地图并提示
        st.warning(
            f"静态底图生成失败（{type(exc).__name__}: {exc}），已临时切回在线地图。"
            "若是首次运行，请连网后点下方按钮重试。",
            icon="🗺",
        )
        return render_map_fallback(campus_key, picked)

    doc, height = frame_picker.build_picker_html(
        fi, campus_key=campus_key, display_width=1200, picked=picked,
        nav=S.take_nav(),
        # 不画地标标记：用户明确说"同学们认不得"，标了反而是干扰
        # （landmarks 里是"北大楼/天文与空间科学学院"这类，不是同学认的路标）。
        landmarks=None,
        max_display_height=980,
        tip="滚轮缩放 · 左键按住拖动 · 右键选点",
        # 立体底图上"点到屋顶"会偏出真实楼基 ⇒ 贴回那栋楼的足迹（用户 2026-10-04 要的）
        roofs=roof_shapes_for(campus_key, fs, fw, fn, fe),
    )
    # width 用 "stretch" 铺满可用宽度：组件内部会自己量父容器宽度并调整
    # #frame 与 iframe 高度（Streamlit 只收整数宽度，写死会在窄屏被裁）。
    components.html(doc, width="stretch", height=int(height) + 4, scrolling=False)
    return None


def render_map_fallback(campus_key: str, picked: tuple[float, float] | None):
    """静态底图不可用时的兜底：原来的在线 Leaflet 地图（功能少一点但能选点）。"""
    fs, fw, fn, fe = frame_of(campus_key)
    center = frame_center(campus_key)
    height = map_height_for_frame(campus_key, view_w=880)
    min_z = min_zoom_for_frame(campus_key, view_w=880, view_h=height)
    m = folium.Map(location=list(center), zoom_start=min_z, tiles=None,
                   control_scale=True, min_lat=fs, max_lat=fn, min_lon=fw, max_lon=fe,
                   max_bounds=True, maxBoundsViscosity=1.0, minZoom=min_z, zoomControl=True)
    folium.TileLayer(tiles=get_campus(campus_key).streets.url,
                     attr=get_campus(campus_key).streets.attr,
                     max_zoom=get_campus(campus_key).streets.max_zoom).add_to(m)
    folium.Rectangle(bounds=[[fs, fw], [fn, fe]], color="#2f6fb5", weight=1.6,
                     fill=False, dash_array="6 4").add_to(m)
    if picked and picked[0] is not None:
        folium.Marker(list(picked), tooltip="你选的机位").add_to(m)
    return st_folium(m, height=height, use_container_width=True,
                     returned_objects=["last_clicked"])


def report_save_failure(exc: Exception, upload) -> None:
    """投稿落库失败：把**可诊断的信息**同时给到日志和用户。

    为什么非做不可：线上出错时 Streamlit 会把原始异常信息涂掉，
    用户只看到一句 `HTTPError`，分不清是
    401（密钥/权限）、404（表没建）、413（照片太大）、429/5xx（配额/暂停）。
    把状态码、云端返回片段、上传文件大小摆出来，一眼就能定位。
    """
    import traceback

    status = getattr(exc, "status", None)
    body = getattr(exc, "body", "") or ""
    path = getattr(exc, "path", "") or ""
    name, size_mb = getattr(upload, "name", "?"), None
    try:                      # 量一下上传文件多大（顺便把游标归位）
        upload.seek(0, 2)
        size_mb = upload.tell() / 1048576
        upload.seek(0)
    except Exception:
        pass

    traceback.print_exc()     # 完整堆栈进 Manage app → Logs

    # "压完还是太大"是我们自己主动拦的，消息本身就是给同学看的，直接展示
    try:
        from photo_shrink import PhotoTooLarge

        if isinstance(exc, PhotoTooLarge):
            st.error(f"**这张照片传不上去：**{exc}")
            return
    except Exception:
        pass

    st.error("**投稿没能保存。**（平台把原始报错涂掉了，下面是能拿到的信息）")
    lines = [f"异常：{type(exc).__name__}: {exc}"]
    if status:
        lines.append(f"HTTP 状态码：{status}")
    if path:
        lines.append(f"接口：{path}")
    if body:
        lines.append(f"云端返回：{body}")
    lines.append(f"上传文件：{name}" + (f" · {size_mb:.1f} MB" if size_mb else ""))
    st.code("\n".join(lines), language="text")

    hints = {
        401: "Supabase 密钥不是 **service_role**（或 RLS 策略被动过）⇒ 去 Supabase → "
             "Project Settings → API 复制 service_role key，更新 Streamlit secrets 的 "
             "`SUPABASE_KEY` 后 Reboot。",
        403: "同上：密钥权限不足 / RLS 没放开。",
        404: "`photos` 表不存在 ⇒ 去 Supabase → SQL Editor 执行 `deploy/supabase_schema.sql`。",
        409: "编号撞车（并发提交），再点一次提交即可。",
        413: "**照片太大**，云端拒收这个请求体 ⇒ 换一张更小的图，或先把照片压到 5 MB 以内。",
        429: "免费层请求配额用完，等一会儿再试。",
        500: "云端内部错误。若上面显示上传文件很大，多半是**照片太大**（单张原图 base64 后"
             "要进数据库 text 字段）⇒ 先用更小的图试一次；若小图也失败，就是库/配额问题。",
        503: "Supabase 项目可能被**暂停**（免费层长期闲置）⇒ 去控制台 Restore 一下。",
    }
    st.info(hints.get(status, "可先换一张**更小的照片**重试；若仍失败，把上面这段文本发给维护者。"))
    st.caption("完整堆栈已写进日志：Streamlit Cloud → 右下角 **Manage app** → Logs。")


def main() -> None:
    S.nav(S.SUBMIT_PAGE)
    # 正文最上方的页面导航条：手机上侧边栏是收起的，光靠它同学找不到
    # 「打卡点地图」和「作品投票」（2026-10-06 用户提的问题）。
    # ⚠ 用 hasattr 兜底：云端只改 .py 时可能出现"新入口脚本 + 旧 site_common"
    #   （2026-10-06 投票页就这么整页崩过一次），旧模块里没有 top_nav。
    if hasattr(S, "top_nav"):
        S.top_nav(S.SUBMIT_PAGE)

    # 云端模块状态自检：campus_config 不是完整新版时给出**可见**提示。
    # 这样"线上加载了旧模块"会自己说出来，而不是等某个页面崩掉才发现。
    if not module_state_ok():
        st.warning(
            "检测到 `campus_config` 模块不是最新版，地图外框已启用**内置兜底数据**"
            f"（模块版本：`{module_version() or '未知（旧版）'}`）。\n\n"
            "功能可用，但若与最新配置不一致，请到 Streamlit Cloud → Manage app → **Reboot** 重启容器。",
            icon="⚠️",
        )
    # site_common 也自检：它是"点选回传"的桥，新旧混用会让选点整条失效。
    # 2026-10-05 线上就是这么崩的：新 app.py + 旧 site_common ⇒ take_pick() 返回值少一个，
    # 取第三个值时 IndexError（而且**只在点选之后**才炸）。归并逻辑已改成两种形状都吃，
    # 这里只额外给一句**可见提示**，让"模块旧"这件事自己说出来。
    if getattr(S, "PICK_API", 1) != 2:
        st.warning(
            "检测到 `site_common` 模块不是最新版（选点接口为旧形状）。"
            "点选功能已做兼容、可正常用，但建议到 Streamlit Cloud → Manage app → "
            "**Reboot** 重启容器，让模块与代码对齐。",
            icon="⚠️",
        )

    st.title("🌤 天光云影 · 校园天空摄影大赛投稿")

    idx = landmark_index()
    existing = load_submissions()
    with st.sidebar:
        st.metric("已收稿", f"{len(existing)} 幅")

    # ---------------- 第一步：选校区与机位 ----------------
    # 排版（2026-10-05 用户要求"保持现有文字/结构，优化排版"）：
    # 三步各放进一个**带边框的卡片**，让"①②③"在页面上真的是三块，
    # 而不是从标题一路平铺到底；文字、字段、逻辑一个字没动，只改视觉分组。
    with st.container(border=True):
        st.subheader("① 选择校区并在图上点出机位")
        key_labels = {k: c.name for k, c in CAMPUSES.items()}
        campus_key = st.radio(
            "校区", list(CAMPUSES), format_func=lambda k: key_labels[k], horizontal=True, key="campus_key"
        )
        # ⚠ 操作说明必须写全、且必须与实现一致，**而且要按设备分开写**：
        #   电脑 = **右键单击**选点（用户 2026-10-05 明确"电脑端和以前一样是右键单击"），
        #   手机 = **双击轻点**（触屏没有右键，而机位是必填 ⇒ 不给手机手势就交不了稿）。
        #   只写一种必然误导另一半人：电脑同学照着手机会去双击，手机同学找不到右键。
        st.caption(
            "在地图上**右键单击**（手机上**双击轻点**）你拍照站的位置（越准越好，**必填**）；"
            "点错了再选一次就会覆盖。"
            "　操作：**滚轮 / 双指** 缩放 · **按住** 拖动 · **右键 / 双击轻点** 选点"
        )

        state_key = f"picked_{campus_key}"
        picked = st.session_state.get(state_key)

        # 归并点选回传。⚠ **这段必须自包含、不许依赖任何新函数/新签名**：
        #   云端可能出现"新 app.py + 旧 site_common.py"。规律是 —— Streamlit 只对
        #   **import 进来的模块**做模块级热重载，而**入口脚本每次都重新执行**，
        #   所以只有 app.py 一定是最新的；兼容逻辑写在这里才靠得住。
        #   旧版 `take_pick()` 只返回 `(lat, lon)`（且会自己清除参数），直接取 `[2]`
        #   会 IndexError 整页崩 —— 2026-10-05 线上就是这么崩的，
        #   而且**只在点选之后才炸**（这行只在收到回传时才执行）。
        raw = S.take_pick()
        pt = None
        if raw:
            try:
                _lat, _lon = float(raw[0]), float(raw[1])
                _pc = str(raw[2]) if len(raw) > 2 else ""   # 旧模块没有第三个值
                if not _pc or _pc == campus_key:             # 缺校区字段就按当前校区接受
                    _cand = (_lat, _lon)
                    pt = None if _cand == picked else _cand
            except (TypeError, ValueError, IndexError):
                pt = None
        if pt:
            st.session_state[state_key] = pt
            picked = pt

        clicked = render_picker(campus_key, picked)
        # 兜底分支（在线地图）仍走 last_clicked
        if clicked and isinstance(clicked, dict) and clicked.get("last_clicked"):
            lc = clicked["last_clicked"]
            newpt = (round(lc["lat"], 6), round(lc["lng"], 6))
            if newpt != picked:
                st.session_state[state_key] = newpt
                st.rerun()

        # 「位置描述」直接放在**地图正下方通栏**。
        # ⚠ 原来是 `st.columns([1, 1])`：左边放"已选机位"、右边放这个输入框，
        # 于是输入框只占右半边、看着"偏"（用户 2026-10-04 反馈"现在偏着很奇怪"）。
        picked = st.session_state.get(state_key)
        if picked:
            st.success(f"已选机位：{picked[0]:.6f}, {picked[1]:.6f}")
        loc_text = st.text_input(
            "位置描述（可选，供人工校对）",
            placeholder="例：北大楼前草坪、图书馆南侧台阶、操场看台…",
        )

    # ---------------- 第二步：上传照片 ----------------
    with st.container(border=True):
        st.subheader("② 上传作品")
        upload = st.file_uploader(
            "选择照片", type=["jpg", "jpeg", "png", "heic", "webp", "tif", "tiff"],
            help="**放心传手机原图**：云端会自动压到约 **2 MB**（长边 ≤ 2560，≈A4 300dpi）再入库，"
                 "既保证评审看得清，也不会把数据库撑爆。单张请尽量不超过 20 MB。",
        )

        exif: dict = {}
        if upload is not None:
            upload.seek(0)
            exif = read_exif(upload)
            upload.seek(0)
            # 预览列略宽于信息列：竖幅照片在窄列里会被压得很小
            c1, c2 = st.columns([1.15, 1])
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
    with st.container(border=True):
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
            weather = st.text_input("天气现象（可选）", placeholder="例：晚霞 / 火烧云")
            weather_detail = st.text_input("天象细节（可选）", placeholder="例：高积云 + 落日侧光，地平线有层积云")

        note = st.text_area("拍摄手记 / 机位提示（会展示在地图上）", height=90,
                            placeholder="例：站在台阶第三级，广角端贴近地面仰拍，等太阳落到楼后 5 分钟出现火烧云")

    # ---------------- 提交 ----------------
    # 勾选与按钮单独一张卡片：它们是"最后一步"，视觉上和上面三段信息分开，
    # 又不用新增任何标题文字。按钮的返回值先接住，处理逻辑仍在卡片外面（缩进不变）。
    with st.container(border=True):
        agree = st.checkbox("我确认作品为本人原创，并授权主办方用于打卡点地图与宣传展示 *")
        submit = st.button("🚀 提交投稿", type="primary", use_container_width=True)

    if submit:
        picked = st.session_state.get(state_key)   # 先取出来，下面的必填校验要用
        problems = []
        if not title.strip():
            problems.append("作品名")
        if not author.strip():
            problems.append("姓名/昵称")
        if upload is None:
            problems.append("照片")
        if not picked:
            # 用户 2026-10-04：机位改成**必填**（以图上点选为准）；
            # 位置描述不再参与自动定位，只作为后期人工校对的依据。
            problems.append("机位（在图上点选）")
        if not agree:
            problems.append("原创与授权声明")
        if problems:
            st.error("还差：" + "、".join(problems))
            return

        exif_pt = (exif.get("lat"), exif.get("lon")) if exif.get("has_gps") else None
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
            except Exception as exc:
                # ⚠ 必须**捕获并自己报告**：线上抛异常时 Streamlit 会把原始信息涂掉
                #   （只显示 "original error message is redacted"），用户只看到一句
                #   `HTTPError`，看不出是密钥/权限/表不存在/照片太大/配额用完。
                #   2026-10-05 提交投稿报 HTTPError 就是因为看不到状态码而卡住。
                report_save_failure(exc, upload)
                return
            finally:
                release_lock()

        st.success(f"投稿成功！编号 **{rec.sid}**")
        _note = st.session_state.pop("_photo_note", "")
        if _note:
            st.caption(_note)     # 云端会压缩入库，跟同学说清楚（透明，也免得他们以为传丢了原图）
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
