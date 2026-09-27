"""照片处理：EXIF 定位读取、拍摄参数提取、缩略图生成。

手机原图的 EXIF 里通常带 GPS 与拍摄时间，这是"零操作"的定点来源；
但微信传输会剥离 EXIF，所以投稿页仍以地图点选为主、EXIF 为辅助。
"""
from __future__ import annotations

import base64
import io
from datetime import datetime
from pathlib import Path

from PIL import Image, ExifTags, ImageOps

THUMB_MAX = 900          # 缩略图长边像素
THUMB_QUALITY = 82
DATAURL_MAX = 560        # 内嵌单文件 HTML 时用的更小尺寸

_GPS_TAGS = {v: k for k, v in ExifTags.GPSTAGS.items()}
_EXIF_TAGS = {v: k for k, v in ExifTags.TAGS.items()}


def _ratio(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        try:
            return v.numerator / v.denominator
        except Exception:
            return 0.0


def _dms_to_deg(dms, ref) -> float | None:
    try:
        d, m, s = (_ratio(x) for x in dms)
    except Exception:
        return None
    val = d + m / 60.0 + s / 3600.0
    if ref in ("S", "W"):
        val = -val
    return round(val, 7)


def read_exif(file_obj) -> dict:
    """从上传文件读 EXIF。返回 dict(lat, lon, shot_time, camera, has_gps)。"""
    out: dict = {"lat": None, "lon": None, "shot_time": "", "camera": "", "has_gps": False}
    try:
        pos = file_obj.tell() if hasattr(file_obj, "tell") else None
        img = Image.open(file_obj)
        exif = img.getexif()
        if not exif:
            return out

        # 拍摄时间 / 机型
        dt = exif.get(_EXIF_TAGS.get("DateTimeOriginal")) or exif.get(_EXIF_TAGS.get("DateTime"))
        if dt:
            out["shot_time"] = str(dt).replace(":", "-", 2).replace(" ", " ")
        make = (exif.get(_EXIF_TAGS.get("Make")) or "").strip()
        model = (exif.get(_EXIF_TAGS.get("Model")) or "").strip()
        out["camera"] = " ".join(x for x in (make, model) if x and x not in model)

        # GPS
        gps = exif.get_ifd(0x8825) or {}
        if gps:
            lat = _dms_to_deg(gps.get(_GPS_TAGS.get("GPSLatitude")), gps.get(_GPS_TAGS.get("GPSLatitudeRef")))
            lon = _dms_to_deg(gps.get(_GPS_TAGS.get("GPSLongitude")), gps.get(_GPS_TAGS.get("GPSLongitudeRef")))
            if lat is not None and lon is not None and not (lat == 0 and lon == 0):
                out["lat"], out["lon"], out["has_gps"] = lat, lon, True
        if pos is not None:
            try:
                file_obj.seek(pos)
            except Exception:
                pass
    except Exception:
        pass
    return out


def _orient(img: Image.Image) -> Image.Image:
    try:
        return ImageOps.exif_transpose(img)
    except Exception:
        return img


def make_thumb_bytes(src, max_side: int = THUMB_MAX) -> bytes:
    """生成缩略图并返回字节。

    存储层可能不是文件系统（云端/内存后端），所以统一用字节入口。
    """
    img = Image.open(src)
    img = _orient(img)
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    img.thumbnail((max_side, max_side), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=THUMB_QUALITY, optimize=True, progressive=True)
    return buf.getvalue()


def make_thumb(file_obj, dest: Path | None = None, max_side: int = THUMB_MAX):
    """按长边缩放并转 JPEG，去掉 EXIF 里的隐私信息（只留图像）。

    dest 是文件路径时写文件并返回 Path；
    dest 是 BytesIO 或 None 时直接写进去 / 返回字节。
    """
    data = make_thumb_bytes(file_obj, max_side)

    if dest is None:
        return data
    if hasattr(dest, "write"):            # BytesIO 之类的文件对象
        dest.write(data)
        return dest
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    return dest


def thumb_dataurl(path: Path, max_side: int = DATAURL_MAX) -> str:
    """把缩略图文件压成 data URL（本地文件版）。"""
    if not path or not Path(path).exists():
        return ""
    return image_to_dataurl(Path(path).read_bytes(), max_side)


def image_to_dataurl(raw: bytes | None, max_side: int = DATAURL_MAX) -> str:
    """把图片字节压成 data URL，用于"单文件、零依赖"分发的离线版地图。

    云端模式下照片存在数据库里，没有本地文件，所以统一走字节入口。
    """
    if not raw:
        return ""
    try:
        img = Image.open(io.BytesIO(raw))
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        img.thumbnail((max_side, max_side), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=78, optimize=True, progressive=True)
        return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception:
        return ""


def parse_shot_time(text: str) -> str:
    """把各种手填/EXIF 时间统一成 YYYY-MM-DD HH:MM 便于排序与分摊时段统计。"""
    if not text:
        return ""
    t = str(text).strip()
    for fmt in (
        "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d",
        "%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M", "%Y/%m/%d",
        "%Y:%m:%d %H:%M:%S", "%Y:%m:%d %H:%M",
    ):
        try:
            return datetime.strptime(t, fmt).strftime("%Y-%m-%d %H:%M" if "%H" in fmt else "%Y-%m-%d")
        except ValueError:
            continue
    return t
