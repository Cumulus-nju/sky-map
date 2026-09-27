"""「天光云影」打卡点地图 —— 数据层。

职责：
1. 定义投稿记录（Submission）的统一数据结构；
2. 把"位置信息"归一化成经纬度（自动定点）：
   地图点选 > 照片 EXIF GPS > 文字描述模糊匹配地标；
3. 加载 OSM 建筑/路网底图，生成机位词典；
4. 把邻近投稿聚合成"打卡点"，供地图渲染。

坐标统一 WGS84。
"""
from __future__ import annotations

import csv
import json
import math
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable

from store import EXPORTS, HERE, RAW, THUMBS, DATA  # noqa: F401  目录常量统一在 store 里定义

SUBMISSIONS = DATA / "submissions.jsonl"

# ---------------------------------------------------------------- 位置归一化

# 常见"位置描述"里的噪声词，匹配前先剥离。
# 分两类：机构/口语前缀，以及方位距离后缀（"图书馆南侧" → "图书馆"）。
_STOPWORDS = (
    # 机构与口语前缀
    "南京大学", "南大", "校区", "附近", "旁边", "边上", "门口", "楼顶", "楼顶平台",
    "拍摄", "机位", "打卡", "这里", "那边", "楼上", "楼下", "前面", "后面",
    # 方位（长词在前，避免残留单字）
    "东南角", "西南角", "东北角", "西北角",
    "东南侧", "西南侧", "东北侧", "西北侧",
    "东侧", "南侧", "西侧", "北侧",
    "东南", "西南", "东北", "西北",
    "东边", "南边", "西边", "北边",
    "左侧", "右侧", "前方", "后方", "对面", "内侧", "外侧",
    "附近一带", "一带", "之间", "中间",
    "南面", "北面", "东面", "西面",
    "正门", "侧门",
    # 距离与量词
    "大约", "大概", "左右", "附近处", "处", "附近点",
)

# 只有出现在末尾时才剥离的方位词。注意不能误伤"北大楼""东大楼""大礼堂"这类真实楼名，
# 所以只处理明确的"方位后缀词组"，不做单字裁剪。
_SUFFIX_WORDS = ("方向", "一侧", "一侧的", "一带", "附近处", "的", "之")


def clean_text(text: str) -> str:
    t = (text or "").strip()
    t = re.sub(r"[\s,，。;；、/\\|]+", "", t)
    for w in _STOPWORDS:
        t = t.replace(w, "")
    for w in _SUFFIX_WORDS:
        if t.endswith(w) and len(t) - len(w) >= 2:
            t = t[: -len(w)]
    return t


def haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """两点距离（米）。"""
    r = 6371008.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _bigrams(s: str) -> set[str]:
    if len(s) < 2:
        return {s} if s else set()
    return {s[i : i + 2] for i in range(len(s) - 1)}


def similarity(a: str, b: str) -> float:
    """中文友好的相似度：二元组 Dice 系数 + 子串包含加成。"""
    a, b = clean_text(a), clean_text(b)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if a in b or b in a:
        base = min(len(a), len(b)) / max(len(a), len(b))
        return max(0.82, base * 0.95)
    ba, bb = _bigrams(a), _bigrams(b)
    if not ba or not bb:
        return 0.0
    return 2 * len(ba & bb) / (len(ba) + len(bb))


# ---------------------------------------------------------------- 数据结构


@dataclass
class Submission:
    """一份投稿。lat/lon 为最终定点结果（可能来自点选、EXIF 或文字匹配）。"""

    sid: str                                  # 投稿编号，如 P0001
    campus: str                               # gulou / xianlin / suzhou
    lat: float | None = None
    lon: float | None = None
    picked_lat: float | None = None           # 同学在地图上点选的纬度
    picked_lon: float | None = None
    exif_lat: float | None = None
    exif_lon: float | None = None
    loc_text: str = ""                        # 同学手填的位置描述
    loc_matched: str = ""                     # 自动匹配到的地标名
    loc_score: float = 0.0
    loc_source: str = "none"                  # picked / exif / text / none
    loc_verify: bool = False                  # 是否落点在校区框外，待复核
    title: str = ""
    author: str = ""
    contact: str = ""
    shot_time: str = ""
    weather: str = ""                         # 天气现象：晚霞/积云/虹/月…
    camera: str = ""
    note: str = ""
    # 照片引用：photo 是原图（本地为相对路径），thumb 是缩略图文件名
    photo: str = ""
    photo_data: str = ""                      # data URL（用于单文件离线版）
    thumb: str = ""                           # 缩略图文件名（本地）
    thumb_ref: str = ""                       # 存储层引用（云端为 photos 表 id）
    submitted_at: str = ""
    status: str = "已收稿"
    award: str = ""                           # 一等奖 / 人气奖 …
    is_demo: bool = False

    @property
    def has_point(self) -> bool:
        return self.lat is not None and self.lon is not None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Submission":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in known})


@dataclass
class Landmark:
    name: str
    lat: float
    lon: float
    campus: str
    kind: str = ""
    source: str = "osm"


# ---------------------------------------------------------------- 底图与词典


def _load_geojson(path: Path) -> dict:
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


def _polygon_rings(geom: dict) -> list[list[list[float]]]:
    """把 Polygon / MultiPolygon 统一成环列表（用于内嵌进 HTML）。"""
    if not geom:
        return []
    t = geom.get("type")
    coords = geom.get("coordinates") or []
    if t == "Polygon":
        return [coords[0]] if coords else []
    if t == "MultiPolygon":
        return [poly[0] for poly in coords if poly]
    return []


def load_buildings(campus_key: str) -> list[dict]:
    """返回 [{name, rings, levels, height, kind, center}]。"""
    path = RAW / f"{campus_key}_buildings.geojson"
    if not path.exists():
        return []
    out: list[dict] = []
    for feat in _load_geojson(path).get("features", []):
        rings = _polygon_rings(feat.get("geometry"))
        if not rings:
            continue
        props = feat.get("properties") or {}
        flat = [pt for ring in rings for pt in ring]
        if not flat:
            continue
        lon = sum(p[0] for p in flat) / len(flat)
        lat = sum(p[1] for p in flat) / len(flat)
        out.append(
            {
                "name": (props.get("name") or "").strip(),
                "rings": rings,
                "levels": props.get("building:levels") or "",
                "height": props.get("height") or "",
                "kind": props.get("building") or "",
                "center": [round(lat, 7), round(lon, 7)],
            }
        )
    return out


def build_landmark_index() -> dict[str, list[Landmark]]:
    """汇总三校区机位词典：配置里的手工地标 + OSM 实名建筑。"""
    from campus_config import CAMPUSES

    index: dict[str, list[Landmark]] = {}
    for key, cfg in CAMPUSES.items():
        marks = [Landmark(n, la, lo, key, kind="手编地标", source="manual") for n, (la, lo) in cfg.landmarks.items()]
        seen = {clean_text(m.name) for m in marks}
        for b in load_buildings(key):
            name = b["name"]
            if not name:
                continue
            ck = clean_text(name)
            if not ck or ck in seen:
                continue
            buildings = b["kind"] or ""
            marks.append(Landmark(name, b["center"][0], b["center"][1], key, kind=buildings, source="osm"))
            seen.add(ck)
        index[key] = marks
    return index


def resolve_location(
    *,
    campus_key: str,
    picked: tuple[float, float] | None = None,
    exif: tuple[float, float] | None = None,
    text: str = "",
    index: dict[str, list[Landmark]] | None = None,
) -> dict:
    """自动定点：按可信度依次尝试 点选 → EXIF → 文字匹配。

    返回 dict(lat, lon, source, matched, score, verify)。
    """
    from campus_config import campus, in_bbox

    cfg = campus(campus_key)

    if picked and picked[0] is not None and picked[1] is not None:
        lat, lon = picked
        verify = not in_bbox(campus_key, lat, lon, pad=0.004)
        return {
            "lat": lat, "lon": lon, "source": "picked", "matched": "地图点选",
            "score": 1.0, "verify": verify,
        }

    if exif and exif[0] is not None and exif[1] is not None:
        lat, lon = exif
        # EXIF 可能落在外地（照片不是在校区拍的），落在框外就降级为待复核
        verify = not in_bbox(campus_key, lat, lon, pad=0.004)
        return {
            "lat": lat, "lon": lon, "source": "exif", "matched": "照片 EXIF 定位",
            "score": 0.9 if not verify else 0.5, "verify": verify,
        }

    if text.strip():
        idx = index if index is not None else build_landmark_index()
        best, best_score = None, 0.0
        for m in idx.get(campus_key, []):
            s = similarity(text, m.name)
            # 配置里的手编地标（经典机位）优先一点点，避免被同名/近似建筑抢走
            if m.source == "manual":
                s = min(1.0, s + 0.05)
            if s > best_score:
                best, best_score = m, s
        # 手编地标优先：分数相同时取手编
        if best and best_score >= 0.55:
            return {
                "lat": best.lat, "lon": best.lon, "source": "text",
                "matched": best.name, "score": round(best_score, 3), "verify": best_score < 0.72,
            }
        if best:
            # 匹配太弱：仍给出最佳猜测，但标记待复核
            return {
                "lat": best.lat, "lon": best.lon, "source": "text",
                "matched": best.name + "（低置信度）", "score": round(best_score, 3), "verify": True,
            }

    return {"lat": None, "lon": None, "source": "none", "matched": "", "score": 0.0, "verify": True}


# ---------------------------------------------------------------- 读写


def _store():
    """当前生效的存储后端（本地文件 / Supabase 云端）。"""
    from store import get_store

    return get_store()


def load_submissions() -> list[Submission]:
    return _store().read_submissions()


def save_submissions(subs: Iterable[Submission]) -> int:
    return _store().write_submissions(list(subs))


def next_sid(existing: Iterable[Submission]) -> str:
    mx = 0
    for s in existing:
        m = re.match(r"P(\d+)$", s.sid or "")
        if m:
            mx = max(mx, int(m.group(1)))
    return f"P{mx + 1:04d}"


FIELDS = list(Submission.__dataclass_fields__)

# 管理端可编辑的字段
EDITABLE = (
    "campus", "lat", "lon", "loc_text", "loc_matched", "loc_verify",
    "title", "author", "contact", "shot_time", "weather", "camera",
    "note", "status", "award",
)


# ---------------------------------------------------------------- 照片读写


def photo_bytes(sub: Submission, which: str = "thumb") -> bytes | None:
    """按引用取照片字节。which='thumb' 缩略图，'origin' 原图。"""
    st = _store()
    if which == "thumb":
        return st.get_thumb(sub.thumb_ref or sub.thumb)
    return st.get_photo(sub.photo)


def thumb_local_path(sub: Submission) -> Path | None:
    """本地模式下的缩略图文件路径；云端模式返回 None（改用 photo_bytes）。"""
    st = _store()
    if hasattr(st, "thumb_path"):
        return st.thumb_path(sub.thumb_ref or sub.thumb)
    return None


def thumb_display_src(sub: Submission, max_side: int = 900) -> str | bytes | None:
    """给 st.image / st.dataframe 用的缩略图：本地给路径，云端给字节。"""
    p = thumb_local_path(sub)
    if p is not None:
        return str(p)
    data = photo_bytes(sub, "thumb")
    return data or None


def update_submission(sid: str, **changes) -> Submission | None:
    """按编号修改一条投稿，落盘后返回修改后的记录。"""
    subs = load_submissions()
    target = None
    for s in subs:
        if s.sid == sid:
            for k, v in changes.items():
                if k in FIELDS:
                    setattr(s, k, v)
            target = s
            break
    if target is not None:
        save_submissions(subs)
    return target


def delete_submission(sid: str, *, purge_files: bool = False) -> bool:
    """按编号删除一条投稿。

    purge_files=True 时，若该照片/缩略图已无其他投稿引用，一并删掉，
    避免存储越积越大（云端也算配额）。
    """
    st = _store()
    subs = st.read_submissions()
    keep = [s for s in subs if s.sid != sid]
    if len(keep) == len(subs):
        return False
    removed = next(s for s in subs if s.sid == sid)
    st.write_submissions(keep)

    if purge_files:
        still_thumbs = {s.thumb_ref or s.thumb for s in keep}
        still_photos = {s.photo for s in keep}
        if (removed.thumb_ref or removed.thumb) not in still_thumbs:
            st.delete_photo(removed.thumb_ref or removed.thumb)
        if removed.photo and removed.photo not in still_photos:
            st.delete_photo(removed.photo)
    return True


def regenerate_thumb(sid: str) -> tuple[bool, str]:
    """按原图重新生成缩略图（比如原图换了、或缩略图损坏）。"""
    from photos import make_thumb_bytes

    subs = load_submissions()
    for s in subs:
        if s.sid != sid:
            continue
        origin = photo_bytes(s, "origin")
        if not origin:
            return False, f"原图不存在：{s.photo}"
        import io

        try:
            ref = _store().put_thumb(_safe_id(s.sid),
                                     make_thumb_bytes(io.BytesIO(origin)))
        except Exception as exc:
            return False, f"{type(exc).__name__}: {exc}"
        s.thumb = Path(ref).name
        s.thumb_ref = ref
        save_submissions(subs)
        return True, ref
    return False, "找不到该投稿"


def _safe_id(raw: str) -> str:
    from store import _safe_id as f

    return f(raw)


def export_csv(subs: list[Submission], path: Path | None = None) -> Path:
    path = path or (EXPORTS / "打卡点_投稿汇总.csv")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS)
        w.writeheader()
        for s in subs:
            row = s.to_dict()
            row.pop("photo_data", None)
            w.writerow(row)
    return path


# ---------------------------------------------------------------- 打卡点聚类


@dataclass
class Spot:
    """一个打卡点：若干位置相近的投稿聚合而成。"""

    sid: str
    campus: str
    lat: float
    lon: float
    name: str
    count: int
    submissions: list[Submission] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "sid": self.sid,
            "campus": self.campus,
            "lat": self.lat,
            "lon": self.lon,
            "name": self.name,
            "count": self.count,
            "submissions": [s.to_dict() for s in self.submissions],
        }


def _nearest_landmark_name(lat: float, lon: float, marks: list[Landmark], max_m: float = 220.0) -> str:
    best, best_d = "", 1e18
    for m in marks:
        d = haversine(lat, lon, m.lat, m.lon)
        if d < best_d:
            best, best_d = m.name, d
    return best if best_d <= max_m else ""


def cluster_spots(
    subs: list[Submission],
    *,
    radius_m: float = 60.0,
    index: dict[str, list[Landmark]] | None = None,
) -> list[Spot]:
    """按校区把 60 m 内的投稿并成一个打卡点（单链聚类，简单且够用）。"""
    idx = index if index is not None else build_landmark_index()
    spots: list[Spot] = []
    by_campus: dict[str, list[Submission]] = {}
    for s in subs:
        if s.has_point:
            by_campus.setdefault(s.campus, []).append(s)

    for ck, items in by_campus.items():
        # 已有点列表：[lat, lon, [投稿...]]
        clusters: list[list] = []
        for s in items:
            placed = False
            for c in clusters:
                if haversine(s.lat, s.lon, c[0], c[1]) <= radius_m:
                    c[2].append(s)
                    n = len(c[2])
                    c[0] = sum(x.lat for x in c[2]) / n
                    c[1] = sum(x.lon for x in c[2]) / n
                    placed = True
                    break
            if not placed:
                clusters.append([s.lat, s.lon, [s]])
        clusters.sort(key=lambda c: -len(c[2]))
        for i, c in enumerate(clusters, 1):
            name = _nearest_landmark_name(c[0], c[1], idx.get(ck, []))
            spots.append(
                Spot(
                    sid=f"{ck.upper()}-{i:02d}",
                    campus=ck,
                    lat=round(c[0], 7),
                    lon=round(c[1], 7),
                    name=name,
                    count=len(c[2]),
                    submissions=c[2],
                )
            )
    return spots
