"""Один экземпляр программы: повторный запуск показывает окно уже запущенной.

Второй экземпляр просит первый показать окно и ждёт подтверждения. Если первый не
отвечает (завис), пользователю предлагается закрыть его и запустить программу заново —
иначе двойной клик по ярлыку выглядел бы так, будто программа «сама закрывается».
"""

from __future__ import annotations

import ctypes
import os
import sys
import threading
import time

MUTEX_NAME = "Local\\SoundFixer.Instance.v2"
EVENT_NAME = "Local\\SoundFixer.Show.v2"
ACK_NAME = "Local\\SoundFixer.Shown.v2"
ERROR_ALREADY_EXISTS = 183
EVENT_MODIFY_STATE = 0x0002
SYNCHRONIZE = 0x00100000
PROCESS_TERMINATE = 0x0001
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
INFINITE = 0xFFFFFFFF
WAIT_OBJECT_0 = 0
MB_YESNO, MB_ICONWARNING, MB_SETFOREGROUND, IDYES = 0x4, 0x30, 0x10000, 6
OUR_IMAGES = {"soundfixer.exe", "pythonw.exe", "python.exe"}


def _pid_file():
    from .config import CONFIG_DIR

    return CONFIG_DIR / "instance.pid"


class SingleInstance:
    def __init__(self):
        self.primary = True
        self._mutex = None
        if sys.platform != "win32":
            return
        from ctypes import wintypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateMutexW.restype = wintypes.HANDLE
        k32.CreateMutexW.argtypes = (ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR)
        k32.CreateEventW.restype = wintypes.HANDLE
        k32.CreateEventW.argtypes = (ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR)
        k32.OpenEventW.restype = wintypes.HANDLE
        k32.OpenEventW.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR)
        k32.WaitForSingleObject.restype = wintypes.DWORD
        k32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        k32.SetEvent.argtypes = (wintypes.HANDLE,)
        k32.CloseHandle.argtypes = (wintypes.HANDLE,)
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        k32.TerminateProcess.argtypes = (wintypes.HANDLE, wintypes.UINT)
        k32.GetProcessTimes.argtypes = (wintypes.HANDLE,) + (ctypes.POINTER(wintypes.FILETIME),) * 4
        k32.QueryFullProcessImageNameW.argtypes = (wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                   ctypes.POINTER(wintypes.DWORD))
        k32.GetCurrentProcess.restype = wintypes.HANDLE
        self._k32 = k32
        self._wintypes = wintypes
        self._mutex = k32.CreateMutexW(None, False, MUTEX_NAME)
        self.primary = ctypes.get_last_error() != ERROR_ALREADY_EXISTS

    # ---------------------------------------------------------------- сведения о процессе
    def _start_time(self, handle) -> int:
        w = self._wintypes
        times = [w.FILETIME() for _ in range(4)]
        if not self._k32.GetProcessTimes(handle, *[ctypes.byref(t) for t in times]):
            return 0
        return (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime

    def _image(self, handle) -> str:
        buf = ctypes.create_unicode_buffer(1024)
        size = self._wintypes.DWORD(len(buf))
        if self._k32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return os.path.basename(buf.value).lower()
        return ""

    def _write_pid(self) -> None:
        try:
            path = _pid_file()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"{os.getpid()} {self._start_time(self._k32.GetCurrentProcess())}", encoding="ascii")
        except OSError:
            pass

    def _primary_pid(self) -> tuple[int, int]:
        try:
            pid, started = _pid_file().read_text(encoding="ascii").split()
            return int(pid), int(started)
        except (OSError, ValueError):
            return 0, 0

    # ---------------------------------------------------------------- второй экземпляр
    def notify_primary(self, timeout: float = 10.0) -> bool:
        """Попросить запущенную программу показать окно. → True, если она ответила."""
        if sys.platform != "win32":
            return True
        k32 = self._k32
        pid, _ = self._primary_pid()
        if pid:
            try:  # без этого Windows не даст другому процессу вывести окно на передний план
                ctypes.windll.user32.AllowSetForegroundWindow(pid)
            except (AttributeError, OSError):
                pass
        ack = k32.CreateEventW(None, False, False, ACK_NAME)
        deadline = time.monotonic() + timeout
        try:
            while True:  # первый экземпляр может ещё запускаться и не успеть создать событие
                handle = k32.OpenEventW(EVENT_MODIFY_STATE, False, EVENT_NAME)
                if handle:
                    k32.SetEvent(handle)
                    k32.CloseHandle(handle)
                    break
                if time.monotonic() > deadline:
                    return False
                time.sleep(0.25)
            if not ack:
                return True
            left = max(0, int((deadline - time.monotonic()) * 1000))
            return k32.WaitForSingleObject(ack, left) == WAIT_OBJECT_0
        finally:
            if ack:
                k32.CloseHandle(ack)

    def ask_restart(self) -> bool:
        """Первый экземпляр не отвечает: предложить закрыть его. → True, если можно запускаться."""
        if sys.platform != "win32":
            return False
        answer = ctypes.windll.user32.MessageBoxW(
            None, "SoundFixer уже запущен, но не отвечает.\n\nЗакрыть его и запустить заново?", "SoundFixer",
            MB_YESNO | MB_ICONWARNING | MB_SETFOREGROUND)
        return answer == IDYES and self.kill_primary()

    def kill_primary(self) -> bool:
        pid, started = self._primary_pid()
        if not pid or pid == os.getpid():
            return False
        k32 = self._k32
        handle = k32.OpenProcess(PROCESS_TERMINATE | SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return True  # процесса уже нет
        try:
            # номер процесса мог достаться другой программе — проверяем имя и время запуска
            if self._image(handle) not in OUR_IMAGES or (started and self._start_time(handle) != started):
                return True
            k32.TerminateProcess(handle, 1)
            k32.WaitForSingleObject(handle, 5000)
            return True
        finally:
            k32.CloseHandle(handle)

    # ---------------------------------------------------------------- первый экземпляр
    def listen(self, on_show) -> None:
        """Ждёт сигнала от второго экземпляра (поток спит в ядре, CPU не тратится)."""
        if sys.platform != "win32":
            return
        self._write_pid()
        event = self._k32.CreateEventW(None, False, False, EVENT_NAME)

        def wait():
            while self._k32.WaitForSingleObject(event, INFINITE) == WAIT_OBJECT_0:
                on_show()

        threading.Thread(target=wait, name="SoundFixer-instance", daemon=True).start()

    def acknowledge(self) -> None:
        """Окно показано — второй экземпляр может спокойно завершиться."""
        if sys.platform != "win32":
            return
        handle = self._k32.OpenEventW(EVENT_MODIFY_STATE, False, ACK_NAME)
        if handle:
            self._k32.SetEvent(handle)
            self._k32.CloseHandle(handle)
