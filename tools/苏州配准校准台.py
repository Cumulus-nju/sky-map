"""苏州校区建模数据 —— 地理配准校准台。

背景：
    `南苏CFD建模文件` 里的 935 个建筑足迹几何质量很高（来自资产处平面图），
    但 **没有绝对地理坐标** —— GLB 元信息写着 "Approximate metres, not surveyed"。
    要在地图上用，必须知道模型原点 (0,0) 对应哪个真实经纬度。

    自动配准试过、不可靠（这个区域农田 + 工业区 + 水网交错，纹理判据会误判到
    2 km 外的工业园）。所以做这个页面让人眼一眼确认 —— 人能认出校园，算法认不出。

怎么用：
    1. 双击 `校准苏州校区.bat`（或 `streamlit run tools/苏州配准校准台.py`）
    2. 左栏拖动「东移 / 北移」滑块，把红色建筑轮廓拖到卫星影像上真实的校园上
       （校园特征：整齐的成排建筑 + 中央水景 + 西侧紧邻庄里山）
    3. 对上了 → 点「保存配准参数」，会写入 data/suzhou_georef.json
    4. 把结果告诉我，我据此把 935 栋真实建筑接进地图底图

    缩放/旋转一般不用动（模型和影像都是正北朝上）。
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import streamlit as st
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

TILE_DIR = ROOT / "data" / "tile_check"
BASE_IMG = TILE_DIR / "suzhou_base_z16.jpg"
BASE_META = TILE_DIR / "suzhou_base_meta.json"
GEOJSON = ROOT / "_suzhou_cfd_import" / "南苏CFD建模文件" / "建模数据" / "site_plan_rect.geojson"
OUT_JSON = ROOT / "data" / "suzhou_georef.json"

M_PER_DEG_LAT = 110574.0

st.set_page_config(page_title="苏州校区配准校准台", page_icon="🎯", layout="wide")


@st.cache_data(show_spinner=False)
def load_base() -> tuple[Image.Image, dict]:
    return Image.open(BASE_IMG).convert("RGB"), json.loads(BASE_META.read_text(encoding="utf-8"))


@st.cache_data(show_spinner=False)
def load_footprints() -> list[list[tuple[float, float]]]:
    """读 935 个建筑足迹的局部米制环。"""
    data = json.loads(GEOJSON.read_text(encoding="utf-8"))
    rings: list[list[tuple[float, float]]] = []
    for feat in data.get("features", []):
        geom = feat.get("geometry") or {}
        if geom.get("type") != "Polygon":
            continue
        for ring in geom.get("coordinates", []):
            rings.append([(float(p[0]), float(p[1])) for p in ring])
    return rings


def deg2num(lat: float, lon: float, z: int) -> tuple[float, float]:
    n = 2.0**z
    x = (lon + 180.0) / 360.0 * n
    y = (1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2.0 * n
    return x, y


def main() -> None:
    st.title("🎯 苏州校区配准校准台")
    st.caption("把红色建筑轮廓拖到真实校园上。目标：整齐的成排建筑 + 中央水景 + 西侧紧邻庄里山。")

    if not BASE_IMG.exists() or not GEOJSON.exists():
        st.error(f"缺文件。底图={BASE_IMG.exists()}，足迹={GEOJSON.exists()}")
        st.stop()

    base, meta = load_base()
    rings = load_footprints()
    z = meta["zoom"]

    # 参考点：底图中心对应的 WGS84（由 GCJ 中心推回）
    def gcj_to_wgs_approx(lat: float, lon: float) -> tuple[float, float]:
        # 用已知的 GCJ 常量偏移反推
        ref_lat, ref_lon = meta["wgs_ref_lat"], meta["wgs_ref_lon"]
        g_lat = meta["gcj_center_lat"]
        # dlat 由参考点与自身 GCJ 的关系近似
        import campus_config  # noqa
        return lat - 0.0082497, lon - 0.0232228

    center_gcj = (meta["gcj_center_lat"], meta["gcj_center_lon"])
    center_wgs = gcj_to_wgs_approx(*center_gcj)

    st.sidebar.header("配准偏移")
    st.sidebar.caption("模型原点 (0,0) 相对底图中心的偏移（米）。东为正、北为正。")

    dx = st.sidebar.slider("东移 (m)", -3000, 3000, 0, 10)
    dy = st.sidebar.slider("北移 (m)", -3000, 3000, 0, 10)
    scale = st.sidebar.slider("缩放", 0.80, 1.25, 1.00, 0.01)
    rot = st.sidebar.slider("旋转 (°)", -10.0, 10.0, 0.0, 0.5)

    m_per_deg_lon = 111320.0 * math.cos(math.radians(center_wgs[0]))
    anchor_lat = center_wgs[0] + dy / M_PER_DEG_LAT
    anchor_lon = center_wgs[1] + dx / m_per_deg_lon

    # 画布像素坐标换算：GCJ 经纬 -> 瓦片像素
    def to_px_wgs(lat: float, lon: float) -> tuple[float, float]:
        g_lat = lat + 0.0082497
        g_lon = lon + 0.0232228
        fx, fy = deg2num(g_lat, g_lon, z)
        return (fx - meta["tile_x0"]) * 256, (fy - meta["tile_y0"]) * 256

    canvas = base.copy()
    overlay = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    ct, sn = math.cos(math.radians(rot)), math.sin(math.radians(rot))
    for ring in rings:
        pts = []
        for x, y in ring:
            lx, ly = x * scale, y * scale
            rx = lx * ct - ly * sn
            ry = lx * sn + ly * ct
            lat = anchor_lat + ry / M_PER_DEG_LAT
            lon = anchor_lon + rx / m_per_deg_lon
            pts.append(to_px_wgs(lat, lon))
        if len(pts) >= 3:
            draw.polygon(pts, outline=(255, 40, 40, 255))

    # 锚点标记
    ax, ay = to_px_wgs(anchor_lat, anchor_lon)
    draw.ellipse([ax - 10, ay - 10, ax + 10, ay + 10], fill=(0, 255, 0, 255), outline=(0, 0, 0, 255))

    composed = Image.alpha_composite(canvas.convert("RGBA"), overlay).convert("RGB")

    left, right = st.columns([3.2, 1])
    with left:
        st.image(composed, caption=f"模型原点 → WGS84 ({anchor_lat:.6f}, {anchor_lon:.6f})", use_container_width=True)
    with right:
        st.metric("原点纬度", f"{anchor_lat:.6f}")
        st.metric("原点经度", f"{anchor_lon:.6f}")
        st.metric("偏移", f"东 {dx:+d} m / 北 {dy:+d} m")
        st.divider()
        st.markdown("**当前底图中心**")
        st.caption(f"WGS84 {center_wgs[0]:.6f}, {center_wgs[1]:.6f}")
        st.caption(f"覆盖约 5.5 × 5.5 km")
        st.divider()
        if st.button("💾 保存配准参数", type="primary", use_container_width=True):
            payload = {
                "anchor_lat": anchor_lat,
                "anchor_lon": anchor_lon,
                "offset_east_m": dx,
                "offset_north_m": dy,
                "scale": scale,
                "rotation_deg": rot,
                "base_meta": meta,
                "source_geojson": str(GEOJSON.relative_to(ROOT)),
                "note": "由人工在卫星影像上校准得出。anchor 是模型局部坐标 (0,0) 对应的 WGS84 经纬度。",
            }
            OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
            OUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            st.success(f"已保存到 {OUT_JSON.relative_to(ROOT)}")
            st.json(payload)

    st.divider()
    st.markdown(
        "**找不到校园？** 用左侧滑块大幅度平移扫一遍。校园特征："
        "① 成排的整齐建筑（宿舍/教学楼）② 中央有水面和绿地 "
        "③ 西侧紧邻一座小山（庄里山，134.9 m）④ 周边是农田和工业厂房。"
    )


main()
