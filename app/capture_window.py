# -*- coding: utf-8 -*-
"""capture_window.py —— 给验收截图用的「不抢焦点」窗口抓图。

为什么不用屏幕截图
------------------
``SetForegroundWindow`` / 最小化-恢复 / 置顶循环都会让窗口闪一下，用户对闪屏极敏感。
这里只用 Win32 的 ``PrintWindow``：它把窗口（含 DWM 合成内容）直接画进内存 DC，
**不激活、不移动、不改 Z 序**，因此不会抢焦点、也不会造成视觉闪烁。

唯一需要的「可见性」前提是窗口没有被最小化；``ensure_visible`` 用
``ShowWindow(SW_SHOWNOACTIVATE)`` 保证这一点（不激活窗口）。
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes
from pathlib import Path

GWL_STYLE = -16
WS_MINIMIZE = 0x20000000
SW_SHOWNOACTIVATE = 4
PW_RENDERFULLCONTENT = 0x00000002


def _user32():
    user32 = ctypes.windll.user32
    user32.SetProcessDPIAware()
    return user32


def find_hwnd(winfo_id: int) -> int:
    """把 Tk 的 winfo_id 换成真正的顶层窗口 HWND。

    Tk 在 8.6 上 ``winfo_id()`` 返回的可能是子窗口；``GetAncestor(GA_ROOT)``
    一定拿得到顶层窗口。拿不到就退回原值。
    """
    if not winfo_id:
        return 0
    user32 = _user32()
    try:
        root = user32.GetAncestor(wintypes.HWND(int(winfo_id)), 2)  # GA_ROOT = 2
    except Exception:  # noqa: BLE001
        root = 0
    return int(root or winfo_id)


def ensure_visible(hwnd: int) -> None:
    """确保窗口不是最小化状态（不激活、不闪烁）。"""
    if not hwnd:
        return
    user32 = _user32()
    try:
        style = user32.GetWindowLongW(wintypes.HWND(hwnd), GWL_STYLE)
        if style & WS_MINIMIZE:
            user32.ShowWindow(wintypes.HWND(hwnd), SW_SHOWNOACTIVATE)
    except Exception:  # noqa: BLE001
        pass


def grab(winfo_id: int, out_path: Path, flags: int = PW_RENDERFULLCONTENT) -> tuple[bool, str]:
    """把窗口画进 ``out_path``（PNG）。返回 (是否成功, 说明)。

    ``flags`` 用 PW_RENDERFULLCONTENT（0x2）能抓到 DWM 合成内容；
    个别系统上该标志会返回黑图，调用方可以传 0 再试一次。
    """
    from PIL import Image

    hwnd = find_hwnd(winfo_id)
    if not hwnd:
        return False, "拿不到窗口句柄"
    ensure_visible(hwnd)
    user32, gdi32 = _user32(), ctypes.windll.gdi32

    rect = wintypes.RECT()
    if not user32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(rect)):
        return False, "GetWindowRect 失败"
    width, height = rect.right - rect.left, rect.bottom - rect.top
    if width <= 0 or height <= 0:
        return False, f"窗口尺寸异常：{width}x{height}"

    hdc = user32.GetWindowDC(wintypes.HWND(hwnd))
    if not hdc:
        return False, "GetWindowDC 失败"
    memdc = gdi32.CreateCompatibleDC(hdc)
    bitmap = gdi32.CreateCompatibleBitmap(hdc, width, height)
    gdi32.SelectObject(memdc, bitmap)
    try:
        ok = user32.PrintWindow(wintypes.HWND(hwnd), memdc, int(flags))
        if not ok:
            return False, f"PrintWindow 返回 0（flags={flags}）"

        class BITMAPINFOHEADER(ctypes.Structure):
            _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                        ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                        ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                        ("biSizeImage", wintypes.DWORD),
                        ("biXPelsPerMeter", wintypes.LONG),
                        ("biYPelsPerMeter", wintypes.LONG),
                        ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD)]

        header = BITMAPINFOHEADER()
        header.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        header.biWidth = width
        header.biHeight = -height            # 负数 = 自上而下
        header.biPlanes = 1
        header.biBitCount = 32
        header.biCompression = 0             # BI_RGB
        buffer = ctypes.create_string_buffer(width * height * 4)
        got = gdi32.GetDIBits(memdc, bitmap, 0, height, buffer,
                              ctypes.byref(header), 0)
        if not got:
            return False, "GetDIBits 失败"
        image = Image.frombuffer("RGBA", (width, height), buffer, "raw", "BGRA", 0, 1)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        image.convert("RGB").save(out_path)
    finally:
        gdi32.DeleteObject(bitmap)
        gdi32.DeleteDC(memdc)
        user32.ReleaseDC(wintypes.HWND(hwnd), hdc)
    return True, f"{width}x{height} hwnd={hwnd}"


def grab_with_fallback(winfo_id: int, out_path: Path) -> tuple[bool, str]:
    ok, note = grab(winfo_id, out_path, PW_RENDERFULLCONTENT)
    if ok and not _is_blank(out_path):
        return True, note
    ok2, note2 = grab(winfo_id, out_path, 0)
    if ok2:
        return True, note2 + "（回退 flags=0）"
    return False, f"{note} / {note2}"


def _is_blank(path: Path) -> bool:
    """全黑/全白图判定（PrintWindow 在某些系统上会画不出来）。"""
    try:
        from PIL import Image
        with Image.open(path) as img:
            small = img.convert("L").resize((16, 16))
            pixels = list(small.getdata())
    except Exception:  # noqa: BLE001
        return False
    return max(pixels) - min(pixels) < 6


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: capture_window.py <winfo_id> [out.png]")
        raise SystemExit(2)
    ident = int(sys.argv[1])
    out = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("shot.png")
    print(grab_with_fallback(ident, out))
