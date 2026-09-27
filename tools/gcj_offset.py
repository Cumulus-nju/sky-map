"""GCJ-02 纠偏：算三校区 WGS84 → GCJ-02 的偏移量（米）。

高德瓦片是 GCJ-02 坐标系，与 OSM/手机 GPS 的 WGS84 差 300~600 m，
不纠偏的话照片点位会整体飘到隔壁街区。

用法: python tools/gcj_offset.py
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from campus_config import CAMPUSES  # noqa: E402

PI = math.pi
A = 6378245.0          # 克拉索夫斯基椭球长半轴
EE = 0.00669342162296594323


def _out_of_china(lat: float, lon: float) -> bool:
    return not (72.004 <= lon <= 137.8347 and 0.8293 <= lat <= 55.8271)


def _transform_lat(x: float, y: float) -> float:
    ret = -100.0 + 2.0 * x + 3.0 * y + 0.2 * y * y + 0.1 * x * y + 0.2 * math.sqrt(abs(x))
    ret += (20.0 * math.sin(6.0 * x * PI) + 20.0 * math.sin(2.0 * x * PI)) * 2.0 / 3.0
    ret += (20.0 * math.sin(y * PI) + 40.0 * math.sin(y / 3.0 * PI)) * 2.0 / 3.0
    ret += (160.0 * math.sin(y / 12.0 * PI) + 320 * math.sin(y * PI / 30.0)) * 2.0 / 3.0
    return ret


def _transform_lon(x: float, y: float) -> float:
    ret = 300.0 + x + 2.0 * y + 0.1 * x * x + 0.1 * x * y + 0.1 * math.sqrt(abs(x))
    ret += (20.0 * math.sin(6.0 * x * PI) + 20.0 * math.sin(2.0 * x * PI)) * 2.0 / 3.0
    ret += (20.0 * math.sin(x * PI) + 40.0 * math.sin(x / 3.0 * PI)) * 2.0 / 3.0
    ret += (150.0 * math.sin(x / 12.0 * PI) + 300.0 * math.sin(x / 30.0 * PI)) * 2.0 / 3.0
    return ret


def wgs84_to_gcj02(lat: float, lon: float) -> tuple[float, float]:
    if _out_of_china(lat, lon):
        return lat, lon
    dlat = _transform_lat(lon - 105.0, lat - 35.0)
    dlon = _transform_lon(lon - 105.0, lat - 35.0)
    rad_lat = lat / 180.0 * PI
    magic = math.sin(rad_lat)
    magic = 1 - EE * magic * magic
    sqrt_magic = math.sqrt(magic)
    dlat = (dlat * 180.0) / ((A * (1 - EE)) / (magic * sqrt_magic) * PI)
    dlon = (dlon * 180.0) / (A / sqrt_magic * math.cos(rad_lat) * PI)
    return lat + dlat, lon + dlon


def meters_offset(lat: float, lon: float) -> tuple[float, float]:
    """返回 (东向偏移米, 北向偏移米)：WGS84 → GCJ-02。"""
    glat, glon = wgs84_to_gcj02(lat, lon)
    m_per_deg_lat = 111132.92 - 559.82 * math.cos(2 * math.radians(lat)) + 1.175 * math.cos(4 * math.radians(lat))
    m_per_deg_lon = 111412.84 * math.cos(math.radians(lat)) - 93.5 * math.cos(3 * math.radians(lat))
    return (glon - lon) * m_per_deg_lon, (glat - lat) * m_per_deg_lat


def main() -> int:
    print(f"{'校区':8s} {'中心纬度':>10s} {'中心经度':>11s} {'东向(m)':>9s} {'北向(m)':>9s} {'位移(m)':>8s}")
    for key, cfg in CAMPUSES.items():
        lat, lon = cfg.center
        dx, dy = meters_offset(lat, lon)
        print(f"{cfg.short:8s} {lat:10.5f} {lon:11.5f} {dx:9.1f} {dy:9.1f} {math.hypot(dx, dy):8.1f}")
    lat, lon = next(iter(CAMPUSES.values())).center
    print(f"\n→ 不纠偏的话，高德卫星底图上照片点位会整体偏移约 "
          f"{math.hypot(*meters_offset(lat, lon)):.0f} 米（约 {math.hypot(*meters_offset(lat, lon)) / 100:.1f} 个街区）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
