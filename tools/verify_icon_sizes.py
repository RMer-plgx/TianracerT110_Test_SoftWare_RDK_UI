"""从 exe 里逐个提取图标并检查内容。

用 ExtractIconExW 按索引取——索引对应 ICO 里的各个尺寸。
检查位图尺寸与中心像素颜色，判断是否真的嵌入了自定义图标。
"""

import ctypes
from ctypes import wintypes
from pathlib import Path

EXE = Path("dist/TianracerT110_Test_SoftWare_RDK.exe").resolve()

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
shell32 = ctypes.windll.shell32


# 声明函数签名，否则 64 位下句柄会被当 32 位 int，导致 OverflowError
gdi32.GetObjectW.argtypes = [wintypes.HGDIOBJ, ctypes.c_int, ctypes.c_void_p]
gdi32.GetObjectW.restype = ctypes.c_int
gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
gdi32.SelectObject.restype = wintypes.HGDIOBJ
gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
gdi32.CreateCompatibleDC.restype = wintypes.HDC
gdi32.GetPixel.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
gdi32.GetPixel.restype = wintypes.COLORREF
gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
gdi32.DeleteDC.argtypes = [wintypes.HDC]
user32.GetDC.argtypes = [wintypes.HWND]
user32.GetDC.restype = wintypes.HDC
user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
user32.DestroyIcon.argtypes = [wintypes.HICON]
user32.GetIconInfo.argtypes = [wintypes.HICON, ctypes.c_void_p]
shell32.ExtractIconExW.argtypes = [
    wintypes.LPCWSTR, ctypes.c_int,
    ctypes.POINTER(wintypes.HICON), ctypes.POINTER(wintypes.HICON),
    wintypes.UINT,
]
shell32.ExtractIconExW.restype = wintypes.UINT


class ICONINFO(ctypes.Structure):
    _fields_ = [
        ("fIcon", wintypes.BOOL),
        ("xHotspot", wintypes.DWORD),
        ("yHotspot", wintypes.DWORD),
        ("hbmMask", wintypes.HBITMAP),
        ("hbmColor", wintypes.HBITMAP),
    ]


class BITMAP(ctypes.Structure):
    _fields_ = [
        ("bmType", wintypes.LONG),
        ("bmWidth", wintypes.LONG),
        ("bmHeight", wintypes.LONG),
        ("bmWidthBytes", wintypes.LONG),
        ("bmPlanes", wintypes.WORD),
        ("bmBitsPixel", wintypes.WORD),
        ("bmBits", ctypes.c_void_p),
    ]


def describe(pixel):
    if pixel == 0xFFFFFFFF:
        return "透明(图标为空)"
    # COLORREF 是 0x00BBGGRR，最低字节是 R，不是 B。
    # 之前按 0x00RRGGBB 解析，导致 R/B 互反，把金棕色误报成蓝色。
    r = pixel & 0xFF
    g = (pixel >> 8) & 0xFF
    b = (pixel >> 16) & 0xFF
    brownish = 120 < r < 210 and 80 < g < 170 and b < 130 and r > b + 40
    return "R={:>3} G={:>3} B={:>3}  {}".format(
        r, g, b, "金棕色 OK" if brownish else "非自定义图标 BAD")


def main():
    print("目标: {}\n".format(EXE.name))

    total = shell32.ExtractIconExW(str(EXE), -1, None, None, 0)
    print("图标总数: {}\n".format(total))

    ok_all = True
    for index in range(total):
        large = wintypes.HICON()
        small = wintypes.HICON()
        n = shell32.ExtractIconExW(
            str(EXE), index, ctypes.byref(large), ctypes.byref(small), 1)
        if n == 0:
            print("[{}] 提取失败".format(index))
            ok_all = False
            continue

        hicon = large.value or small.value
        info = ICONINFO()
        if not user32.GetIconInfo(hicon, ctypes.byref(info)):
            print("[{}] GetIconInfo 失败".format(index))
            continue

        bmp = BITMAP()
        gdi32.GetObjectW(info.hbmColor, ctypes.sizeof(bmp), ctypes.byref(bmp))

        hdc = user32.GetDC(None)
        memdc = gdi32.CreateCompatibleDC(hdc)
        old = gdi32.SelectObject(memdc, info.hbmColor)
        pixel = gdi32.GetPixel(memdc, bmp.bmWidth // 2, bmp.bmHeight // 2)
        gdi32.SelectObject(memdc, old)
        gdi32.DeleteDC(memdc)
        user32.ReleaseDC(None, hdc)

        line = "[{}] {}x{}  {}".format(
            index, bmp.bmWidth, bmp.bmHeight, describe(pixel))
        if "BAD" in line or "透明" in line:
            ok_all = False
        print(line)

        if large.value:
            user32.DestroyIcon(large.value)
        if small.value:
            user32.DestroyIcon(small.value)
        if info.hbmMask:
            gdi32.DeleteObject(info.hbmMask)
        if info.hbmColor:
            gdi32.DeleteObject(info.hbmColor)

    print()
    print("所有尺寸均为自定义图标" if ok_all else "存在非自定义尺寸")
    return 0 if ok_all else 1


if __name__ == "__main__":
    raise SystemExit(main())
