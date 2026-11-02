"""Автозапуск через HKCU\\...\\Run.

Источник правды — реестр, а не конфиг: если пользователь отключил программу в
Диспетчере задач (StartupApproved), мы это видим и не включаем молча обратно.
Если программу перенесли в другую папку, путь в реестре чинится сам.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from . import APP_NAME

log = logging.getLogger(__name__)

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
APPROVED_KEY = r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run"
TRAY_ARG = "--tray"

try:
    import winreg
except ImportError:  # не Windows — модуль работает как заглушка
    winreg = None


def base_command() -> str:
    """Команда запуска программы без аргументов (exe или pythonw + скрипт)."""
    if getattr(sys, "frozen", False):  # PyInstaller exe
        return f'"{Path(sys.executable).resolve()}"'
    exe = Path(sys.executable).resolve()
    if exe.name.lower() == "python.exe":
        pyw = exe.with_name("pythonw.exe")
        if pyw.exists():
            exe = pyw
    script = Path(__file__).resolve().parent.parent / "SoundFixer.pyw"
    return f'"{exe}" "{script}"'


def launch_command() -> str:
    return f"{base_command()} {TRAY_ARG}"


def _read_run_value():
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
        try:
            return winreg.QueryValueEx(key, APP_NAME)[0]
        except FileNotFoundError:
            return None


def _approved_disabled() -> bool:
    """True, если автозапуск выключен в Диспетчере задач / Параметрах Windows."""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, APPROVED_KEY) as key:
            data = winreg.QueryValueEx(key, APP_NAME)[0]
    except OSError:
        return False
    # Первый байт: 0x02/0x06 — включено, 0x03/0x07 и прочие нечётные — выключено
    return bool(data) and bool(data[0] & 0x01)


def is_enabled() -> bool:
    if winreg is None:
        return False
    try:
        return bool(_read_run_value()) and not _approved_disabled()
    except OSError as err:
        log.warning("Не удалось прочитать автозапуск: %s", err)
        return False


def set_enabled(enabled: bool) -> None:
    if winreg is None:
        return
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, launch_command())
        else:
            try:
                winreg.DeleteValue(key, APP_NAME)
            except FileNotFoundError:
                pass
    # Снимаем/убираем отметку «отключено» из Диспетчера задач
    try:
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, APPROVED_KEY, 0, winreg.KEY_SET_VALUE) as key:
            if enabled:
                winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_BINARY, b"\x02" + b"\x00" * 11)
            else:
                try:
                    winreg.DeleteValue(key, APP_NAME)
                except FileNotFoundError:
                    pass
    except OSError as err:
        log.warning("StartupApproved: %s", err)


def repair() -> None:
    """Если автозапуск включён, но путь устарел (программу перенесли / обновили Python) — исправить."""
    if winreg is None:
        return
    try:
        current = _read_run_value()
        if current and current != launch_command():
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
                winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, launch_command())
            log.info("Путь автозапуска обновлён: %s", launch_command())
    except OSError as err:
        log.warning("Не удалось обновить автозапуск: %s", err)
