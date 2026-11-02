"""Экран: яркость мониторов, ночной режим (тёплые цвета) и тёмная тема по расписанию.

Идеи из открытых проектов: Twinkle Tray / Monitorian (яркость внешних мониторов по DDC/CI),
LightBulb / f.lux (цветовая температура через гамму), Windows Auto Dark Mode (тема по времени).
"""

from __future__ import annotations

import ctypes
import logging
import math
import subprocess
import threading
import time

from . import Tool, winapi
from .winapi import IS_WINDOWS

log = logging.getLogger(__name__)

DAY_TEMP = 6500


# --------------------------------------------------------------------------- расписание
def parse_hhmm(text: str, default: int = 0) -> int:
    """'21:30' → минуты от полуночи."""
    try:
        h, m = str(text).strip().split(":")
        h, m = int(h), int(m)
        if 0 <= h < 24 and 0 <= m < 60:
            return h * 60 + m
    except (ValueError, AttributeError):
        pass
    return default


def now_minutes() -> float:
    t = time.localtime()
    return t.tm_hour * 60 + t.tm_min + t.tm_sec / 60


def night_level(now: float, start: int, end: int, transition: float) -> float:
    """0 — день, 1 — ночь. Вечером плавно теплеет после start, утром — остывает к end."""
    night_len = (end - start) % 1440
    if night_len == 0:
        return 0.0
    t = (now - start) % 1440
    if t >= night_len:
        return 0.0
    trans = min(max(transition, 0.0), night_len / 2)
    if trans <= 0:
        return 1.0
    if t < trans:
        return t / trans
    if t > night_len - trans:
        return (night_len - t) / trans
    return 1.0


def in_period(now: float, start: int, end: int) -> bool:
    return (now - start) % 1440 < (end - start) % 1440


def minutes_until(now: float, target: int) -> float:
    return (target - now) % 1440 or 1440


# --------------------------------------------------------------------------- цветовая температура
def kelvin_rgb(kelvin: float) -> tuple[float, float, float]:
    """Цвет «чёрного тела» (приближение Таннера Хелланда), каналы 0..1."""
    t = max(1000.0, min(40000.0, kelvin)) / 100.0
    if t <= 66:
        r = 255.0
        g = 99.4708025861 * math.log(t) - 161.1195681661
        b = 0.0 if t <= 19 else 138.5177312231 * math.log(t - 10) - 305.0447927307
    else:
        r = 329.698727446 * (t - 60) ** -0.1332047592
        g = 288.1221695283 * (t - 60) ** -0.0755148492
        b = 255.0
    return tuple(max(0.0, min(255.0, c)) / 255.0 for c in (r, g, b))


def gamma_for(kelvin: float, dim: float = 1.0) -> tuple[float, float, float]:
    """Множители каналов: 6500 K → (1, 1, 1); dim — программное затемнение 0.3..1."""
    base = kelvin_rgb(DAY_TEMP)
    c = kelvin_rgb(kelvin)
    return tuple(min(1.0, c[i] / base[i]) * dim for i in range(3))


GAMMA_RANGE_CMD = r'add "HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\ICM" /v GdiIcmGammaRange /t REG_DWORD /d 256 /f'


def unlock_gamma_range() -> bool:
    """Снять ограничение Windows на сильную смену гаммы (нужны права администратора)."""
    if not IS_WINDOWS:
        return False
    rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", "reg.exe", GAMMA_RANGE_CMD, None, 0)
    return rc > 32


class NightLight(Tool):
    key = "night"
    title = "Ночной режим"

    def __init__(self, app):
        super().__init__(app)
        self.cfg: dict = {}
        self.manual: bool | None = None   # горячая клавиша: True — ночь, False — день, None — по расписанию
        self._manual_until = 0.0
        self.applied: tuple | None = None
        self.limited = False              # Windows не дала сделать цвета такими тёплыми
        self.unsupported = False          # драйвер вообще не даёт менять гамму (удалённый стол и т.п.)
        self.kelvin = DAY_TEMP
        self._job = None
        self._watcher = None
        self._paused = False
        self._base: dict | None = None   # исходная гамма (с калибровкой монитора), поверх неё и меняем

    def start(self, cfg):
        super().start(cfg)
        self.cfg = dict(cfg)
        self._base = winapi.get_gamma_ramps()
        self._sync_watcher()
        self._tick()

    def update(self, cfg):
        self.cfg = dict(cfg)
        self._sync_watcher()
        self.applied = None
        self._tick()

    def stop(self):
        super().stop()
        if self._job is not None:
            self.app.root.after_cancel(self._job)
            self._job = None
        if self._watcher is not None:
            self._watcher.unsubscribe(self._on_window)
            self._watcher = None
        if self.applied is not None and self.applied != (DAY_TEMP, 1.0):
            winapi.set_gamma(1.0, 1.0, 1.0, self._base)  # вернуть как было (с калибровкой)
        self.applied = None

    def _sync_watcher(self):
        want = bool(self.cfg.get("pause_games", True))
        if want and self._watcher is None:
            self._watcher = self.app.tools.get_watcher()
            self._watcher.subscribe(self._on_window)
        elif not want and self._watcher is not None:
            self._watcher.unsubscribe(self._on_window)
            self._watcher = None
            self._paused = False

    def _on_window(self, info):
        from .gaming import is_game

        paused = is_game(info, self.app.cfg["tools"]["games"])
        if paused != self._paused:
            self._paused = paused
            self.app.ui(self._tick)

    def hotkeys(self, cfg):
        return {"toggle": cfg.get("hotkey", "")}

    def on_hotkey(self, action):
        night = self.target_level() < 0.5
        self.manual = night
        # ручной выбор действует до следующего переключения по расписанию
        now = now_minutes()
        start, end = parse_hhmm(self.cfg.get("start", "21:00"), 1260), parse_hhmm(self.cfg.get("end", "07:00"), 420)
        nxt = minutes_until(now, end if in_period(now, start, end) else start)
        self._manual_until = time.monotonic() + nxt * 60
        self._tick()
        osd = getattr(self.app, "osd", None)
        if osd is not None:
            osd.show("Ночной режим" if night else "Дневные цвета", icon="sun",
                     accent=(255, 170, 80, 255) if night else (255, 230, 150, 255), ms=1200)

    def target_level(self) -> float:
        if self.manual is not None and time.monotonic() < self._manual_until:
            return 1.0 if self.manual else 0.0
        self.manual = None
        if self.cfg.get("mode", "schedule") == "always":
            return 1.0
        start = parse_hhmm(self.cfg.get("start", "21:00"), 1260)
        end = parse_hhmm(self.cfg.get("end", "07:00"), 420)
        return night_level(now_minutes(), start, end, float(self.cfg.get("transition", 30)))

    def _tick(self):
        if self._job is not None:
            self.app.root.after_cancel(self._job)
            self._job = None
        if not self.running:
            return
        level = 0.0 if self._paused else self.target_level()
        night_k = float(self.cfg.get("temperature", 4000))
        dim = 1.0 - (1.0 - float(self.cfg.get("dim", 100)) / 100.0) * level
        kelvin = DAY_TEMP + (night_k - DAY_TEMP) * level
        self.apply(kelvin, dim)
        # во время перехода — чаще, иначе раз в минуту (гамму сбрасывают смена режима экрана и сон)
        delay = 15_000 if 0.0 < level < 1.0 else 60_000
        self._job = self.app.root.after(delay, self._tick)

    def apply(self, kelvin: float, dim: float = 1.0) -> None:
        self.kelvin = round(kelvin)
        target = (round(kelvin / 25) * 25, round(dim, 3))
        if target == (DAY_TEMP, 1.0) and self.applied in (None, target):
            # днём гамму не трогаем вовсе: не мешаем калибровке и другим программам
            self.limited = self.unsupported = False
            self.applied = target
            return
        # повторяем и неизменную гамму: драйвер мог её сбросить
        ok = winapi.set_gamma(*gamma_for(*target), self._base)
        self.limited = self.unsupported = False
        if not ok and (target[0] < DAY_TEMP or target[1] < 1):
            # Windows отклоняет «слишком сильную» гамму — ищем самую тёплую допустимую
            # (отклонённые попытки ничего не меняют на экране, мерцания нет)
            for k in range(1, 11):
                kk = target[0] + (DAY_TEMP - target[0]) * k / 10
                dd = target[1] + (1 - target[1]) * k / 10
                if winapi.set_gamma(*gamma_for(kk, dd), self._base):
                    self.kelvin, self.limited, ok = round(kk), True, True
                    break
        if not ok:  # не принимается даже обычная гамма
            self.unsupported, self.kelvin = True, DAY_TEMP
        if target != self.applied:
            self.applied = target
            self.app.ui(self.app.tool_status_changed, self.key)

    def status(self):
        if not self.running:
            return ""
        if self.unsupported:
            return "Ошибка: видеодрайвер не даёт менять цвета (удалённый рабочий стол или драйвер)"
        if self._paused:
            return "Пауза: игра на экране — цвета обычные"
        text = f"Сейчас {self.kelvin} K" + (" (день)" if self.kelvin >= DAY_TEMP else "")
        if self.manual is not None:
            text += " — вручную до следующего переключения"
        if self.limited:
            text += ". Windows ограничивает тёплые цвета — нажмите «Снять ограничение»"
        return text

    def tray_text(self):
        return "ночной режим" if self.running and self.kelvin < DAY_TEMP - 100 else ""


# --------------------------------------------------------------------------- тема Windows
PERSONALIZE = r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"


def get_theme() -> tuple[bool | None, bool | None]:
    """→ (программы светлые?, система светлая?)."""
    if not IS_WINDOWS:
        return None, None
    import winreg

    out = []
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, PERSONALIZE) as key:
            for name in ("AppsUseLightTheme", "SystemUsesLightTheme"):
                try:
                    out.append(bool(winreg.QueryValueEx(key, name)[0]))
                except OSError:
                    out.append(None)
    except OSError:
        return None, None
    return out[0], out[1]


def set_theme(light: bool, apps: bool = True, system: bool = True) -> bool:
    if not IS_WINDOWS:
        return False
    import winreg

    try:
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, PERSONALIZE, 0, winreg.KEY_SET_VALUE) as key:
            if apps:
                winreg.SetValueEx(key, "AppsUseLightTheme", 0, winreg.REG_DWORD, int(light))
            if system:
                winreg.SetValueEx(key, "SystemUsesLightTheme", 0, winreg.REG_DWORD, int(light))
    except OSError as err:
        log.warning("Тема: %s", err)
        return False
    winapi.broadcast_setting_change("ImmersiveColorSet")
    return True


class ThemeSwitcher(Tool):
    key = "theme"
    title = "Тёмная тема по расписанию"

    def __init__(self, app):
        super().__init__(app)
        self.cfg: dict = {}
        self._job = None
        self._override: tuple[bool, float] | None = None  # (светлая?, до какого времени)
        self.current: bool | None = None
        self._last_wanted: bool | None = None

    def start(self, cfg):
        super().start(cfg)
        self.cfg = dict(cfg)
        self._tick()

    def update(self, cfg):
        self.cfg = dict(cfg)
        self._last_wanted = None  # расписание или галочки поменялись — применить сразу
        self._tick()

    def stop(self):
        super().stop()
        if self._job is not None:
            self.app.root.after_cancel(self._job)
            self._job = None
        self._last_wanted = None

    def hotkeys(self, cfg):
        return {"toggle": cfg.get("hotkey", "")}

    def on_hotkey(self, action):
        apps, _system = get_theme()
        light = not (apps if apps is not None else True)
        now = now_minutes()
        lt, dk = self._times()
        nxt = minutes_until(now, dk if in_period(now, lt, dk) else lt)
        self._override = (light, time.monotonic() + nxt * 60)
        self._last_wanted = None  # клавиша — применить сразу, даже если расписание хочет то же
        self._tick()

    def _times(self):
        return parse_hhmm(self.cfg.get("light", "07:00"), 420), parse_hhmm(self.cfg.get("dark", "19:00"), 1140)

    def wanted(self) -> bool:
        if self._override and time.monotonic() < self._override[1]:
            return self._override[0]
        self._override = None
        lt, dk = self._times()
        return in_period(now_minutes(), lt, dk)

    def _tick(self):
        if self._job is not None:
            self.app.root.after_cancel(self._job)
            self._job = None
        if not self.running:
            return
        light = self.wanted()
        # переключаем только когда наступило время (или нажата клавиша): если вы сами сменили тему
        # в Параметрах Windows, программа не станет спорить до следующего переключения
        if light != self._last_wanted:
            self._last_wanted = light
            apps_on, system_on = bool(self.cfg.get("apps", True)), bool(self.cfg.get("system", True))
            cur_apps, cur_sys = get_theme()
            if (apps_on and cur_apps != light) or (system_on and cur_sys != light):
                if set_theme(light, apps_on, system_on):
                    log.info("Тема Windows: %s", "светлая" if light else "тёмная")
        if light != self.current:
            self.current = light
            self.app.ui(self.app.tool_status_changed, self.key)
        self._job = self.app.root.after(60_000, self._tick)

    def status(self):
        if not self.running or self.current is None:
            return ""
        text = "Сейчас светлая тема" if self.current else "Сейчас тёмная тема"
        if self._override:
            text += " (вручную до следующего переключения)"
        return text


# --------------------------------------------------------------------------- яркость
VCP_BRIGHTNESS = 0x10
SC_MONITORPOWER, WM_SYSCOMMAND = 0xF170, 0x0112

if IS_WINDOWS:
    from ctypes import wintypes

    class PHYSICAL_MONITOR(ctypes.Structure):
        _pack_ = 1
        _fields_ = [("hPhysicalMonitor", wintypes.HANDLE), ("szPhysicalMonitorDescription", wintypes.WCHAR * 128)]


class Display:
    """Один монитор: внешний (DDC/CI) или экран ноутбука (WMI)."""

    def __init__(self, name: str, kind: str, handle=None, maximum: int = 100, current: int | None = None):
        self.name, self.kind, self.handle, self.maximum, self.current = name, kind, handle, maximum, current

    @property
    def percent(self) -> int | None:
        if self.current is None:
            return None
        return round(self.current * 100 / max(1, self.maximum))


class DisplayControl:
    """Только из потока яркости: DDC/CI отвечает медленно (десятки миллисекунд на монитор)."""

    def __init__(self, runner=None):
        self.displays: list[Display] = []
        self._phys: list = []  # (массив PHYSICAL_MONITOR, count) — освободить при пересканировании
        self._run = runner or self._powershell
        self._dxva2 = None

    # --- DDC/CI
    def _dxva(self):
        if self._dxva2 is None and IS_WINDOWS:
            d = ctypes.WinDLL("dxva2", use_last_error=True)
            winapi._proto(d.GetNumberOfPhysicalMonitorsFromHMONITOR, wintypes.BOOL, wintypes.HANDLE,
                          ctypes.POINTER(wintypes.DWORD))
            winapi._proto(d.GetPhysicalMonitorsFromHMONITOR, wintypes.BOOL, wintypes.HANDLE, wintypes.DWORD,
                          ctypes.POINTER(PHYSICAL_MONITOR))
            winapi._proto(d.GetVCPFeatureAndVCPFeatureReply, wintypes.BOOL, wintypes.HANDLE, ctypes.c_ubyte,
                          ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD),
                          ctypes.POINTER(wintypes.DWORD))
            winapi._proto(d.SetVCPFeature, wintypes.BOOL, wintypes.HANDLE, ctypes.c_ubyte, wintypes.DWORD)
            winapi._proto(d.DestroyPhysicalMonitors, wintypes.BOOL, wintypes.DWORD, ctypes.POINTER(PHYSICAL_MONITOR))
            self._dxva2 = d
        return self._dxva2

    def release(self):
        d = self._dxva2
        for arr, n in self._phys:
            try:
                d.DestroyPhysicalMonitors(n, arr)
            except Exception:  # noqa: BLE001
                pass
        self._phys = []
        self.displays = []

    def scan(self) -> list[Display]:
        self.release()
        found: list[Display] = []
        d = self._dxva()
        if d is not None:
            for mon in winapi.monitors():
                n = wintypes.DWORD()
                if not d.GetNumberOfPhysicalMonitorsFromHMONITOR(mon.handle, ctypes.byref(n)) or not n.value:
                    continue
                arr = (PHYSICAL_MONITOR * n.value)()
                if not d.GetPhysicalMonitorsFromHMONITOR(mon.handle, n.value, arr):
                    continue
                self._phys.append((arr, n.value))
                for pm in arr:
                    cur, mx, kind = wintypes.DWORD(), wintypes.DWORD(), wintypes.DWORD()
                    if d.GetVCPFeatureAndVCPFeatureReply(pm.hPhysicalMonitor, VCP_BRIGHTNESS, ctypes.byref(kind),
                                                         ctypes.byref(cur), ctypes.byref(mx)) and mx.value:
                        name = pm.szPhysicalMonitorDescription or "Монитор"
                        found.append(Display(name, "ddc", pm.hPhysicalMonitor, mx.value, cur.value))
        wmi = self._wmi_get()
        if wmi is not None:
            found.insert(0, Display("Экран ноутбука", "wmi", None, 100, wmi))
        self.displays = found
        return found

    def set(self, percent: int) -> int:
        """→ сколько мониторов удалось изменить."""
        percent = max(0, min(100, int(percent)))
        ok = 0
        for disp in self.displays:
            if disp.kind == "ddc":
                value = round(percent * disp.maximum / 100)
                if self._dxva().SetVCPFeature(disp.handle, VCP_BRIGHTNESS, value):
                    disp.current = value
                    ok += 1
            elif disp.kind == "wmi" and self._wmi_set(percent):
                disp.current = percent
                ok += 1
        return ok

    # --- экран ноутбука (WMI через PowerShell: без зависимостей, ~0.5 с на вызов)
    @staticmethod
    def _powershell(script: str) -> str | None:
        if not IS_WINDOWS:
            return None
        try:
            r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                               capture_output=True, text=True, timeout=15, creationflags=0x08000000)
        except (OSError, subprocess.TimeoutExpired):
            return None
        return r.stdout.strip() if r.returncode == 0 else None

    def _wmi_get(self) -> int | None:
        out = self._run("(Get-CimInstance -Namespace root/WMI -ClassName WmiMonitorBrightness "
                        "-ErrorAction Stop | Select-Object -First 1).CurrentBrightness")
        try:
            return int(out.splitlines()[-1]) if out else None
        except (ValueError, IndexError):
            return None

    def _wmi_set(self, percent: int) -> bool:
        out = self._run("Get-CimInstance -Namespace root/WMI -ClassName WmiMonitorBrightnessMethods "
                        f"-ErrorAction Stop | Invoke-CimMethod -MethodName WmiSetBrightness "
                        f"-Arguments @{{Timeout=1; Brightness={int(percent)}}} | Out-Null; 'ok'")
        return out is not None and out.endswith("ok")


def screen_off() -> None:
    """Погасить экраны (как кнопка на мониторе); любое движение мыши включит обратно."""
    if not IS_WINDOWS:
        return
    winapi.wait_modifiers_released()
    time.sleep(0.4)  # иначе отпускание клавиш сразу разбудит экран
    winapi.user32.PostMessageW(winapi.HWND_BROADCAST, WM_SYSCOMMAND, SC_MONITORPOWER, 2)


class Brightness(Tool):
    key = "brightness"
    title = "Яркость мониторов"

    def __init__(self, app, control: DisplayControl | None = None):
        super().__init__(app)
        from ..control import Worker

        self.cfg: dict = {}
        self.ctrl = control or DisplayControl()
        self.worker: Worker | None = None
        self._worker_cls = Worker
        self.level: int | None = None
        self.names: list[str] = []
        self.error = ""
        self.scanned = False

    def start(self, cfg):
        super().start(cfg)
        self.cfg = dict(cfg)
        if self.worker is None:
            self.worker = self._worker_cls("SoundFixer-brightness")
            self.worker.start()
        self.worker.submit(self._w_scan, key="scan")

    def update(self, cfg):
        self.cfg = dict(cfg)

    def stop(self):
        super().stop()
        if self.worker is not None:
            w = self.worker
            w.submit(self.ctrl.release)
            w.submit(w.stop)
            self.worker = None

    def _w_scan(self):
        try:
            disps = self.ctrl.scan()
        except Exception as err:  # noqa: BLE001
            log.exception("Яркость: поиск мониторов")
            disps, self.error = [], str(err)
        self.names = [d.name for d in disps]
        self.scanned = True
        levels = [d.percent for d in disps if d.percent is not None]
        self.level = levels[0] if levels else None
        self.app.ui(self.app.tool_status_changed, self.key)

    def set_level(self, percent: int, osd: bool = False):
        """GUI-поток: применяется в фоне, частые изменения схлопываются."""
        percent = max(0, min(100, int(percent)))
        self.level = percent
        if osd and getattr(self.app, "osd", None) is not None:
            self.app.osd.show(f"Яркость {percent}%", value=percent / 100, icon="sun", ms=1200)
        if self.worker is not None:
            self.worker.submit(lambda: self._w_set(percent), delay=0.12, key="set")

    def _w_set(self, percent: int):
        if not self.ctrl.displays:
            self.ctrl.scan()
        ok = self.ctrl.set(percent)
        if not ok and self.ctrl.displays:  # монитор переподключили — дескрипторы устарели
            self.ctrl.scan()
            ok = self.ctrl.set(percent)
        self.error = "" if ok else "мониторы не отвечают (включите DDC/CI в меню монитора)"
        self.app.ui(self.app.tool_status_changed, self.key)

    def hotkeys(self, cfg):
        return {"up": cfg.get("up", ""), "down": cfg.get("down", ""), "off": cfg.get("off", "")}

    def on_hotkey(self, action):
        if action == "off":
            threading.Thread(target=screen_off, daemon=True).start()
            return
        step = int(self.cfg.get("step", 10))
        cur = self.level if self.level is not None else 50
        self.set_level(cur + (step if action == "up" else -step), osd=True)

    def status(self):
        if not self.running:
            return ""
        if self.error:
            return f"Ошибка: {self.error}"
        if not self.names:
            if self.scanned:
                return ("Ошибка: мониторы не ответили. Включите DDC/CI в меню монитора; через HDMI-переходники "
                        "и док-станции яркость часто не меняется")
            return "Ищу мониторы…"
        lvl = f" — {self.level}%" if self.level is not None else ""
        return "Мониторы: " + ", ".join(self.names) + lvl
