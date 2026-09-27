"""生成示例投稿数据，用于端到端验证地图效果。

合成的是"天空感"的渐变+云图，不是真实照片；正式征稿时把
data/submissions.jsonl 与 data/thumbs/ 清空即可。

用法: python demo_data.py
"""
from __future__ import annotations

import json
import math
import random
from datetime import datetime, timedelta
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

from campus_config import CAMPUSES
from photos import make_thumb
from submission_data import DATA, THUMBS, Submission, save_submissions

random.seed(20261008)

SKY_PALETTES = {
    "晚霞": [(28, 42, 92), (86, 62, 128), (214, 108, 74), (247, 186, 110)],
    "朝霞": [(38, 58, 112), (108, 108, 168), (232, 158, 120), (252, 216, 164)],
    "积云": [(74, 140, 222), (132, 184, 236), (200, 222, 244), (246, 250, 255)],
    "层云": [(96, 106, 122), (140, 150, 166), (186, 194, 206), (216, 222, 232)],
    "雨虹": [(64, 78, 96), (98, 128, 138), (150, 190, 180), (206, 230, 224)],
    "雷电": [(18, 20, 38), (46, 44, 78), (86, 78, 124), (140, 126, 168)],
    "月相": [(10, 14, 34), (24, 32, 66), (48, 60, 104), (92, 106, 152)],
    "雾霾": [(150, 148, 140), (186, 182, 170), (214, 208, 194), (236, 230, 218)],
    "其他": [(52, 92, 152), (110, 152, 200), (176, 204, 230), (228, 240, 250)],
}

DEMO = [
    # (campus, 位置描述, 作品名, 天象, 机位提示)
    ("gulou", "北大楼前草坪", "北大楼上的火烧云", "晚霞", "草坪正中低机位仰拍，让北大楼剪影压住右下角，等日落后 10 分钟云底透亮"),
    ("gulou", "北大楼", "爬山虎与积云", "积云", "北大楼东侧墙面，顺着爬山虎的斜线往天空带，午后 3 点光比最合适"),
    ("gulou", "大礼堂", "礼堂尖顶上的高积云", "积云", "大礼堂正前方 20 米，长焦压缩，让云层贴着尖顶"),
    ("gulou", "鼓楼", "鼓楼夕照", "晚霞", "鼓楼广场西北角，日落方向无遮挡，广角收全景"),
    ("gulou", "南京大学图书馆（鼓楼）", "图书馆玻璃幕墙的晚霞", "晚霞", "图书馆南侧水面前，利用幕墙与倒影做对称构图"),
    ("gulou", "小礼堂", "雨后小礼堂与虹", "雨虹", "雨后太阳出来立刻转到小礼堂东南侧，背对太阳找虹"),
    ("gulou", "赛珍珠故居", "故居屋顶的月升", "月相", "故居西侧小道，月亮刚过屋顶线时按快门，长焦"),
    ("gulou", "北园草坪", "草坪上空的平流雾", "雾霾", "清晨 6:30 前后，教学楼高层俯拍，雾面与屋顶形成层次"),
    ("gulou", "天文与空间科学学院", "学院楼顶拍卷云", "积云", "楼顶制高点，用 24mm 横扫，云丝从楼角斜穿画面"),
    ("xianlin", "仙林图书馆", "图书馆前的星空", "月相", "图书馆北侧台阶，避开路灯后长曝 15 秒，月亮做点景"),
    ("xianlin", "仙林操场", "操场看台的雷暴云", "雷电", "看台最高一排，广角抓云砧；雷声近时立刻撤离"),
    ("xianlin", "仙林校区大门", "校门与雨后彩虹", "雨虹", "校门外 30 米回望校门，让彩虹从门楣上方跨过"),
    ("xianlin", "藜照湖", "藜照湖落日倒影", "晚霞", "湖东岸贴近水面，倒影占下半幅，注意别踩进水里"),
    ("xianlin", "仙林教学楼", "教学楼玻璃上的朝霞", "朝霞", "教学楼高层窗内向外拍，玻璃反光叠出双重天空"),
    ("xianlin", "仙林体育馆", "馆顶与积雨云", "积云", "体育馆西侧空地，仰角 45°，云顶像雪山压在馆顶上"),
    ("suzhou", "苏州校区图书馆", "苏州校区第一场晚霞", "晚霞", "图书馆前广场，日落方向正对中轴，对称构图"),
    ("suzhou", "苏州校区操场", "操场上的卷云", "积云", "操场中圈，广角贴地仰拍，跑道弧线做引导线"),
    ("suzhou", "苏州校区南门", "南门暮色", "晚霞", "南门内侧台阶，蓝调时刻（日落后 20 分钟）拍灯光与暮色"),
    ("suzhou", "庄里山", "庄里山看平流雾", "雾霾", "半山腰平台俯拍校园，晨雾未散时的“天空之城”"),
    ("suzhou", "苏州校区湖面", "湖面倒影与层云", "层云", "湖西岸，无风时段水面最平静，倒影最完整"),
]

AUTHORS = ["付云航", "陈思远", "林晚晴", "周予安", "苏和光", "顾一苇", "许听澜", "沈亦白", "叶知秋", "温言之"]
AWARDS = ["", "", "一等奖", "", "二等奖", "", "", "三等奖", "", "人气奖"]


def synth_sky(kind: str, w: int = 1200, h: int = 800, seed: int = 0) -> Image.Image:
    """合成一张"天空感"的示意图：渐变天空 + 云 + 太阳/月亮 + 建筑剪影。

    分层合成，最后叠暗角；不做全局混色，避免把天空洗灰。
    """
    rnd = random.Random(seed)
    pal = SKY_PALETTES.get(kind, SKY_PALETTES["其他"])
    horizon = int(h * rnd.uniform(0.78, 0.90))

    # --- 1. 天空垂直渐变（水平条带 + 轻微横向色偏，避免死板） ---
    sky = Image.new("RGB", (w, horizon))
    sd = ImageDraw.Draw(sky)
    for y in range(horizon):
        t = y / max(1, horizon - 1)
        seg = t * (len(pal) - 1)
        i = min(int(seg), len(pal) - 2)
        f = seg - i
        base = [int(pal[i][k] * (1 - f) + pal[i + 1][k] * f) for k in range(3)]
        sd.line([(0, y), (w, y)], fill=tuple(base))
        # 暖色偏移：靠近太阳那一侧更暖
        warm = int(14 * (1 - abs(t - 0.72) * 2.4)) if t > 0.35 else 0
        if warm > 0:
            sd.line([(0, y), (w, y)], fill=tuple(min(255, base[k] + (warm if k == 0 else 0)) for k in range(3)))

    # --- 2. 太阳 / 月亮：加色发光（radial glow） ---
    glow = Image.new("L", (w, horizon), 0)
    gd = ImageDraw.Draw(glow)
    if kind == "月相":
        mx, my = rnd.randint(int(w * 0.55), int(w * 0.85)), int(horizon * 0.22)
        mr = 22
        gd.ellipse([mx - mr * 5, my - mr * 5, mx + mr * 5, my + mr * 5], fill=60)
        gd.ellipse([mx - mr, my - mr, mx + mr, my + mr], fill=255)
        moon = Image.new("RGB", (w, horizon), (250, 248, 232))
        sky = Image.composite(moon, sky, glow.point(lambda v: 255 if v > 200 else 0))
    elif kind in ("晚霞", "朝霞", "积云", "其他"):
        sx = rnd.randint(int(w * 0.22), int(w * 0.78))
        sy = int(horizon * (0.66 if kind in ("晚霞", "朝霞") else 0.24))
        gd.ellipse([sx - 220, sy - 220, sx + 220, sy + 220], fill=150)
    glow = glow.filter(ImageFilter.GaussianBlur(58))
    tint = (255, 214, 150) if kind in ("晚霞", "朝霞") else (255, 250, 230)
    glow_rgb = Image.merge("RGB", [glow.point(lambda v, c=c: min(255, int(v * c / 255))) for c in tint])
    sky = _screen(sky, glow_rgb, 0.72)

    # --- 3. 云：模糊的亮斑，直接以遮罩合成 ---
    cloud_mask = Image.new("L", (w, horizon), 0)
    cd = ImageDraw.Draw(cloud_mask)
    for _ in range(rnd.randint(10, 20)):
        cx, cy = rnd.randint(-120, w + 120), rnd.randint(int(horizon * 0.05), int(horizon * 0.72))
        rx, ry = rnd.randint(80, 300), rnd.randint(22, 70)
        cd.ellipse([cx - rx, cy - ry, cx + rx, cy + ry], fill=rnd.randint(130, 235))
    cloud_mask = cloud_mask.filter(ImageFilter.GaussianBlur(rnd.randint(24, 46)))
    bright = tuple(min(255, int(c * 1.22) + 30) for c in pal[-1])
    cloud_rgb = Image.new("RGB", (w, horizon), bright)
    sky = Image.composite(cloud_rgb, sky, cloud_mask)

    # --- 4. 拼上地面剪影 ---
    img = Image.new("RGB", (w, h), (10, 12, 20))
    img.paste(sky, (0, 0))
    d = ImageDraw.Draw(img)
    sil = (16, 20, 34) if kind not in ("雾霾", "层云") else (58, 62, 74)
    x = -20
    while x < w:
        bw = rnd.randint(48, 150)
        bh = rnd.randint(70, 250)
        top = max(0, horizon - bh)
        d.rectangle([x, top, x + bw, horizon], fill=sil)
        if rnd.random() > 0.6:
            d.polygon([(x, top), (x + bw // 2, max(0, top - rnd.randint(20, 60))), (x + bw, top)], fill=sil)
        x += bw + rnd.randint(6, 28)

    # --- 5. 天气特效 ---
    if kind == "雨虹":
        for i, col in enumerate([(226, 92, 92), (232, 184, 88), (112, 200, 126), (92, 152, 232)]):
            d.arc([-w // 4, horizon - int(h * 0.72) - i * 10, w + w // 4, horizon + int(h * 0.5) - i * 10],
                  205, 335, fill=col, width=10)
    if kind == "雷电":
        px, py = rnd.randint(int(w * 0.3), int(w * 0.7)), int(horizon * 0.12)
        for _ in range(8):
            nx, ny = px + rnd.randint(-70, 70), py + rnd.randint(45, 85)
            if ny > horizon:
                break
            d.line([px, py, nx, ny], fill=(255, 252, 214), width=rnd.randint(2, 6))
            px, py = nx, ny

    # --- 6. 暗角（只压四周，中心保持亮度） ---
    vig = Image.new("L", (w, h), 0)
    ImageDraw.Draw(vig).ellipse([-w * 0.35, -h * 0.35, w * 1.35, h * 1.35], fill=255)
    vig = vig.filter(ImageFilter.GaussianBlur(140))
    dark = Image.new("RGB", (w, h), (0, 0, 0))
    return Image.composite(img, dark, vig)


def _screen(a: Image.Image, b: Image.Image, amount: float = 1.0) -> Image.Image:
    """屏幕混合（比 blend 更接近"发光"），用于太阳光晕。"""
    from PIL import ImageChops

    out = ImageChops.screen(a, b)
    return out if amount >= 1.0 else Image.blend(a, out, amount)


def main() -> int:
    for d in (DATA, THUMBS):
        d.mkdir(parents=True, exist_ok=True)
    photos = DATA / "photos"
    photos.mkdir(exist_ok=True)

    start = datetime(2026, 10, 8, 9, 0)
    subs: list[Submission] = []
    today = datetime(2026, 11, 15, 20, 0)

    for i, (ck, loc, title, kind, tip) in enumerate(DEMO, 1):
        cfg = CAMPUSES[ck]
        # 位置：优先落在配置地标附近，否则落在校区框内随机
        base = None
        for nm, (la, lo) in cfg.landmarks.items():
            if nm and nm in loc:
                base = (la, lo)
                break
        if base is None:
            s, w, n, e = cfg.bbox
            base = ((s + n) / 2 + random.uniform(-0.0025, 0.0025), (w + e) / 2 + random.uniform(-0.0025, 0.0025))
        la = base[0] + random.uniform(-0.00045, 0.00045)
        lo = base[1] + random.uniform(-0.00045, 0.00045)

        sid = f"P{i:04d}"
        shot = start + timedelta(days=random.randint(0, 37), hours=random.randint(6, 20), minutes=random.choice([0, 12, 25, 40, 55]))
        img = synth_sky(kind, seed=1000 + i)
        origin = photos / f"demo_{sid}.jpg"
        img.save(origin, "JPEG", quality=88)
        thumb = f"demo_{sid}.jpg"
        make_thumb(origin, THUMBS / thumb, max_side=900)

        subs.append(
            Submission(
                sid=sid, campus=ck,
                lat=round(la, 7), lon=round(lo, 7),
                picked_lat=round(la, 7), picked_lon=round(lo, 7),
                loc_text=loc, loc_matched=loc, loc_score=1.0, loc_source="picked", loc_verify=False,
                title=title, author=AUTHORS[(i - 1) % len(AUTHORS)], contact=f"2413800{i:02d}",
                shot_time=shot.strftime("%Y-%m-%d %H:%M"),
                weather=kind if kind != "雨虹" else "雨后彩虹",
                camera=random.choice(["iPhone 15 Pro", "Sony A7M4 + 24-70GM", "Nikon Z6 + 14-24", "DJI Air 3", "Canon R6 + 70-200"]),
                note=tip,
                photo=str(origin.relative_to(DATA)), thumb=thumb,
                submitted_at=(shot + timedelta(hours=random.randint(2, 40))).strftime("%Y-%m-%d %H:%M:%S"),
                award=AWARDS[(i - 1) % len(AWARDS)],
                is_demo=True,
            )
        )

    save_submissions(subs)
    print(f"已生成 {len(subs)} 条示例投稿 -> {DATA / 'submissions.jsonl'}")
    print(f"缩略图 -> {THUMBS}")
    print("提示：正式征稿前请清空 data/submissions.jsonl 与 data/thumbs/ 里的 demo_* 文件，")
    print("      或直接运行 python reset_data.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
