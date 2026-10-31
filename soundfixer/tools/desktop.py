"""Окна и питание: «поверх всех», прозрачность, «не засыпать»."""

from __future__ import annotations

import logging

from . import Tool, winapi

log = logging.getLogger(__name__)


class WindowTools(Tool):
    key = "windows"
    title = "Окна"

    def hotkeys(self, cfg):
        return {"topmost": cfg.get("topmost_hotkey", ""), "more": cfg.get("fade_more", ""),
                "less": cfg.get("fade_less", "")}

    def on_hotkey(self, action):
        hwnd = winapi.foreground_window()
        if not hwnd:
            return
        title = winapi.window_title(hwnd)[:40] or "окно"
        if action == "topmost":
            state = winapi.toggle_topmost(hwnd)
            if state is None:
                self.app.notify("Окна", "Не удалось: окно запущено от имени администратора?")
            else:
                self.app.notify("Окна", f"{'Поверх всех' if state else 'Обычное окно'}: {title}")
        elif action in ("more", "less"):
            alpha = winapi.change_opacity(hwnd, -26 if action == "more" else 26)
            if alpha is None:
                return
            self.last = f"{title}: {round(alpha / 255 * 100)}%"
            self.app.tool_status_changed(self.key)

    last = ""

    def status(self):
        return f"Последнее: {self.last}" if self.running and self.last else ""


class KeepAwake(Tool):
    key = "awake"
    title = "Не засыпать"

    def start(self, cfg):
        super().start(cfg)
        self._display = bool(cfg.get("display", True))
        winapi.keep_awake(True, self._display)  # вызывается в GUI-потоке — состояние держится, пока он жив

    def update(self, cfg):
        if bool(cfg.get("display", True)) != self._display:
            self.start(cfg)

    def stop(self):
        super().stop()
        winapi.keep_awake(False)

    def status(self):
        if not self.running:
            return ""
        return "Компьютер не уснёт" + (" и экран не погаснет" if self._display else " (экран может погаснуть)")

    def tray_text(self):
        return "не засыпать"
