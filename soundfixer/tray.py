"""Иконка в трее (pystray, штатный API без приватных хаков).

ЛКМ — открыть окно (пункт default=True), ПКМ — меню.
Колбэки приходят из потока трея и сразу передаются в GUI-поток через app.ui().
"""

from __future__ import annotations

import threading

import pystray
from pystray import Menu, MenuItem as Item

from . import APP_NAME
from .icons import tray_icon


class Tray:
    def __init__(self, app):
        self.app = app
        self.icon = pystray.Icon(APP_NAME, tray_icon(), APP_NAME, menu=self._menu())
        self._thread = None
        self._title = APP_NAME

    def _menu(self) -> Menu:
        app = self.app
        ui = app.ui

        def switch(name):
            # pystray считает аргументы действия — функция должна быть без параметров
            return lambda: ui(app.switch_profile, name)

        def profile_items():
            for name in list(app.cfg["profiles"]):
                yield Item(
                    name,
                    switch(name),
                    checked=lambda _it, n=name: app.cfg["active_profile"] == n,
                    radio=True,
                )

        return Menu(
            Item("Открыть SoundFixer", lambda: ui(app.show_window), default=True),
            Item("Профиль", Menu(profile_items)),
            Menu.SEPARATOR,
            Item("Выключить микрофон", lambda: ui(app.toggle_mute), checked=lambda _it: app.muted),
            Item("Обработка микрофона", lambda: ui(app.toggle_feature, "mic"),
                 checked=lambda _it: app.cfg["mic"]["enabled"]),
            Item("Обработка наушников", lambda: ui(app.toggle_feature, "spk"),
                 checked=lambda _it: app.cfg["spk"]["enabled"]),
            Item("Применить устройства сейчас", lambda: ui(app.apply_now)),
            Menu.SEPARATOR,
            Item(lambda _it: f"Интернет: {app.tools.tray_text() or '—'}", None, enabled=False,
                 visible=lambda _it: bool(app.tools.get("ping") and app.tools.get("ping").running)),
            Item("Не засыпать", lambda: ui(app.toggle_tool, "awake"),
                 checked=lambda _it: app.cfg["tools"]["awake"]["enabled"]),
            Item(lambda _it: "Отменить выключение после загрузки" if app.download_active
                 else "Выключить после загрузки",
                 lambda: ui(app.cancel_download_watch if app.download_active else app.start_download_watch)),
            Menu.SEPARATOR,
            Item("Запускать с Windows", lambda: ui(app.toggle_autostart),
                 checked=lambda _it: app.autostart_enabled),
            Item("Выход", lambda: ui(app.quit)),
        )

    def start(self) -> None:
        self._thread = threading.Thread(target=self.icon.run, name="SoundFixer-tray", daemon=True)
        self._thread.start()

    def refresh(self) -> None:
        """Обновить иконку/подсказку/меню (вызывать после изменения состояния)."""
        app = self.app
        active = app.cfg["mic"]["enabled"] or app.cfg["spk"]["enabled"]
        self.icon.icon = tray_icon(active, app.muted)
        self.refresh_title()
        try:
            self.icon.update_menu()
        except Exception:  # noqa: BLE001 — меню ещё не создано
            pass

    def refresh_title(self) -> None:
        """Только подсказка у иконки (дёшево — можно часто, например для пинга)."""
        app = self.app
        tip = f"{APP_NAME} — {app.cfg['active_profile']}"
        if app.muted:
            tip += " (микрофон выключен)"
        extra = app.tools.tray_text()
        if extra:
            tip += "\n" + extra
        if tip[:127] != self._title:
            self._title = tip[:127]
            self.icon.title = self._title

    def notify(self, title: str, text: str) -> None:
        if self.icon.visible:
            try:
                self.icon.notify(text, title)
            except Exception:  # noqa: BLE001
                pass

    def stop(self) -> None:
        try:
            self.icon.stop()
        except Exception:  # noqa: BLE001
            pass
