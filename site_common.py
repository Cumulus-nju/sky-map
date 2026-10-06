"""共享启动代码：各页面共用的路径处理、导航、权限与口令。

放在项目根目录，供 app.py 与 pages/*.py 导入。
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import sys
from datetime import datetime
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 站点名称
SITE = "天光云影 · 打卡点地图"
SUBMIT_PAGE = "投稿"
MAP_PAGE = "打卡点地图"
VOTE_PAGE = "作品投票"
ADMIN_PAGE = "管理员"

# 构建版本：显示在侧边栏，用来确认线上部署的是哪一版。
# 改代码时**一起改这个**，push 后刷新线上即可确认是否真的更新了。
BUILD = "2026-10-06a"

# 页面文件（Streamlit 按文件路由：根目录 app.py = "/"，pages/ 下的各占一个路径）
SUBMIT_FILE = "app.py"
MAP_FILE = "pages/打卡点地图.py"
VOTE_FILE = "pages/投票.py"
ADMIN_FILE = "pages/管理员.py"

# 查询参数
QP_FRESH = "fresh"      # 点了"刷新地图"后，绕过缓存重新生成
QP_DEMO = "demo"        # 是否载入示例数据（1/0）

# 管理员口令存放位置（首次设置后写入）
ADMIN_META = ROOT / "data" / "admin.json"
DEFAULT_PASSWORD = "nju-sky-2026"


def setup(page_title: str, icon: str = "🌤") -> None:
    st.set_page_config(
        page_title=f"{page_title} · {SITE}",
        page_icon=icon,
        layout="wide",
        # ⚠ 必须用 "auto"，**不能写死 "expanded"**（2026-10-05 移动端适配时改的）。
        # 写死 expanded 的后果：手机上（390px 宽）侧边栏照样展开占 300px，
        # 把地图挤成右边一条窄缝 —— 更糟的是侧边栏**盖在地图上面**
        # （stSidebar 的 z-index 是 999991），触点全被它接走，
        # 同学连着双击几次都不会有反应，也看不出为什么。
        # "auto" = 窗口够宽就展开（桌面观感不变）、窄就自动收起（手机让位给地图）。
        initial_sidebar_state="auto",
    )


def qp(name: str, default: str = "") -> str:
    """安全读取查询参数（不同 Streamlit 版本 API 略有差异）。"""
    try:
        v = st.query_params.get(name)
    except Exception:
        v = None
    if isinstance(v, list):
        v = v[0] if v else None
    return str(v) if v is not None else default


def clear_qp(*names: str) -> None:
    """删掉指定的查询参数（读走之后要清掉，避免 URL 一直挂着旧值）。"""
    for n in names:
        try:
            del st.query_params[n]
        except Exception:
            pass


# `take_pick()` 的**接口形状版本**（自检用，别删）：
#   1 = 旧版：返回 (lat, lon)，并且读走就 `del st.query_params['pick']`
#   2 = 新版：返回 (lat, lon, campus)，**不清除**参数
# ⚠ 为什么要有这个常量：Streamlit Cloud 只改 .py 时可能只做**模块级热重载**，
#   于是出现"新 app.py + 旧 site_common.py"。app.py 靠这个常量判断要不要告警，
#   并**两种形状都兼容**（2026-10-05 线上就是因此 IndexError 整页崩掉的）。
PICK_API = 2


def take_pick() -> tuple[float, float, str] | None:
    """读取"刚在静态底图上点选"的坐标，返回 `(lat, lon, campus_key)`。

    机制：点选组件是内嵌 iframe，点一下会把 `pick=lat,lon` 写进父窗口 URL
    （同源可写），Streamlit 检测到查询参数变化就 rerun；这里读走它。

    ⚠⚠ **故意不清除这个参数**（2026-10-05 踩坑后改的，别再"顺手清理"）：
    后端一旦 `del st.query_params['pick']`，Streamlit 前端的查询参数同步就**坏掉** ——
    之后**再写同名参数 + 派发 popstate 都不会再触发 rerun**。
    症状：第一次点选正常，**从第二次起"重新选点"完全不生效**
    （URL 里的 pick 一直挂着没人消费、页面下方的「已选机位」永远停在第一次）。
    实测证据（tools/cdp_dblclick_pick.mjs 第⑨项）：
      清除开启 → 第 2/3 次写 pick：无 rerun；
      清除关闭 → 第 2/3 次写 pick：rerun 正常、页面跟着更新。
    代价：`pick` 会一直留在 URL 里 —— 刷新页面会重新套用同一个点（这其实是好事），
    但**换校区时必须按 `pick_campus` 归位**，否则会把鼓楼的点记到仙林名下
    （调用方 `app.py` 已按校区校验）。

    顺带一提 `pnav` 从来不清除，所以它一直是好的。
    """
    raw = qp("pick")
    if not raw:
        return None
    campus = qp("pick_campus") or ""
    try:
        la_s, lo_s = raw.split(",")[:2]
        lat, lon = float(la_s), float(lo_s)
    except Exception:
        return None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    return (round(lat, 7), round(lon, 7), campus)


def take_nav() -> tuple[float, float, int] | None:
    """读取底图当前视图状态 `pnav=lat,lon,zoom`（**不清除**）。

    为什么要跨 rerun 记住它：用户放大到某一层、拖到某个位置去精细点选，
    如果每次点击后的 rerun 都把视图重置回全图，就白放大了 —— 体验会非常糟。
    所以 `pnav` 幂等保留在 URL 里，每次渲染按它恢复视图。
    """
    raw = qp("pnav")
    if not raw:
        return None
    try:
        parts = raw.split(",")
        lat, lon, z = float(parts[0]), float(parts[1]), int(float(parts[2]))
    except Exception:
        return None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    # 夹到组件真正支持的缩放级（避免 URL 里塞个 99 让组件白算）
    try:
        from frame_picker import MAX_ZOOM
    except Exception:
        MAX_ZOOM = 2
    return (lat, lon, max(0, min(MAX_ZOOM, z)))


def nav(current: str) -> None:
    """侧边栏导航。

    ⚠ **页面入口交给 Streamlit 自动导航**（侧边栏顶部那个），这里不再重复放
    `page_link` —— 用户 2026-10-04 要求简洁化，而"打卡点地图"当时在侧边栏
    出现了**三次**（自动导航 + 这里的 page_link + 投稿页自己的"看打卡点地图"）。

    这里只保留站点名，以及**管理后台入口**（登录后才出现，避免同学误入）。
    `current` 参数保留只为兼容调用方。
    """
    st.sidebar.markdown(f"### 🌤 {SITE}")
    if is_admin():
        st.sidebar.divider()
        st.sidebar.page_link(ADMIN_FILE, label="🔧 管理后台", icon=None,
                             help="审核、修改、删除投稿")


def storage_status() -> None:
    """显示当前存储后端 —— 一眼看出云端有没有接上数据库。

    为什么需要它：没配 SUPABASE_URL/KEY 时会静默退回本地文件存储，
    而云端容器重启就清空文件系统，同学投的稿会全部丢失。让"没接上数据库"
    变成可见状态，而不是等到丢稿才发现。

    ⚠ 原来写死 `st.sidebar`；用户 2026-10-04 要求别在同学端侧边栏显示，
    已移到管理后台 ⇒ 改成输出到**主区**（`st`）。
    """
    try:
        from store import storage_kind

        kind = storage_kind()
    except Exception:
        return
    if kind == "supabase":
        st.caption("🗄 存储：Supabase 云端（投稿持久保存）")
    else:
        st.warning(
            "⚠️ 当前为**本地临时存储**：服务器重启会清空所有投稿！\n\n"
            "请在应用设置里配置 Supabase 的 `SUPABASE_URL` / `SUPABASE_KEY`。",
            icon="🗄",
        )


def demo_mode() -> bool:
    """是否处于示例数据模式：默认走项目里现有数据，显式传 demo=0 才排除。"""
    return qp(QP_DEMO, "1") != "0"


# ---------------------------------------------------------------- 管理员鉴权


def _hash(pw: str, salt: str) -> str:
    return hashlib.sha256((salt + pw).encode("utf-8")).hexdigest()


def admin_configured() -> bool:
    if configured_password():
        return True
    if not ADMIN_META.exists():
        return False
    try:
        return bool(json.loads(ADMIN_META.read_text(encoding="utf-8")).get("hash"))
    except Exception:
        return False


def configured_password() -> str:
    """从环境变量或 Streamlit secrets 读预设管理员口令（部署时用）。"""
    try:
        v = os.environ.get("ADMIN_PASSWORD", "")
        if v:
            return v.strip()
    except Exception:
        pass
    try:
        v = st.secrets.get("ADMIN_PASSWORD", "")
        return str(v).strip() if v else ""
    except Exception:
        return ""


def ensure_admin_from_secrets() -> None:
    """部署时在 secrets 里配了 ADMIN_PASSWORD，就自动把口令写好。

    这样云端重启后不会退回"首次设置"状态——否则文件系统一清，
    任何人都能重新设置管理员口令，等于后台没锁。
    """
    pw = configured_password()
    if not pw:
        return
    need = True
    if ADMIN_META.exists():
        try:
            meta = json.loads(ADMIN_META.read_text(encoding="utf-8"))
            need = meta.get("hash") != _hash(pw, meta.get("salt", ""))
        except Exception:
            need = True
    if need:
        set_admin_password(pw)


def set_admin_password(pw: str) -> None:
    ADMIN_META.parent.mkdir(parents=True, exist_ok=True)
    salt = secrets.token_hex(16)
    ADMIN_META.write_text(
        json.dumps(
            {"salt": salt, "hash": _hash(pw, salt),
             "updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S")},
            ensure_ascii=False, indent=2,
        ),
        encoding="utf-8",
    )
    st.session_state["admin_ok"] = True


def check_admin_password(pw: str) -> bool:
    if not ADMIN_META.exists():
        return False
    try:
        meta = json.loads(ADMIN_META.read_text(encoding="utf-8"))
    except Exception:
        return False
    ok = secrets.compare_digest(_hash(pw, meta.get("salt", "")), meta.get("hash", ""))
    if ok:
        st.session_state["admin_ok"] = True
    return ok


def is_admin() -> bool:
    try:
        return bool(st.session_state.get("admin_ok"))
    except Exception:
        return False


def logout() -> None:
    st.session_state["admin_ok"] = False

