import ctypes
import json
import os
import sys
import threading
import winreg
from ctypes import wintypes
from pathlib import Path

import comtypes
import pystray
from PIL import Image, ImageDraw
from pystray import MenuItem as menu
from pystray._util import win32
from pycaw.constants import DEVICE_STATE, ERole
from pycaw.pycaw import AudioUtilities, EDataFlow

APP_NAME = "SoundFixer"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
CONFIG_DIR = Path(os.environ.get("APPDATA", "")) / APP_NAME
CONFIG_FILE = CONFIG_DIR / "config.json"


class TrayIcon(pystray.Icon):
    left_click_action = None

    def _on_notify(self, wparam, lparam):
        if lparam == win32.WM_LBUTTONUP:
            if self.left_click_action:
                threading.Thread(target=self.left_click_action, daemon=True).start()
            return

        if not self._menu_handle or lparam != win32.WM_RBUTTONUP:
            return

        point = wintypes.POINT()
        win32.GetCursorPos(ctypes.byref(point))
        cursor = (point.x, point.y)

        win32.SetForegroundWindow(self._hwnd)
        hmenu, descriptors = self._menu_handle
        index = win32.TrackPopupMenuEx(
            hmenu,
            win32.TPM_RIGHTALIGN | win32.TPM_BOTTOMALIGN | win32.TPM_RETURNCMD,
            cursor[0],
            cursor[1],
            self._menu_hwnd,
            None,
        )

        if index > 0:
            descriptors[index - 1](self)


def normalize_id(value):
    if not value:
        return ""
    return str(value).strip().lower()


def load_config():
    defaults = {
        "input_device_id": None,
        "output_device_id": None,
        "start_with_windows": False,
    }
    if not CONFIG_FILE.exists():
        return defaults
    try:
        with CONFIG_FILE.open(encoding="utf-8") as handle:
            data = json.load(handle)
    except (json.JSONDecodeError, OSError):
        return defaults
    defaults.update(data)
    return defaults


def save_config(data):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    temp = CONFIG_FILE.with_suffix(".tmp")
    with temp.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2, ensure_ascii=False)
    temp.replace(CONFIG_FILE)


def launch_command():
    if getattr(sys, "frozen", False):
        return f'"{Path(sys.executable).resolve()}"'
    exe = Path(sys.executable)
    if exe.name.lower() == "python.exe":
        exe = exe.with_name("pythonw.exe")
    script = Path(__file__).resolve()
    return f'"{exe}" "{script}"'


def set_startup(enabled):
    with winreg.OpenKey(
        winreg.HKEY_CURRENT_USER,
        RUN_KEY,
        0,
        winreg.KEY_SET_VALUE,
    ) as key:
        if enabled:
            winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, launch_command())
        else:
            try:
                winreg.DeleteValue(key, APP_NAME)
            except FileNotFoundError:
                pass


def make_icon(size=64):
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    pad = size // 8

    draw.rounded_rectangle(
        [pad, pad * 2, size - pad * 3, size - pad * 2],
        radius=pad,
        fill=(52, 120, 220, 255),
    )
    draw.polygon(
        [
            size - pad * 3,
            size // 2 - pad,
            size - pad,
            size // 2 + pad,
        ],
        fill=(52, 120, 220, 255),
    )

    wave_x = size - pad * 2
    for step, radius in enumerate((pad // 2, pad, pad + pad // 2)):
        box = [
            wave_x + step * (pad // 2),
            size // 2 - radius,
            wave_x + step * (pad // 2) + radius,
            size // 2 + radius,
        ]
        draw.arc(box, start=300, end=60, fill=(255, 255, 255, 255), width=2)

    mic_y = size // 2 - pad
    draw.rounded_rectangle(
        [size // 2 - pad // 2, mic_y, size // 2 + pad // 2, mic_y + pad],
        radius=pad // 4,
        fill=(255, 255, 255, 255),
    )
    draw.line(
        [(size // 2, mic_y + pad), (size // 2, mic_y + pad + pad // 2)],
        fill=(255, 255, 255, 255),
        width=2,
    )
    return image


def _enum_devices(flow):
    """Returns list of (device_id, friendly_name) — plain strings, no COM lifetime issues."""
    devices = AudioUtilities.GetAllDevices(flow, DEVICE_STATE.ACTIVE.value)
    return [(d.id, d.FriendlyName or "Unknown device") for d in devices]


def _apply_devices(input_id, output_id):
    errors = []
    for role_id, label in ((output_id, "Output"), (input_id, "Input")):
        if not role_id:
            continue
        try:
            AudioUtilities.SetDefaultDevice(
                role_id,
                [ERole.eConsole, ERole.eMultimedia, ERole.eCommunications],
            )
        except Exception as err:
            errors.append(f"{label}: {err}")
    return errors


class App:
    def __init__(self):
        self.cfg = load_config()
        comtypes.CoInitialize()
        try:
            self.inputs = _enum_devices(EDataFlow.eCapture.value)
            self.outputs = _enum_devices(EDataFlow.eRender.value)
        finally:
            comtypes.CoUninitialize()
        self.icon = TrayIcon(
            APP_NAME,
            make_icon(),
            APP_NAME,
            menu=self.build_menu(),
        )
        self.icon.left_click_action = lambda: self._do_update()

    def save(self):
        save_config(self.cfg)

    def refresh_menu(self):
        self.icon.menu = self.build_menu()
        self.icon.update_menu()

    def notify(self, title, text):
        if self.icon.visible:
            self.icon.notify(text, title)

    def pick_input(self, device_id):
        self.cfg["input_device_id"] = device_id
        self.save()

    def pick_output(self, device_id):
        self.cfg["output_device_id"] = device_id
        self.save()

    def _input_action(self, dev_id):
        def action(icon):
            self.pick_input(dev_id)
        return action

    def _output_action(self, dev_id):
        def action(icon):
            self.pick_output(dev_id)
        return action

    def _input_check(self, dev_id):
        def check(item):
            return normalize_id(self.cfg.get("input_device_id")) == normalize_id(dev_id)
        return check

    def _output_check(self, dev_id):
        def check(item):
            return normalize_id(self.cfg.get("output_device_id")) == normalize_id(dev_id)
        return check

    def input_items(self):
        if not self.inputs:
            return [menu("No microphones found", None, enabled=False)]
        return [
            menu(name, self._input_action(dev_id), checked=self._input_check(dev_id))
            for dev_id, name in self.inputs
        ]

    def output_items(self):
        if not self.outputs:
            return [menu("No output devices found", None, enabled=False)]
        return [
            menu(name, self._output_action(dev_id), checked=self._output_check(dev_id))
            for dev_id, name in self.outputs
        ]

    def _do_update(self):
        comtypes.CoInitialize()
        try:
            self.inputs = _enum_devices(EDataFlow.eCapture.value)
            self.outputs = _enum_devices(EDataFlow.eRender.value)
            input_id = self.cfg.get("input_device_id")
            output_id = self.cfg.get("output_device_id")
            _apply_devices(input_id, output_id)
        finally:
            comtypes.CoUninitialize()

        self.refresh_menu()

    def on_update(self, icon=None):
        self._do_update()

    def on_startup_toggle(self, icon=None):
        enabled = not self.cfg.get("start_with_windows", False)
        self.cfg["start_with_windows"] = enabled
        set_startup(enabled)
        self.save()
        self.refresh_menu()

    def on_quit(self, icon=None):
        self.icon.stop()

    def build_menu(self):
        return pystray.Menu(
            menu("Input", None, enabled=False),
            *self.input_items(),
            menu("Output", None, enabled=False),
            *self.output_items(),
            pystray.Menu.SEPARATOR,
            menu("Update", self.on_update),
            menu(
                "Turn with Windows",
                self.on_startup_toggle,
                checked=lambda item: self.cfg.get("start_with_windows", False),
            ),
            menu("Turn Off", self.on_quit),
        )

    def _startup_update(self):
        if self.cfg.get("input_device_id") or self.cfg.get("output_device_id"):
            self._do_update()

    def run(self):
        set_startup(self.cfg.get("start_with_windows", False))
        threading.Timer(5.0, self._startup_update).start()
        self.icon.run()


if __name__ == "__main__":
    App().run()
