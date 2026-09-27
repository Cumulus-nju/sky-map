"""存储层：让「本地跑」和「部署到云」共用同一套代码。

为什么需要它：
    本地版把投稿写进 data/submissions.jsonl、照片写进 data/photos。
    但 Streamlit Community Cloud 这类免费平台**重启就清空文件系统**，
    同学投的稿会丢。所以上云时改用 Supabase（免费层：500MB 数据库）。

两种后端，接口一致，用环境变量自动切换：
    未配 SUPABASE_URL / SUPABASE_KEY  →  LocalStore（当前本地用法，不变）
    配了                              →  SupabaseStore（云端）

用法::

    from store import get_store
    st = get_store()
    subs = st.read_submissions()          # list[Submission]
    st.write_submissions(subs)
    st.put_photo(pid, b"...", "image/png")   # 原图
    st.put_thumb(pid, b"...")                # 缩略图
    data = st.get_photo(pid)                 # bytes
"""
from __future__ import annotations

import base64
import json
import os
import re
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Iterable

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
RAW = DATA / "raw"
SUBMISSIONS = DATA / "submissions.jsonl"
PHOTOS = DATA / "photos"
THUMBS = DATA / "thumbs"
EXPORTS = DATA / "exports"

for _d in (DATA, RAW, PHOTOS, THUMBS, EXPORTS):
    _d.mkdir(parents=True, exist_ok=True)

TABLE_SUBS = "submissions"
TABLE_PHOTOS = "photos"


def _safe_id(raw: str) -> str:
    """把投稿编号清洗成安全的存储 id（也当文件名用）。"""
    return re.sub(r"[^A-Za-z0-9_.-]", "_", (raw or "").strip()) or "unnamed"


# ---------------------------------------------------------------- 抽象接口


class Store(ABC):
    """投稿与照片的读写接口。"""

    kind = "base"

    # ---- 投稿元数据 ----
    @abstractmethod
    def read_submissions(self) -> list:
        ...

    @abstractmethod
    def write_submissions(self, subs: Iterable) -> int:
        ...

    # ---- 照片 ----
    @abstractmethod
    def put_photo(self, pid: str, data: bytes, mime: str = "image/jpeg") -> str:
        """存原图，返回引用（本地是相对路径，云端是 id）。"""

    @abstractmethod
    def put_thumb(self, pid: str, data: bytes) -> str:
        """存缩略图，返回引用。"""

    @abstractmethod
    def get_photo(self, ref: str) -> bytes | None:
        """按引用取原图字节。"""

    @abstractmethod
    def get_thumb(self, ref: str) -> bytes | None:
        """按引用取缩略图字节。"""

    @abstractmethod
    def delete_photo(self, ref: str) -> None:
        ...

    def describe(self) -> str:
        return f"{self.kind} 存储"


# ---------------------------------------------------------------- 本地实现


class LocalStore(Store):
    """本地文件存储：投稿写在 data/submissions.jsonl，照片写 data/photos、data/thumbs。"""

    kind = "local"

    def __init__(self, root: Path | None = None):
        self.root = Path(root) if root else DATA
        self.subs_path = self.root / "submissions.jsonl"
        self.photos_dir = self.root / "photos"
        self.thumbs_dir = self.root / "thumbs"
        for d in (self.root, self.photos_dir, self.thumbs_dir):
            d.mkdir(parents=True, exist_ok=True)

    # ---- 投稿 ----
    def read_submissions(self) -> list:
        from submission_data import Submission

        if not self.subs_path.exists():
            return []
        out = []
        for line in self.subs_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(Submission.from_dict(json.loads(line)))
            except Exception:
                continue
        return out

    def write_submissions(self, subs: Iterable) -> int:
        subs = list(subs)
        tmp = self.subs_path.with_suffix(".jsonl.tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            for s in subs:
                fh.write(json.dumps(s.to_dict(), ensure_ascii=False) + "\n")
        tmp.replace(self.subs_path)
        return len(subs)

    # ---- 照片 ----
    def put_photo(self, pid: str, data: bytes, mime: str = "image/jpeg") -> str:
        ext = {"image/png": ".png", "image/webp": ".webp", "image/tiff": ".tif"}.get(mime, ".jpg")
        name = f"{_safe_id(pid)}{ext}"
        (self.photos_dir / name).write_bytes(data)
        return f"photos/{name}"

    def put_thumb(self, pid: str, data: bytes) -> str:
        name = f"{_safe_id(pid)}.jpg"
        (self.thumbs_dir / name).write_bytes(data)
        return name

    def get_photo(self, ref: str) -> bytes | None:
        p = self.root / ref if ref else None
        if not p:
            return None
        p = p if p.is_absolute() else self.root / ref
        return p.read_bytes() if p.exists() else None

    def get_thumb(self, ref: str) -> bytes | None:
        if not ref:
            return None
        p = Path(ref)
        p = p if p.is_absolute() else self.thumbs_dir / p.name
        return p.read_bytes() if p.exists() else None

    def delete_photo(self, ref: str) -> None:
        if not ref:
            return
        p = Path(ref)
        if not p.is_absolute():
            p = (self.root / ref) if "/" in ref else (self.thumbs_dir / p.name)
        p.unlink(missing_ok=True)

    def thumb_path(self, ref: str) -> Path | None:
        """本地专用：直接拿到缩略图文件路径（云端没有这个概念）。"""
        if not ref:
            return None
        p = Path(ref)
        p = p if p.is_absolute() else self.thumbs_dir / p.name
        return p if p.exists() else None

    def describe(self) -> str:
        return f"本地文件存储（{self.root}）"


# ---------------------------------------------------------------- Supabase 实现


class SupabaseStore(Store):
    """Supabase 后端。

    表结构（在 Supabase SQL Editor 里执行 deploy/supabase_schema.sql）：
        submissions(sid text pk, data jsonb, updated_at timestamptz)
        photos(id text pk, mime text, origin text, thumb text)

    照片直接以 base64 存在数据库里 —— 几百幅作品的量级（免费层 500MB）够用，
    而且只需要一对 URL/KEY，比再配一套对象存储简单得多。
    """

    kind = "supabase"

    def __init__(self, url: str, key: str, timeout: int = 30):
        import requests  # 局部导入：本地模式不需要它

        self._requests = requests
        self.url = url.rstrip("/")
        self.key = key
        self.timeout = timeout

    # ---- 底层 HTTP ----
    def _headers(self, extra: dict | None = None) -> dict:
        """构造 PostgREST 请求头。

        密钥分两代，请求头写法不同（Supabase 2026 年底弃用旧密钥）：
          * 旧版 JWT（`eyJ...`，即 anon / service_role）：必须同时放在
            `apikey` 和 `Authorization: Bearer` 里，PostgREST 靠 Bearer
            解出的 JWT 决定 Postgres 角色（service_role 才能绕过 RLS）。
          * 新版非 JWT（`sb_secret_...` / `sb_publishable_...`）：官方明确
            "无法在 Authorization: Bearer 中发送，除非与 apikey 完全相等，
            否则会被拒绝，因为该值不是 JWT"。所以只发 `apikey`，不发
            Authorization，由平台边缘层按密钥解析权限。
        """
        h = {"apikey": self.key, "Content-Type": "application/json"}
        if not self._is_new_key_format(self.key):
            h["Authorization"] = f"Bearer {self.key}"
        if extra:
            h.update(extra)
        return h

    @staticmethod
    def _is_new_key_format(key: str) -> bool:
        return (key or "").strip().startswith(("sb_secret_", "sb_publishable_"))

    def _rest(self, path: str) -> str:
        return f"{self.url}/rest/v1/{path}"

    def _get(self, path: str, params: dict | None = None) -> list:
        r = self._requests.get(self._rest(path), headers=self._headers(), params=params,
                               timeout=self.timeout)
        r.raise_for_status()
        return r.json() if r.text else []

    def _post(self, path: str, rows, *, upsert: bool = False) -> None:
        extra = {"Prefer": "resolution=merge-duplicates,return=minimal"} if upsert else \
                {"Prefer": "return=minimal"}
        r = self._requests.post(self._rest(path), headers=self._headers(extra),
                                data=json.dumps(rows, ensure_ascii=False).encode("utf-8"),
                                timeout=self.timeout)
        r.raise_for_status()

    def _delete(self, path: str, params: dict) -> None:
        r = self._requests.delete(self._rest(path), headers=self._headers(),
                                  params=params, timeout=self.timeout)
        r.raise_for_status()

    def ping(self) -> tuple[bool, str]:
        """连通性自检，部署时很有用。"""
        try:
            self._get(TABLE_SUBS, {"select": "sid", "limit": 1})
            return True, "Supabase 连接正常"
        except Exception as exc:
            return False, f"{type(exc).__name__}: {exc}"

    # ---- 投稿 ----
    def read_submissions(self) -> list:
        from submission_data import Submission

        rows = self._get(TABLE_SUBS, {"select": "sid,data", "order": "sid.asc"})
        out = []
        for row in rows:
            payload = row.get("data")
            if isinstance(payload, str):
                try:
                    payload = json.loads(payload)
                except Exception:
                    continue
            if not isinstance(payload, dict):
                continue
            try:
                out.append(Submission.from_dict(payload))
            except Exception:
                continue
        return out

    def write_submissions(self, subs: Iterable) -> int:
        subs = list(subs)
        rows = [{"sid": s.sid, "data": s.to_dict()} for s in subs]
        if rows:
            # 分批 upsert，避免单次请求过大
            for i in range(0, len(rows), 50):
                self._post(TABLE_SUBS, rows[i:i + 50], upsert=True)
        # 删掉云端已不存在的记录（比如管理员删了稿）
        keep = {r["sid"] for r in rows}
        existing = {r["sid"] for r in self._get(TABLE_SUBS, {"select": "sid"})}
        for gone in existing - keep:
            self._delete(TABLE_SUBS, {"sid": f"eq.{gone}"})
            self._delete(TABLE_PHOTOS, {"id": f"eq.{_safe_id(gone)}"})
        return len(subs)

    # ---- 照片 ----
    def _put_blob(self, pid: str, origin: bytes | None, thumb: bytes | None,
                  mime: str = "image/jpeg") -> None:
        row: dict = {"id": _safe_id(pid), "mime": mime}
        if origin is not None:
            row["origin"] = base64.b64encode(origin).decode("ascii")
        if thumb is not None:
            row["thumb"] = base64.b64encode(thumb).decode("ascii")
        self._post(TABLE_PHOTOS, row, upsert=True)

    def put_photo(self, pid: str, data: bytes, mime: str = "image/jpeg") -> str:
        sid = _safe_id(pid)
        self._put_blob(sid, origin=data, thumb=None, mime=mime)
        return sid

    def put_thumb(self, pid: str, data: bytes) -> str:
        sid = _safe_id(pid)
        self._put_blob(sid, origin=None, thumb=data)
        return sid

    def _fetch_blob(self, ref: str, column: str) -> bytes | None:
        if not ref:
            return None
        rows = self._get(TABLE_PHOTOS, {"select": column, "id": f"eq.{_safe_id(ref)}", "limit": 1})
        if not rows:
            return None
        val = self._get_one(rows[0], column)
        if not val:
            return None
        try:
            return base64.b64decode(val)
        except Exception:
            return None

    @staticmethod
    def get_one(row: dict, column: str):
        return row.get(column)

    def get_photo(self, ref: str) -> bytes | None:
        return self._fetch_blob(ref, "origin")

    def get_thumb(self, ref: str) -> bytes | None:
        return self._fetch_blob(ref, "thumb")

    def delete_photo(self, ref: str) -> None:
        if ref:
            self._delete(TABLE_PHOTOS, {"id": f"eq.{_safe_id(ref)}"})

    def describe(self) -> str:
        return f"Supabase 云端存储（{self.url.split('//')[-1].split('.')[0]}…）"


# ---------------------------------------------------------------- 工厂


_STORE: Store | None = None


def supabase_config() -> tuple[str, str] | None:
    """从环境变量或 Streamlit secrets 读 Supabase 配置。"""
    url = key = ""
    try:
        url = os.environ.get("SUPABASE_URL", "") or ""
        key = os.environ.get("SUPABASE_KEY", "") or ""
    except Exception:
        pass
    if not (url and key):
        try:
            import streamlit as st

            url = url or str(st.secrets.get("SUPABASE_URL", "") or "")
            key = key or str(st.secrets.get("SUPABASE_KEY", "") or "")
        except Exception:
            pass
    return (url.strip(), key.strip()) if url and key else None


def get_store(force: Store | None = None) -> Store:
    """取当前生效的存储后端（进程内缓存）。"""
    global _STORE
    if force is not None:
        _STORE = force
        return _STORE
    if _STORE is not None:
        return _STORE
    cfg = supabase_config()
    _STORE = SupabaseStore(*cfg) if cfg else LocalStore()
    return _STORE


def reset_store() -> None:
    """切换配置后强制重新判断（测试用）。"""
    global _STORE
    _STORE = None


def storage_kind() -> str:
    return get_store().kind
