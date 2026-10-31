"""Интернет: пинг и потери пакетов в трее, «выключить компьютер после загрузки».

Пинг — через IcmpSendEcho (входит в Windows, права администратора не нужны), раз в 2 секунды
в отдельном спящем потоке. Загрузка определяется по скорости сети (psutil) раз в 5 секунд и
только пока включено ожидание.
"""

from __future__ import annotations

import ctypes
import logging
import socket
import struct
import subprocess
import threading
import time
from collections import deque

from . import Tool
from .winapi import IS_WINDOWS

log = logging.getLogger(__name__)

PING_INTERVAL = 2.0
WINDOW = 30          # последних замеров для статистики (~1 минута)
ALERT_WINDOW = 15    # замеров для предупреждения (~30 секунд)
ALERT_COOLDOWN = 180.0

if IS_WINDOWS:
    from ctypes import wintypes

    class IP_OPTION_INFORMATION(ctypes.Structure):
        _fields_ = [("Ttl", ctypes.c_ubyte), ("Tos", ctypes.c_ubyte), ("Flags", ctypes.c_ubyte),
                    ("OptionsSize", ctypes.c_ubyte), ("OptionsData", ctypes.c_void_p)]

    class ICMP_ECHO_REPLY(ctypes.Structure):
        _fields_ = [("Address", ctypes.c_ulong), ("Status", ctypes.c_ulong), ("RoundTripTime", ctypes.c_ulong),
                    ("DataSize", ctypes.c_ushort), ("Reserved", ctypes.c_ushort), ("Data", ctypes.c_void_p),
                    ("Options", IP_OPTION_INFORMATION)]


class IcmpPinger:
    def __init__(self):
        self._lib = None
        self._handle = None
        if IS_WINDOWS:
            lib = ctypes.WinDLL("iphlpapi", use_last_error=True)
            lib.IcmpCreateFile.restype = wintypes.HANDLE
            lib.IcmpCloseHandle.argtypes = (wintypes.HANDLE,)
            lib.IcmpSendEcho.restype = wintypes.DWORD
            lib.IcmpSendEcho.argtypes = (wintypes.HANDLE, ctypes.c_ulong, ctypes.c_void_p, wintypes.WORD,
                                         ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD)
            self._lib = lib
            self._handle = lib.IcmpCreateFile()
            self._payload = ctypes.create_string_buffer(b"SoundFixer-ping!", 16)
            self._reply = ctypes.create_string_buffer(ctypes.sizeof(ICMP_ECHO_REPLY) + 16 + 8)
            self._payload_p = ctypes.c_void_p(ctypes.addressof(self._payload))
            self._reply_p = ctypes.c_void_p(ctypes.addressof(self._reply))

    def ping(self, ip: str, timeout_ms: int = 1000) -> float | None:
        """→ время отклика в мс или None (нет ответа)."""
        if self._lib is None or not self._handle:
            return None
        addr = struct.unpack("<I", socket.inet_aton(ip))[0]  # IPAddr — байты в сетевом порядке
        n = self._lib.IcmpSendEcho(self._handle, addr, self._payload_p, 16, None, self._reply_p,
                                   len(self._reply), timeout_ms)
        if n == 0:
            return None
        rep = ICMP_ECHO_REPLY.from_buffer(self._reply)
        return float(rep.RoundTripTime) if rep.Status == 0 else None

    def close(self):
        if self._lib is not None and self._handle:
            self._lib.IcmpCloseHandle(self._handle)
            self._handle = None


def ping_stats(samples) -> dict:
    """samples: последовательность мс или None → {last, avg, loss, jitter, n}."""
    samples = list(samples)
    ok = [s for s in samples if s is not None]
    n = len(samples)
    stats = {"n": n, "last": samples[-1] if samples else None, "avg": None, "loss": 0.0, "jitter": None}
    if n:
        stats["loss"] = 100.0 * (n - len(ok)) / n
    if ok:
        stats["avg"] = sum(ok) / len(ok)
        if len(ok) > 1:
            stats["jitter"] = sum(abs(a - b) for a, b in zip(ok, ok[1:])) / (len(ok) - 1)
    return stats


class PingMonitor(Tool):
    key = "ping"
    title = "Пинг"

    def __init__(self, app, pinger=None):
        super().__init__(app)
        self.cfg: dict = {}
        self.samples: deque = deque(maxlen=WINDOW)
        self._pinger = pinger
        self._stop = threading.Event()
        self._thread = None
        self._ip = None
        self._ip_time = 0.0
        self._last_alert = 0.0
        self.error = ""

    def start(self, cfg):
        super().start(cfg)
        self.cfg = dict(cfg)
        self.samples.clear()
        self._ip = None
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="SoundFixer-ping", daemon=True)
        self._thread.start()

    def update(self, cfg):
        if cfg.get("host") != self.cfg.get("host"):
            self._ip = None
            self.samples.clear()
        self.cfg = dict(cfg)

    def stop(self):
        super().stop()
        self._stop.set()

    def _resolve(self) -> str | None:
        now = time.monotonic()
        if self._ip and now - self._ip_time < 600:
            return self._ip
        host = (self.cfg.get("host") or "1.1.1.1").strip()
        try:
            self._ip = socket.gethostbyname(host)
            self._ip_time = now
            self.error = ""
        except OSError:
            self.error = f"не удалось найти адрес «{host}»"
            self._ip = None
        return self._ip

    def _run(self):
        if self._pinger is None:
            self._pinger = IcmpPinger()
        try:
            while not self._stop.is_set():
                ip = self._resolve()
                self.samples.append(self._pinger.ping(ip) if ip else None)
                self._check_alert()
                self.app.ui(self.app.tool_status_changed, self.key)
                self._stop.wait(PING_INTERVAL)
        finally:
            if isinstance(self._pinger, IcmpPinger):
                self._pinger.close()
                self._pinger = None

    def _check_alert(self):
        if not self.cfg.get("notify", True) or len(self.samples) < ALERT_WINDOW:
            return
        st = ping_stats(list(self.samples)[-ALERT_WINDOW:])
        bad_loss = st["loss"] >= self.cfg.get("warn_loss", 5)
        bad_ping = st["avg"] is not None and st["avg"] >= self.cfg.get("warn_ms", 150)
        now = time.monotonic()
        if (bad_loss or bad_ping) and now - self._last_alert > ALERT_COOLDOWN:
            self._last_alert = now
            parts = []
            if bad_ping:
                parts.append(f"пинг {st['avg']:.0f} мс")
            if bad_loss:
                parts.append(f"потери {st['loss']:.0f}%")
            self.app.ui(self.app.notify, "Интернет хуже обычного", ", ".join(parts))

    def summary(self) -> str:
        if self.error:
            return self.error
        st = ping_stats(self.samples)
        if not st["n"]:
            return "замеряю…"
        if st["avg"] is None:
            return "нет ответа"
        last = "нет ответа" if st["last"] is None else f"{st['last']:.0f} мс"
        jit = f", разброс {st['jitter']:.0f} мс" if st["jitter"] is not None else ""
        return f"сейчас {last}, среднее {st['avg']:.0f} мс, потери {st['loss']:.0f}%{jit}"

    def status(self) -> str:
        return f"Пинг до {self.cfg.get('host', '1.1.1.1')}: {self.summary()}" if self.running else ""

    def tray_text(self) -> str:
        st = ping_stats(self.samples)
        if st["avg"] is None:
            return ""
        return f"пинг {st['avg']:.0f} мс" + (f", потери {st['loss']:.0f}%" if st["loss"] else "")


# ---------------------------------------------------------------- выключение после загрузки
ACTIONS = {"sleep": "сон", "hibernate": "гибернация", "shutdown": "выключение"}
COUNTDOWN = 60


class DownloadWatcher:
    """Ждёт, пока скорость загрузки упадёт ниже порога на N минут, затем — сон/выключение."""

    def __init__(self, app, action: str, kbps: float, minutes: float, counter=None, period: float = 5.0):
        self.app = app
        self.action = action if action in ACTIONS else "sleep"
        self.kbps = kbps
        self.minutes = minutes
        self.period = period
        self._counter = counter or self._psutil_counter
        self._stop = threading.Event()
        self.state = "waiting"      # waiting → downloading → countdown → done / cancelled / error
        self.error = ""
        self.speed = 0.0            # КБ/с
        self.seen_download = False
        self.left = COUNTDOWN
        self._thread = threading.Thread(target=self._run, name="SoundFixer-download", daemon=True)

    @staticmethod
    def _psutil_counter() -> int:
        import psutil

        return psutil.net_io_counters().bytes_recv

    def start(self):
        self._thread.start()

    def cancel(self):
        self._stop.set()
        if self.state != "done":
            self.state = "cancelled"

    def _run(self):
        try:
            self._watch()
        except Exception as err:  # noqa: BLE001
            log.exception("Ожидание загрузки")
            self.state = "error"
            self.error = str(err)
            self.app.ui(self.app.tool_status_changed, "download")

    def _watch(self):
        hist: deque = deque(maxlen=max(2, int(self.minutes * 60 / self.period)))
        prev = self._counter()
        while not self._stop.wait(self.period):
            cur = self._counter()
            self.speed = max(0.0, (cur - prev) / 1024 / self.period)
            prev = cur
            hist.append(self.speed)
            if self.speed >= self.kbps:
                self.seen_download = True
                self.state = "downloading"
            if self.seen_download and len(hist) == hist.maxlen and max(hist) < self.kbps:
                self._countdown()
                return
            self.app.ui(self.app.tool_status_changed, "download")

    def _countdown(self):
        self.state = "countdown"
        self.app.ui(self.app.notify, "Загрузка завершена",
                    f"{ACTIONS[self.action].capitalize()} через {COUNTDOWN} с. Отменить — в меню трея.")
        for left in range(COUNTDOWN, 0, -1):
            self.left = left
            self.app.ui(self.app.tool_status_changed, "download")
            if self._stop.wait(1.0):
                return
        self.state = "done"
        self.app.ui(self.app.tool_status_changed, "download")
        do_power_action(self.action)

    def status(self) -> str:
        if self.state == "waiting":
            return f"Жду начала загрузки (сейчас {self.speed:.0f} КБ/с)…"
        if self.state == "downloading":
            return (f"Идёт загрузка: {self.speed:.0f} КБ/с. Когда скорость будет ниже {self.kbps:.0f} КБ/с "
                    f"{self.minutes:g} мин — {ACTIONS[self.action]}.")
        if self.state == "countdown":
            return f"Загрузка завершена — {ACTIONS[self.action]} через {self.left} с"
        if self.state == "done":
            return "Выполнено"
        if self.state == "error":
            return f"Ошибка: {self.error}"
        return "Отменено"


def do_power_action(action: str) -> None:
    log.info("Действие после загрузки: %s", action)
    if not IS_WINDOWS:
        return
    if action == "shutdown":
        subprocess.Popen(["shutdown", "/s", "/t", "0"], creationflags=0x08000000)
        return
    powrprof = ctypes.WinDLL("powrprof")
    powrprof.SetSuspendState(action == "hibernate", False, False)
