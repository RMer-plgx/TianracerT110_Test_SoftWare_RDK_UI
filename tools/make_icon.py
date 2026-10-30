"""把源 PNG 处理成 exe 用的 ico。

**关键：小尺寸必须用 BMP(DIB) 格式，不能用 PNG。**

Windows 对 ICO 里的 PNG 压缩只可靠支持 256x256。
16/32/48 这类常用尺寸如果是 PNG，资源管理器在列表/中等图标视图下
读不出来，就会回退成默认图标——表现为「明明嵌了图标却不显示」。

Pillow 的 ICO 保存默认把所有尺寸都压成 PNG，这正是之前的坑。
所以这里手写 ICO 容器：<256 用 BMP(DIB)，256 用 PNG。
"""

import io
import struct
from pathlib import Path

from PIL import Image

SOURCE = Path(r"D:\Download2\Tianbot\DM_20261030220930_001.png")
OUTPUT = Path(__file__).resolve().parent.parent / "assets" / "app.ico"

SIZES = [16, 32, 48, 64, 128, 256]
MARGIN_RATIO = 0.08


def square_canvas(image):
    """裁掉空白，补成正方形，图案居中。"""
    bbox = image.getbbox()
    if bbox is None:
        raise SystemExit("源图为空")
    cropped = image.crop(bbox)

    side = max(cropped.size)
    margin = int(side * MARGIN_RATIO)
    canvas_side = side + margin * 2
    canvas = Image.new("RGBA", (canvas_side, canvas_side), (0, 0, 0, 0))
    canvas.paste(
        cropped,
        ((canvas_side - cropped.size[0]) // 2,
         (canvas_side - cropped.size[1]) // 2),
    )
    return canvas


def to_dib(image):
    """RGBA 图 → ICO 内嵌的 BMP(DIB)。

    结构：BITMAPINFOHEADER + BGRA 像素（自下而上）+ AND 透明遮罩。
    biHeight 要写成图像高度的 2 倍，规范要求同时容纳 XOR 和 AND 两张位图。
    """
    width, height = image.size
    pixels = image.load()

    header = struct.pack(
        "<IiiHHIIiiII",
        40,                  # biSize
        width,               # biWidth
        height * 2,          # biHeight
        1,                   # biPlanes
        32,                  # biBitCount
        0,                   # biCompression = BI_RGB
        width * height * 4,  # biSizeImage
        0, 0, 0, 0,
    )

    xor_rows = []
    for y in range(height - 1, -1, -1):
        row = bytearray()
        for x in range(width):
            r, g, b, a = pixels[x, y]
            row += bytes((b, g, r, a))
        xor_rows.append(bytes(row))
    xor_data = b"".join(xor_rows)

    mask_stride = ((width + 31) // 32) * 4
    and_rows = []
    for y in range(height - 1, -1, -1):
        row = bytearray(mask_stride)
        for x in range(width):
            if pixels[x, y][3] == 0:
                row[x // 8] |= 1 << (7 - (x % 8))
        and_rows.append(bytes(row))
    and_data = b"".join(and_rows)

    return header + xor_data + and_data


def to_png(image):
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def build_ico(canvas):
    entries = []
    for size in SIZES:
        resized = canvas.resize((size, size), Image.LANCZOS)
        payload = to_png(resized) if size >= 256 else to_dib(resized)
        entries.append((size, payload))

    directory = b""
    data = b""
    offset = 6 + 16 * len(entries)
    for size, payload in entries:
        directory += struct.pack(
            "<BBBBHHII",
            size if size < 256 else 0,
            size if size < 256 else 0,
            0, 0, 1, 32,
            len(payload),
            offset,
        )
        data += payload
        offset += len(payload)

    return struct.pack("<HHH", 0, 1, len(entries)) + directory + data


def verify(blob):
    """自检：小尺寸必须是 BMP，否则资源管理器读不出来。"""
    _, _, count = struct.unpack_from("<HHH", blob, 0)
    offset = 6
    ok = True
    for _ in range(count):
        w, _h, _, _, _, _, size, data_off = struct.unpack_from(
            "<BBBBHHII", blob, offset)
        w = w or 256
        kind = "PNG" if blob[data_off:data_off + 8] == b"\x89PNG\r\n\x1a\n" else "BMP"
        bad = (w < 256 and kind == "PNG")
        if bad:
            ok = False
        print(f"  {w}x{w}  {kind}  {size} bytes" + ("  ← 小尺寸不能用 PNG" if bad else ""))
        offset += 16
    return ok


def main():
    image = Image.open(SOURCE).convert("RGBA")
    canvas = square_canvas(image)
    print(f"正方形画布: {canvas.size[0]}x{canvas.size[1]}")

    blob = build_ico(canvas)
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_bytes(blob)

    print(f"已生成: {OUTPUT}（{len(blob)} 字节）")
    print("条目检查:")
    if not verify(blob):
        print("❌ 校验失败")
        return 1
    print("✅ 校验通过：小尺寸均为 BMP")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
