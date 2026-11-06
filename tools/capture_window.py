"""抓取指定窗口的内容（不依赖窗口是否在前台）。

全屏截图会被其他窗口遮挡，PrintWindow 直接让窗口把自己画到内存 DC，
能得到真实渲染结果，用来验证打包后的 exe 界面是否正常。
"""

import ctypes
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32

user32.PrintWindow.argtypes = [wintypes.HWND, wintypes.HDC, wintypes.UINT]
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
user32.FindWindowW.restype = wintypes.HWND


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


def capture(hwnd, out_path):
    rect = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    width = rect.right - rect.left
    height = rect.bottom - rect.top
    if width <= 0 or height <= 0:
        return False, "窗口尺寸无效"

    hdc_screen = user32.GetDC(None)
    hdc_mem = gdi32.CreateCompatibleDC(hdc_screen)
    hbmp = gdi32.CreateCompatibleBitmap(hdc_screen, width, height)
    gdi32.SelectObject(hdc_mem, hbmp)

    # 2 = PW_RENDERFULLCONTENT，能抓到 DirectComposition 内容
    ok = user32.PrintWindow(hwnd, hdc_mem, 2)
    if not ok:
        ok = user32.PrintWindow(hwnd, hdc_mem, 0)

    class BITMAPINFO(ctypes.Structure):
        _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]

    bmi = BITMAPINFO()
    bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bmi.bmiHeader.biWidth = width
    bmi.bmiHeader.biHeight = -height      # 负值 = 自上而下
    bmi.bmiHeader.biPlanes = 1
    bmi.bmiHeader.biBitCount = 32
    bmi.bmiHeader.biCompression = 0

    buf_len = width * height * 4
    buffer = ctypes.create_string_buffer(buf_len)
    gdi32.GetDIBits(hdc_mem, hbmp, 0, height, buffer, ctypes.byref(bmi), 0)

    # 用 PIL 存成 png
    from PIL import Image
    img = Image.frombuffer("RGBA", (width, height), buffer, "raw", "BGRA", 0, 1)
    img.convert("RGB").save(out_path)

    gdi32.DeleteObject(hbmp)
    gdi32.DeleteDC(hdc_mem)
    user32.ReleaseDC(None, hdc_screen)
    return bool(ok), f"{width}x{height}"


def main():
    exe = Path("dist/tb-verify-ui.exe").resolve()
    if not exe.exists():
        print(f"找不到 {exe}")
        return 1

    proc = subprocess.Popen([str(exe)])
    hwnd = None
    for _ in range(40):
        time.sleep(1)
        hwnd = user32.FindWindowW(None, "Tianracer T110 整车测试")
        if hwnd:
            break
    if not hwnd:
        print("未找到窗口")
        proc.kill()
        return 1

    time.sleep(3)   # 等界面画完

    rect = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    print(f"GetWindowRect: {rect.left},{rect.top} -> {rect.right},{rect.bottom} "
          f"= {rect.right - rect.left}x{rect.bottom - rect.top}")

    # 客户区（不含标题栏与边框）
    client = wintypes.RECT()
    user32.GetClientRect(hwnd, ctypes.byref(client))
    print(f"GetClientRect: {client.right - client.left}x{client.bottom - client.top}")

    ok, info = capture(hwnd, "_exe_window.png")
    print(f"PrintWindow 结果: {'成功' if ok else '失败'}, 尺寸 {info}")

    try:
        proc.kill()
    except Exception:
        pass
    subprocess.run(["taskkill", "/F", "/IM", exe.name], capture_output=True)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
