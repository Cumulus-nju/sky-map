"""成品页：在同一网站里直接看「校园天空打卡点地图」。

侧边栏「🗺 打卡点地图」进入。地图就是 map_build.py 生成的那份 HTML，
本地模式下嵌进页面，和分享出去的文件完全一致。

云端模式（Supabase）注意：
    平台的文件系统重启会清空，所以不依赖本地文件，改为按投稿内容缓存后
    在内存里生成 HTML 直接内嵌；导出的文件只作为"下载/分享"用途。
"""
from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st
import streamlit.components.v1 as components

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import site_common as S  # noqa: E402

S.setup("打卡点地图", "🗺")

from campus_config import CAMPUSES  # noqa: E402
from store import EXPORTS, storage_kind  # noqa: E402
from submission_data import load_submissions  # noqa: E402

ONLINE = EXPORTS / "校园天空打卡点地图_在线版.html"
OFFLINE = EXPORTS / "校园天空打卡点地图.html"
CLOUD = storage_kind() != "local"


def build_maps(force: bool = False) -> tuple[bool, str]:
    """生成地图。本地写文件；云端也写（当缓存），但展示以内存为主。"""
    import contextlib
    import io

    import map_build
    from submission_data import export_csv

    subs = load_submissions()
    if not subs:
        return False, "还没有任何投稿。"

    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            # 云端没有可分享的本地 thumbs 文件，照片直接内嵌
            payload = map_build.build_payload(subs, embed_photos=CLOUD)
            map_build.write_map(payload, offline=False)
            payload_off = map_build.build_payload(subs, embed_photos=True)
            map_build.write_map(payload_off, offline=True)
            export_csv(subs)
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}\n{buf.getvalue()}"
    return True, buf.getvalue()


@st.cache_data(show_spinner=False, ttl=300)
def cached_map_html(n: int, offline: bool) -> str:
    """按"投稿条数 + 版本"缓存生成的 HTML，避免每次交互都重算。

    ⚠ **照片一律内嵌**（`embed_photos=True`），不能像导出文件那样走 `thumbs/` 相对路径：
    这段 HTML 是塞进 `components.html` 的 iframe（srcdoc）里显示的，相对路径会相对
    **Streamlit 页面地址**去解析 → 404 → 缩略图变成一个"白边框的裂图"。
    症状（用户 2026-10-05 反馈）："打卡点地图上序号右侧有一条白色竖线，有点难看"。
    （导出/分享用的 HTML 仍然用相对路径 —— 那个是连 `thumbs/` 文件夹一起发出去的。）
    """
    subs = load_submissions()
    import contextlib
    import io

    import map_build

    with contextlib.redirect_stdout(io.StringIO()):
        payload = map_build.build_payload(subs, embed_photos=True)
        return map_build.render_html(payload, offline=offline or CLOUD)


def embed_map(html: str, height: int = 820) -> None:
    """把地图 HTML 嵌进页面。CSS 里直接补上嵌入样式，不依赖 JS 时序。"""
    inject = """
    <style>
      html,body{height:100%;margin:0;overflow:hidden}
      #app{height:100vh;display:block}
      #main{width:100%;height:100vh;position:relative}
      #map{height:100%}
      #side,.hero,#filters{display:none !important}
      #campusFloat{display:flex !important}
      .leaflet-container{width:100%;height:100%}
    </style>
    """
    components.html(html.replace("</head>", inject + "</head>"),
                    height=height, scrolling=False, width="stretch")


# ---------------- 侧边栏 ----------------
S.nav(S.MAP_PAGE)
# 正文最上方的页面导航条：手机上侧边栏是收起的，同学很难发现别的页面。
# ⚠ 用 hasattr 兜底：云端只改 .py 时可能出现"新页面 + 旧 site_common"
#   （2026-10-06 投票页就这么整页崩过一次），旧模块里没有 top_nav。
if hasattr(S, "top_nav"):
    S.top_nav(S.MAP_PAGE)

subs = load_submissions()
demo_on = S.demo_mode()
with st.sidebar:
    if demo_on and any(s.is_demo for s in subs):
        st.warning("当前含 **示例数据**，正式征稿前请清空。", icon="⚠️")
    st.metric("收稿", f"{len(subs)} 幅")
    try:
        from submission_data import cluster_spots

        st.metric("打卡点", f"{len(cluster_spots(subs))} 个")
    except Exception:
        st.metric("打卡点", "—")

with st.sidebar:
    st.divider()
    # 标签原来是「底图版本」——用户 2026-10-06 指出容易被误解成"换底图"，
    # 而这个单选换的是**照片怎么进 HTML**（相对路径 vs 内嵌），与底图无关
    # （底图早就是同一种了）。改成「导出方式」。
    mode = st.radio("导出方式", ["在线版（推荐分享）", "离线单文件版"], index=0,
                    help="选的是**照片怎么装进这份 HTML**，不是换底图 —— "
                         "两个版本用的是同一张自绘立体底图（与投稿选机位同一张）。\n\n"
                         "· 在线版：照片走 thumbs/ 相对路径，体积小、适合挂网页分享；\n"
                         "· 离线单文件版：连照片一起内嵌，断网也能看、适合发单个文件。")
    if st.button("🔄 重新生成并刷新", use_container_width=True, type="primary"):
        cached_map_html.clear()
        st.rerun()
    st.divider()
    if ONLINE.exists():
        st.download_button("⬇️ 下载地图 HTML", ONLINE.read_bytes(),
                           file_name=ONLINE.name, mime="text/html",
                           use_container_width=True,
                           help="把这份 html 和同目录 thumbs/ 一起发出去就能分享")

# ---------------- 主体 ----------------
st.markdown("## 🗺 校园天空打卡点地图")

if not subs:
    st.info("还没有投稿。先去「📝 投稿」页提交一幅，再回来看地图。")
    st.stop()

offline_view = mode.startswith("离线")
with st.spinner("正在汇总投稿并生成地图…"):
    try:
        html = cached_map_html(len(subs), offline_view)
    except Exception as exc:
        st.error(f"生成失败：{type(exc).__name__}: {exc}")
        st.stop()

import datetime

st.caption(f"共 {len(subs)} 幅作品　·　"
           f"{datetime.datetime.now().strftime('%Y-%m-%d %H:%M')} 生成")
# 原来这里还有一句"在地图上按 Ctrl+F5 可强制刷新底图缓存" —— 用户 2026-10-06
# 要求删掉。它也确实**已经没有意义**了：底图切换条去掉后，唯一的底图是
# **内嵌的图片**（data URL），不存在"在线瓦片缓存"这回事；真要看最新地图，
# 该刷新的是 Streamlit 这边的生成缓存（侧栏的「🔄 重新生成并刷新」按钮）。

embed_map(html)

with st.expander("怎么把地图分享出去？"):
    st.markdown(
        """
        1. **发网页 / 群文件**：点侧边栏「🔄 重新生成并刷新」，再「⬇️ 下载地图 HTML」，
           连同一个 `thumbs/` 目录一起发出去。
        2. **发单个文件**：侧边栏「导出方式」选**离线单文件版**，照片和底图都嵌在里面，
           断网也能看，适合 U 盘、内网、打印前预览。
        3. **挂到网页 / 公众号**：把 HTML 与 `thumbs/` 一起上传到服务器即可。
        4. 本网站本身也可以直接分享链接，别人打开就能看这个页面。
        """
    )
