"""端到端集成测试：模拟一次真实投稿 → 汇总 → 生成地图，确认全链路打通。

跑法: python tools/test_e2e.py
会在临时目录里生成一份投稿，构建地图，然后校验 HTML 里的关键内容。
"""
from __future__ import annotations

import json
import re
import sys
import tempfile
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from campus_config import CAMPUSES  # noqa: E402
from photos import make_thumb  # noqa: E402
from submission_data import Submission, build_landmark_index, cluster_spots, resolve_location  # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  {'✓' if cond else '✗'} {name}{('  ' + detail) if detail else ''}")
    if not cond:
        FAILS.append(name)


def main() -> int:
    import map_build
    from demo_data import synth_sky

    tmp = Path(tempfile.mkdtemp())
    idx = build_landmark_index()

    print("\n[1] 模拟 3 个校区的投稿（含点选 / EXIF / 纯文字三种定点路径）")
    cases = [
        # (校区, 点选坐标, EXIF, 文字, 天象)
        ("gulou", (32.05714, 118.77421), None, "北大楼前草坪", "晚霞"),
        ("xianlin", None, (32.116308, 118.955387), "", "朝霞"),
        ("suzhou", None, None, "庄里山", "雾霾"),
    ]
    subs: list[Submission] = []
    thumbs = tmp / "thumbs"
    thumbs.mkdir(parents=True, exist_ok=True)
    for i, (ck, picked, exif, text, kind) in enumerate(cases, 1):
        r = resolve_location(campus_key=ck, picked=picked, exif=exif, text=text, index=idx)
        img = tmp / f"p{i}.jpg"
        synth_sky(kind, w=800, h=560, seed=200 + i).save(img, "JPEG")
        th = f"t{i}.jpg"
        make_thumb(img, thumbs / th, max_side=600)
        subs.append(
            Submission(
                sid=f"P{i:04d}", campus=ck, lat=r["lat"], lon=r["lon"],
                loc_text=text, loc_matched=r["matched"], loc_score=r["score"],
                loc_source=r["source"], loc_verify=r["verify"],
                title=f"测试作品{i}", author=f"同学{i}", shot_time=f"2026-10-{10 + i:02d} 17:3{i}",
                weather=kind, thumb=th, submitted_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            )
        )
        check(f"{ck} 定点成功", r["lat"] is not None, f"来源={r['source']} 匹配={r['matched']}")

    print("\n[2] 聚类")
    spots = cluster_spots(subs, radius_m=60, index=idx)
    check("生成 3 个打卡点（三校区各一）", len(spots) == 3, f"{len(spots)} 个")

    print("\n[3] 构建地图 payload")
    payload = map_build.build_payload(subs, embed_photos=False, thumbs_dir=thumbs)
    check("payload 含 3 个校区", len(payload["campuses"]) == 3)
    for k in ("gulou", "xianlin", "suzhou"):
        c = payload["campuses"][k]
        check(f"{k} 有街道/影像瓦片", bool(c["streets"]["url"]) and bool(c["imagery"]["url"]))
        check(f"{k} 有纠偏量", "image" in c["offsets"], str([round(v) for v in c["offsets"]["image"]]))
    check("瓦片源不是 CARTO（已知会返回占位图）",
          all("cartocdn" not in payload["campuses"][k]["streets"]["url"] for k in CAMPUSES))
    check("建筑底图已内嵌", sum(len(v) for v in payload["buildings"].values()) > 500,
          f"{sum(len(v) for v in payload['buildings'].values())} 栋")
    check("天象分类齐全", len(payload["weatherKinds"]) >= 9)

    print("\n[4] 渲染 HTML")
    out = map_build.write_map(payload, offline=False, out=tmp / "map.html")
    html = out.read_text(encoding="utf-8")
    check("HTML 已生成", out.exists() and len(html) > 100_000, f"{len(html) // 1024} KB")
    check("Leaflet 已引入", "leaflet@1.9.4" in html)
    check("OFFLINE 标志为 false", "const OFFLINE = false;" in html)
    check("payload 已注入", '"spots"' in html and '"campuses"' in html)
    m = re.search(r"const DATA = (\{.*?\});\n", html, re.S)
    check("payload 是合法 JSON", bool(m))
    if m:
        data = json.loads(m.group(1))
        check("JSON 里打卡点数一致", len(data["spots"]) == 3)
        check("每个打卡点都有照片", all(sp["shots"] and sp["shots"][0]["src"] for sp in data["spots"]))
    # 2026-10-06：底图切换去掉后，在线瓦片与 GCJ 纠偏代码都删了（只剩自绘立体图，
    # 它本身就是 WGS84、不需要纠偏）。判据相应改成"**不再有** GCJ 瓦片逻辑"，
    # ⚠ 查的是定义/调用（`gcjTileLayer(`），不是裸名字 —— 生成出来的 JS 里留着
    #   解释这件事的注释，注释里就会提到它（本项目为此误报过多次）。
    check("已移除 GCJ 瓦片纠偏逻辑（只剩自绘立体底图）",
          "gcjTileLayer(" not in html and "__OFFSET__" not in html)
    check("点位坐标变换仍在（现在是恒等，保留以免改十几处调用点）", "function tp(" in html)

    print("\n[5] 无未定点遗漏")
    check("unresolved 为空", payload["unresolved"] == [], str(payload["unresolved"]))

    print("\n" + "=" * 56)
    if FAILS:
        print(f"✗ {len(FAILS)} 项失败：" + "、".join(FAILS))
        return 1
    print("✓ 端到端全部通过")
    print(f"（产物在临时目录 {tmp}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
