"""Подсказки поверх всех окон, которые не забирают фокус и пропускают клики мыши.

Обычное окно tkinter при показе становится активным — полноэкранная игра от этого
сворачивается. Здесь — «слоистое» окно Win32 (WS_EX_NOACTIVATE | WS_EX_TRANSPARENT),
картинку с прозрачностью рисует Pillow. Создавать и показывать — только из GUI-потока
(сообщения окна обрабатывает цикл tkinter этого же потока).
"""

from __future__ import annotations

import ctypes
import logging
import os

from . import winapi
from .winapi import IS_WINDOWS

log = logging.getLogger(__name__)

WS_POPUP = 0x80000000
WS_EX_TOPMOST, WS_EX_TRANSPARENT, WS_EX_TOOLWINDOW = 0x8, 0x20, 0x80
WS_EX_LAYERED, WS_EX_NOACTIVATE = 0x80000, 0x08000000
SW_HIDE, SW_SHOWNOACTIVATE = 0, 4
ULW_ALPHA, AC_SRC_ALPHA = 0x2, 0x1
HWND_TOPMOST = -1
SWP_NOSIZE, SWP_NOMOVE, SWP_NOACTIVATE = 0x1, 0x2, 0x10

BG = (23, 24, 28, 235)
FG = (232, 233, 237, 255)
MUTED = (154, 160, 173, 255)
ACCENT = (79, 140, 255, 255)
GOOD = (46, 196, 132, 255)
BAD = (235, 72, 72, 255)

if IS_WINDOWS:
    from ctypes import wintypes

    user32, gdi32, kernel32 = winapi.user32, winapi.gdi32, winapi.kernel32
    LRESULT = ctypes.c_ssize_t
    WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)

    class WNDCLASSEXW(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.UINT), ("style", wintypes.UINT), ("lpfnWndProc", WNDPROC),
                    ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int), ("hInstance", wintypes.HINSTANCE),
                    ("hIcon", wintypes.HICON), ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
                    ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR),
                    ("hIconSm", wintypes.HICON)]

    class BLENDFUNCTION(ctypes.Structure):
        _fields_ = [("BlendOp", ctypes.c_ubyte), ("BlendFlags", ctypes.c_ubyte),
                    ("SourceConstantAlpha", ctypes.c_ubyte), ("AlphaFormat", ctypes.c_ubyte)]

    class SIZE(ctypes.Structure):
        _fields_ = [("cx", wintypes.LONG), ("cy", wintypes.LONG)]

    winapi._proto(user32.DefWindowProcW, LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
    winapi._proto(user32.RegisterClassExW, wintypes.ATOM, ctypes.POINTER(WNDCLASSEXW))
    winapi._proto(user32.CreateWindowExW, wintypes.HWND, wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR,
                  wintypes.DWORD, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HWND,
                  wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID)
    winapi._proto(user32.DestroyWindow, wintypes.BOOL, wintypes.HWND)
    winapi._proto(user32.UpdateLayeredWindow, wintypes.BOOL, wintypes.HWND, wintypes.HDC,
                  ctypes.POINTER(wintypes.POINT), ctypes.POINTER(SIZE), wintypes.HDC, ctypes.POINTER(wintypes.POINT),
                  wintypes.COLORREF, ctypes.POINTER(BLENDFUNCTION), wintypes.DWORD)
    winapi._proto(kernel32.GetModuleHandleW, wintypes.HMODULE, wintypes.LPCWSTR)
    winapi._proto(gdi32.CreateDIBSection, wintypes.HBITMAP, wintypes.HDC, ctypes.c_void_p, wintypes.UINT,
                  ctypes.POINTER(ctypes.c_void_p), wintypes.HANDLE, wintypes.DWORD)

    @WNDPROC
    def _wndproc(hwnd, msg, wparam, lparam):
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

_class_atom = None
CLASS_NAME = "SoundFixer.Overlay"


def _register_class() -> bool:
    global _class_atom
    if _class_atom is None:
        wc = WNDCLASSEXW()
        wc.cbSize = ctypes.sizeof(wc)
        wc.lpfnWndProc = _wndproc
        wc.hInstance = kernel32.GetModuleHandleW(None)
        wc.lpszClassName = CLASS_NAME
        _class_atom = user32.RegisterClassExW(ctypes.byref(wc)) or 0
        if not _class_atom:
            log.warning("RegisterClassEx: ошибка %s", ctypes.get_last_error())
    return bool(_class_atom)


def premultiplied_bgra(img) -> bytes:
    """RGBA (Pillow) → BGRA с предумноженной альфой, как требует UpdateLayeredWindow."""
    import numpy as np

    a = np.asarray(img.convert("RGBA"), dtype=np.uint16)
    alpha = a[..., 3:4]
    rgb = (a[..., :3] * alpha + 127) // 255
    out = np.empty(a.shape, np.uint8)
    out[..., 0] = rgb[..., 2]
    out[..., 1] = rgb[..., 1]
    out[..., 2] = rgb[..., 0]
    out[..., 3] = alpha[..., 0]
    return out.tobytes()


class LayeredWindow:
    """Картинка с прозрачностью поверх всех окон. Не активируется и не ловит мышь."""

    def __init__(self, click_through: bool = True):
        self.hwnd = 0
        self.visible = False
        self.click_through = click_through
        self.rect = (0, 0, 0, 0)

    def _ensure(self) -> bool:
        if self.hwnd or not IS_WINDOWS:
            return bool(self.hwnd)
        if not _register_class():
            return False
        ex = WS_EX_LAYERED | WS_EX_TOPMOST | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE
        if self.click_through:
            ex |= WS_EX_TRANSPARENT
        self.hwnd = user32.CreateWindowExW(ex, CLASS_NAME, "SoundFixer", WS_POPUP, 0, 0, 1, 1, None, None,
                                           kernel32.GetModuleHandleW(None), None) or 0
        return bool(self.hwnd)

    def show(self, img, x: int, y: int) -> bool:
        if not self._ensure():
            return False
        w, h = img.size
        data = premultiplied_bgra(img)
        screen = user32.GetDC(None)
        mem = gdi32.CreateCompatibleDC(screen)
        bi = winapi.BITMAPINFOHEADER()
        bi.biSize = ctypes.sizeof(bi)
        bi.biWidth, bi.biHeight, bi.biPlanes, bi.biBitCount = w, -h, 1, 32
        bits = ctypes.c_void_p()
        bmp = gdi32.CreateDIBSection(mem, ctypes.byref(bi), 0, ctypes.byref(bits), None, 0)
        if not bmp:
            gdi32.DeleteDC(mem)
            user32.ReleaseDC(None, screen)
            return False
        ctypes.memmove(bits, data, len(data))
        old = gdi32.SelectObject(mem, bmp)
        try:
            blend = BLENDFUNCTION(0, 0, 255, AC_SRC_ALPHA)
            ok = user32.UpdateLayeredWindow(self.hwnd, screen, ctypes.byref(wintypes.POINT(x, y)),
                                            ctypes.byref(SIZE(w, h)), mem, ctypes.byref(wintypes.POINT(0, 0)),
                                            0, ctypes.byref(blend), ULW_ALPHA)
        finally:
            gdi32.SelectObject(mem, old)
            gdi32.DeleteObject(bmp)
            gdi32.DeleteDC(mem)
            user32.ReleaseDC(None, screen)
        if not ok:
            return False
        self.rect = (x, y, w, h)
        if not self.visible:
            user32.ShowWindow(self.hwnd, SW_SHOWNOACTIVATE)
            self.visible = True
        # некоторые игры сами встают «поверх всех» — поднимаем окно при каждом показе
        user32.SetWindowPos(self.hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
        return True

    def hide(self) -> None:
        if self.hwnd and self.visible:
            user32.ShowWindow(self.hwnd, SW_HIDE)
        self.visible = False

    def destroy(self) -> None:
        if self.hwnd:
            user32.DestroyWindow(self.hwnd)
        self.hwnd = 0
        self.visible = False


# --------------------------------------------------------------------------- рисование
_fonts: dict = {}


def font(size: int, bold: bool = False):
    from PIL import ImageFont

    key = (size, bold)
    if key not in _fonts:
        dirs = [os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts"),
                os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft", "Windows", "Fonts")]
        names = (("seguisb.ttf", "segoeuib.ttf", "arialbd.ttf", "tahomabd.ttf", "verdanab.ttf") if bold else
                 ("segoeui.ttf", "arial.ttf", "tahoma.ttf", "verdana.ttf"))
        f = None
        for name in names:
            for d in dirs:
                try:
                    f = ImageFont.truetype(os.path.join(d, name), size)
                    break
                except OSError:
                    continue
            if f is not None:
                break
        if f is None:
            try:
                f = ImageFont.truetype("DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf", size)
            except OSError:
                f = ImageFont.load_default()
        _fonts[key] = f
    return _fonts[key]


def dpi_scale() -> float:
    if not IS_WINDOWS:
        return 1.0
    try:
        return max(1.0, user32.GetDpiForSystem() / 96.0)
    except AttributeError:
        return 1.0


def draw_mic(d, box, color, muted: bool, width: int) -> None:
    """Значок микрофона (перечёркнутый, если выключен) в прямоугольнике box."""
    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0
    cx = x0 + w / 2
    cw = w * 0.36
    d.rounded_rectangle((cx - cw / 2, y0, cx + cw / 2, y0 + h * 0.62), radius=cw / 2, fill=color)
    d.arc((x0 + w * 0.14, y0 + h * 0.18, x1 - w * 0.14, y0 + h * 0.80), 0, 180, fill=color, width=width)
    d.line((cx, y0 + h * 0.80, cx, y1 - h * 0.06), fill=color, width=width)
    d.line((cx - w * 0.2, y1 - h * 0.06, cx + w * 0.2, y1 - h * 0.06), fill=color, width=width)
    if muted:
        d.line((x0 + w * 0.08, y0 + h * 0.06, x1 - w * 0.08, y1 - h * 0.06), fill=(23, 24, 28, 255),
               width=width * 3)
        d.line((x0 + w * 0.08, y0 + h * 0.06, x1 - w * 0.08, y1 - h * 0.06), fill=color, width=width)


def render_badge(text: str, *, sub: str = "", value: float | None = None, accent=ACCENT, icon: str | None = None,
                 muted: bool = False, scale: float | None = None):
    """Плашка: [значок] текст (+ мелкая подпись, + полоска уровня 0..1)."""
    from PIL import Image, ImageDraw

    k = dpi_scale() if scale is None else scale
    f_main, f_sub = font(round(17 * k), bold=True), font(round(12 * k))
    pad, gap = round(14 * k), round(10 * k)
    icon_w = round(26 * k) if icon else 0
    probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    tw = probe.textlength(text, font=f_main)
    sw = probe.textlength(sub, font=f_sub) if sub else 0
    th = round(24 * k)
    sh = round(17 * k) if sub else 0
    bar_h = round(6 * k) if value is not None else 0
    content_w = max(tw, sw, round(160 * k) if value is not None else 0)
    w = int(pad * 2 + icon_w + (gap if icon else 0) + content_w)
    h = int(pad * 2 + th + sh + (bar_h + round(8 * k) if bar_h else 0))
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((0, 0, w - 1, h - 1), radius=round(12 * k), fill=BG, outline=(255, 255, 255, 28))
    x = pad
    if icon == "mic":
        draw_mic(d, (x, (h - icon_w) / 2, x + icon_w, (h + icon_w) / 2), accent, muted, max(2, round(2.2 * k)))
        x += icon_w + gap
    elif icon == "sun":
        r = icon_w * 0.22
        cx, cy = x + icon_w / 2, h / 2
        d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=accent)
        import math

        for i in range(8):
            a = i * math.pi / 4
            d.line((cx + math.cos(a) * r * 1.5, cy + math.sin(a) * r * 1.5, cx + math.cos(a) * r * 2.2,
                    cy + math.sin(a) * r * 2.2), fill=accent, width=max(2, round(2 * k)))
        x += icon_w + gap
    elif icon == "note":
        cx, cy = x + icon_w * 0.35, h / 2 + icon_w * 0.25
        r = icon_w * 0.16
        d.ellipse((cx - r * 1.3, cy - r, cx + r * 1.3, cy + r), fill=accent)
        d.line((cx + r * 1.2, cy, cx + r * 1.2, cy - icon_w * 0.7), fill=accent, width=max(2, round(2.2 * k)))
        d.line((cx + r * 1.2, cy - icon_w * 0.7, cx + icon_w * 0.55, cy - icon_w * 0.5), fill=accent,
               width=max(2, round(2.2 * k)))
        x += icon_w + gap
    y = pad
    d.text((x, y), text, font=f_main, fill=FG)
    y += th
    if sub:
        d.text((x, y), sub, font=f_sub, fill=MUTED)
        y += sh
    if bar_h:
        y += round(6 * k)
        d.rounded_rectangle((x, y, x + content_w, y + bar_h), radius=bar_h // 2, fill=(255, 255, 255, 40))
        v = max(0.0, min(1.0, value))
        if v > 0:
            d.rounded_rectangle((x, y, x + max(bar_h, content_w * v), y + bar_h), radius=bar_h // 2, fill=accent)
    return img


def cursor_monitor_work() -> tuple[int, int, int, int]:
    """Рабочая область монитора, на котором курсор (или основного)."""
    x, y = winapi.cursor_pos()
    mons = winapi.monitors()
    for m in mons:
        l, t, r, b = m.rect
        if l <= x < r and t <= y < b:
            return m.work
    if mons:
        return mons[0].work
    vx, vy, vw, vh = winapi.virtual_screen()
    return vx, vy, vx + vw, vy + vh


class Osd:
    """Короткая подсказка внизу экрана: яркость, громкость, микрофон. Одна на программу."""

    def __init__(self, root):
        self.root = root
        self.win = LayeredWindow()
        self._job = None

    def show(self, text: str, *, sub: str = "", value: float | None = None, accent=ACCENT, icon=None,
             muted: bool = False, ms: int = 1400) -> None:
        try:
            img = render_badge(text, sub=sub, value=value, accent=accent, icon=icon, muted=muted)
        except Exception:  # noqa: BLE001 — нет Pillow/шрифтов: подсказка не важнее работы
            log.exception("OSD")
            return
        l, t, r, b = cursor_monitor_work()
        x = l + (r - l - img.width) // 2
        y = b - img.height - round(72 * dpi_scale())
        if not self.win.show(img, x, y):
            return
        if self._job is not None:
            self.root.after_cancel(self._job)
        self._job = self.root.after(ms, self._hide)

    def _hide(self):
        self._job = None
        self.win.hide()

    def destroy(self):
        if self._job is not None:
            self.root.after_cancel(self._job)
            self._job = None
        self.win.destroy()
