"""投稿页冒烟测试：不启动浏览器，直接校验端到端逻辑。

检查：地标词典是否建起来、三种定点路径是否都能出坐标、
文字模糊匹配有没有命中、缩略图能否生成、投稿能否落盘。

用法: python tools/test_submit.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from campus_config import CAMPUSES, in_bbox  # noqa: E402
from photos import make_thumb, parse_shot_time, read_exif  # noqa: E402
from submission_data import (  # noqa: E402
    SPOT_MERGE_RADIUS_M,
    Submission,
    build_landmark_index,
    cluster_spots,
    export_csv,
    haversine,
    resolve_location,
    similarity,
)

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  {'✓' if cond else '✗'} {name}{('  ' + detail) if detail else ''}")
    if not cond:
        FAILS.append(name)


def main() -> int:
    print("\n[1] 地标词典")
    idx = build_landmark_index()
    for k in CAMPUSES:
        n = len(idx.get(k, []))
        check(f"{CAMPUSES[k].short}校区地标数 > 0", n > 0, f"{n} 个")

    print("\n[2] 地图点选定点（最高优先级）")
    lat, lon = 32.0571, 118.7742
    r = resolve_location(campus_key="gulou", picked=(lat, lon), text="")
    check("来源为 picked", r["source"] == "picked", r["source"])
    check("坐标一致", abs(r["lat"] - lat) < 1e-9 and abs(r["lon"] - lon) < 1e-9)
    check("框内不标待复核", r["verify"] is False)

    print("\n[3] 框外点选 → 标记待复核")
    r = resolve_location(campus_key="gulou", picked=(39.9, 116.4), text="")
    check("框外标待复核", r["verify"] is True)

    print("\n[4] EXIF 定位（点选缺失时兜底）")
    r = resolve_location(campus_key="gulou", exif=(32.0572, 118.7743), text="")
    check("来源为 exif", r["source"] == "exif", r["source"])

    print("\n[5] 文字描述自动匹配（模糊匹配）")
    cases = [
        ("gulou", "北大楼前草坪", "北大楼"),
        ("gulou", "图书馆南侧", "图书馆"),
        ("gulou", "大礼堂前面", "大礼堂"),
        ("xianlin", "杜厦图书馆", "杜厦图书馆"),
        ("xianlin", "藜照湖", "藜照湖"),
        ("suzhou", "庄里山", "庄里山"),
    ]
    for ck, text, expect in cases:
        rr = resolve_location(campus_key=ck, text=text, index=idx)
        hit = expect in (rr["matched"] or "")
        check(f"「{text}」→ {rr['matched']} (score={rr['score']})", hit and rr["source"] == "text")

    print("\n[6] 相似度函数")
    check("完全相同 = 1.0", similarity("北大楼", "北大楼") == 1.0)
    check("包含关系 > 0.8", similarity("北大楼", "北大楼前草坪") > 0.8, f"{similarity('北大楼','北大楼前草坪'):.2f}")
    check("无关词很低", similarity("北大楼", "藜照湖") < 0.35, f"{similarity('北大楼','藜照湖'):.2f}")

    print("\n[7] 时间解析")
    check("ISO 格式", parse_shot_time("2026-10-20 17:40:00") == "2026-10-20 17:40")
    check("EXIF 格式", parse_shot_time("2026:10:20 17:40:00") == "2026-10-20 17:40")
    check("纯日期", parse_shot_time("2026-10-20") == "2026-10-20")

    print("\n[8] 缩略图生成 + EXIF 读取")
    try:
        from demo_data import synth_sky
        tmp = Path(tempfile.mkdtemp())
        raw = tmp / "t.jpg"
        synth_sky("晚霞", w=600, h=400, seed=7).save(raw, "JPEG")
        out = make_thumb(raw, tmp / "thumb.jpg", max_side=300)
        from PIL import Image
        im = Image.open(out)
        check("缩略图长边 ≤ 300", max(im.size) <= 300, f"{im.size}")
        with raw.open("rb") as fh:
            ex = read_exif(fh)
        check("无 EXIF 时不报错", ex["has_gps"] is False)
    except Exception as exc:
        check("缩略图流程", False, f"{type(exc).__name__}: {exc}")

    print("\n[9] 打卡点聚类")
    subs = [
        Submission(sid="P0001", campus="gulou", lat=32.0571, lon=118.7742, title="A", author="甲"),
        Submission(sid="P0002", campus="gulou", lat=32.05711, lon=118.77421, title="B", author="乙"),
        Submission(sid="P0003", campus="gulou", lat=32.0528, lon=118.7732, title="C", author="丙"),
    ]
    # 不传 radius_m，用**生产默认值**（别在测试里写死数字，否则改了阈值测试还绿）
    spots = cluster_spots(subs, index=idx)
    check("相近两点并成一簇", len(spots) == 2, f"{len(spots)} 个打卡点")
    check("簇内计数正确", sorted(s.count for s in spots) == [1, 2])
    check("打卡点有编号", all(s.sid for s in spots), " ".join(s.sid for s in spots))

    print("\n[9b] 合并阈值要**严**：离得真的很近才算同一个点")
    # 用户 2026-10-05："'过近'的判断应该稍微严苛一点，防止误判 ——
    # 离得真的很近才能算。" 原来 60 m 太松（会把相邻机位并掉）
    base_lat, base_lon = 32.0571, 118.7742

    def two_at(meters: float) -> list:
        d = meters / 111320.0                       # 纬度方向：1° ≈ 111320 m
        return [
            Submission(sid="Q1", campus="gulou", lat=base_lat, lon=base_lon, title="A"),
            Submission(sid="Q2", campus="gulou", lat=base_lat + d, lon=base_lon, title="B"),
        ]

    check("阈值已按用户要求收到很严（≤ 10 m）", SPOT_MERGE_RADIUS_M <= 10.0,
          f"当前 {SPOT_MERGE_RADIUS_M:g} m（演变 60→30→10→5）")
    check("haversine 自检（5 m 换算无误）",
          abs(haversine(base_lat, base_lon, base_lat + 5 / 111320.0, base_lon) - 5) < 0.5)
    for meters, want, why in (
        (2, 1, "2 m：几乎同一个点，并"),
        (4, 1, "4 m：仍在阈值内，并"),
        (8, 2, "8 m：已超出阈值，拆开"),
        (25, 2, "25 m：明显是另一个机位，拆开"),
        (55, 2, "55 m：**旧阈值 60 m 会并，现在必须分开**"),
    ):
        got = len(cluster_spots(two_at(meters), index=idx))
        check(f"{meters:>2} m → {want} 个打卡点（{why}）", got == want, f"得到 {got}")

    print("\n[10] CSV 导出")
    subs_csv = [
        Submission(sid="P0001", campus="gulou", lat=32.0571, lon=118.7742,
                   title="A", author="甲", contact="2413800001"),
    ]
    p = export_csv(subs_csv, Path(tempfile.mkdtemp()) / "out.csv")
    check("CSV 已生成且有内容", p.exists() and p.stat().st_size > 0, f"{p.stat().st_size} bytes")
    # 隐私回归：投稿页承诺 contact「不公开」，导出表默认不能带它
    head = p.read_text(encoding="utf-8-sig").splitlines()[0]
    body = p.read_text(encoding="utf-8-sig")
    check("默认导出不含 contact 表头", "contact" not in head, head)
    check("默认导出不含联系方式数值", "2413800001" not in body)
    check("默认导出不含 photo_data（无用大字段）", "photo_data" not in head)
    # 需要发奖时显式打开
    p2 = export_csv(subs_csv, Path(tempfile.mkdtemp()) / "out2.csv", include_contact=True)
    body2 = p2.read_text(encoding="utf-8-sig")
    check("include_contact=True 时才带联系方式",
          "contact" in body2.splitlines()[0] and "2413800001" in body2)

    print("\n" + "=" * 56)
    if FAILS:
        print(f"✗ {len(FAILS)} 项失败：" + "、".join(FAILS))
        return 1
    print("✓ 全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
