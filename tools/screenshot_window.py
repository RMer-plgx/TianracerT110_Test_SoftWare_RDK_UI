"""真实屏幕截图：把 exe 窗口置顶后抓全屏。

PrintWindow 在 DPI 缩放场景下会有失真（按物理矩形抓、按逻辑尺寸绘制），
要确认真实显示效果必须走屏幕截屏。
"""

import ctypes
import subprocess
import time
from ctypes import wintypes
from pathlib import Path

user32 = ctypes.windll.user32
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
user32.FindWindowW.restype = wintypes.HWND
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]


def main():
    exe = Path("dist/tb-verify-ui.exe").resolve()
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

    time.sleep(3)
    user32.ShowWindow(hwnd, 9)          # SW_RESTORE
    user32.SetForegroundWindow(hwnd)
    time.sleep(2)

    from PIL import ImageGrab
    img = ImageGrab.grab()
    img.save("_exe_screen.png")
    print(f"屏幕截图: {img.size[0]}x{img.size[1]}")

    try:
        proc.kill()
    except Exception:
        pass
    subprocess.run(["taskkill", "/F", "/IM", exe.name], capture_output=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
