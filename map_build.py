"""「天光云影」校园天空打卡点地图 —— HTML 生成器。

产出单文件 map.html（可直接发公众号 / 挂网页 / U 盘带走）：
  * 左侧：照片墙 + 打卡点清单，点条目联动地图
  * 右侧：Leaflet 交互地图，三种底图 —— OSM 街道 / 高德卫星 / 矢量建筑
  * 高德图层是 GCJ-02 坐标系，渲染时按校区自动纠偏（否则点位整体飘 500 m）
  * 每个打卡点：编号、照片、拍摄时间、天气现象、机位提示
  * 支持按校区 / 天气现象 / 关键词过滤，筛选状态可复制成链接

用法：
    python map_build.py               # 在线版（在线瓦片，体积小、底图漂亮）
    python map_build.py --offline     # 离线单文件版（照片内嵌 + 矢量底图）
"""
from __future__ import annotations

import argparse
import html
import json
import math
import shutil
from datetime import datetime
from pathlib import Path

from campus_config import CAMPUSES, frame_of, frame_size_m
from submission_data import (
    EXPORTS,
    HERE,
    THUMBS,
    Landmark,
    Spot,
    Submission,
    build_landmark_index,
    cluster_spots,
    export_csv,
    load_buildings,
    load_submissions,
    photo_bytes,
    thumb_local_path,
)
from photos import thumb_dataurl, image_to_dataurl

# ---------------------------------------------------------------- GCJ-02 纠偏
# 高德瓦片是 GCJ-02，与 WGS84 差 300~600 m。这里按校区算出 EPSG:3857
# 平面下的偏移量（米），传给前端做瓦片坐标偏移与点位补偿。

_PI = math.pi
_A = 6378245.0
_EE = 0.00669342162296594323


def _out_of_china(lat: float, lon: float) -> bool:
    return not (72.004 <= lon <= 137.8347 and 0.8293 <= lat <= 55.8271)


def _tf_lat(x: float, y: float) -> float:
    ret = -100.0 + 2.0 * x + 3.0 * y + 0.2 * y * y + 0.1 * x * y + 0.2 * math.sqrt(abs(x))
    ret += (20.0 * math.sin(6.0 * x * _PI) + 20.0 * math.sin(2.0 * x * _PI)) * 2.0 / 3.0
    ret += (20.0 * math.sin(y * _PI) + 40.0 * math.sin(y / 3.0 * _PI)) * 2.0 / 3.0
    ret += (160.0 * math.sin(y / 12.0 * _PI) + 320 * math.sin(y * _PI / 30.0)) * 2.0 / 3.0
    return ret


def _tf_lon(x: float, y: float) -> float:
    ret = 300.0 + x + 2.0 * y + 0.1 * x * x + 0.1 * x * y + 0.1 * math.sqrt(abs(x))
    ret += (20.0 * math.sin(6.0 * x * _PI) + 20.0 * math.sin(2.0 * x * _PI)) * 2.0 / 3.0
    ret += (20.0 * math.sin(x * _PI) + 40.0 * math.sin(x / 3.0 * _PI)) * 2.0 / 3.0
    ret += (150.0 * math.sin(x / 12.0 * _PI) + 300.0 * math.sin(x / 30.0 * _PI)) * 2.0 / 3.0
    return ret


def wgs84_to_gcj02(lat: float, lon: float) -> tuple[float, float]:
    if _out_of_china(lat, lon):
        return lat, lon
    dlat = _tf_lat(lon - 105.0, lat - 35.0)
    dlon = _tf_lon(lon - 105.0, lat - 35.0)
    rad = lat / 180.0 * _PI
    magic = 1 - _EE * math.sin(rad) ** 2
    sqrt_magic = math.sqrt(magic)
    dlat = (dlat * 180.0) / ((_A * (1 - _EE)) / (magic * sqrt_magic) * _PI)
    dlon = (dlon * 180.0) / (_A / sqrt_magic * math.cos(rad) * _PI)
    return lat + dlat, lon + dlon


def gcj_offset_meters(lat: float, lon: float) -> tuple[float, float]:
    """WGS84 → GCJ-02 的 EPSG:3857 平面偏移 (dx, dy)，单位米。"""
    glat, glon = wgs84_to_gcj02(lat, lon)
    world = 2 * _PI * 6378137.0
    mx = world / 256.0                       # 每像素米数（z=0）

    def project(la: float, lo: float) -> tuple[float, float]:
        x = lo * world / 360.0
        y = math.log(math.tan((90 + la) * _PI / 360.0)) * 6378137.0
        return x, y

    x1, y1 = project(lat, lon)
    x2, y2 = project(glat, glon)
    return x2 - x1, y2 - y1


# 天气现象分类，用于图例与配色
WEATHER_KINDS = {
    "晚霞": {"label": "晚霞 / 火烧云", "color": "#ff6b3d"},
    "朝霞": {"label": "朝霞 / 日出", "color": "#ffab3d"},
    "积云": {"label": "积云 / 蓝天白云", "color": "#4a9eff"},
    "层云": {"label": "层云 / 阴天", "color": "#8b9bb4"},
    "雨虹": {"label": "雨 / 虹 / 霓", "color": "#48c9b0"},
    "雷电": {"label": "雷电 / 强对流", "color": "#a56bff"},
    "月相": {"label": "月亮 / 星空", "color": "#3b4fa8"},
    "雾霾": {"label": "雾 / 霾 / 平流雾", "color": "#b0a08a"},
    "其他": {"label": "其他天象", "color": "#7f8c9b"},
}


def classify_weather(text: str) -> str:
    """把自由填写的天气现象归到图例类别。"""
    t = text or ""
    table = [
        ("晚霞", ("晚霞", "火烧云", "日落", "夕阳", "晚")),
        ("朝霞", ("朝霞", "日出", "晨曦", "清晨")),
        ("雨虹", ("虹", "霓", "雨", "彩虹", "阵雨")),
        ("雷电", ("雷", "闪电", "对流", "暴雨")),
        ("月相", ("月", "星", "银河", "夜")),
        ("雾霾", ("雾", "霾", "平流", "能见度")),
        ("层云", ("层云", "阴", "层积", "乌云")),
        ("积云", ("积云", "白云", "晴天", "蓝天", "卷云", "高积")),
    ]
    for kind, keys in table:
        if any(k in t for k in keys):
            return kind
    return "其他"


def _spot_weather(spot: Spot) -> str:
    kinds: dict[str, int] = {}
    for s in spot.submissions:
        k = classify_weather(s.weather)
        kinds[k] = kinds.get(k, 0) + 1
    return max(kinds, key=kinds.get) if kinds else "其他"


def _strip_name(name: str) -> str:
    n = (name or "").strip()
    n = n.split(";")[0].split("；")[0]
    n = n.replace("（", "(").split("(")[0]
    return n or "无名建筑"


# ---------------------------------------------------------------- 打包数据


def _relief_dataurl(campus_key: str) -> str:
    """自绘立体底图的 data URL —— **与投稿页"选机位"那张完全同源**。

    为什么要让成品地图也用这一张（用户 2026-10-05 提的）：
    投稿页选机位时看到的是 2.5D 立体底图，而这张成品地图过去是 OSM 街道 /
    高德卫星 —— 同一栋楼在两边长得完全不一样，同学会怀疑"我点的那里到底对不对"。
    换成同一张图，两块屏幕一眼就能对上。

    ⚠ `target_width=2000` 是**故意**的：这个宽度已经预生成并随仓库提交
    （`data/frame_images/*_w2000.*`，由 `tools/test_static_basemap.py` 第 8 节守着）。
    换个宽度，云端容器会**现场重绘** —— 慢，还可能被瓦片源限流（2026-10-01 的教训）。
    """
    from static_basemap import frame_image_for

    return frame_image_for(campus_key, *frame_of(campus_key), target_width=2000).data_url()


def build_payload(subs: list[Submission], *, embed_photos: bool,
                  thumbs_dir: Path | None = None) -> dict:
    """构建地图数据。

    embed_photos=True（离线单文件版）时，把缩略图压成 data URL 内嵌；
    否则在线版：本地模式写成 "thumbs/xxx.jpg" 相对路径，云端模式直接用
    存储层返回的图片字节转 data URL（云端没有可分享的本地文件）。
    """
    from store import storage_kind

    cloud = storage_kind() != "local"
    local_thumbs = Path(thumbs_dir) if thumbs_dir else THUMBS
    index = build_landmark_index()
    spots = cluster_spots(subs, index=index)

    # 在线版是否也要内嵌照片：云端没有本地文件可引用，只能内嵌
    inline = embed_photos or cloud

    spot_payload = []
    for sp in spots:
        shots = []
        for s in sorted(sp.submissions, key=lambda x: x.shot_time or x.submitted_at or ""):
            src = ""
            if inline:
                src = s.photo_data or image_to_dataurl(photo_bytes(s, "thumb"))
            else:
                path = thumb_local_path(s)
                if path is None:
                    path = (local_thumbs / s.thumb) if s.thumb else None
                if path and Path(path).exists():
                    src = f"thumbs/{Path(path).name}"
            shots.append(
                {
                    "sid": s.sid,
                    "title": s.title or "未命名",
                    "author": s.author or "匿名",
                    "time": s.shot_time or "",
                    "weather": s.weather or "",
                    "kind": classify_weather(s.weather),
                    "camera": s.camera or "",
                    "note": s.note or "",
                    "award": s.award or "",
                    "src": src,
                    "demo": bool(s.is_demo),
                }
            )
        spot_payload.append(
            {
                "sid": sp.sid,
                "campus": sp.campus,
                "lat": sp.lat,
                "lon": sp.lon,
                "name": sp.name,
                "count": sp.count,
                "kind": _spot_weather(sp),
                "shots": shots,
                "verify": any(s.loc_verify for s in sp.submissions),
            }
        )

    # 未能定点的投稿单独列出，提醒人工复核
    unresolved = [
        {
            "sid": s.sid, "campus": s.campus, "title": s.title or "未命名",
            "loc_text": s.loc_text, "matched": s.loc_matched, "author": s.author or "匿名",
        }
        for s in subs
        if not s.has_point
    ]

    buildings = {k: load_buildings(k) for k in CAMPUSES}

    def tile_cfg(src) -> dict:
        return {
            "name": src.name, "url": src.url, "attr": src.attr,
            "gcj02": src.gcj02, "subdomains": list(src.subdomains), "maxZoom": src.max_zoom,
        }

    # 立体底图：逐校区取，缺一个就只让那个校区退回在线瓦片，**整张地图不能因此挂掉**
    relief: dict[str, str | None] = {}
    for k in CAMPUSES:
        try:
            relief[k] = _relief_dataurl(k)
        except Exception as exc:   # noqa: BLE001 —— 底图是"锦上添花"，绝不能拖垮成品页
            print(f"  ⚠ {k} 的立体底图不可用（{type(exc).__name__}: {exc}），该校区退回在线瓦片")
            relief[k] = None

    return {
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "campuses": {
            k: {
                "key": k, "name": c.name, "short": c.short,
                "center": list(c.center), "zoom": c.zoom, "bbox": list(c.bbox),
                # 地图外框：校园 + 周边缓冲，视野锁定在此框内（只可放大、不可缩出）
                "frame": list(frame_of(k)),
                "frameSize": list(frame_size_m(k)),
                "streets": tile_cfg(c.streets), "imagery": tile_cfg(c.imagery),
                # 自绘立体底图（data URL）：直接按 frame 四角贴上去就是严丝合缝的
                "relief": relief.get(k),
                # 高德图层需要的 GCJ-02 纠偏量（米），EPSG:3857 平面
                "offsets": {
                    "street": list(gcj_offset_meters(*c.center)) if c.streets.gcj02 else [0.0, 0.0],
                    "image": list(gcj_offset_meters(*c.center)) if c.imagery.gcj02 else [0.0, 0.0],
                },
                "accuracy": c.landmark_accuracy, "osmNote": c.osm_note,
                "landmarks": {n: list(v) for n, v in c.landmarks.items()},
            }
            for k, c in CAMPUSES.items()
        },
        "spots": spot_payload,
        "unresolved": unresolved,
        "buildings": buildings,
        "weatherKinds": WEATHER_KINDS,
        "stats": {
            "submissions": len(subs),
            "located": sum(1 for s in subs if s.has_point),
            "spots": len(spots),
            "authors": len({s.author for s in subs if s.author}),
            "byCampus": {k: sum(1 for s in subs if s.campus == k) for k in CAMPUSES},
        },
    }


# ---------------------------------------------------------------- HTML


PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=5">
<title>天光云影 · 南大校园天空打卡点地图</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<style>
*{box-sizing:border-box;margin:0;padding:0}
:root{
  --ink:#101418; --ink2:#2b3540; --muted:#6b7a8c; --line:rgba(120,140,165,.22);
  --glass:rgba(255,255,255,.86); --accent:#e0713c; --accent2:#2f6fb5;
  --dawn1:#1b2f5c; --dawn2:#4a5f9e; --dawn3:#c98b6b; --dawn4:#f0c188;
}
html,body{height:100%}
body{
  font:14px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Hiragino Sans GB","Microsoft YaHei",sans-serif;
  color:var(--ink); background:#eef1f5; overflow:hidden;
}
#app{display:flex;height:100vh}

/* 嵌入模式（?embed=1，用于 Streamlit 成品页）：外页已有标题和筛选说明，
   这里把大标题/统计/筛选都收起来，只留地图 + 一个悬浮校区切换。 */
body.embed .hero,body.embed #filters{display:none}
body.embed #side{width:0;min-width:0;overflow:hidden;border:0}
#campusFloat{
  display:none;position:absolute;left:50%;top:12px;transform:translateX(-50%);z-index:600;
  background:var(--glass);backdrop-filter:blur(9px);border-radius:999px;padding:4px;
  box-shadow:0 3px 16px rgba(15,30,60,.18);gap:3px;
}
body.embed #campusFloat{display:flex}
#campusFloat .tab{border:0;padding:5px 14px;font-size:12.5px}

/* ---------- 左栏 ---------- */
#side{
  width:390px;min-width:390px;height:100%;display:flex;flex-direction:column;
  background:#fbfcfe;border-right:1px solid var(--line);z-index:600;
}
#hero{
  position:relative;padding:20px 20px 16px;color:#fff;overflow:hidden;
  background:linear-gradient(160deg,var(--dawn1) 0%,var(--dawn2) 42%,var(--dawn3) 74%,var(--dawn4) 100%);
}
#hero .cloud{position:absolute;inset:0;opacity:.5;pointer-events:none}
#hero h1{position:relative;font-size:20px;letter-spacing:.06em;font-weight:700}
#hero .sub{position:relative;margin-top:6px;font-size:11.5px;opacity:.88;letter-spacing:.03em}
#stat{position:relative;display:flex;gap:14px;margin-top:14px;flex-wrap:wrap}
#stat b{font-size:19px;font-weight:700;line-height:1.1}
#stat span{display:block;font-size:10.5px;opacity:.82;letter-spacing:.04em}

#filters{padding:12px 16px;border-bottom:1px solid var(--line);background:#fff}
.row{display:flex;gap:6px;align-items:center;flex-wrap:wrap;margin-bottom:8px}
.row:last-child{margin-bottom:0}
.lbl{font-size:11px;color:var(--muted);letter-spacing:.06em;min-width:30px}
.tab{
  border:1px solid var(--line);background:#fff;color:var(--ink2);cursor:pointer;
  border-radius:999px;padding:3px 11px;font-size:11.5px;transition:.15s;font-family:inherit;
}
.tab:hover{border-color:var(--accent2);color:var(--accent2)}
.tab.on{background:var(--ink);border-color:var(--ink);color:#fff}
.chip{
  border-radius:999px;padding:2px 9px;font-size:11px;cursor:pointer;
  border:1px solid var(--line);background:#fff;display:inline-flex;align-items:center;gap:4px;
  font-family:inherit;color:var(--ink2);transition:.15s;
}
.chip:hover{transform:translateY(-1px)}
.chip.on{color:#fff;border-color:transparent}
.dot{width:7px;height:7px;border-radius:50%;display:inline-block}
#q{
  width:100%;border:1px solid var(--line);border-radius:8px;padding:6px 10px;
  font-size:12.5px;font-family:inherit;outline:none;background:#fff;
}
#q:focus{border-color:var(--accent2)}

#list{flex:1;overflow-y:auto;padding:10px 12px 24px;scrollbar-width:thin}
#list::-webkit-scrollbar{width:8px}
#list::-webkit-scrollbar-thumb{background:#c9d3de;border-radius:4px}
.card{
  display:flex;gap:10px;padding:9px;border-radius:11px;cursor:pointer;
  border:1px solid transparent;transition:.15s;background:#fff;margin-bottom:7px;
  box-shadow:0 1px 3px rgba(20,35,60,.06);
}
.card:hover{border-color:var(--accent2);transform:translateX(2px)}
.card.sel{border-color:var(--accent);box-shadow:0 0 0 2px rgba(224,113,60,.18)}
/* 列表缩略图：用 contain 而不是 cover —— 天空照片主体在上半部，
   cover 会把天空裁掉只剩底部建筑剪影，看着像一块黑图。 */
.card img{width:78px;height:78px;object-fit:contain;border-radius:8px;background:#eef2f7;flex:none}
.card .no{
  flex:none;width:26px;height:26px;border-radius:50%;color:#fff;font-size:11px;font-weight:700;
  display:flex;align-items:center;justify-content:center;margin-top:2px;
}
.card .body{min-width:0;flex:1}
.card .ttl{font-weight:600;font-size:13px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.card .meta{font-size:11px;color:var(--muted);margin-top:2px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.card .tags{margin-top:4px;display:flex;gap:4px;flex-wrap:wrap}
.tag{font-size:10px;padding:1px 6px;border-radius:4px;background:#eef2f7;color:var(--ink2)}
.tag.warn{background:#fdece6;color:#b8481f}
.empty{text-align:center;color:var(--muted);padding:40px 20px;font-size:12.5px;line-height:2}

/* ---------- 地图 ---------- */
#main{flex:1;position:relative}
#map{position:absolute;inset:0;background:#0d1520}
/* 自绘立体底图只覆盖**外框**，框外没有瓦片可铺 —— 容器本色（深蓝）会露出来成黑边。
   所以切到立体底图时把底色换成和投稿页组件一样的浅灰（#e9ecef）：
   框外看起来就是一圈"留白"，和选机位那页的观感一致。 */
#map.relief{background:#e9ecef}
.leaflet-popup-content-wrapper{border-radius:12px;padding:0;overflow:hidden;box-shadow:0 8px 30px rgba(10,20,40,.28)}
.leaflet-popup-content{margin:0;width:268px}
.pop img{width:100%;display:block;background:#e5eaf0;max-height:230px;object-fit:cover}
.pop .pb{padding:10px 12px 12px}
.pop h3{font-size:14px;margin-bottom:2px;line-height:1.35}
.pop .au{font-size:11px;color:var(--muted);margin-bottom:7px}
.pop .kv{font-size:11.5px;color:var(--ink2);display:flex;gap:6px;margin-top:3px}
.pop .kv i{font-style:normal;color:var(--muted);flex:none;width:52px}
.pop .tip{
  margin-top:8px;padding:7px 9px;background:#f6f8fb;border-left:3px solid var(--accent);
  border-radius:0 6px 6px 0;font-size:11px;color:var(--ink2);line-height:1.6;
}
.pop .shot{border-top:1px dashed var(--line);margin-top:9px;padding-top:9px}
.pop .shot:first-child{border:0;margin:0;padding:0}
.sp{
  background:transparent;border:0;
}
.sp .pin{
  width:34px;height:34px;border-radius:50% 50% 50% 4px;transform:rotate(-45deg);
  display:flex;align-items:center;justify-content:center;
  box-shadow:0 3px 10px rgba(10,20,40,.35);border:2.5px solid #fff;
}
.sp .pin span{transform:rotate(45deg);color:#fff;font-weight:700;font-size:12px}
.pinwrap{position:relative}
.pinwrap .cover{
  position:absolute;left:26px;top:-4px;width:52px;height:52px;border-radius:9px;
  border:2.5px solid #fff;object-fit:cover;box-shadow:0 3px 10px rgba(10,20,40,.32);background:#dfe5ec;
}
#legend{
  position:absolute;right:12px;bottom:20px;z-index:500;background:var(--glass);
  backdrop-filter:blur(9px);border-radius:11px;padding:10px 12px;font-size:11px;
  box-shadow:0 4px 18px rgba(15,30,60,.16);max-width:190px;
}
#legend h4{font-size:11px;letter-spacing:.08em;color:var(--muted);margin-bottom:6px;font-weight:600}
#legend .li{display:flex;align-items:center;gap:6px;margin-top:3px;cursor:pointer}
#legend .li:hover{color:var(--accent2)}
#topbar{
  position:absolute;left:12px;top:12px;z-index:500;display:flex;gap:7px;align-items:center;
}
#topbar button{
  background:var(--glass);backdrop-filter:blur(9px);border:1px solid var(--line);
  border-radius:8px;padding:5px 10px;font-size:11.5px;cursor:pointer;font-family:inherit;
  color:var(--ink2);box-shadow:0 2px 10px rgba(15,30,60,.1);
}
#topbar button:hover{border-color:var(--accent2);color:var(--accent2)}
#toggleSide{
  position:absolute;left:12px;top:12px;z-index:700;display:none;
  background:var(--glass);border:1px solid var(--line);border-radius:8px;
  padding:6px 11px;font-size:12px;cursor:pointer;font-family:inherit;
}
#toast{
  position:absolute;left:50%;top:14px;transform:translateX(-50%);z-index:900;
  background:rgba(20,30,45,.9);color:#fff;padding:7px 15px;border-radius:999px;
  font-size:12px;opacity:0;transition:.3s;pointer-events:none;
}
#toast.on{opacity:1}
.leaflet-tile-pane{filter:saturate(1.04)}
@media(max-width:820px){
  #side{position:absolute;left:0;top:0;width:86vw;min-width:0;transform:translateX(-100%);transition:.25s}
  #side.open{transform:none;box-shadow:0 0 40px rgba(0,0,0,.3)}
  #toggleSide{display:block}
}
</style>
</head>
<body>
<div id="app">
  <aside id="side">
    <div id="hero">
      <svg class="cloud" viewBox="0 0 400 120" preserveAspectRatio="none">
        <path d="M-20 96 Q40 58 96 78 Q150 30 214 60 Q276 20 340 56 Q384 44 420 74 L420 120 L-20 120Z" fill="rgba(255,255,255,.22)"/>
        <path d="M-20 112 Q60 84 128 98 Q196 66 268 92 Q330 76 420 104 L420 120 L-20 120Z" fill="rgba(255,255,255,.16)"/>
      </svg>
      <h1>天光云影</h1>
      <div class="sub">南京大学校园天空打卡点地图 · __GENERATED__</div>
      <div id="stat"></div>
    </div>
    <div id="filters">
      <div class="row"><span class="lbl">校区</span><span id="campusTabs"></span></div>
      <div class="row"><span class="lbl">天象</span><span id="kindChips"></span></div>
      <div class="row"><input id="q" placeholder="搜索打卡点 / 机位 / 作者 / 作品名…"></div>
    </div>
    <div id="list"></div>
  </aside>

  <main id="main">
    <div id="map"></div>
    <div id="campusFloat"></div>
    <button id="toggleSide">☰ 列表</button>
    <div id="topbar">
      <button id="btnRelief">🏞 立体底图</button>
      <button id="btnStreet">🗺 OSM 街道</button>
      <button id="btnSat">🛰 高德卫星</button>
      <button id="btnVector">⛰ 矢量底图</button>
      <button id="btnFit">⤢ 全览</button>
      <button id="btnTarget">🎯 鼠标定位</button>
      <button id="btnShare">🔗 复制当前视图</button>
    </div>
    <div id="legend">
      <h4>天象图例</h4><div id="legendBody"></div>
      <div id="note" style="margin-top:8px;padding-top:7px;border-top:1px dashed var(--line);color:var(--muted);line-height:1.5"></div>
    </div>
    <div id="toast"></div>
  </main>
</div>

<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script>
const DATA = __PAYLOAD__;
const OFFLINE = __OFFLINE__;
let map, vectorLayer = null, currentCampus = 'all';
let selSpots = [], markerBySid = {}, tileFails = 0, fellBack = false;
let baseLayer = 'none';           // none | relief | street | image | vector
let tileStreet = null, tileImage = null, reliefLayers = [];

const el = (id) => document.getElementById(id);
const esc = (s) => String(s ?? '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));

/* ---------------- 地图初始化 ---------------- */
function initMap(){
  const c = DATA.campuses.gulou;
  map = L.map('map', {zoomControl:true, minZoom:3, maxZoom:19, preferCanvas:false})
          .setView(c.center, c.zoom);
  L.control.scale({imperial:false, position:'bottomleft'}).addTo(map);
  applyFrame('gulou');
  buildVectorLayer();
  applyHash();
}

function cfgOf(ck){ return DATA.campuses[ck === 'all' ? 'gulou' : ck]; }
function offsetOf(which){
  const o = (cfgOf(currentCampus).offsets || {})[which] || [0, 0];
  return { x: o[0], y: o[1] };
}

/* ---------------- 外框（校园 + 周边缓冲）----------------
   框由后端按各校区实际建筑范围算出（aspect 自适应），这里只负责：
     1) 取框的四角；2) 画出来；3) 把视野锁进框内。            */
function frameOf(ck){
  const c = DATA.campuses[ck];
  if(c && c.frame && c.frame.length === 4 && c.frame[2] > c.frame[0]) return c.frame;
  return c ? c.bbox : null;
}
/* 框的内接矩形：整框能装进视口时的最大缩放级。
   比这级再缩出去，框就不满屏了 —— 也就是"不能缩出框"。
   推导：Web Mercator 缩放 0 时 1px = 156543.03392·cos(lat) 米，
   所以缩放 z 时框占的像素 = 框米数 · 2^z / (156543.03392·cos(lat))。 */
function frameFitZoom(ck){
  const f = frameOf(ck); if(!f) return 3;
  const lat = (f[0] + f[2]) / 2;
  const px0 = 156543.03392 * Math.cos(lat * Math.PI / 180);   // 缩放0时 1px 多少米
  const sz = DATA.campuses[ck].frameSize;
  const wM = sz ? sz[0] : (f[3]-f[1]) * 111320 * Math.cos(lat*Math.PI/180);
  const hM = sz ? sz[1] : (f[2]-f[0]) * 111320;
  const box = document.getElementById('map');
  const vw = (box ? box.clientWidth : 0) || 900;
  const vh = (box ? box.clientHeight : 0) || 700;
  const zoom = Math.log2(Math.min(vw / (wM / px0), vh / (hM / px0)));
  return Math.max(3, Math.min(18, Math.floor(zoom)));
}
/* 把视野锁进外框。maxBoundsViscosity=1 → 边界是硬的，不会弹性回弹。 */
function applyFrame(ck){
  const f = frameOf(ck); if(!f) return;
  const b = L.latLngBounds([[f[0], f[1]], [f[2], f[3]]]);
  map.setMaxBounds(b.pad(0.02));
  map.options.maxBoundsViscosity = 1.0;
  map.setMinZoom(frameFitZoom(ck));
}
function frameRect(ck){
  const f = frameOf(ck); if(!f) return null;
  return L.rectangle([[f[0], f[1]], [f[2], f[3]]],
    {color:'#4a9eff', weight:1.5, dashArray:'7 5', fill:false, interactive:false});
}

/* 点位坐标变换：底图是高德（GCJ-02）时，把 WGS84 的点平移过去，
   这样照片点位才压得准建筑；OSM / 矢量底图时原样返回。
   注意：位置数据本身始终是 WGS84，这里只是显示层补偿。 */
function tp(lat, lon){
  if(baseLayer !== 'image') return [lat, lon];
  const o = offsetOf('image');
  if(!o.x && !o.y) return [lat, lon];
  const dLat = o.y / 111320;
  const dLon = o.x / (111320 * Math.cos(lat * Math.PI / 180));
  return [lat + dLat, lon + dLon];
}

/* 高德是高德坐标系（GCJ-02），必须把瓦片网格和点位一起平移，
   否则照片点位会整体偏 500 米。这里在 getTileUrl 上做平面偏移。 */
function gcjTileLayer(src, offset){
  const T = L.TileLayer.extend({
    getTileUrl(coords){
      const z = this._getZoomForUrl();
      if(!offset.x && !offset.y) return L.TileLayer.prototype.getTileUrl.call(this, coords);
      const scale = Math.pow(2, z);
      const cx = coords.x + (offset.x / (2 * Math.PI * 6378137) * scale);
      const cy = coords.y - (offset.y / (2 * Math.PI * 6378137) * scale);
      return L.Util.template(this._url, L.Util.extend({}, this.options, {
        x: Math.round(cx), y: Math.round(cy), z: z,
        s: this.options.subdomains[Math.abs(Math.round(cx) + Math.round(cy)) % this.options.subdomains.length],
        r: L.Browser.retina ? '@2x' : ''
      }));
    }
  });
  const t = new T(src.url, {
    maxZoom: src.maxZoom || 19, attribution: src.attr,
    subdomains: src.subdomains || 'abc', noWrap: true,
  });
  t.on('tileerror', onTileError);
  return t;
}

function onTileError(){
  if(fellBack) return;
  tileFails++;
  // 瓦片加载不动时退到**自绘立体底图**（图片是内嵌的，不依赖网络），
  // 比退到矢量底图更贴近投稿页的观感。
  if(tileFails >= 3){
    fellBack = true;
    if(hasRelief()) { setBase('relief'); toast('在线瓦片加载失败，已自动切换为自绘立体底图'); }
    else { setBase('vector'); toast('在线瓦片加载失败，已自动切换为矢量底图'); }
  }
}

function hasRelief(ck){
  const keys = (!ck || ck === 'all') ? Object.keys(DATA.campuses) : [ck];
  return keys.some(k => DATA.campuses[k] && DATA.campuses[k].relief);
}

function dropTiles(){
  if(tileStreet && map.hasLayer(tileStreet)) map.removeLayer(tileStreet);
  if(tileImage && map.hasLayer(tileImage)) map.removeLayer(tileImage);
  reliefLayers.forEach(l => { try { if(map.hasLayer(l)) map.removeLayer(l); } catch(e){} });
  reliefLayers = [];
  const m = document.getElementById('map');
  if(m) m.classList.remove('relief');
  tileStreet = tileImage = null;
}

/* 自绘立体底图：把投稿页"选机位"用的**同一张图**按外框四角贴上去。
   为什么贴图而不是重画一份：重画一定会与投稿页漂移，而这套"像素↔经纬度"
   口径已经被 test_static_basemap 钉死了（图片四角 == 外框四角，容差 2e-3°）。
   又因为图片本身就是 Web Mercator 投影下裁出来的、四角对的就是 frame，
   所以这里**不需要任何 GCJ-02 纠偏**（和矢量底图一样是 WGS84 原样）。*/
function addReliefLayers(){
  const m = document.getElementById('map');
  if(m) m.classList.add('relief');
  const keys = currentCampus === 'all' ? Object.keys(DATA.campuses) : [currentCampus];
  keys.forEach(ck => {
    const cfg = DATA.campuses[ck];
    if(!cfg || !cfg.relief || !cfg.frame || cfg.frame.length !== 4) return;
    const f = cfg.frame;                                  // [south, west, north, east]
    const ov = L.imageOverlay(cfg.relief, [[f[0], f[1]], [f[2], f[3]]],
                              {interactive:false, className:'reliefLayer'});
    ov.addTo(map);
    reliefLayers.push(ov);
  });
}

function setBase(which){
  if(which === 'street' && OFFLINE){ toast('离线版不含在线街道底图，请看矢量底图'); which = 'vector'; }
  if(which === 'image' && OFFLINE){ toast('离线版不含在线影像底图，请看矢量底图'); which = 'vector'; }
  // 该校区没有立体底图（生成失败）时退回矢量底图 —— 必须在 baseLayer = which
  // **之前**判，否则 baseLayer 记的是 'relief' 而实际画的是矢量，按钮高亮会错位。
  if(which === 'relief' && !hasRelief(currentCampus)) which = 'vector';
  if(baseLayer !== which) { dropTiles(); tileFails = 0; }

  // 当前校区中心加上"底图坐标系补偿"，作为该底图下的视野中心
  const c = cfgOf(currentCampus).center;
  const o = offsetOf('image');
  const dLat = o.y / 111320;
  const dLon = o.x / (111320 * Math.cos(c[0] * Math.PI / 180));
  const off = (which === 'image') ? [dLat, dLon] : [0, 0];
  const wantZoom = map.getZoom();
  baseLayer = which;

  if(which === 'relief'){
    if(vectorLayer && map.hasLayer(vectorLayer)) map.removeLayer(vectorLayer);
    addReliefLayers();
    map.setView([c[0], c[1]], wantZoom, {animate:false});
  } else if(which === 'vector'){
    if(vectorLayer && !map.hasLayer(vectorLayer)) vectorLayer.addTo(map);
    map.setView([c[0] + off[0], c[1] + off[1]], wantZoom, {animate:false});
  } else {
    if(vectorLayer && map.hasLayer(vectorLayer)) map.removeLayer(vectorLayer);
    const src = which === 'street' ? cfgOf(currentCampus).streets : cfgOf(currentCampus).imagery;
    const sh = offsetOf(which);
    const t = gcjTileLayer(src, src.gcj02 ? sh : { x: 0, y: 0 });
    t.addTo(map);
    if(which === 'street') tileStreet = t; else tileImage = t;
    map.setView([c[0] + off[0], c[1] + off[1]], wantZoom, {animate:false});
  }
  updateBaseButtons();
}

function updateBaseButtons(){
  const mark = (id, on, disabled) => {
    const b = el(id); if(!b) return;
    b.style.color = on ? 'var(--accent2)' : '';
    b.disabled = !!disabled;
    b.style.opacity = disabled ? .45 : 1;
  };
  mark('btnRelief', baseLayer === 'relief', !hasRelief(currentCampus));
  mark('btnStreet', baseLayer === 'street', false);
  mark('btnSat', baseLayer === 'image', false);
  mark('btnVector', baseLayer === 'vector', false);
  const bt = el('btnTarget');
  if(bt) bt.style.color = tracking ? '#c0392b' : '';
}

/* 矢量底图：用 OSM 建筑轮廓画深色 3D 风格底图，无网络也能看。
   注意底图是高德卫星时会给建筑加同样的 GCJ-02 偏移，保证与影像对齐。 */
function buildVectorLayer(){
  vectorLayer = L.layerGroup();
  const shift = baseLayer === 'image' ? offsetOf('image') : { x: 0, y: 0 };
  const mv = (la, lo) => {
    if(!shift.x && !shift.y) return [la, lo];
    return [la + shift.y / 111320, lo + shift.x / (111320 * Math.cos(la * Math.PI / 180))];
  };
  const all = currentCampus === 'all' ? Object.keys(DATA.buildings) : [currentCampus];
  all.forEach(ck => {
    const cfg = DATA.campuses[ck];
    // 深色块表示校区外框范围；OSM 无建筑数据的校区（苏州）靠它和地标定位
    const fr = frameOf(ck);
    if(fr){
      L.rectangle([mv(fr[0], fr[1]), mv(fr[2], fr[3])],
        {color:'#1d3350',weight:1,fillColor:'#0e1a2b',fillOpacity:0.55,interactive:false}).addTo(vectorLayer);
    }
    (DATA.buildings[ck]||[]).forEach(bd => {
      const n = parseInt(bd.levels||'0',10) || (parseFloat(bd.height||'0')/3.3|0) || 3;
      const h = Math.max(0, Math.min(1, n/30));
      // 高度越高越亮，模拟俯瞰立体感
      const fill = `hsl(${212 - h*14}, ${26 + h*16}%, ${15 + h*22}%)`;
      bd.rings.forEach(ring => {
        const latlngs = ring.map(p => mv(p[1], p[0]));
        L.polygon(latlngs, {
          color:'#2c4468', weight:0.6, opacity:.85,
          fillColor:fill, fillOpacity:.95, interactive:false
        }).addTo(vectorLayer);
      });
    });
    L.circleMarker(mv(cfg.center[0], cfg.center[1]), {radius:5,color:'#fff',weight:2,fillColor:'#4a9eff',fillOpacity:1,interactive:false}).addTo(vectorLayer);
    // 地标：矢量底图（尤其苏州那种 OSM 没数据的校区）靠它才能定位
    Object.entries(cfg.landmarks || {}).forEach(([nm, ll]) => {
      L.circleMarker(mv(ll[0], ll[1]), {radius:3.5, color:'#f0c188', weight:1.4, fillColor:'#e0713c', fillOpacity:.95, interactive:false})
        .bindTooltip(nm, {permanent:false, direction:'top'})
        .addTo(vectorLayer);
    });
    L.marker(mv(cfg.center[0], cfg.center[1]), {interactive:false, icon:L.divIcon({
      className:'', html:`<div style="color:#cfe0f5;font-size:12px;font-weight:600;white-space:nowrap;text-shadow:0 1px 4px #000">${esc(cfg.name)}</div>`,
      iconSize:[0,0]
    })}).addTo(vectorLayer);
  });
}

/* ---------------- 渲染打卡点 ---------------- */
function spotColor(kind){ return (DATA.weatherKinds[kind]||{}).color || '#7f8c9b'; }

function renderMarkers(spots){
  markerBySid = {};
  spots.forEach((sp, i) => {
    const color = spotColor(sp.kind);
    const cover = (sp.shots.find(s=>s.src)||{}).src || '';
    const ic = L.divIcon({
      className:'sp',
      html:`<div class="pinwrap">
              <div class="pin" style="background:${color}"><span>${i+1}</span></div>
              ${cover?`<img class="cover" src="${cover}" alt="">`:''}
            </div>`,
      iconSize:[34,34], iconAnchor:[17,34], popupAnchor:[0,-36]
    });
    const m = L.marker(tp(sp.lat, sp.lon), {icon:ic, riseOnHover:true}).addTo(map);
    m.bindPopup(popupHtml(sp), {maxWidth:300});
    m.on('click', () => highlight(sp.sid));
    markerBySid[sp.sid] = m;
  });
}

function popupHtml(sp){
  const cfg = DATA.campuses[sp.campus];
  const shots = sp.shots.map((s, i) => `
    <div class="shot">
      ${s.src?`<img src="${s.src}" alt="">`:''}
      <div class="pb">
        <h3>${esc(s.title)}</h3>
        <div class="au">${esc(s.author)}${s.award?` · <b style="color:var(--accent)">${esc(s.award)}</b>`:''}</div>
        ${s.time?`<div class="kv"><i>拍摄</i><span>${esc(s.time)}</span></div>`:''}
        ${s.weather?`<div class="kv"><i>天象</i><span>${esc(s.weather)}</span></div>`:''}
        ${s.camera?`<div class="kv"><i>器材</i><span>${esc(s.camera)}</span></div>`:''}
        ${s.note?`<div class="kv"><i>手记</i><span>${esc(s.note)}</span></div>`:''}
      </div>
    </div>`).join('');
  const tip = (DATA.spotTips && DATA.spotTips[sp.kind]) || '';
  const dz = (cfg.offsets && cfg.offsets.image) ? Math.hypot(cfg.offsets.image[0], cfg.offsets.image[1]) : 0;
  return `<div class="pop">
      ${shots}
      <div class="pb" style="border-top:2px solid ${spotColor(sp.kind)}">
        <div class="kv" style="margin-top:0"><i>打卡点</i><span><b>${esc(sp.sid)}</b> ${esc(sp.name||cfg.name)}</span></div>
        <div class="kv"><i>经纬度</i><span>${sp.lat.toFixed(5)}, ${sp.lon.toFixed(5)} <span style="color:var(--muted)">(WGS84)</span></span></div>
        <div class="kv"><i>收录</i><span>${sp.count} 幅作品</span></div>
        ${sp.verify?`<div class="tip" style="border-color:#c0392b">⚠ 该点位置待人工复核（落点可能不准）</div>`:''}
        ${tip?`<div class="tip">📷 ${esc(tip)}</div>`:''}
      </div>
    </div>`;
}

/* ---------------- 过滤 ---------------- */
function currentFilter(){
  return {
    campus: currentCampus,
    kinds: new Set([...document.querySelectorAll('.chip.on')].map(c=>c.dataset.kind)),
    q: el('q').value.trim().toLowerCase()
  };
}

function visibleSpots(){
  const f = currentFilter();
  return DATA.spots.filter(sp => {
    if(f.campus !== 'all' && sp.campus !== f.campus) return false;
    if(f.kinds.size && !f.kinds.has(sp.kind)) return false;
    if(f.q){
      const hay = [sp.name, sp.sid, DATA.campuses[sp.campus].name,
                   ...sp.shots.flatMap(s=>[s.title,s.author,s.weather,s.note])].join(' ').toLowerCase();
      if(!hay.includes(f.q)) return false;
    }
    return true;
  });
}

function render(){
  const spots = visibleSpots();
  selSpots = spots;
  Object.values(markerBySid).forEach(m => map.removeLayer(m));
  renderMarkers(spots);
  renderList(spots);
  el('stat').innerHTML = [
    ['收录作品', DATA.stats.submissions],
    ['打卡点', spots.length + (spots.length!==DATA.spots.length?` / ${DATA.spots.length}`:'')],
    ['参与作者', DATA.stats.authors],
    ['已定点', DATA.stats.located]
  ].map(([k,v])=>`<div><b>${v}</b><span>${k}</span></div>`).join('');
}

function renderList(spots){
  const box = el('list');
  if(!spots.length){ box.innerHTML = `<div class="empty">没有匹配的打卡点<br>试试清空筛选或换个关键词</div>`; return; }
  box.innerHTML = spots.map((sp,i) => {
    const cover = (sp.shots.find(s=>s.src)||{}).src || '';
    const first = sp.shots[0] || {};
    const cfg = DATA.campuses[sp.campus];
    return `<div class="card" data-sid="${sp.sid}">
      ${cover?`<img src="${cover}" alt="" loading="lazy">`:`<img alt="">`}
      <div class="no" style="background:${spotColor(sp.kind)}">${i+1}</div>
      <div class="body">
        <div class="ttl">${esc(first.title||sp.name||cfg.short)}</div>
        <div class="meta">${esc(sp.name||cfg.short)} · ${sp.count} 幅${first.author?` · ${esc(first.author)}`:''}</div>
        <div class="tags">
          <span class="tag">${esc(DATA.campuses[sp.campus].short)}</span>
          <span class="tag">${esc((DATA.weatherKinds[sp.kind]||{}).label||sp.kind)}</span>
          ${first.time?`<span class="tag">${esc(String(first.time).slice(0,16))}</span>`:''}
          ${sp.verify?`<span class="tag warn">待复核</span>`:''}
        </div>
      </div>
    </div>`;
  }).join('');
  box.querySelectorAll('.card').forEach(c => c.addEventListener('click', () => {
    const sid = c.dataset.sid;
    document.querySelectorAll('.card').forEach(x=>x.classList.toggle('sel', x===c));
    const sp = spots.find(s=>s.sid===sid);
    if(!sp) return;
    map.flyTo(tp(sp.lat, sp.lon), Math.max(map.getZoom(), 18), {duration:.6});
    const m = markerBySid[sid];
    if(m){ setTimeout(()=>m.openPopup(), 380); }
  }));
}

function highlight(sid){
  document.querySelectorAll('.card').forEach(x=>x.classList.toggle('sel', x.dataset.sid===sid));
  const c = document.querySelector(`.card[data-sid="${sid}"]`);
  if(c) c.scrollIntoView({block:'nearest', behavior:'smooth'});
}

/* ---------------- 控件 ---------------- */
function buildControls(){
  const tabs = el('campusTabs');
  const list = [['all','全校']].concat(Object.keys(DATA.campuses).map(k=>[k, DATA.campuses[k].short]));
  tabs.innerHTML = list.map(([k,label]) => {
    const n = k==='all' ? DATA.spots.length : DATA.spots.filter(s=>s.campus===k).length;
    return `<button class="tab${k==='all'?' on':''}" data-campus="${k}">${esc(label)} ${n}</button>`;
  }).join('');
  // 嵌入模式下侧栏被收起，给地图上加一个同样的悬浮切换条
  const cf = el('campusFloat');
  cf.innerHTML = tabs.innerHTML;
  tabs.querySelectorAll('.tab').forEach(b => b.addEventListener('click', () => switchCampus(b.dataset.campus)));
  cf.querySelectorAll('.tab').forEach(b => b.addEventListener('click', () => switchCampus(b.dataset.campus)));

  const chips = el('kindChips');
  const kinds = [...new Set(DATA.spots.map(s=>s.kind))];
  chips.innerHTML = `<button class="chip" data-kind="__all" style="border-color:var(--line)">全部</button>` +
    kinds.map(k => {
      const n = DATA.spots.filter(s=>s.kind===k).length;
      const col = spotColor(k);
      return `<button class="chip" data-kind="${k}" style="--c:${col}">
        <span class="dot" style="background:${col}"></span>${esc((DATA.weatherKinds[k]||{}).label||k)} ${n}
      </button>`;
    }).join('');
  chips.querySelectorAll('.chip').forEach(ch => ch.addEventListener('click', () => {
    if(ch.dataset.kind === '__all'){
      chips.querySelectorAll('.chip').forEach(x=>{ x.classList.remove('on'); x.style.background='#fff'; });
      render(); syncHash(); return;
    }
    ch.classList.toggle('on');
    ch.style.background = ch.classList.contains('on') ? ch.dataset.kindColor || spotColor(ch.dataset.kind) : '#fff';
    ch.style.color = ch.classList.contains('on') ? '#fff' : 'var(--ink2)';
    ch.style.borderColor = ch.classList.contains('on') ? 'transparent' : 'var(--line)';
    render(); syncHash();
  }));

  const lg = el('legendBody');
  lg.innerHTML = Object.entries(DATA.weatherKinds).filter(([k])=>kinds.includes(k)).map(([k,v])=>
    `<div class="li" data-kind="${k}"><span class="dot" style="background:${v.color}"></span>${esc(v.label)}</div>`).join('');
  lg.querySelectorAll('.li').forEach(d => d.addEventListener('click', () => {
    const ch = chips.querySelector(`.chip[data-kind="${d.dataset.kind}"]`);
    if(ch) ch.click();
  }));

  el('q').addEventListener('input', debounce(()=>{ render(); syncHash(); }, 200));
  el('btnRelief').onclick = () => { setBase('relief'); buildVectorLayer(); render(); renderNote(); };
  el('btnStreet').onclick = () => { setBase('street'); buildVectorLayer(); renderNote(); };
  el('btnSat').onclick    = () => { setBase('image');  buildVectorLayer(); render(); renderNote(); };
  el('btnVector').onclick = () => { setBase('vector'); buildVectorLayer(); render(); renderNote(); };
  el('btnFit').onclick = fitAll;
  el('btnShare').onclick = copyView;
  el('btnTarget').onclick = toggleTracking;
  el('toggleSide').onclick = () => el('side').classList.toggle('open');
}

/* 鼠标定位：点一下按钮后，把鼠标指到的位置读成经纬度，方便人工核对机位 */
let tracking = false;
function toggleTracking(){
  tracking = !tracking;
  if(tracking){
    map.on('mousemove', onTrack);
    toast('鼠标定位已开：把鼠标移到图上，左上角会显示经纬度，再点一次关闭');
  } else {
    map.off('mousemove', onTrack);
    el('note').innerHTML = noteHtml();
  }
  updateBaseButtons();
}
function onTrack(e){
  el('note').innerHTML = `🧭 <b style="color:#c0392b">${e.latlng.lat.toFixed(6)}, ${e.latlng.lng.toFixed(6)}</b><br>` +
    `<span style="font-size:10px">（当前底图坐标系）</span>`;
}

function noteHtml(){
  const cfg = cfgOf(currentCampus);
  if(tracking) return '';
  const parts = [];
  if(baseLayer === 'image'){
    const dz = cfg.offsets && cfg.offsets.image ? Math.hypot(cfg.offsets.image[0], cfg.offsets.image[1]) : 0;
    if(dz > 1) parts.push(`🛰 当前为高德卫星底图（GCJ-02），已自动纠偏 ${dz.toFixed(0)} m，点位与影像对齐`);
    else parts.push('🛰 当前为高德卫星底图');
  } else if(baseLayer === 'street'){
    parts.push('🗺 当前为 OSM 街道底图（WGS84 原始坐标，无需纠偏）');
  } else if(baseLayer === 'relief'){
    parts.push('🏞 当前为自绘立体底图（与投稿选机位时是同一张图）');
  } else {
    parts.push('⛰ 当前为矢量建筑底图（断网可用）');
  }
  if(cfg.accuracy === 'approx') parts.push(`⚠ ${cfg.short}校区地标坐标为估算值，请在卫星影像上校准`);
  if(DATA.unresolved && DATA.unresolved.length) parts.push(`待人工补录 ${DATA.unresolved.length} 幅`);
  return parts.join('<br>');
}
function renderNote(){ const n = el('note'); if(n && !tracking) n.innerHTML = noteHtml(); }

function fitAll(){
  if(currentCampus === 'all'){
    // 三个校区的外框合起来 —— 视野锁在这三个框的总范围里，缩不到更远
    const boxes = Object.keys(DATA.campuses).map(k => frameOf(k)).filter(Boolean);
    if(boxes.length){
      const latlngs = [];
      boxes.forEach(f => { latlngs.push([f[0],f[1]]); latlngs.push([f[2],f[3]]); });
      const b = L.latLngBounds(latlngs);
      map.setMaxBounds(b.pad(0.02));
      map.options.maxBoundsViscosity = 1.0;
      map.fitBounds(b.pad(0.05));
      map.setMinZoom(map.getZoom());
    } else {
      const pts = selSpots.length ? selSpots.map(s=>[s.lat,s.lon]) : Object.values(DATA.campuses).map(c=>c.center);
      map.fitBounds(L.latLngBounds(pts).pad(0.3));
    }
    return;
  }
  const c = cfgOf(currentCampus);
  applyFrame(currentCampus);
  map.setView(tp(c.center[0], c.center[1]), Math.max(frameFitZoom(currentCampus), Math.min(c.zoom, 17)), {animate:false});
}

/* 切换校区：侧栏与悬浮条共用 */
function switchCampus(key){
  currentCampus = key;
  document.querySelectorAll('#campusTabs .tab, #campusFloat .tab').forEach(x => {
    x.classList.toggle('on', x.dataset.campus === key);
  });
  dropTiles();
  buildVectorLayer();
  setBase(baseLayer === 'none' ? 'relief' : baseLayer);
  if(baseLayer !== 'none'){
    if(key === 'all'){ fitAll(); }
    else {
      applyFrame(key);
      map.setZoom(Math.max(frameFitZoom(key), cfgOf(key).zoom), {animate:false});
    }
  }
  render(); syncHash(); renderNote();
}

function debounce(fn, ms){ let t; return (...a)=>{ clearTimeout(t); t=setTimeout(()=>fn(...a), ms); }; }

/* ---------------- 分享链接 ---------------- */
let hashWritable = true;
function syncHash(){
  if(!hashWritable) return;
  const f = currentFilter();
  const h = new URLSearchParams();
  if(f.campus!=='all') h.set('c', f.campus);
  if(f.kinds.size) h.set('k', [...f.kinds].join(','));
  if(f.q) h.set('q', f.q);
  h.set('v', `${map.getCenter().lat.toFixed(5)},${map.getCenter().lng.toFixed(5)},${map.getZoom()}`);
  // 地图被嵌进 iframe（如 Streamlit 成品页）时，浏览器禁止改父页 URL，
  // 这里会抛 SecurityError —— 捕获后关掉同步，地图其余功能不受影响。
  try {
    history.replaceState(null, '', '#' + h.toString());
  } catch (e) {
    hashWritable = false;
  }
}
function applyHash(){
  let raw = '';
  try { raw = location.hash.slice(1); } catch (e) { raw = ''; }
  const h = new URLSearchParams(raw);
  if(h.get('c')){
    const b = document.querySelector(`.tab[data-campus="${h.get('c')}"]`);
    if(b) b.click();
  }
  (h.get('k')||'').split(',').filter(Boolean).forEach(k => {
    const ch = document.querySelector(`.chip[data-kind="${k}"]`); if(ch) ch.click();
  });
  if(h.get('q')) el('q').value = h.get('q');
  if(h.get('v')){
    const [la,ln,z] = h.get('v').split(',').map(Number);
    if(!isNaN(la)) map.setView([la,ln], z||17);
  }
  map.on('moveend zoomend', debounce(syncHash, 300));
}
function copyView(){
  syncHash();
  let url = '';
  try { url = location.href; } catch (e) { url = ''; }
  const c = map.getCenter();
  const fallback = `坐标 ${c.lat.toFixed(5)}, ${c.lng.toFixed(5)} · 缩放 ${map.getZoom()}`;
  if(!hashWritable || !url || url.startsWith('about:')){
    toast('嵌入预览里无法生成链接，请打开独立 HTML 版复制分享链接');
    return;
  }
  navigator.clipboard?.writeText(url).then(
    ()=>toast('已复制当前视图链接，可直接分享'),
    ()=>toast(fallback)
  );
}
function toast(msg){
  const t = el('toast'); t.textContent = msg; t.classList.add('on');
  setTimeout(()=>t.classList.remove('on'), 2200);
}

/* ---------------- 启动 ---------------- */
/* 嵌入模式：被 Streamlit 成品页用 iframe 嵌进去时，收起左侧照片墙与顶部大标题，
   只留地图，并跟随容器尺寸变化（ResizeObserver 比定时器可靠）。 */
const EMBED = (() => {
  try {
    if(new URLSearchParams(location.search).get('embed') === '1') return true;
  } catch (e) {}
  try { return window.self !== window.top; } catch (e) { return true; }  // 跨域 iframe 会抛错
})();
if(EMBED){ document.body.classList.add('embed'); }

initMap();
buildControls();
render();
// 默认**自绘立体底图**：与投稿页选机位看到的是同一张图（用户 2026-10-05 要求），
// 而且它是内嵌的、不依赖网络。在线版/离线版都默认它；想比对真实影像再手动切别的。
setBase('relief');
buildVectorLayer();
renderNote();

if(EMBED){
  const host = document.getElementById('map');
  const resize = () => { try { map.invalidateSize(); } catch (e) {} };
  // 容器尺寸一变就重算：Streamlit 的 iframe 高度/宽度是异步确定的
  if(window.ResizeObserver && host){
    let last = 0;
    new ResizeObserver(() => {
      const now = Date.now();
      if(now - last < 120) return;   // 节流，避免拖动时抖动
      last = now;
      resize();
    }).observe(host);
  }
  [0, 150, 400, 900, 1800].forEach(t => setTimeout(resize, t));
  window.addEventListener('resize', () => setTimeout(resize, 150));
}
</script>
</body>
</html>
"""


def render_html(payload: dict, *, offline: bool) -> str:
    """把 payload 渲染成完整 HTML 字符串（不落盘，供网站在内存里直接内嵌）。"""
    payload = dict(payload)
    payload["spotTips"] = SPOT_TIPS
    data_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return (
        PAGE.replace("__PAYLOAD__", data_json)
        .replace("__OFFLINE__", "true" if offline else "false")
        .replace("__GENERATED__", html.escape(payload["generated"]))
    )


def write_map(payload: dict, *, offline: bool, out: Path | None = None) -> Path:
    out = out or (EXPORTS / ("校园天空打卡点地图.html" if offline else "校园天空打卡点地图_在线版.html"))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_html(payload, offline=offline), encoding="utf-8")

    # 在线版引用 thumbs/ 相对路径 —— 把缩略图复制到导出目录旁边，
    # 这样直接打包 exports/ 整个文件夹就能分享，不会出现图片裂图。
    # 云端模式的照片已内嵌成 data URL，不需要这步。
    if not offline:
        dest = out.parent / "thumbs"
        dest.mkdir(parents=True, exist_ok=True)
        used = {
            s["src"].split("/", 1)[-1]
            for sp in payload["spots"] for s in sp["shots"]
            if s.get("src") and not s["src"].startswith("data:")
        }
        copied = 0
        for name in used:
            src = THUMBS / name
            if src.exists():
                target = dest / name
                if not target.exists() or target.stat().st_mtime < src.stat().st_mtime:
                    shutil.copy2(src, target)
                    copied += 1
        if copied:
            print(f"  缩略图同步到 {dest.name}/：新增/更新 {copied} 张（共 {len(used)} 张）")
    return out


SPOT_TIPS = {
    "晚霞": "西向开阔处，日落前 20 分钟到日落后 25 分钟是火烧云爆发的窗口，注意云底高度与低空水汽。",
    "朝霞": "东向开阔处，日出前 30 分钟最有戏；朝霞比晚霞更依赖高空卷云做“幕布”。",
    "积云": "晴天午后对流旺盛，积云轮廓最饱满；用建筑线条或树枝做前景框更出片。",
    "层云": "阴天并非没戏，层云的高光层次适合拍低调色温与剪影。",
    "雨虹": "雨后太阳一出，背对太阳找虹；广角拍全拱，长焦拍虹与地景局部。",
    "雷电": "安全第一：远离高处与孤立树木，用长曝 + 间隔连拍抓闪电。",
    "月相": "月升月落贴地平线时最大最好拍，配合建筑剪影；查月相可用天文通。",
    "雾霾": "平流雾与晨雾适合拍“天空之城”，选高处俯拍，注意逆光层次。",
    "其他": "开阔无遮挡、有地标做前景的位置，通常都是好机位。",
}


def main() -> int:
    ap = argparse.ArgumentParser(description="生成校园天空打卡点地图")
    ap.add_argument("--offline", action="store_true",
                    help="生成照片内嵌的单文件离线版（体积大，断网也能看）")
    ap.add_argument("--csv", action="store_true", help="同时导出投稿汇总 CSV")
    args = ap.parse_args()

    subs = load_submissions()
    if not subs:
        print("！data/submissions.jsonl 里还没有投稿，先跑 python demo_data.py 造示例数据")
        return 1

    # 默认在线版：引用缩略图文件、用在线瓦片，体积小、底图漂亮；
    # --offline 则把照片压成 data URL 内嵌，配合矢量底图，断网也能看。
    embed = bool(args.offline)
    payload = build_payload(subs, embed_photos=embed)
    out = write_map(payload, offline=embed)
    if args.csv:
        print("CSV ->", export_csv(subs))
    mode = "离线单文件版" if embed else "在线版"
    print(f"✓ 地图已生成（{mode}）")
    print(f"  {out}")
    print(f"  作品 {payload['stats']['submissions']} 幅 / 打卡点 {payload['stats']['spots']} 个 / 已定点 {payload['stats']['located']} 幅")
    if not embed:
        print("  分享时把 data\\exports 整个文件夹一起发出去（地图 + thumbs 缩略图）")
    if payload["unresolved"]:
        print(f"  ⚠ {len(payload['unresolved'])} 幅未能自动定点，需人工补录：")
        for u in payload["unresolved"][:8]:
            print(f"    {u['sid']} [{u['campus']}] 位置描述“{u['loc_text']}”")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
