"""缓存苏州校区周边的高清卫星底图（供校准页离线使用）。"""
import math, io, json
from pathlib import Path
import requests
from PIL import Image

OUT = Path(r"C:\Users\Administrator\.dsh\sky_map\data\tile_check")
OUT.mkdir(parents=True, exist_ok=True)

# 覆盖范围：以 OSM 湖面点为中心，半径足够大（约 ±3 km）
REF = (31.351231, 120.392197)
M_LAT, M_LON = 110574.0, 111320.0*math.cos(math.radians(REF[0]))

def w2g(lat,lon):
    a,ee=6378245.0,0.00669342162296594323
    dlat=-100.0+2.0*lon+3.0*lat+0.2*lat*lat+0.1*lon*lat+0.2*math.sqrt(abs(lon))
    dlat+=(20.0*math.sin(6.0*lon*math.pi)+20.0*math.sin(2.0*lon*math.pi))*2.0/3.0
    dlat+=(20.0*math.sin(lat*math.pi)+40.0*math.sin(lat/3.0*math.pi))*2.0/3.0
    dlat+=(160.0*math.sin(lat/12.0*math.pi)+320*math.sin(lat*math.pi/30.0))*2.0/3.0
    dlon=300.0+lon+2.0*lat+0.1*lon*lon+0.1*lon*lat+0.1*math.sqrt(abs(lon))
    dlon+=(20.0*math.sin(6.0*lon*math.pi)+20.0*math.sin(2.0*lon*math.pi))*2.0/3.0
    dlon+=(20.0*math.sin(lon*math.pi)+40.0*math.sin(lon/3.0*math.pi))*2.0/3.0
    dlon+=(150.0*math.sin(lon/12.0*math.pi)+300.0*math.sin(lon/30.0))*2.0/3.0
    rl=math.radians(lat); m=1-ee*math.sin(rl)**2; sq=math.sqrt(m)
    return lat+(dlat*180.0)/((a*(1-ee))/(m*sq)*math.pi), lon+(dlon*180.0)/(a/sq*math.cos(rl)*math.pi)

Z = 16
def d2n(lat,lon,z):
    n=2.0**z; return (lon+180.0)/360.0*n,(1.0-math.asinh(math.tan(math.radians(lat)))/math.pi)/2.0*n

g = w2g(*REF)
cx, cy = d2n(g[0], g[1], Z)
# 覆盖 ±2.5 km：zoom16 每瓦片约 611 m
half_t = 5
tx0, tx1 = int(cx)-half_t, int(cx)+half_t
ty0, ty1 = int(cy)-half_t, int(cy)+half_t
Wpx, Hpx = (tx1-tx0+1)*256, (ty1-ty0+1)*256

canvas = Image.new("RGB", (Wpx, Hpx), (15,15,15))
sess = requests.Session(); sess.headers["User-Agent"]="Mozilla/5.0"
ok=fail=0
for tx in range(tx0, tx1+1):
    for ty in range(ty0, ty1+1):
        u = f"https://webst0{(tx+ty)%4+1}.is.autonavi.com/appmaptile?style=6&x={tx}&y={ty}&z={Z}"
        try:
            r = sess.get(u, timeout=20)
            canvas.paste(Image.open(io.BytesIO(r.content)).convert("RGB"), ((tx-tx0)*256, (ty-ty0)*256))
            ok += 1
        except Exception:
            fail += 1
print(f"瓦片 成功 {ok} / 失败 {fail}  -> {Wpx}x{Hpx}")

jpg = OUT / "suzhou_base_z16.jpg"
canvas.save(jpg, quality=88)
print(f"底图已保存: {jpg}  ({jpg.stat().st_size/1e6:.2f} MB)")

meta = {
    "zoom": Z, "tile_x0": tx0, "tile_y0": ty0, "tile_x1": tx1, "tile_y1": ty1,
    "width": Wpx, "height": Hpx,
    "gcj_center_lat": g[0], "gcj_center_lon": g[1],
    "wgs_ref_lat": REF[0], "wgs_ref_lon": REF[1],
}
(OUT / "suzhou_base_meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
print(f"元信息: {json.dumps(meta, ensure_ascii=False)}")
