"""Тонкие обёртки над Win32 API для инструментов (окна, клавиатура, буфер обмена, раскладки).

На других ОС функции ничего не делают — так модули можно импортировать и тестировать где угодно.
"""

from __future__ import annotations

import ctypes
import ntpath
import sys
import time
from dataclasses import dataclass

IS_WINDOWS = sys.platform == "win32"

# --------------------------------------------------------------------------- константы
VK_SHIFT, VK_CONTROL, VK_MENU = 0x10, 0x11, 0x12
VK_LWIN, VK_RWIN = 0x5B, 0x5C
VK_LEFT = 0x25
KEYEVENTF_EXTENDEDKEY, KEYEVENTF_KEYUP = 0x0001, 0x0002
INPUT_KEYBOARD = 1
CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002
WM_INPUTLANGCHANGEREQUEST = 0x0050
GWL_EXSTYLE = -20
WS_EX_TOPMOST, WS_EX_LAYERED = 0x00000008, 0x00080000
HWND_TOPMOST, HWND_NOTOPMOST = -1, -2
SWP_NOSIZE, SWP_NOMOVE, SWP_NOACTIVATE = 0x0001, 0x0002, 0x0010
LWA_ALPHA = 0x2
MONITOR_DEFAULTTONEAREST = 2
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
LANG_ENGLISH, LANG_RUSSIAN = 0x09, 0x19

if IS_WINDOWS:
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    ULONG_PTR = ctypes.c_size_t
    HKL = wintypes.HANDLE

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                    ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]

    class MOUSEINPUT(ctypes.Structure):  # нужен в union, иначе размер INPUT неверный и SendInput не работает
        _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                    ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]

    class HARDWAREINPUT(ctypes.Structure):
        _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD), ("wParamH", wintypes.WORD)]

    class _INPUTUNION(ctypes.Union):
        _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]

    class INPUT(ctypes.Structure):
        _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]

    class MONITORINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT), ("rcWork", wintypes.RECT),
                    ("dwFlags", wintypes.DWORD)]

    def _proto(fn, restype, *argtypes):
        fn.restype = restype
        fn.argtypes = argtypes
        return fn

    _proto(user32.GetForegroundWindow, wintypes.HWND)
    _proto(user32.GetWindowThreadProcessId, wintypes.DWORD, wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
    _proto(user32.GetWindowTextLengthW, ctypes.c_int, wintypes.HWND)
    _proto(user32.GetWindowTextW, ctypes.c_int, wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
    _proto(user32.GetClassNameW, ctypes.c_int, wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
    _proto(user32.GetWindowRect, wintypes.BOOL, wintypes.HWND, ctypes.POINTER(wintypes.RECT))
    _proto(user32.IsWindowVisible, wintypes.BOOL, wintypes.HWND)
    _proto(user32.MonitorFromWindow, wintypes.HANDLE, wintypes.HWND, wintypes.DWORD)
    _proto(user32.GetMonitorInfoW, wintypes.BOOL, wintypes.HANDLE, ctypes.POINTER(MONITORINFO))
    _proto(user32.GetWindowLongPtrW, ctypes.c_ssize_t, wintypes.HWND, ctypes.c_int)
    _proto(user32.SetWindowLongPtrW, ctypes.c_ssize_t, wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t)
    _proto(user32.SetWindowPos, wintypes.BOOL, wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
           ctypes.c_int, ctypes.c_int, wintypes.UINT)
    # BYTE в wintypes знаковый — для прозрачности 0..255 нужен беззнаковый c_ubyte
    _proto(user32.SetLayeredWindowAttributes, wintypes.BOOL, wintypes.HWND, wintypes.DWORD, ctypes.c_ubyte,
           wintypes.DWORD)
    _proto(user32.GetLayeredWindowAttributes, wintypes.BOOL, wintypes.HWND, ctypes.POINTER(wintypes.DWORD),
           ctypes.POINTER(ctypes.c_ubyte), ctypes.POINTER(wintypes.DWORD))
    _proto(user32.SendInput, wintypes.UINT, wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
    _proto(user32.GetAsyncKeyState, ctypes.c_short, ctypes.c_int)
    _proto(user32.GetKeyboardLayout, HKL, wintypes.DWORD)
    _proto(user32.GetKeyboardLayoutList, ctypes.c_int, ctypes.c_int, ctypes.POINTER(HKL))
    _proto(user32.PostMessageW, wintypes.BOOL, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
    _proto(user32.OpenClipboard, wintypes.BOOL, wintypes.HWND)
    _proto(user32.CloseClipboard, wintypes.BOOL)
    _proto(user32.EmptyClipboard, wintypes.BOOL)
    _proto(user32.GetClipboardData, wintypes.HANDLE, wintypes.UINT)
    _proto(user32.SetClipboardData, wintypes.HANDLE, wintypes.UINT, wintypes.HANDLE)
    _proto(user32.GetClipboardSequenceNumber, wintypes.DWORD)
    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    _proto(user32.EnumWindows, wintypes.BOOL, WNDENUMPROC, wintypes.LPARAM)
    _proto(kernel32.GlobalAlloc, wintypes.HGLOBAL, wintypes.UINT, ctypes.c_size_t)
    _proto(kernel32.GlobalLock, wintypes.LPVOID, wintypes.HGLOBAL)
    _proto(kernel32.GlobalUnlock, wintypes.BOOL, wintypes.HGLOBAL)
    _proto(kernel32.GlobalFree, wintypes.HGLOBAL, wintypes.HGLOBAL)
    _proto(kernel32.OpenProcess, wintypes.HANDLE, wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    _proto(kernel32.QueryFullProcessImageNameW, wintypes.BOOL, wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
           ctypes.POINTER(wintypes.DWORD))
    _proto(kernel32.CloseHandle, wintypes.BOOL, wintypes.HANDLE)
    _proto(kernel32.SetThreadExecutionState, wintypes.DWORD, wintypes.DWORD)
    _proto(kernel32.GetCurrentProcessId, wintypes.DWORD)


# --------------------------------------------------------------------------- окна
@dataclass(frozen=True)
class WindowInfo:
    hwnd: int
    pid: int
    exe: str          # имя файла в нижнем регистре, например "cs2.exe"
    path: str
    title: str
    fullscreen: bool


SHELL_CLASSES = {"Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd"}


def process_path(pid: int) -> str:
    if not IS_WINDOWS or not pid:
        return ""
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(len(buf))
        return buf.value if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)) else ""
    finally:
        kernel32.CloseHandle(h)


def _class_name(hwnd) -> str:
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def window_title(hwnd) -> str:
    if not IS_WINDOWS or not hwnd:
        return ""
    n = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


def is_fullscreen(hwnd) -> bool:
    """Окно занимает весь монитор (полноэкранная игра/видео), и это не рабочий стол."""
    if not IS_WINDOWS or not hwnd or _class_name(hwnd) in SHELL_CLASSES:
        return False
    rect = wintypes.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return False
    mi = MONITORINFO()
    mi.cbSize = ctypes.sizeof(MONITORINFO)
    mon = user32.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST)
    if not mon or not user32.GetMonitorInfoW(mon, ctypes.byref(mi)):
        return False
    m = mi.rcMonitor
    return rect.left <= m.left and rect.top <= m.top and rect.right >= m.right and rect.bottom >= m.bottom


def window_info(hwnd) -> WindowInfo | None:
    if not IS_WINDOWS or not hwnd:
        return None
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    path = process_path(pid.value)
    return WindowInfo(int(hwnd), pid.value, ntpath.basename(path).lower(), path, window_title(hwnd),
                      is_fullscreen(hwnd))


def foreground_window() -> int:
    return int(user32.GetForegroundWindow() or 0) if IS_WINDOWS else 0


def visible_programs() -> list[tuple[str, str]]:
    """Программы с видимыми окнами → [(exe, заголовок)] без дублей (для выбора игр)."""
    if not IS_WINDOWS:
        return []
    found: dict[str, str] = {}
    own = kernel32.GetCurrentProcessId()

    def cb(hwnd, _lparam):
        if user32.IsWindowVisible(hwnd) and user32.GetWindowTextLengthW(hwnd) > 0:
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value != own:
                exe = ntpath.basename(process_path(pid.value)).lower()
                if exe and exe not in found:
                    found[exe] = window_title(hwnd)
        return True

    user32.EnumWindows(WNDENUMPROC(cb), 0)
    return sorted(found.items())


def toggle_topmost(hwnd) -> bool | None:
    """Закрепить окно поверх всех / открепить. → новое состояние."""
    if not IS_WINDOWS or not hwnd:
        return None
    on_top = bool(user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE) & WS_EX_TOPMOST)
    after = HWND_NOTOPMOST if on_top else HWND_TOPMOST
    if not user32.SetWindowPos(hwnd, after, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE):
        return None
    return not on_top


_layered_by_us: set[int] = set()


def change_opacity(hwnd, delta: int) -> int | None:
    """Прозрачность окна (40..255). Стиль «layered» снимается, когда окно снова непрозрачное."""
    if not IS_WINDOWS or not hwnd:
        return None
    ex = user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
    alpha = 255
    if ex & WS_EX_LAYERED:
        key, a, flags = wintypes.DWORD(), ctypes.c_ubyte(), wintypes.DWORD()
        if user32.GetLayeredWindowAttributes(hwnd, ctypes.byref(key), ctypes.byref(a), ctypes.byref(flags)) \
                and flags.value & LWA_ALPHA:
            alpha = a.value
    else:
        user32.SetWindowLongPtrW(hwnd, GWL_EXSTYLE, ex | WS_EX_LAYERED)
        _layered_by_us.add(int(hwnd))
    alpha = max(40, min(255, alpha + delta))
    user32.SetLayeredWindowAttributes(hwnd, 0, alpha, LWA_ALPHA)
    if alpha == 255 and int(hwnd) in _layered_by_us:
        user32.SetWindowLongPtrW(hwnd, GWL_EXSTYLE, user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE) & ~WS_EX_LAYERED)
        _layered_by_us.discard(int(hwnd))
    return alpha


# --------------------------------------------------------------------------- клавиатура
def send_keys(events: list[tuple[int, bool]]) -> bool:
    """events: [(vk, отпустить?)] — синтетические нажатия через SendInput."""
    if not IS_WINDOWS:
        return False
    arr = (INPUT * len(events))()
    for i, (vk, up) in enumerate(events):
        arr[i].type = INPUT_KEYBOARD
        flags = KEYEVENTF_KEYUP if up else 0
        if vk in (VK_LEFT, 0x26, 0x27, 0x28, 0x2D, 0x2E, 0x21, 0x22, 0x23, 0x24):
            flags |= KEYEVENTF_EXTENDEDKEY
        arr[i].u.ki = KEYBDINPUT(vk, 0, flags, 0, 0)
    return user32.SendInput(len(events), arr, ctypes.sizeof(INPUT)) == len(events)


def combo(*vks: int) -> list[tuple[int, bool]]:
    """combo(VK_CONTROL, ord('C')) → нажать по порядку и отпустить в обратном."""
    return [(vk, False) for vk in vks] + [(vk, True) for vk in reversed(vks)]


def wait_modifiers_released(timeout: float = 1.5) -> None:
    """Ждём, пока пользователь отпустит Ctrl/Alt/Shift/Win после горячей клавиши."""
    if not IS_WINDOWS:
        return
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if not any(user32.GetAsyncKeyState(vk) & 0x8000 for vk in (VK_SHIFT, VK_CONTROL, VK_MENU, VK_LWIN, VK_RWIN)):
            return
        time.sleep(0.01)


# --------------------------------------------------------------------------- буфер обмена
def _open_clipboard() -> bool:
    for _ in range(25):  # буфер может быть ненадолго занят другой программой
        if user32.OpenClipboard(None):
            return True
        time.sleep(0.01)
    return False


def clipboard_sequence() -> int:
    return int(user32.GetClipboardSequenceNumber()) if IS_WINDOWS else 0


def get_clipboard_text() -> str | None:
    if not IS_WINDOWS or not _open_clipboard():
        return None
    try:
        h = user32.GetClipboardData(CF_UNICODETEXT)
        if not h:
            return None
        p = kernel32.GlobalLock(h)
        if not p:
            return None
        try:
            return ctypes.wstring_at(p)
        finally:
            kernel32.GlobalUnlock(h)
    finally:
        user32.CloseClipboard()


def set_clipboard_text(text: str) -> bool:
    if not IS_WINDOWS:
        return False
    data = (text + "\0").encode("utf-16-le")
    h = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
    if not h:
        return False
    p = kernel32.GlobalLock(h)
    ctypes.memmove(p, data, len(data))
    kernel32.GlobalUnlock(h)
    if not _open_clipboard():
        kernel32.GlobalFree(h)
        return False
    try:
        user32.EmptyClipboard()
        if not user32.SetClipboardData(CF_UNICODETEXT, h):
            kernel32.GlobalFree(h)
            return False
        return True  # после успешного SetClipboardData памятью владеет система
    finally:
        user32.CloseClipboard()


# --------------------------------------------------------------------------- раскладки
def keyboard_layouts() -> list[int]:
    if not IS_WINDOWS:
        return []
    n = user32.GetKeyboardLayoutList(0, None)
    arr = (HKL * n)()
    user32.GetKeyboardLayoutList(n, arr)
    return [int(h or 0) for h in arr]


def switch_layout(hwnd, primary_lang: int) -> bool:
    """Переключить раскладку окна на язык (LANG_ENGLISH / LANG_RUSSIAN), если она установлена."""
    if not IS_WINDOWS or not hwnd:
        return False
    for hkl in keyboard_layouts():
        if (hkl & 0x3FF) == primary_lang:
            return bool(user32.PostMessageW(hwnd, WM_INPUTLANGCHANGEREQUEST, 0, hkl))
    return False


def window_layout_lang(hwnd) -> int:
    if not IS_WINDOWS or not hwnd:
        return 0
    tid = user32.GetWindowThreadProcessId(hwnd, None)
    return int(user32.GetKeyboardLayout(tid) or 0) & 0x3FF


# --------------------------------------------------------------------------- питание
ES_CONTINUOUS, ES_SYSTEM_REQUIRED, ES_DISPLAY_REQUIRED = 0x80000000, 0x00000001, 0x00000002


def keep_awake(on: bool, display: bool = True) -> None:
    """Состояние действует, пока жив вызвавший поток, — вызывать из GUI-потока."""
    if not IS_WINDOWS:
        return
    flags = ES_CONTINUOUS
    if on:
        flags |= ES_SYSTEM_REQUIRED | (ES_DISPLAY_REQUIRED if display else 0)
    kernel32.SetThreadExecutionState(flags)
