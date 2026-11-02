"""Мультитул: независимые инструменты, каждый включается галочкой.

Выключенный инструмент не загружается и ничего не тратит. Инструменты запускаются
и останавливаются из GUI-потока; всё долгое они делают в своих потоках.
"""

from __future__ import annotations

import importlib
import logging

log = logging.getLogger(__name__)


class Tool:
    key = ""
    title = ""

    def __init__(self, app):
        self.app = app
        self.running = False

    # жизненный цикл (GUI-поток)
    def start(self, cfg: dict) -> None:
        self.running = True

    def stop(self) -> None:
        self.running = False

    def update(self, cfg: dict) -> None:
        """Настройки изменились, пока инструмент работает."""

    # интеграция
    def hotkeys(self, cfg: dict) -> dict[str, str]:
        return {}

    def on_hotkey(self, action: str) -> None:
        pass

    def status(self) -> str:
        return ""

    def tray_text(self) -> str:
        return ""


# ключ → (модуль, класс); модуль импортируется только при первом включении
REGISTRY = {
    "layout": ("layout", "LayoutFixer"),
    "games": ("gaming", "GameGuard"),
    "ping": ("network", "PingMonitor"),
    "windows": ("desktop", "WindowTools"),
    "awake": ("desktop", "KeepAwake"),
    "translate": ("textutils", "TranslateTool"),
    "plainpaste": ("textutils", "PlainPaste"),
    "qr": ("textutils", "QRTool"),
    "ocr": ("ocr", "ScreenText"),
    "snap": ("desktop", "WindowSnap"),
    "color": ("desktop", "ColorPicker"),
    "night": ("screen", "NightLight"),
    "theme": ("screen", "ThemeSwitcher"),
    "brightness": ("screen", "Brightness"),
    "apps": ("audio_tools", "AppVolume"),
    "micguard": ("audio_tools", "MicGuard"),
    "autoprofile": ("audio_tools", "AutoProfiles"),
    "breaks": ("system", "BreakReminder"),
    "power": ("system", "PowerPlans"),
}


class ToolManager:
    def __init__(self, app):
        self.app = app
        self.tools: dict[str, Tool] = {}
        self.watcher = None
        self.download = None  # network.DownloadWatcher, когда включено «выключить после загрузки»
        self.sleep_timer = None  # system.SleepTimer

    def _tool(self, key: str) -> Tool:
        if key not in self.tools:
            mod_name, cls_name = REGISTRY[key]
            mod = importlib.import_module(f"{__name__}.{mod_name}")
            self.tools[key] = getattr(mod, cls_name)(self.app)
        return self.tools[key]

    def get(self, key: str) -> Tool | None:
        return self.tools.get(key)

    def sync(self, cfg_tools: dict) -> None:
        for key in REGISTRY:
            cfg = cfg_tools.get(key, {})
            want = bool(cfg.get("enabled"))
            tool = self.tools.get(key)
            try:
                if want and (tool is None or not tool.running):
                    self._tool(key).start(cfg)
                elif want:
                    tool.update(cfg)
                elif tool is not None and tool.running:
                    tool.stop()
            except Exception:  # noqa: BLE001 — один инструмент не должен ронять остальные
                log.exception("Инструмент %s", key)
        if self.watcher is not None and self.watcher.idle:
            self.watcher.stop()
            self.watcher = None

    def get_watcher(self):
        if self.watcher is None:
            from .foreground import ForegroundWatcher

            self.watcher = ForegroundWatcher()
            self.watcher.start()
        return self.watcher

    def hotkeys(self, cfg_tools: dict) -> dict[str, str]:
        out = {}
        for key, tool in self.tools.items():
            if tool.running:
                for action, combo in tool.hotkeys(cfg_tools.get(key, {})).items():
                    if combo:
                        out[f"tool:{key}:{action}"] = combo
        return out

    def on_hotkey(self, name: str) -> None:
        _, key, action = name.split(":", 2)
        tool = self.tools.get(key)
        if tool is not None and tool.running:
            tool.on_hotkey(action)

    def tray_text(self) -> str:
        parts = [t.tray_text() for t in self.tools.values() if t.running]
        return " • ".join(p for p in parts if p)

    def broadcast(self, event: str, *args) -> None:
        """Событие программы (например, on_mute) — всем работающим инструментам, которым оно нужно."""
        for tool in list(self.tools.values()):
            fn = getattr(tool, event, None)
            if tool.running and fn is not None:
                try:
                    fn(*args)
                except Exception:  # noqa: BLE001
                    log.exception("Инструмент %s: %s", tool.key, event)

    def instance(self, key: str) -> Tool:
        """Объект инструмента без запуска (например, окно микшера из трея)."""
        return self._tool(key)

    def stop_all(self) -> None:
        for tool in self.tools.values():
            if tool.running:
                try:
                    tool.stop()
                except Exception:  # noqa: BLE001
                    log.exception("Остановка инструмента %s", tool.key)
        if self.download is not None:
            self.download.cancel()
        if self.sleep_timer is not None:
            self.sleep_timer.cancel()
        if self.watcher is not None:
            self.watcher.stop()
            self.watcher = None
