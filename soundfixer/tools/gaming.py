"""Игры: распознавание игры на экране и защита от случайных нажатий.

* Клавиша Win не сворачивает игру (перехват ставится только пока игра активна).
* Не выскакивает «Залипание клавиш» (5× Shift), «Фильтрация ввода» (удержание Shift)
  и «Озвучивание переключения» (удержание NumLock). Меняется только для текущего сеанса
  и возвращается при выключении инструмента — настройки Windows не портятся.
* В игре автоматически включается английская раскладка.
"""

from __future__ import annotations

import ctypes
import logging

from . import Tool, winapi
from .winapi import IS_WINDOWS, VK_LWIN, VK_RWIN, WindowInfo

log = logging.getLogger(__name__)

GAME_DIRS = (
    "\\steamapps\\common\\", "\\epic games\\", "\\riot games\\", "\\gog galaxy\\games\\", "\\gog games\\",
    "\\xboxgames\\", "\\ubisoft game launcher\\games\\", "\\ea games\\", "\\origin games\\",
    "\\rockstar games\\", "\\wargaming.net\\", "\\lesta\\", "\\games\\", "\\battle.net\\games\\",
    "\\roblox\\versions\\", "\\minecraft",
)
NOT_GAMES = {
    "explorer.exe", "chrome.exe", "msedge.exe", "firefox.exe", "opera.exe", "browser.exe", "brave.exe",
    "vivaldi.exe", "yandex.exe", "vlc.exe", "mpc-hc64.exe", "mpc-hc.exe", "mpc-be64.exe", "potplayermini64.exe",
    "potplayer.exe", "wmplayer.exe", "applicationframehost.exe", "powerpnt.exe", "obs64.exe", "discord.exe",
    "telegram.exe", "steam.exe", "steamwebhelper.exe", "epicgameslauncher.exe", "battle.net.exe",
    "riotclientservices.exe", "riotclientux.exe", "pythonw.exe", "python.exe", "soundfixer.exe",
    "searchhost.exe", "startmenuexperiencehost.exe", "lockapp.exe", "textinputhost.exe", "taskmgr.exe",
    "shellexperiencehost.exe", "galaxyclient.exe", "upc.exe", "eadesktop.exe", "unitycrashhandler64.exe",
}


def is_game(info: WindowInfo | None, cfg: dict) -> bool:
    """Ваш список → исключения (браузеры, плееры, лаунчеры) → папки игр → полноэкранное окно."""
    if info is None or not info.exe:
        return False
    if info.exe in {g.lower() for g in cfg.get("list", [])}:
        return True
    if info.exe in NOT_GAMES:
        return False
    path = info.path.lower()
    if any(d in path for d in GAME_DIRS):
        return True
    return bool(cfg.get("fullscreen", True) and info.fullscreen)


# ---------------------------------------------------------------- специальные возможности Windows
SPI = {  # (get, set, размер структуры в DWORD после cbSize)
    "sticky": (0x003A, 0x003B, 1),
    "toggle": (0x0034, 0x0035, 1),
    "filter": (0x0032, 0x0033, 5),
}
HOTKEY_FLAGS = 0x4 | 0x8  # *_HOTKEYACTIVE | *_CONFIRMHOTKEY
FEATURE_ON = 0x1          # сама функция включена пользователем — тогда не трогаем


def _spi_struct(n: int):
    class S(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_uint)] + [(f"f{i}", ctypes.c_uint) for i in range(n)]
    return S


def set_accessibility_hotkeys(enabled: bool, saved: dict) -> dict:
    """Выключает/возвращает «горячие клавиши» специальных возможностей. saved — исходные флаги."""
    if not IS_WINDOWS:
        return saved
    spi_fn = winapi.user32.SystemParametersInfoW
    out = dict(saved)
    for name, (get, set_, n) in SPI.items():
        s = _spi_struct(n)()
        s.cbSize = ctypes.sizeof(s)
        if not spi_fn(get, s.cbSize, ctypes.byref(s), 0):
            continue
        if not enabled:
            if s.f0 & FEATURE_ON or not s.f0 & HOTKEY_FLAGS:
                continue
            out[name] = s.f0
            s.f0 &= ~HOTKEY_FLAGS
        elif name in out:
            s.f0 = out.pop(name)
        else:
            continue
        spi_fn(set_, s.cbSize, ctypes.byref(s), 0)  # 0 — только текущий сеанс, без записи в реестр
    return out


class GameGuard(Tool):
    key = "games"
    title = "Игры"

    def __init__(self, app):
        super().__init__(app)
        self.cfg: dict = {}
        self.in_game = False
        self.current: WindowInfo | None = None
        self._saved_spi: dict = {}
        self._watcher = None
        self._last_hwnd = 0

    def start(self, cfg):
        super().start(cfg)
        self.cfg = dict(cfg)
        self._apply_spi()
        self._watcher = self.app.tools.get_watcher()
        self._watcher.subscribe(self._on_window)

    def update(self, cfg):
        self.cfg = dict(cfg)
        self._apply_spi()
        if self._watcher is not None and self._watcher.current is not None:
            self._last_hwnd = 0
            self._on_window(self._watcher.current)

    def stop(self):
        super().stop()
        if self._watcher is not None:
            self._watcher.unsubscribe(self._on_window)
            self._watcher.set_key_block(())
            self._watcher = None
        self._saved_spi = set_accessibility_hotkeys(True, self._saved_spi)
        self.in_game = False

    def _apply_spi(self):
        self._saved_spi = set_accessibility_hotkeys(not self.cfg.get("no_sticky", True), self._saved_spi)

    def _on_window(self, info: WindowInfo):
        """Поток наблюдателя: только быстрые действия."""
        self.current = info
        game = is_game(info, self.cfg)
        changed = game != self.in_game
        self.in_game = game
        if self._watcher is not None:
            self._watcher.set_key_block({VK_LWIN, VK_RWIN} if game and self.cfg.get("block_win", True) else ())
        if game and self.cfg.get("english", True) and info.hwnd != self._last_hwnd:
            winapi.switch_layout(info.hwnd, winapi.LANG_ENGLISH)
        self._last_hwnd = info.hwnd
        if changed:
            log.info("Игра на экране: %s", info.exe if game else "нет")
        self.app.ui(self.app.tool_status_changed, self.key)

    def status(self) -> str:
        if not self.running:
            return ""
        info = self.current
        if info is None:
            return "Жду смены окна…"
        if self.in_game:
            protect = []
            if self.cfg.get("block_win", True):
                protect.append("Win заблокирована")
            if self.cfg.get("english", True):
                protect.append("английская раскладка")
            return f"Сейчас игра: {info.exe}" + (f" — {', '.join(protect)}" if protect else "")
        return f"Сейчас не игра: {info.exe or '—'}"
