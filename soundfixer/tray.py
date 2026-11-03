"""Иконка в трее (pystray, штатный API без приватных хаков).

ЛКМ — открыть окно (пункт default=True), ПКМ — меню.
Колбэки приходят из потока трея и сразу передаются в GUI-поток через app.ui().
"""

from __future__ import annotations

import sys
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

        def timer(minutes):
            return lambda: ui(app.start_sleep_timer, minutes)

        def timer_items():
            if app.sleep_timer_active:
                yield Item(lambda _it: app.tools.sleep_timer.status(), None, enabled=False)
                yield Item("Отменить таймер", lambda: ui(app.cancel_sleep_timer))
                yield Menu.SEPARATOR
            for m in (15, 30, 45, 60, 90, 120):
                yield Item(f"Через {m} мин", timer(m))

        def bright(level):
            return lambda: ui(app.set_brightness, level)

        def power(option):
            return lambda: ui(app.set_power_option, option)

        def power_items():
            from .tools.system import active_power, power_plans

            try:
                plans, active = power_plans(), active_power()
            except Exception:  # noqa: BLE001 — меню трея не должно падать из-за схем питания
                plans, active = [], ()
            for option, name in plans:
                yield Item(name, power(option), radio=True, checked=lambda _it, o=option: o in active)

        def tool_items():
            tools = app.cfg["tools"]
            yield Item("Громкость программ…", lambda: ui(app.open_mixer))
            yield Item("Текст с экрана (распознать)", lambda: ui(app.screen_text))
            yield Item("Чем занят файл…", lambda: ui(app.pick_locksmith))
            yield Menu.SEPARATOR
            yield Item("Ночной режим", lambda: ui(app.toggle_tool, "night"), checked=lambda _it: tools["night"]["enabled"])
            yield Item("Тёмная тема по расписанию", lambda: ui(app.toggle_tool, "theme"),
                       checked=lambda _it: tools["theme"]["enabled"])
            if app.tools.get("brightness") is not None and app.tools.get("brightness").running:
                yield Item("Яркость", Menu(*[Item(f"{v}%", bright(v)) for v in (100, 75, 50, 25, 10)]))
            if sys.platform == "win32":
                yield Item("Схема питания", Menu(power_items))
            yield Menu.SEPARATOR
            yield Item("Таймер сна", Menu(timer_items))
            yield Item("Не засыпать", lambda: ui(app.toggle_tool, "awake"), checked=lambda _it: tools["awake"]["enabled"])
            yield Item(lambda _it: "Отменить выключение после загрузки" if app.download_active
                       else "Выключить после загрузки",
                       lambda: ui(app.cancel_download_watch if app.download_active else app.start_download_watch))
            yield Item("Перерывы для глаз", lambda: ui(app.toggle_tool, "breaks"),
                       checked=lambda _it: tools["breaks"]["enabled"])

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
            Item("Инструменты", Menu(tool_items)),
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
