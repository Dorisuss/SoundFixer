"""Глобальные горячие клавиши (RegisterHotKey) и push-to-talk.

Горячие клавиши — событийные: поток спит в GetMessage и просыпается только при
нажатии. Push-to-talk опрашивает одну клавишу раз в 20 мс и только пока включён
(поддерживаются и боковые кнопки мыши: mouse4 / mouse5).
"""

from __future__ import annotations

import ctypes
import logging
import sys
import threading

log = logging.getLogger(__name__)

MODS = {"ctrl": 0x2, "control": 0x2, "alt": 0x1, "shift": 0x4, "win": 0x8}
MOD_NOREPEAT = 0x4000
WM_HOTKEY = 0x0312
WM_APP_UPDATE = 0x8001
WM_QUIT = 0x0012

KEYS = {
    "space": 0x20, "tab": 0x09, "enter": 0x0D, "backspace": 0x08, "escape": 0x1B,
    "caps lock": 0x14, "scroll lock": 0x91, "num lock": 0x90, "pause": 0x13,
    "insert": 0x2D, "delete": 0x2E, "home": 0x24, "end": 0x23,
    "page up": 0x21, "page down": 0x22, "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
    "left ctrl": 0xA2, "right ctrl": 0xA3, "left alt": 0xA4, "right alt": 0xA5,
    "left shift": 0xA0, "right shift": 0xA1, "`": 0xC0, "-": 0xBD, "=": 0xBB,
    "[": 0xDB, "]": 0xDD, ";": 0xBA, "'": 0xDE, ",": 0xBC, ".": 0xBE, "/": 0xBF, "\\": 0xDC,
    "mouse4": 0x05, "mouse5": 0x06, "mouse middle": 0x04,
}
KEYS.update({chr(c).lower(): c for c in range(ord("A"), ord("Z") + 1)})
KEYS.update({str(d): 0x30 + d for d in range(10)})
KEYS.update({f"f{i}": 0x6F + i for i in range(1, 25)})
KEYS.update({f"num {d}": 0x60 + d for d in range(10)})
KEYS.update({"num *": 0x6A, "num +": 0x6B, "num -": 0x6D, "num .": 0x6E, "num /": 0x6F})

PTT_KEYS = ("caps lock", "mouse4", "mouse5", "mouse middle", "left alt", "right alt", "right ctrl",
            "left ctrl", "left shift", "right shift", "space", "`", "v", "b", "t", "x", "f13", "f14")


def parse(combo: str):
    """'ctrl+alt+m' → (modifiers, vk) или None."""
    if not combo:
        return None
    parts = [p.strip().lower() for p in combo.split("+") if p.strip()]
    mods, vk = 0, None
    for p in parts:
        if p in MODS:
            mods |= MODS[p]
        elif p in KEYS and vk is None:
            vk = KEYS[p]
        else:
            return None
    return (mods, vk) if vk is not None else None


IS_WINDOWS = sys.platform == "win32"
if IS_WINDOWS:
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32.GetMessageW.argtypes = (ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT)
    user32.PeekMessageW.argtypes = (ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT,
                                    wintypes.UINT, wintypes.UINT)
    user32.PostThreadMessageW.argtypes = (wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
    user32.RegisterHotKey.argtypes = (wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT)
    user32.UnregisterHotKey.argtypes = (wintypes.HWND, ctypes.c_int)
    user32.GetAsyncKeyState.argtypes = (ctypes.c_int,)
    user32.GetAsyncKeyState.restype = ctypes.c_short


class Hotkeys(threading.Thread):
    def __init__(self, on_hotkey):
        super().__init__(name="SoundFixer-hotkeys", daemon=True)
        self._on_hotkey = on_hotkey
        self._wanted: dict[str, str] = {}
        self._lock = threading.Lock()
        self._tid = None
        self._ready = threading.Event()
        self.failed: list[str] = []

    def set_bindings(self, bindings: dict[str, str]) -> None:
        """bindings: имя действия → сочетание. Применяется асинхронно."""
        with self._lock:
            self._wanted = {k: v for k, v in bindings.items() if v}
        if IS_WINDOWS and self._ready.wait(2) and self._tid:
            user32.PostThreadMessageW(self._tid, WM_APP_UPDATE, 0, 0)

    def run(self):
        if not IS_WINDOWS:
            return
        msg = wintypes.MSG()
        user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 0)  # создаёт очередь сообщений
        self._tid = kernel32.GetCurrentThreadId()
        self._ready.set()
        registered: dict[int, str] = {}
        self._apply(registered)
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == WM_HOTKEY:
                name = registered.get(msg.wParam)
                if name:
                    try:
                        self._on_hotkey(name)
                    except Exception:  # noqa: BLE001
                        log.exception("Горячая клавиша %s", name)
            elif msg.message == WM_APP_UPDATE:
                self._apply(registered)
        for hid in registered:
            user32.UnregisterHotKey(None, hid)

    def _apply(self, registered: dict[int, str]) -> None:
        for hid in list(registered):
            user32.UnregisterHotKey(None, hid)
        registered.clear()
        with self._lock:
            wanted = dict(self._wanted)
        failed = []
        for hid, (name, combo) in enumerate(wanted.items(), start=1):
            parsed = parse(combo)
            if not parsed or not user32.RegisterHotKey(None, hid, parsed[0] | MOD_NOREPEAT, parsed[1]):
                failed.append(f"{combo} ({name})")
                continue
            registered[hid] = name
        self.failed = failed
        if failed:
            log.warning("Не удалось назначить горячие клавиши: %s", ", ".join(failed))

    def stop(self):
        if IS_WINDOWS and self._tid:
            user32.PostThreadMessageW(self._tid, WM_QUIT, 0, 0)


class PushToTalk:
    def __init__(self, on_change):
        self._on_change = on_change
        self._stop = threading.Event()
        self._thread = None
        self.vk = None

    def start(self, key: str) -> bool:
        self.stop()
        self.vk = KEYS.get(key.lower()) if key else None
        if not IS_WINDOWS or self.vk is None:
            return False
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="SoundFixer-ptt", daemon=True)
        self._thread.start()
        return True

    def _run(self):
        down = None
        vk = self.vk
        while not self._stop.wait(0.02):
            now = bool(user32.GetAsyncKeyState(vk) & 0x8000)
            if now != down:
                down = now
                self._on_change(now)

    @property
    def running(self) -> bool:
        return self._thread is not None

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(0.5)
            self._thread = None
