# -*- coding: utf-8 -*-
"""把 GLB 三维模型拆成"按图层分组的三角面"，存成 npz 供渲染器直接用。

为什么要这样：GLB 里是**真三维**（建筑有高度、还有地形/道路/水面/树）。
与其把建筑拍扁成足迹再挤出（信息损失），不如直接按**轴测平行投影**渲染真实三角面 ——
立体感是模型自带的，墙面明暗可以按真实法向算，而且**地面仍是仿射映射**，
点选换算不受影响。

输出：data/raw/suzhou_glb.npz
    <group>_v : (N,3) float32  局部米制顶点（X 东、Y 上、Z **南**，见下面注释）
    <group>_f : (M,3) uint32    三角面索引
"""
from __future__ import annotations

import json
import struct
import sys
from pathlib import Path

import numpy as np

GLB = Path.home() / "Desktop" / "nju-suzhou-campus_副本4_fixed.glb"
OUT = Path(__file__).resolve().parent.parent / "data" / "raw" / "suzhou_glb.npz"

CT = {5120: ("b", 1), 5121: ("B", 1), 5122: ("h", 2), 5123: ("H", 2),
      5125: ("I", 4), 5126: ("f", 4)}
NCOMP = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4, "MAT4": 16}


def load_glb(path: Path):
    raw = path.read_bytes()
    off, chunks = 12, {}
    while off < len(raw):
        clen, ctype = struct.unpack_from("<II", raw, off)
        chunks[ctype] = raw[off + 8: off + 8 + clen]
        off += 8 + clen
    gltf = json.loads(chunks[0x4E4F534A].decode("utf-8"))
    return gltf, chunks[0x004E4942]


def read_accessor(gltf, bin_blob, idx):
    acc = gltf["accessors"][idx]
    fmt, size = CT[acc["componentType"]]
    n = acc["count"]
    ncomp = NCOMP[acc["type"]]
    bv = gltf["bufferViews"][acc["bufferView"]]
    base = bv.get("byteOffset", 0) + acc.get("byteOffset", 0)
    stride = bv.get("byteStride") or (size * ncomp)
    if stride == size * ncomp:
        arr = np.frombuffer(bin_blob, dtype=np.dtype(fmt).newbyteorder("<"),
                            count=n * ncomp, offset=base)
        return arr.reshape(n, ncomp)
    out = np.empty((n, ncomp), dtype=np.dtype(fmt).newbyteorder("<"))
    for i in range(n):
        out[i] = struct.unpack_from("<" + fmt * ncomp, bin_blob, base + i * stride)
    return out


def node_matrix(node):
    if "matrix" in node:
        return np.array(node["matrix"], dtype=np.float64).reshape(4, 4).T
    t = np.eye(4)
    if "scale" in node:
        t = np.diag(list(node["scale"]) + [1.0]) @ t
    if "rotation" in node:
        x, y, z, w = node["rotation"]
        r = np.array([
            [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w), 0],
            [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w), 0],
            [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y), 0],
            [0, 0, 0, 1]], dtype=np.float64)
        t = r @ t
    if "translation" in node:
        tr = np.eye(4)
        tr[:3, 3] = node["translation"]
        t = tr @ t
    return t


def main():
    gltf, blob = load_glb(GLB)
    print(f"GLB: {GLB.name}  meshes={len(gltf['meshes'])} nodes={len(gltf['nodes'])} "
          f"materials={len(gltf.get('materials', []))}")

    groups: dict[str, list] = {}
    stack = [(i, np.eye(4), "") for i in gltf["scenes"][gltf.get("scene", 0)]["nodes"]]
    seen = 0
    while stack:
        ni, parent_m, parent_name = stack.pop()
        node = gltf["nodes"][ni]
        name = node.get("name") or parent_name
        m = parent_m @ node_matrix(node)
        if "mesh" in node:
            mesh = gltf["meshes"][node["mesh"]]
            for prim in mesh.get("primitives", []):
                if prim.get("mode", 4) != 4:
                    continue
                attrs = prim["attributes"]
                pos = read_accessor(gltf, blob, attrs["POSITION"]).astype(np.float64)
                if pos.shape[1] == 2:
                    pos = np.hstack([pos, np.zeros((len(pos), 1))])
                v4 = np.hstack([pos, np.ones((len(pos), 1))])
                world = (v4 @ m.T)[:, :3]
                if "indices" in prim:
                    tri = read_accessor(gltf, blob, prim["indices"]).reshape(-1).astype(np.int64)
                else:
                    tri = np.arange(len(pos), dtype=np.int64)
                tri = tri.reshape(-1, 3)
                groups.setdefault(name, []).append((world, tri))
                seen += len(tri)
        for ch in node.get("children", []):
            stack.append((ch, m, name))

    print(f"  总三角面 = {seen:,}")
    out = {}
    print(f"  {'图层':28s} {'面数':>9s}   bbox")
    for name in sorted(groups):
        parts = groups[name]
        vs = [p[0] for p in parts]
        fs = [p[1] for p in parts]
        V = np.vstack(vs).astype(np.float32)
        # 合并索引时按各块偏移
        off = 0
        F = []
        for v, f in zip(vs, fs):
            F.append(f + off)
            off += len(v)
        F = np.vstack(F).astype(np.uint32)
        out[f"{name}_v"] = V
        out[f"{name}_f"] = F
        lo = V.min(axis=0); hi = V.max(axis=0)
        print(f"  {name[:28]:28s} {len(F):9,d}   "
              f"X[{lo[0]:7.1f},{hi[0]:7.1f}] Y[{lo[1]:6.1f},{hi[1]:6.1f}] Z[{lo[2]:7.1f},{hi[2]:7.1f}]")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(OUT, **out)
    print(f"\n写出 -> {OUT}  ({OUT.stat().st_size//1024} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
