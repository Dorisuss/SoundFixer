"""Поток управления: все COM-вызовы и запуск/остановка аудиопотоков идут через него.

Задачи выполняются строго последовательно (никаких гонок), отложенные задачи
с ключом схлопываются (debounce) — пачка системных событий даёт одну проверку.
Когда задач нет, поток спит на condition variable и не тратит CPU.
"""

from __future__ import annotations

import heapq
import itertools
import logging
import threading
import time
from concurrent.futures import Future

log = logging.getLogger(__name__)


class Worker(threading.Thread):
    def __init__(self, name: str = "SoundFixer-control"):
        super().__init__(name=name, daemon=True)
        self._cv = threading.Condition()
        self._heap: list = []  # (when, seq, key, fn, future)
        self._keys: dict = {}  # key -> seq актуальной задачи
        self._seq = itertools.count()
        self._running = True

    def run(self):
        com = None
        try:
            import comtypes  # уже импортирован в главном потоке, здесь только MTA

            comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
            com = comtypes
        except Exception:
            pass
        try:
            self._loop()
        finally:
            if com is not None:
                try:
                    com.CoUninitialize()
                except Exception:
                    pass

    def _loop(self):
        while True:
            with self._cv:
                while self._running:
                    if self._heap:
                        delay = self._heap[0][0] - time.monotonic()
                        if delay <= 0:
                            break
                        self._cv.wait(delay)
                    else:
                        self._cv.wait()
                if not self._running:
                    return
                _, seq, key, fn, fut = heapq.heappop(self._heap)
                if key is not None:
                    if self._keys.get(key) != seq:
                        continue  # задачу заменили более свежей
                    del self._keys[key]
            if not fut.set_running_or_notify_cancel():
                continue
            try:
                fut.set_result(fn())
            except BaseException as err:  # noqa: BLE001 — поток не должен умирать
                log.exception("Ошибка в задаче %s", getattr(fn, "__name__", fn))
                fut.set_exception(err)

    def submit(self, fn, delay: float = 0.0, key=None) -> Future:
        fut: Future = Future()
        with self._cv:
            seq = next(self._seq)
            if key is not None:
                self._keys[key] = seq
            heapq.heappush(self._heap, (time.monotonic() + delay, seq, key, fn, fut))
            self._cv.notify()
        return fut

    def call(self, fn, timeout: float | None = 10.0):
        """Синхронный вызов (не использовать из самого потока управления)."""
        if threading.current_thread() is self:
            return fn()
        return self.submit(fn).result(timeout)

    def stop(self):
        with self._cv:
            self._running = False
            self._cv.notify()
