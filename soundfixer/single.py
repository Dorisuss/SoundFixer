"""Один экземпляр программы: повторный запуск просто показывает окно уже запущенной."""

from __future__ import annotations

import ctypes
import sys
import threading

MUTEX_NAME = "Local\\SoundFixer.Instance.v2"
EVENT_NAME = "Local\\SoundFixer.Show.v2"
ERROR_ALREADY_EXISTS = 183
EVENT_MODIFY_STATE = 0x0002
INFINITE = 0xFFFFFFFF


class SingleInstance:
    def __init__(self):
        self.primary = True
        self._mutex = None
        if sys.platform != "win32":
            return
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateMutexW.restype = ctypes.c_void_p
        k32.CreateEventW.restype = ctypes.c_void_p
        k32.OpenEventW.restype = ctypes.c_void_p
        k32.WaitForSingleObject.argtypes = (ctypes.c_void_p, ctypes.c_uint32)
        k32.SetEvent.argtypes = (ctypes.c_void_p,)
        k32.CloseHandle.argtypes = (ctypes.c_void_p,)
        self._k32 = k32
        self._mutex = k32.CreateMutexW(None, False, MUTEX_NAME)
        self.primary = ctypes.get_last_error() != ERROR_ALREADY_EXISTS

    def notify_primary(self) -> None:
        if sys.platform != "win32":
            return
        handle = self._k32.OpenEventW(EVENT_MODIFY_STATE, False, EVENT_NAME)
        if handle:
            self._k32.SetEvent(handle)
            self._k32.CloseHandle(handle)

    def listen(self, on_show) -> None:
        """Ждёт сигнала от второго экземпляра (поток спит в ядре, CPU не тратится)."""
        if sys.platform != "win32":
            return
        event = self._k32.CreateEventW(None, False, False, EVENT_NAME)

        def wait():
            while self._k32.WaitForSingleObject(event, INFINITE) == 0:
                on_show()

        threading.Thread(target=wait, name="SoundFixer-instance", daemon=True).start()
