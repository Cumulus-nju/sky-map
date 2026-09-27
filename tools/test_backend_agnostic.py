"""后端无关性测试：用内存假存储跑完整流程，证明换后端不影响功能。

这是部署到 Supabase 前最重要的一道保险 —— 只要这个测试过，
就说明"本地能跑"的功能，换到任意实现了 Store 接口的后端都能跑。

用法: python tools/test_backend_agnostic.py
"""
from __future__ import annotations

import io
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  {'✓' if cond else '✗'} {name}{('  ' + detail) if detail else ''}")
    if not cond:
        FAILS.append(name)


class MemoryStore:
    """完全内存的 Store 实现，用来验证上层不依赖文件系统。"""

    kind = "memory"

    def __init__(self):
        self._subs: list = []
        self._photos: dict[str, bytes] = {}
        self._thumbs: dict[str, bytes] = {}

    # ---- 投稿 ----
    def read_submissions(self):
        return list(self._subs)

    def write_submissions(self, subs):
        self._subs = list(subs)
        return len(self._subs)

    # ---- 照片 ----
    def put_photo(self, pid, data, mime="image/jpeg"):
        self._photos[pid] = data
        return pid

    def put_thumb(self, pid, data):
        self._thumbs[pid] = data
        return pid

    def get_photo(self, ref):
        return self._photos.get(ref)

    def get_thumb(self, ref):
        return self._thumbs.get(ref)

    def delete_photo(self, ref):
        self._photos.pop(ref, None)
        self._thumbs.pop(ref, None)

    def describe(self):
        return "内存假后端"


def main() -> int:
    import submission_data as sd
    import store as store_mod
    from demo_data import synth_sky
    from photos import make_thumb

    mem = MemoryStore()
    store_mod.get_store(force=mem)          # 全局切到假后端
    # submission_data 每次都通过 get_store() 取，所以自动生效
    assert sd._store() is mem, "submission_data 没有走存储层"

    print("\n[1] 假后端生效")
    check("storage_kind = memory", store_mod.storage_kind() == "memory")

    print("\n[2] 投稿写真（模拟 app.py 的 save_photo）")
    from submission_data import Submission, next_sid, resolve_location

    kind = "晚霞"
    raw = io.BytesIO()
    synth_sky(kind, w=800, h=560, seed=99).save(raw, "JPEG")
    raw_bytes = raw.getvalue()

    origin_ref = mem.put_photo("s1", raw_bytes, "image/jpeg")
    buf = io.BytesIO()
    make_thumb(io.BytesIO(raw_bytes), buf)
    thumb_ref = mem.put_thumb("s1", buf.getvalue())
    check("原图入库", mem.get_photo(origin_ref) == raw_bytes)
    check("缩略图入库", bool(mem.get_thumb(thumb_ref)))

    print("\n[3] 元数据与照片流经数据层")
    r = resolve_location(campus_key="gulou", picked=(32.05714, 118.77421))
    rec = Submission(sid="P0001", campus="gulou", lat=r["lat"], lon=r["lon"],
                     title="假后端测试", author="测试", photo=origin_ref,
                     thumb=f"{thumb_ref}.jpg", thumb_ref=thumb_ref,
                     weather=kind, shot_time="2026-10-20 17:40")
    mem.write_submissions([rec])
    subs = sd.load_submissions()
    check("读到 1 条", len(subs) == 1)
    s = subs[0]
    check("photo_bytes 原图可取", sd.photo_bytes(s, "origin") == raw_bytes)
    check("photo_bytes 缩略图可取", bool(sd.photo_bytes(s, "thumb")))
    check("thumb_local_path 为 None（非本地后端）", sd.thumb_local_path(s) is None)
    check("thumb_display_src 回退成字节", isinstance(sd.thumb_display_src(s), (bytes, bytearray)))

    print("\n[4] 生成地图（照片应从后端取，不受文件系统影响）")
    import map_build

    payload = map_build.build_payload(subs, embed_photos=True)
    check("有 1 个打卡点", len(payload["spots"]) == 1)
    shots = payload["spots"][0]["shots"]
    check("作品列表非空", len(shots) == 1)
    check("照片已内嵌为 data URL", bool(shots and shots[0]["src"].startswith("data:image")),
          (shots[0]["src"][:24] + "…") if shots and shots[0]["src"] else "无")
    html = map_build.render_html(payload, offline=True)
    check("HTML 渲染成功", len(html) > 100_000, f"{len(html)//1024} KB")

    print("\n[5] 云端模式：在线版也要内嵌照片（没有本地 thumbs 文件可引用）")
    payload_online = map_build.build_payload(subs, embed_photos=False)
    src_online = payload_online["spots"][0]["shots"][0]["src"]
    check("在线版也内嵌（cloud 判定）", src_online.startswith("data:image"),
          src_online[:24] + "…")

    print("\n[6] 管理端：改与删")
    sd.update_submission("P0001", title="改过了", loc_verify=False)
    check("修改生效", sd.load_submissions()[0].title == "改过了")
    check("删除成功", sd.delete_submission("P0001", purge_files=True) is True)
    check("删干净了", len(sd.load_submissions()) == 0)
    check("照片也清了", mem.get_photo(origin_ref) is None)

    print("\n[7] 恢复：本地后端仍然正常")
    store_mod.reset_store()
    check("恢复为 local", store_mod.storage_kind() == "local")

    print("\n" + "=" * 60)
    if FAILS:
        print(f"✗ {len(FAILS)} 项失败：" + "、".join(FAILS))
        return 1
    print("✓ 后端无关性验证通过 —— 上层代码不依赖本地文件")
    return 0


if __name__ == "__main__":
    sys.exit(main())
