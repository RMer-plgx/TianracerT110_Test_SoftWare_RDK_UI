"""把源 PNG 处理成 exe 用的 ico。

源图 2000x696 是横向的，图案在右侧、左边大片空白。
直接当 ico 会：图案很小 + 四周留白 + 非正方形被拉伸。

处理方式：
1. 按图案实际边界裁剪（去空白）
2. 补成正方向，图案居中，留 8% 边距
3. 输出多尺寸 ico（Windows 图标会在不同场景用不同尺寸）
"""

from pathlib import Path

from PIL import Image

SOURCE = Path(r"D:\Download2\Tianbot\DM_20261030220930_001.png")
OUTPUT = Path(__file__).resolve().parent / "assets" / "app.ico"

# Windows 常用图标尺寸
SIZES = [256, 128, 64, 48, 32, 16]
MARGIN_RATIO = 0.08


def main() -> int:
    image = Image.open(SOURCE).convert("RGBA")

    bbox = image.getbbox()
    if bbox is None:
        print("源图为空，无法处理")
        return 1
    cropped = image.crop(bbox)
    print(f"裁剪后: {cropped.size[0]}x{cropped.size[1]}")

    # 补成正方形：以较长边为准，图案居中
    side = max(cropped.size)
    margin = int(side * MARGIN_RATIO)
    canvas_side = side + margin * 2
    canvas = Image.new("RGBA", (canvas_side, canvas_side), (0, 0, 0, 0))
    offset = (
        (canvas_side - cropped.size[0]) // 2,
        (canvas_side - cropped.size[1]) // 2,
    )
    canvas.paste(cropped, offset)
    print(f"正方形画布: {canvas_side}x{canvas_side}（含 {margin}px 边距）")

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    largest = canvas.resize((256, 256), Image.LANCZOS)
    largest.save(OUTPUT, format="ICO", sizes=[(s, s) for s in SIZES])

    print(f"已生成: {OUTPUT}")
    print(f"包含尺寸: {', '.join(str(s) for s in SIZES)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
