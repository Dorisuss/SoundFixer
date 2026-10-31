"""Наблюдатель за активным окном + блокировка клавиш.

* Смена активного окна приходит событием Windows (SetWinEventHook) — без опроса,
  поток спит в GetMessage и ничего не тратит.
* Низкоуровневый перехват клавиатуры ставится только на время, когда он нужен
  (например, пока на экране игра и включена блокировка клавиши Win), и сразу снимается.
"""

from __future__ import annotations

import ctypes
import logging
import threading

from . import winapi
from .winapi import IS_WINDOWS, WindowInfo

log = logging.getLogger(__name__)

EVENT_SYSTEM_FOREGROUND = 0x0003
EVENT_SYSTEM_MINIMIZEEND = 0x0017
WINEVENT_OUTOFCONTEXT = 0x0000
WH_KEYBOARD_LL = 13
LLKHF_INJECTED = 0x10
WM_APP_KEYBLOCK = 0x8002
WM_QUIT = 0x0012

if IS_WINDOWS:
    from ctypes import wintypes

    user32 = winapi.user32
    kernel32 = winapi.kernel32
    WINEVENTPROC = ctypes.WINFUNCTYPE(None, wintypes.HANDLE, wintypes.DWORD, wintypes.HWND, wintypes.LONG,
                                      wintypes.LONG, wintypes.DWORD, wintypes.DWORD)
    HOOKPROC = ctypes.WINFUNCTYPE(wintypes.LPARAM, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)

    class KBDLLHOOKSTRUCT(ctypes.Structure):
        _fields_ = [("vkCode", wintypes.DWORD), ("scanCode", wintypes.DWORD), ("flags", wintypes.DWORD),
                    ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]

    user32.SetWinEventHook.restype = wintypes.HANDLE
    user32.SetWinEventHook.argtypes = (wintypes.DWORD, wintypes.DWORD, wintypes.HMODULE, WINEVENTPROC,
                                       wintypes.DWORD, wintypes.DWORD, wintypes.DWORD)
    user32.UnhookWinEvent.argtypes = (wintypes.HANDLE,)
    user32.SetWindowsHookExW.restype = wintypes.HANDLE
    user32.SetWindowsHookExW.argtypes = (ctypes.c_int, HOOKPROC, wintypes.HINSTANCE, wintypes.DWORD)
    user32.CallNextHookEx.restype = wintypes.LPARAM
    user32.CallNextHookEx.argtypes = (wintypes.HANDLE, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
    user32.UnhookWindowsHookEx.argtypes = (wintypes.HANDLE,)
    user32.GetMessageW.argtypes = (ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT)
    user32.PeekMessageW.argtypes = (ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT,
                                    wintypes.UINT)
    user32.PostThreadMessageW.argtypes = (wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
    user32.TranslateMessage.argtypes = (ctypes.POINTER(wintypes.MSG),)
    user32.DispatchMessageW.argtypes = (ctypes.POINTER(wintypes.MSG),)
    kernel32.GetModuleHandleW.restype = wintypes.HMODULE
    kernel32.GetModuleHandleW.argtypes = (wintypes.LPCWSTR,)


class ForegroundWatcher(threading.Thread):
    def __init__(self):
        super().__init__(name="SoundFixer-foreground", daemon=True)
        self.current: WindowInfo | None = None
        self._subs: list = []
        self._lock = threading.Lock()
        self._tid = 0
        self._ready = threading.Event()
        self._block: frozenset[int] = frozenset()
        self._hook = None
        self._kb_proc = None
        self._ev_proc = None

    # ---------- подписки (колбэки вызываются из потока наблюдателя — должны быть быстрыми)
    def subscribe(self, cb) -> None:
        with self._lock:
            if cb not in self._subs:
                self._subs.append(cb)
        if self.current is not None:
            cb(self.current)

    def unsubscribe(self, cb) -> None:
        with self._lock:
            if cb in self._subs:
                self._subs.remove(cb)

    @property
    def idle(self) -> bool:
        return not self._subs

    # ---------- блокировка клавиш
    def set_key_block(self, vks) -> None:
        vks = frozenset(vks)
        if vks == self._block:
            return
        self._block = vks
        if IS_WINDOWS and self._ready.wait(2) and self._tid:
            user32.PostThreadMessageW(self._tid, WM_APP_KEYBLOCK, 0, 0)

    def _apply_key_block(self) -> None:
        want = bool(self._block)
        if want and self._hook is None:
            self._kb_proc = HOOKPROC(self._on_key)
            self._hook = user32.SetWindowsHookExW(WH_KEYBOARD_LL, self._kb_proc, kernel32.GetModuleHandleW(None), 0)
            if not self._hook:
                log.warning("Не удалось перехватить клавиатуру: %s", ctypes.get_last_error())
                self._hook = None
        elif not want and self._hook is not None:
            user32.UnhookWindowsHookEx(self._hook)
            self._hook = None

    def _on_key(self, code, wparam, lparam):
        try:
            if code == 0 and self._block:
                kb = ctypes.cast(lparam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
                if kb.vkCode in self._block and not kb.flags & LLKHF_INJECTED:
                    return 1  # клавиша «съедена»
        except Exception:  # noqa: BLE001 — исключение в перехватчике нельзя пропускать наружу
            pass
        return user32.CallNextHookEx(None, code, wparam, lparam)

    # ---------- поток
    def run(self):
        if not IS_WINDOWS:
            self._ready.set()
            return
        msg = wintypes.MSG()
        user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 0)  # создаёт очередь сообщений потока
        self._tid = kernel32.GetCurrentThreadId()
        self._ev_proc = WINEVENTPROC(self._on_event)
        hooks = [user32.SetWinEventHook(ev, ev, None, self._ev_proc, 0, 0, WINEVENT_OUTOFCONTEXT)
                 for ev in (EVENT_SYSTEM_FOREGROUND, EVENT_SYSTEM_MINIMIZEEND)]
        self._ready.set()
        self._emit(winapi.foreground_window())
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == WM_APP_KEYBLOCK:
                self._apply_key_block()
            else:
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
        for h in hooks:
            if h:
                user32.UnhookWinEvent(h)
        self._block = frozenset()
        self._apply_key_block()

    def _on_event(self, _hook, _event, hwnd, id_object, _id_child, _thread, _time):
        if id_object == 0 and hwnd:  # OBJID_WINDOW
            self._emit(hwnd)

    def _emit(self, hwnd) -> None:
        try:
            info = winapi.window_info(hwnd)
        except Exception:  # noqa: BLE001
            log.exception("Не удалось получить сведения об окне")
            return
        if info is None or info == self.current:
            return
        self.current = info
        with self._lock:
            subs = list(self._subs)
        for cb in subs:
            try:
                cb(info)
            except Exception:  # noqa: BLE001
                log.exception("Обработчик смены окна")

    def stop(self) -> None:
        if IS_WINDOWS and self._tid:
            user32.PostThreadMessageW(self._tid, WM_QUIT, 0, 0)
