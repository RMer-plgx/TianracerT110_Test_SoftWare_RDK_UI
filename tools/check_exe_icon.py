"""严格核查 PE 文件里嵌入的所有图标资源。

不能只用 Icon.ExtractAssociatedIcon —— 它只返回一个尺寸，
而且可能返回 icon group 里排序最靠前的那个，掩盖其它尺寸没换的情况。
这里直接把 RT_GROUP_ICON / RT_ICON 解析出来，逐个尺寸保存成图片。
"""

import struct
import sys
from pathlib import Path

EXE = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("dist/TianracerT110_Test_SoftWare_RDK.exe")
OUT_DIR = Path("_icon_dump")


def read_pe(path: Path):
    data = path.read_bytes()
    if data[:2] != b"MZ":
        raise SystemExit("不是 PE 文件")
    pe_off = struct.unpack_from("<I", data, 0x3C)[0]
    if data[pe_off:pe_off + 4] != b"PE\0\0":
        raise SystemExit("PE 签名不对")
    coff = pe_off + 4
    num_sections = struct.unpack_from("<H", data, coff + 2)[0]
    opt_size = struct.unpack_from("<H", data, coff + 16)[0]
    opt_off = coff + 20
    magic = struct.unpack_from("<H", data, opt_off)[0]
    if magic == 0x20B:
        dd_off = opt_off + 112
    else:
        dd_off = opt_off + 96
    res_rva, res_size = struct.unpack_from("<II", data, dd_off + 16)
    sec_off = opt_off + opt_size
    sections = []
    for i in range(num_sections):
        base = sec_off + i * 40
        name = data[base:base + 8].rstrip(b"\0").decode("ascii", "replace")
        vsize, vaddr, rsize, raddr = struct.unpack_from("<IIII", data, base + 8)
        sections.append((name, vaddr, vsize, raddr, rsize))
    return data, res_rva, res_size, sections


def rva_to_off(rva, sections):
    for name, vaddr, vsize, raddr, rsize in sections:
        if vaddr <= rva < vaddr + max(vsize, rsize):
            return raddr + (rva - vaddr)
    raise SystemExit(f"RVA {rva:#x} 不在任何 section 内")


def walk_dir(data, base, off, sections, depth=0, prefix=()):
    """遍历资源目录，返回 [(type_id, name_id, lang_id, data_rva, size)]。"""
    out = []
    named = struct.unpack_from("<H", data, base + off + 12)[0]
    id_count = struct.unpack_from("<H", data, base + off + 14)[0]
    entry = base + off + 16 + named * 8
    for i in range(id_count):
        name_id, data_off = struct.unpack_from("<II", data, entry + i * 8)
        if data_off & 0x80000000:
            sub = data_off & 0x7FFFFFFF
            out.extend(walk_dir(data, base, sub, sections, depth + 1,
                                prefix + (name_id,)))
        else:
            rva, size = struct.unpack_from("<II", data, base + data_off)
            out.append((prefix + (name_id,), rva, size))
    return out


def main() -> int:
    data, res_rva, res_size, sections = read_pe(EXE)
    base = rva_to_off(res_rva, sections)
    resources = walk_dir(data, base, 0, sections)

    groups = [r for r in resources if len(r[0]) >= 2 and r[0][0] == 14]
    icons = [r for r in resources if len(r[0]) >= 2 and r[0][0] == 3]

    print(f"文件: {EXE}")
    print(f"图标组 (RT_GROUP_ICON): {len(groups)}")
    print(f"图标项 (RT_ICON): {len(icons)}")

    if not icons:
        print("\n❌ 没有任何图标资源 —— 图标根本没打进去")
        return 1

    OUT_DIR.mkdir(exist_ok=True)
    for path_ids, rva, size in icons:
        name_id = path_ids[1] if len(path_ids) > 1 else 0
        offset = rva_to_off(rva, sections)
        blob = data[offset:offset + size]
        target = OUT_DIR / f"icon_{name_id}.ico"
        # RT_ICON 是裸的 DIB 或 PNG，包成 ico 容器看尺寸
        target.write_bytes(blob)
        kind = "PNG" if blob[:8] == b"\x89PNG\r\n\x1a\n" else "DIB"
        dims = ""
        if kind == "PNG":
            w, h = struct.unpack_from(">II", blob, 16)
            dims = f"{w}x{h}"
        else:
            header = struct.unpack_from("<IiiHHIIiiII", blob, 0)
            w, h = header[1], header[2] // 2
            dims = f"{w}x{h}"
        print(f"  RT_ICON #{name_id}: {size} 字节, {kind}, {dims}")

    print(f"\n已导出到 {OUT_DIR}/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
