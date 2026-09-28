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
ADMIN_PAGE = "管理员"

# 页面文件（Streamlit 按文件路由：根目录 app.py = "/"，pages/ 下的各占一个路径）
SUBMIT_FILE = "app.py"
MAP_FILE = "pages/打卡点地图.py"
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
        initial_sidebar_state="expanded",
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


def nav(current: str) -> None:
    """侧边栏导航。管理员入口只在登录后出现，避免同学误入。"""
    st.sidebar.markdown(f"### 🌤 {SITE}")
    st.sidebar.caption("南京大学校园天空摄影大赛 · 南赫学院 × 摄影社")
    st.sidebar.divider()
    st.sidebar.page_link(SUBMIT_FILE, label="📝 投稿", icon=None,
                         help="同学在这里提交作品并点选机位")
    st.sidebar.page_link(MAP_FILE, label="🗺 打卡点地图", icon=None,
                         help="已经收到的作品汇总成的交互地图")
    if is_admin():
        st.sidebar.page_link(ADMIN_FILE, label="🔧 管理后台", icon=None,
                             help="审核、修改、删除投稿")
    st.sidebar.divider()
    st.sidebar.caption(f"当前位置：**{current}**")
    _storage_badge()


def _storage_badge() -> None:
    """在侧边栏显示当前存储后端 —— 一眼看出云端有没有接上数据库。

    为什么需要它：没配 SUPABASE_URL/KEY 时会静默退回本地文件存储，
    而云端容器重启就清空文件系统，同学投的稿会全部丢失。这个徽标让
    "没接上数据库"变成可见状态，而不是等到丢稿才发现。
    """
    try:
        from store import storage_kind

        kind = storage_kind()
    except Exception:
        return
    if kind == "supabase":
        st.sidebar.caption("🗄 存储：Supabase 云端（投稿持久保存）")
    else:
        st.sidebar.warning(
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
