"""「天光云影」校园天空打卡点地图 —— 三校区配置。

坐标一律 WGS84（经纬度），与 OSM / 手机 GPS / EXIF 一致。
地图上的高德图层会按校区自动做 GCJ-02 纠偏（见 tools/gcj_offset.py）。
校区边界为实际抓取 OSM 数据后校准的结果。
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class TileSource:
    """一个底图瓦片源。gcj02=True 表示该源是 GCJ-02 坐标系，渲染时需纠偏。"""

    name: str
    url: str
    attr: str
    gcj02: bool = False
    subdomains: tuple[str, ...] = ("1", "2", "3", "4")
    max_zoom: int = 19


@dataclass(frozen=True)
class Campus:
    key: str
    name: str
    short: str
    center: tuple[float, float]         # (lat, lon) WGS84
    zoom: int
    # (south, west, north, east) —— 投稿点位落在此框外的会被标为"待复核"
    bbox: tuple[float, float, float, float]
    streets: TileSource                 # 默认街道底图
    imagery: TileSource                 # 卫星影像底图
    landmarks: dict[str, tuple[float, float]] = field(default_factory=dict)
    # 手编地标坐标精度：exact=实测（OSM），approx=依卫星影像估算、待校准
    landmark_accuracy: str = "exact"
    osm_note: str = ""                  # 该校区 OSM 覆盖情况说明


# ---- 瓦片源（2026-09 逐个在浏览器里实测过）--------------------------------
# OSM   ：WGS84，中文标注清楚，鼓楼/仙林覆盖好，稳定直连、无需 key
# 高德   ：GCJ-02，卫星影像清晰、街道中文标注好，覆盖全市（苏州校区尤其依赖它）
# 已排除：CARTO（返回 "API KEY REQUIRED" 占位图）、Esri（403）、天地图（需 key）
OSM_STREET = TileSource(
    name="OSM 街道",
    url="https://tile.openstreetmap.org/{z}/{x}/{y}.png",
    attr='&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
    gcj02=False,
    max_zoom=19,
)
AMAP_SAT = TileSource(
    name="高德卫星影像",
    url="https://webst0{s}.is.autonavi.com/appmaptile?style=6&x={x}&y={y}&z={z}",
    attr="&copy; 高德地图 AutoNavi（已做 GCJ-02 坐标纠偏）",
    gcj02=True,
    max_zoom=18,
)
AMAP_STREET = TileSource(
    name="高德街道",
    url="https://webrd0{s}.is.autonavi.com/appmaptile?lang=zh_cn&size=1&scale=1&style=8&x={x}&y={y}&z={z}",
    attr="&copy; 高德地图 AutoNavi（已做 GCJ-02 坐标纠偏）",
    gcj02=True,
    max_zoom=18,
)


CAMPUSES: dict[str, Campus] = {
    "gulou": Campus(
        key="gulou",
        name="南京大学鼓楼校区",
        short="鼓楼",
        center=(32.0566, 118.7736),
        zoom=17,
        bbox=(32.051019, 118.767759, 32.062262, 118.779466),
        streets=OSM_STREET,
        imagery=AMAP_SAT,
        landmarks={
            # 鼓楼校区经典天空机位（依据 site_plan.geojson 的 139 个实名建筑）
            "北大楼": (32.05714, 118.77421),
            "大礼堂": (32.05700, 118.77510),
            "小礼堂": (32.05660, 118.77550),
            "南京大学图书馆（鼓楼）": (32.05590, 118.77360),
            "蒙民伟楼": (32.05540, 118.77280),
            "鼓楼": (32.05830, 118.77700),
            "赛珍珠故居": (32.05600, 118.77180),
            "拉贝故居": (32.05390, 118.77310),
            "北园草坪": (32.05690, 118.77440),
            "西南楼": (32.05520, 118.77230),
            "东大楼": (32.05670, 118.77560),
            "西大楼（数学系）": (32.05630, 118.77240),
            "体育馆": (32.05460, 118.77400),
            "大学生活动中心": (32.05530, 118.77540),
            "天文与空间科学学院": (32.05660, 118.77160),
        },
        landmark_accuracy="exact",
        osm_note="OSM 覆盖良好：298 栋建筑轮廓，163 栋有实名，可直接作为机位底图；"
                 "另有鼓楼校区完整 CFD 建模（Desktop/my_campus，250 栋建筑 + 三向风场）。",
    ),
    "xianlin": Campus(
        key="xianlin",
        name="南京大学仙林校区",
        short="仙林",
        center=(32.1197, 118.9530),
        zoom=15,
        bbox=(32.110178, 118.94453, 32.129306, 118.961756),
        streets=OSM_STREET,
        imagery=AMAP_SAT,
        landmarks={
            # 坐标取自 OSM 实测（建筑代表点 / POI 点位）
            "杜厦图书馆": (32.116308, 118.955387),
            "藜照湖": (32.116212, 118.956101),
            "菜根潭": (32.116399, 118.951430),
            "二源广场": (32.114836, 118.955241),
            "左涤江天文台": (32.1210, 118.9570),
            "左涤江楼": (32.1160, 118.9500),
            "敬文学生活动中心": (32.1159, 118.9527),
            "恩玲剧场": (32.115850, 118.953183),
            "张心瑜剧场": (32.116156, 118.952172),
            "校史馆（沈小平楼）": (32.1146, 118.9546),
            "香雪海": (32.120945, 118.948692),
            "紫云海": (32.123061, 118.954225),
            "啓園": (32.118345, 118.954742),
            "南雍山": (32.124063, 118.955907),
            "梅岭": (32.120400, 118.952225),
            "九乡河生态公园": (32.115511, 118.946045),
            "仙林校区第二体育场": (32.122618, 118.947940),
            "人工草皮足球场": (32.121844, 118.946488),
            "三组团操场": (32.1177, 118.9549),
            "大气科学学院": (32.1180, 118.9560),
            "天文与空间科学学院": (32.1175, 118.9565),
            "化学化工学院": (32.1167, 118.9577),
            "匡亚明学院": (32.1160, 118.9500),
            "行政南楼": (32.1150, 118.9540),
            "扬州楼（行政北楼）": (32.1160, 118.9535),
        },
        landmark_accuracy="exact",
        osm_note="OSM 覆盖良好：297 栋建筑轮廓，168 栋有实名；水系（藜照湖、菜根潭）"
                 "与山体（南雍山、梅岭）齐全，是拍晚霞与倒影的好点位。",
    ),
    "suzhou": Campus(
        key="suzhou",
        name="南京大学苏州校区",
        short="苏州",
        center=(31.3532, 120.3794),
        zoom=16,
        bbox=(31.34695, 120.371035, 31.359362, 120.387796),
        streets=OSM_STREET,
        imagery=AMAP_SAT,
        landmarks={
            # 只有"庄里山"是 OSM 实测；其余为依卫星影像估算的骨架点位，
            # 正式使用前请在地图上对一遍（投稿页里同学自己点选精度更高）。
            "庄里山": (31.350534, 120.380887),
            "苏州校区湖面": (31.351231, 120.392197),
            "图书馆（中轴）": (31.3532, 120.3794),
            "校园中轴广场": (31.3545, 120.3794),
            "操场": (31.3560, 120.3760),
            "南门": (31.3478, 120.3794),
            "北门": (31.3588, 120.3794),
            "学生宿舍区": (31.3555, 120.3825),
            "教学组团": (31.3520, 120.3810),
            "体育馆": (31.3552, 120.3752),
        },
        landmark_accuracy="approx",
        osm_note="⚠ OSM 覆盖极稀：仅 23 栋建筑且无楼名、2 个 POI（庄里山、一处水面）。"
                 "该校区 2023 年启用，属新建校区。因此苏州校区以**卫星影像底图**为主，"
                 "打卡点位置靠同学地图点选 + 手编骨架地标（标 approx）。"
                 "若要更高精度，建议用无人机正射影像或校区总平面 CAD 替换底图。",
    ),
}

# 打卡点"是不是拍天空的好机位"的先验提示，按天象类别给文案
SKY_TIPS: dict[str, str] = {
    "晚霞": "西向开阔处，日落前 20 分钟到日落后 25 分钟是火烧云爆发窗口，盯住云底与低空水汽。",
    "朝霞": "东向开阔处，日出前 30 分钟最有戏；朝霞比晚霞更依赖高空卷云做“幕布”。",
    "积云": "晴天午后对流旺盛，积云轮廓最饱满；用建筑线条或树枝做前景框更出片。",
    "层云": "阴天也有戏，层云的高光层次适合拍低调色温与剪影。",
    "雨虹": "雨后太阳一出，背对太阳找虹；广角拍全拱，长焦拍虹与地景局部。",
    "雷电": "安全第一：远离高处与孤立树木，用长曝 + 间隔连拍抓闪电。",
    "月相": "月升月落贴地平线时最大最好拍，配合建筑剪影；可用天文通查月相。",
    "雾霾": "平流雾与晨雾适合拍“天空之城”，选高处俯拍，注意逆光层次。",
    "其他": "开阔无遮挡、有地标做前景的位置，通常都是好机位。",
}


def campus(key: str) -> Campus:
    try:
        return CAMPUSES[key]
    except KeyError:
        raise KeyError(f"未知校区 {key!r}，可选：{list(CAMPUSES)}") from None


def all_keys() -> list[str]:
    return list(CAMPUSES)


def in_bbox(key: str, lat: float, lon: float, pad: float = 0.0) -> bool:
    s, w, n, e = campus(key).bbox
    return (s - pad) <= lat <= (n + pad) and (w - pad) <= lon <= (e + pad)
