"""Система: перерывы для глаз, схемы питания, «чем занят файл», таймер сна.

Идеи из открытых проектов: Stretchly / EyeLeo (перерывы 20-20-20), PowerToys File Locksmith
(Restart Manager), PowerPlanSwitcher (схема питания в играх и от батареи).
"""

from __future__ import annotations

import ctypes
import logging
import os
import threading
import time
import uuid

from . import Tool, winapi
from .winapi import IS_WINDOWS

log = logging.getLogger(__name__)

if IS_WINDOWS:
    from ctypes import wintypes


# =========================================================================== перерывы
class BreakReminder(Tool):
    """Каждые N минут работы — короткий перерыв для глаз, раз в час — длинный.

    Считается только время, когда вы за компьютером: отошли на 5 минут — это уже перерыв.
    Короткий перерыв — плашка поверх всех окон, она не забирает фокус и не мешает печатать.
    """

    key = "breaks"
    title = "Перерывы для глаз"
    TICK = 10.0

    def __init__(self, app, idle=None, foreground=None):
        super().__init__(app)
        self.cfg: dict = {}
        self.active = 0.0        # минут работы с последнего перерыва
        self.since_long = 0.0    # минут работы с последнего длинного перерыва
        self._job = None
        self._overlay = None
        self._break_end = 0.0
        self._break_job = None
        self._long_win = None
        self._idle = idle or winapi.idle_seconds
        self._foreground = foreground or (lambda: winapi.window_info(winapi.foreground_window()))
        self.taken = 0

    def start(self, cfg):
        super().start(cfg)
        self.cfg = dict(cfg)
        self.active = self.since_long = 0.0
        self._schedule()

    def update(self, cfg):
        self.cfg = dict(cfg)

    def stop(self):
        super().stop()
        for job in (self._job, self._break_job):
            if job is not None:
                self.app.root.after_cancel(job)
        self._job = self._break_job = None
        self._end_break(count=False)
        if self._overlay is not None:
            self._overlay.destroy()
            self._overlay = None

    def _schedule(self):
        self._job = self.app.root.after(int(self.TICK * 1000), self.tick)

    def tick(self):
        self._job = None
        if not self.running:
            return
        if not self.in_break:
            self.step(self._idle())
        self._schedule()

    @property
    def in_break(self) -> bool:
        return self._break_job is not None or self._long_win is not None

    def step(self, idle: float) -> str | None:
        """Один такт (раз в 10 с): учёт времени и решение о перерыве. → тип начатого перерыва."""
        c = self.cfg
        if idle >= float(c.get("idle_reset", 5)) * 60:
            if self.active or self.since_long:
                log.info("Перерыв засчитан: вас не было %.0f мин", idle / 60)
            self.active = self.since_long = 0.0
            return None
        if idle < 60:  # за компьютером
            self.active += self.TICK / 60
            self.since_long += self.TICK / 60
        if self.active + 1e-6 < float(c.get("every", 20)):  # сумма шагов по 1/6 мин не точна
            return None
        if c.get("skip_fullscreen", True):
            info = self._foreground()
            if info is not None and info.fullscreen:
                self.active -= 2.0  # игра или фильм на весь экран — напомним через пару минут
                return None
        long = bool(c.get("long", True)) and self.since_long + 1e-6 >= float(c.get("long_every", 60))
        self.begin_break(long)
        return "long" if long else "short"

    # ---------------------------------------------------------------- перерывы
    def begin_break(self, long: bool):
        if long:
            self._show_long()
        else:
            self._break_end = time.monotonic() + float(self.cfg.get("seconds", 20))
            self._short_frame()

    def _short_frame(self):
        left = self._break_end - time.monotonic()
        if left <= 0 or not self.running:
            self._end_break()
            return
        from .overlay import LayeredWindow, cursor_monitor_work, render_badge

        total = float(self.cfg.get("seconds", 20))
        try:
            img = render_badge(f"Перерыв для глаз · {left:.0f} с",
                               sub="Посмотрите на что-нибудь вдали — дальше 6 метров", value=left / total,
                               accent=(46, 196, 132, 255))
            if self._overlay is None:
                self._overlay = LayeredWindow()
            l, t, r, _b = cursor_monitor_work()
            self._overlay.show(img, l + (r - l - img.width) // 2, t + 24)
        except Exception:  # noqa: BLE001
            log.exception("Перерыв")
        self._break_job = self.app.root.after(1000, self._short_frame)

    def _end_break(self, count: bool = True):
        if self._break_job is not None:
            self.app.root.after_cancel(self._break_job)
            self._break_job = None
        if self._overlay is not None:
            self._overlay.hide()
        if self._long_win is not None:
            try:
                self._long_win.destroy()
            except Exception:  # noqa: BLE001
                pass
            self._long_win = None
        if count:
            self.taken += 1
            self.active = 0.0
            self.app.tool_status_changed(self.key)

    def _show_long(self):
        import tkinter as tk

        from .popup import ACCENT, FG, MUTED, PANEL, _button

        minutes = float(self.cfg.get("long_minutes", 5))
        end = time.monotonic() + minutes * 60
        win = self._long_win = tk.Toplevel(self.app.root)
        win.title("Перерыв")
        win.configure(bg=PANEL)
        win.attributes("-topmost", True)
        win.resizable(False, False)
        tk.Label(win, text="Время размяться", bg=PANEL, fg=ACCENT, font=("Segoe UI Semibold", 16)).pack(
            padx=30, pady=(22, 4))
        tk.Label(win, text="Встаньте, пройдитесь, посмотрите в окно.\nГлаза и спина скажут спасибо.",
                 bg=PANEL, fg=FG, font=("Segoe UI", 11), justify="center").pack(padx=30)
        timer = tk.Label(win, bg=PANEL, fg=MUTED, font=("Segoe UI", 22))
        timer.pack(pady=10)
        row = tk.Frame(win, bg=PANEL)
        row.pack(pady=(0, 18))

        def skip():
            self.since_long = 0.0
            self._end_break()

        def later():
            self._long_win = None
            win.destroy()
            self.active = max(0.0, float(self.cfg.get("every", 20)) - 10)
            self.since_long = max(0.0, float(self.cfg.get("long_every", 60)) - 10)

        _button(row, "Пропустить", skip).pack(side="left", padx=4)
        _button(row, "Через 10 минут", later).pack(side="left", padx=4)

        def tick():
            if self._long_win is not win:
                return
            left = end - time.monotonic()
            if left <= 0:
                self.since_long = 0.0
                self._end_break()
                self.app.notify("Перерыв окончен", "Можно продолжать")
                return
            timer.configure(text=f"{int(left) // 60}:{int(left) % 60:02d}")
            win.after(1000, tick)

        win.protocol("WM_DELETE_WINDOW", skip)
        win.configure(bg=PANEL)
        win.update_idletasks()
        x, y, w, h = winapi.virtual_screen()
        win.geometry(f"+{x + (w - win.winfo_reqwidth()) // 2}+{y + (h - win.winfo_reqheight()) // 3}")
        tick()

    def status(self):
        if not self.running:
            return ""
        every = float(self.cfg.get("every", 20))
        left = max(0, round(every - self.active))
        text = f"До перерыва ~{left} мин работы"
        if self.taken:
            text += f" · перерывов сегодня: {self.taken}"
        return text


# =========================================================================== схемы питания
ACCESS_SCHEME = 16
ERROR_NO_MORE_ITEMS = 259
# «Режим питания» Windows 10/11 (ползунок в Параметрах): работает и там, где схема одна
POWER_MODES = {
    "961cc777-2547-4f9d-8174-7d86181b8a7a": "Режим: максимальная экономия",
    "00000000-0000-0000-0000-000000000000": "Режим: сбалансированный",
    "ded574b5-45a0-4f42-8737-46345c09c238": "Режим: максимальная производительность",
}

if IS_WINDOWS:
    class GUID(ctypes.Structure):
        _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD),
                    ("Data4", ctypes.c_ubyte * 8)]

        @classmethod
        def parse(cls, text: str) -> "GUID":
            return cls.from_buffer_copy(uuid.UUID(text).bytes_le)

        def __str__(self) -> str:
            return str(uuid.UUID(bytes_le=bytes(self)))

    class SYSTEM_POWER_STATUS(ctypes.Structure):
        _fields_ = [("ACLineStatus", ctypes.c_ubyte), ("BatteryFlag", ctypes.c_ubyte),
                    ("BatteryLifePercent", ctypes.c_ubyte), ("SystemStatusFlag", ctypes.c_ubyte),
                    ("BatteryLifeTime", wintypes.DWORD), ("BatteryFullLifeTime", wintypes.DWORD)]

_powrprof = None


def _pp():
    global _powrprof
    if _powrprof is None and IS_WINDOWS:
        p = ctypes.WinDLL("powrprof")
        winapi._proto(p.PowerEnumerate, wintypes.DWORD, wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
                      wintypes.DWORD, wintypes.ULONG, ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD))
        winapi._proto(p.PowerReadFriendlyName, wintypes.DWORD, wintypes.HANDLE, ctypes.POINTER(GUID),
                      ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD))
        winapi._proto(p.PowerGetActiveScheme, wintypes.DWORD, wintypes.HANDLE, ctypes.POINTER(ctypes.POINTER(GUID)))
        winapi._proto(p.PowerSetActiveScheme, wintypes.DWORD, wintypes.HANDLE, ctypes.POINTER(GUID))
        # недокументированные функции «режима питания» (Windows 10 1709+): Set принимает GUID по значению
        fn = getattr(p, "PowerGetEffectiveOverlayScheme", None)
        if fn is not None:
            winapi._proto(fn, wintypes.DWORD, ctypes.POINTER(GUID))
        fn = getattr(p, "PowerSetActiveOverlayScheme", None)
        if fn is not None:
            winapi._proto(fn, wintypes.DWORD, GUID)
        _powrprof = p
    return _powrprof


def power_plans() -> list[tuple[str, str]]:
    """Схемы питания → [("plan:<guid>", название)] и режимы питания Windows 10/11."""
    p = _pp()
    if p is None:
        return []
    out = []
    i = 0
    while True:
        g = GUID()
        size = wintypes.DWORD(ctypes.sizeof(g))
        rc = p.PowerEnumerate(None, None, None, ACCESS_SCHEME, i, ctypes.byref(g), ctypes.byref(size))
        if rc != 0:
            break
        need = wintypes.DWORD(0)
        p.PowerReadFriendlyName(None, ctypes.byref(g), None, None, None, ctypes.byref(need))
        buf = ctypes.create_string_buffer(max(2, need.value))
        name = ""
        if p.PowerReadFriendlyName(None, ctypes.byref(g), None, None, buf, ctypes.byref(need)) == 0:
            name = buf.raw[:need.value].decode("utf-16-le", "ignore").rstrip("\0")
        out.append((f"plan:{g}", name or str(g)))
        i += 1
    if getattr(p, "PowerSetActiveOverlayScheme", None) is not None:
        out += [(f"mode:{g}", n) for g, n in POWER_MODES.items()]
    return out


def active_power() -> tuple[str | None, str | None]:
    """→ (активная схема "plan:<guid>", активный режим "mode:<guid>")."""
    p = _pp()
    if p is None:
        return None, None
    plan = mode = None
    ptr = ctypes.POINTER(GUID)()
    if p.PowerGetActiveScheme(None, ctypes.byref(ptr)) == 0 and ptr:
        plan = f"plan:{ptr.contents}"
        winapi.kernel32.LocalFree(ptr)
    fn = getattr(p, "PowerGetEffectiveOverlayScheme", None)
    if fn is not None:
        g = GUID()
        if fn(ctypes.byref(g)) == 0:
            mode = f"mode:{g}"
    return plan, mode


def set_power(option: str) -> bool:
    p = _pp()
    if p is None or ":" not in option:
        return False
    kind, guid = option.split(":", 1)
    try:
        g = GUID.parse(guid)
    except ValueError:
        return False
    if kind == "plan":
        return p.PowerSetActiveScheme(None, ctypes.byref(g)) == 0
    fn = getattr(p, "PowerSetActiveOverlayScheme", None)
    return fn is not None and fn(g) == 0


def on_battery() -> bool:
    if not IS_WINDOWS:
        return False
    st = SYSTEM_POWER_STATUS()
    if not winapi.kernel32.GetSystemPowerStatus(ctypes.byref(st)):
        return False
    return st.ACLineStatus == 0 and st.BatteryFlag != 128


class PowerPlans(Tool):
    """Своя схема питания в играх и от батареи; при выходе из игры — как было."""

    key = "power"
    title = "Схемы питания"

    def __init__(self, app):
        super().__init__(app)
        self.cfg: dict = {}
        self._watcher = None
        self.in_game = False
        self.battery = False
        self.base: dict[str, str | None] = {}  # что было до наших переключений: {"plan": ..., "mode": ...}
        self.applied: str | None = None
        self._job = None

    def start(self, cfg):
        super().start(cfg)
        self.cfg = dict(cfg)
        plan, mode = active_power()
        self.base = {"plan": plan, "mode": mode}
        self._sync()

    def update(self, cfg):
        self.cfg = dict(cfg)
        self._sync()

    def stop(self):
        super().stop()
        if self._job is not None:
            self.app.root.after_cancel(self._job)
            self._job = None
        if self._watcher is not None:
            self._watcher.unsubscribe(self._on_window)
            self._watcher = None
        self._restore()

    def _sync(self):
        if self.cfg.get("game") and self._watcher is None:
            self._watcher = self.app.tools.get_watcher()
            self._watcher.subscribe(self._on_window)
        elif not self.cfg.get("game") and self._watcher is not None:
            self._watcher.unsubscribe(self._on_window)
            self._watcher = None
            self.in_game = False
        if self._job is not None:
            self.app.root.after_cancel(self._job)
            self._job = None
        self._poll()

    def _poll(self):
        self.battery = on_battery()
        self.decide()
        if self.running and self.cfg.get("battery"):
            self._job = self.app.root.after(30_000, self._poll)

    def _on_window(self, info):
        from .gaming import is_game

        game = is_game(info, self.app.cfg["tools"]["games"])
        if game != self.in_game:
            self.in_game = game
            self.app.ui(self.decide)

    def wanted(self) -> str | None:
        if self.in_game and self.cfg.get("game"):
            return self.cfg["game"]
        if self.battery and self.cfg.get("battery"):
            return self.cfg["battery"]
        return None

    def decide(self):
        if not self.running:
            return
        want = self.wanted()
        if want == self.applied:
            return
        if want is None:
            self._restore()
        else:
            if self.applied is None:  # запоминаем выбор пользователя перед первым переключением
                plan, mode = active_power()
                self.base = {"plan": plan, "mode": mode}
            if set_power(want):
                log.info("Питание: %s", want)
            self.applied = want
        self.app.tool_status_changed(self.key)

    def _restore(self):
        if self.applied is None:
            return
        kind = self.applied.split(":", 1)[0]
        prev = self.base.get(kind)
        if prev:
            set_power(prev)
        self.applied = None

    def user_choice(self, option: str):
        """Пользователь выбрал схему вручную (трей): она становится основной."""
        set_power(option)
        self.base[option.split(":", 1)[0]] = option
        self.applied = None
        self.decide()

    def status(self):
        if not self.running:
            return ""
        names = dict(power_plans())
        plan, mode = active_power()
        cur = names.get(plan or "", "?")
        if mode and mode in names and not mode.endswith("00000000-0000-0000-0000-000000000000"):
            cur += f", {names[mode].lower()}"
        why = " (игра)" if self.applied and self.in_game else " (от батареи)" if self.applied else ""
        return f"Сейчас: {cur}{why}"


# =========================================================================== чем занят файл
ERROR_MORE_DATA = 234
CCH_RM_SESSION_KEY = 32
MAX_FILES = 3000

if IS_WINDOWS:
    class RM_UNIQUE_PROCESS(ctypes.Structure):
        _fields_ = [("dwProcessId", wintypes.DWORD), ("ProcessStartTime", wintypes.FILETIME)]

    class RM_PROCESS_INFO(ctypes.Structure):
        _fields_ = [("Process", RM_UNIQUE_PROCESS), ("strAppName", wintypes.WCHAR * 256),
                    ("strServiceShortName", wintypes.WCHAR * 64), ("ApplicationType", ctypes.c_int),
                    ("AppStatus", wintypes.ULONG), ("TSSessionId", wintypes.DWORD), ("bRestartable", wintypes.BOOL)]


def list_files(paths: list[str], limit: int = MAX_FILES) -> tuple[list[str], bool]:
    """Файлы для проверки (папки — рекурсивно). → (файлы, обрезан ли список)."""
    out: list[str] = []
    for p in paths:
        if os.path.isdir(p):
            for root, _dirs, files in os.walk(p):
                for f in files:
                    out.append(os.path.join(root, f))
                    if len(out) >= limit:
                        return out, True
        elif p:
            out.append(p)
    return out[:limit], len(out) > limit


def who_locks(paths: list[str]) -> list[tuple[int, str, str]]:
    """Процессы, которые держат файлы открытыми → [(pid, название, путь к exe)]."""
    if not IS_WINDOWS:
        return []
    rm = ctypes.WinDLL("rstrtmgr")
    winapi._proto(rm.RmStartSession, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), wintypes.DWORD, wintypes.LPWSTR)
    winapi._proto(rm.RmRegisterResources, wintypes.DWORD, wintypes.DWORD, wintypes.UINT,
                  ctypes.POINTER(wintypes.LPCWSTR), wintypes.UINT, ctypes.c_void_p, wintypes.UINT, ctypes.c_void_p)
    winapi._proto(rm.RmGetList, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(wintypes.UINT),
                  ctypes.POINTER(wintypes.UINT), ctypes.POINTER(RM_PROCESS_INFO), ctypes.POINTER(wintypes.DWORD))
    winapi._proto(rm.RmEndSession, wintypes.DWORD, wintypes.DWORD)
    files, _cut = list_files(paths)
    if not files:
        return []
    session = wintypes.DWORD()
    key = ctypes.create_unicode_buffer(CCH_RM_SESSION_KEY + 1)
    rc = rm.RmStartSession(ctypes.byref(session), 0, key)
    if rc != 0:
        raise OSError(rc, "RmStartSession")
    try:
        for i in range(0, len(files), 500):
            chunk = files[i:i + 500]
            arr = (wintypes.LPCWSTR * len(chunk))(*chunk)
            rc = rm.RmRegisterResources(session, len(chunk), arr, 0, None, 0, None)
            if rc != 0:
                raise OSError(rc, "RmRegisterResources")
        needed, count, reasons = wintypes.UINT(0), wintypes.UINT(0), wintypes.DWORD(0)
        rc = rm.RmGetList(session, ctypes.byref(needed), ctypes.byref(count), None, ctypes.byref(reasons))
        infos = []
        if rc == ERROR_MORE_DATA and needed.value:
            arr = (RM_PROCESS_INFO * needed.value)()
            count = wintypes.UINT(needed.value)
            rc = rm.RmGetList(session, ctypes.byref(needed), ctypes.byref(count), arr, ctypes.byref(reasons))
            infos = arr[:count.value]
        if rc not in (0, ERROR_MORE_DATA):
            raise OSError(rc, "RmGetList")
    finally:
        rm.RmEndSession(session)
    own = os.getpid()
    seen, out = set(), []
    for pi in infos:
        pid = pi.Process.dwProcessId
        if pid in seen or pid == own:
            continue
        seen.add(pid)
        out.append((pid, pi.strAppName or pi.strServiceShortName, winapi.process_path(pid)))
    return out


def kill_process(pid: int) -> bool:
    try:
        import psutil

        proc = psutil.Process(pid)
        proc.terminate()
        proc.wait(3)
        return True
    except Exception as err:  # noqa: BLE001
        log.warning("Не удалось завершить %s: %s", pid, err)
        return False


MENU_KEYS = (r"Software\Classes\*\shell\SoundFixer.Locksmith", r"Software\Classes\Directory\shell\SoundFixer.Locksmith")
LOCKSMITH_ARG = "--locksmith"


def context_menu_enabled() -> bool:
    if not IS_WINDOWS:
        return False
    import winreg

    try:
        winreg.OpenKey(winreg.HKEY_CURRENT_USER, MENU_KEYS[0] + r"\command").Close()
        return True
    except OSError:
        return False


def set_context_menu(enabled: bool) -> None:
    """Пункт «Чем занят этот файл?» в меню проводника (только для текущего пользователя)."""
    if not IS_WINDOWS:
        return
    import winreg

    from ..autostart import base_command

    for path in MENU_KEYS:
        if enabled:
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, path) as key:
                winreg.SetValueEx(key, "MUIVerb", 0, winreg.REG_SZ, "Чем занят? (SoundFixer)")
                winreg.SetValueEx(key, "Icon", 0, winreg.REG_SZ, "shell32.dll,47")
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, path + r"\command") as key:
                winreg.SetValueEx(key, "", 0, winreg.REG_SZ, f'{base_command()} {LOCKSMITH_ARG} "%1"')
        else:
            for sub in (path + r"\command", path):
                try:
                    winreg.DeleteKey(winreg.HKEY_CURRENT_USER, sub)
                except OSError:
                    pass


class LocksmithWindow:
    """Окно со списком процессов, которые держат файл/папку, и кнопкой «Завершить»."""

    def __init__(self, root, paths: list[str], on_close=None):
        import tkinter as tk

        from .popup import ACCENT, FG, FIELD, MUTED, PANEL, _button

        self.paths = paths
        self.items: list[tuple[int, str, str]] = []
        self.on_close = on_close
        win = self.win = tk.Toplevel(root) if root is not None else tk.Tk()
        win.title("Чем занят файл — SoundFixer")
        win.configure(bg=PANEL)
        win.geometry("620x380")
        win.minsize(460, 260)
        shown = paths[0] if len(paths) == 1 else f"{len(paths)} объектов"
        tk.Label(win, text=shown, bg=PANEL, fg=ACCENT, font=("Segoe UI Semibold", 10), anchor="w",
                 wraplength=590, justify="left").pack(fill="x", padx=12, pady=(10, 2))
        self.info = tk.Label(win, text="Ищу…", bg=PANEL, fg=MUTED, font=("Segoe UI", 9), anchor="w")
        self.info.pack(fill="x", padx=12)
        self.lb = tk.Listbox(win, bg=FIELD, fg=FG, selectbackground=ACCENT, relief="flat", highlightthickness=0,
                             activestyle="none", selectmode="extended", font=("Segoe UI", 10))
        self.lb.pack(fill="both", expand=True, padx=12, pady=8)
        row = tk.Frame(win, bg=PANEL)
        row.pack(fill="x", padx=12, pady=(0, 10))
        _button(row, "Завершить выбранные", self.kill_selected, accent=True).pack(side="left")
        _button(row, "Обновить", self.refresh).pack(side="left", padx=6)
        _button(row, "Закрыть", self.close).pack(side="right")
        win.protocol("WM_DELETE_WINDOW", self.close)
        self.refresh()

    def refresh(self):
        self.info.configure(text="Ищу…")
        threading.Thread(target=self._scan, daemon=True).start()

    def _scan(self):
        try:
            items, err = who_locks(self.paths), ""
        except Exception as e:  # noqa: BLE001
            items, err = [], str(e)
        try:
            self.win.after(0, self._show, items, err)
        except RuntimeError:
            pass

    def _show(self, items, err):
        self.items = items
        self.lb.delete(0, "end")
        for pid, name, path in items:
            self.lb.insert("end", f"{name}   (PID {pid})   {path}")
        if err:
            self.info.configure(text=f"Ошибка: {err}")
        elif items:
            self.info.configure(text=f"Заняли: {len(items)}. Сохраните работу в этих программах, прежде чем "
                                     "завершать их.")
        else:
            self.info.configure(text="Никто не держит — файл свободен.")

    def kill_selected(self):
        from tkinter import messagebox

        sel = [self.items[i] for i in self.lb.curselection()]
        if not sel:
            return
        names = ", ".join(n for _p, n, _x in sel)
        if not messagebox.askyesno("Завершить процессы", f"Завершить: {names}?\nНесохранённые данные пропадут.",
                                   parent=self.win):
            return
        for pid, _n, _x in sel:
            kill_process(pid)
        self.refresh()

    def close(self):
        try:
            self.win.destroy()
        except Exception:  # noqa: BLE001
            pass
        if self.on_close:
            self.on_close()


def run_locksmith_standalone(paths: list[str]) -> int:
    """Запуск из меню проводника: отдельное маленькое окно, без трея и звука."""
    w = LocksmithWindow(None, paths)
    w.win.mainloop()
    return 0


# =========================================================================== таймер сна
class SleepTimer:
    """Через N минут: плавно убрать громкость и выполнить действие (сон, выключение, тишина)."""

    def __init__(self, app, minutes: float, action: str, fade: bool = True, volume=None):
        self.app = app
        self.minutes = float(minutes)
        self.action = action
        self.fade = fade
        self.state = "running"
        self.error = ""
        self._cancel = threading.Event()
        self._end = time.monotonic() + self.minutes * 60
        self._volume = volume  # (get, set) для тестов
        self._v0: float | None = None
        self._thread = None

    def start(self):
        self._thread = threading.Thread(target=self._run, name="SoundFixer-sleeptimer", daemon=True)
        self._thread.start()

    def cancel(self):
        if self.state in ("running", "fading"):
            self.state = "cancelled"
        self._cancel.set()

    @property
    def left(self) -> float:
        return max(0.0, self._end - time.monotonic())

    def _vol(self):
        if self._volume is not None:
            return self._volume
        app = self.app

        def dev():
            from ..winaudio import RENDER

            return app.audio.default_id(RENDER) if app.audio else None

        def get():
            return app.worker.call(lambda: app.audio.get_volume(dev()) if app.audio and dev() else None, timeout=5)

        def put(v):
            app.worker.submit(lambda: app.audio and dev() and app.audio.set_volume(dev(), v), key="sleep-vol")

        return get, put

    def _run(self):
        from .network import do_power_action

        try:
            fade_secs = min(300.0, self.minutes * 60 / 3) if self.fade else 0.0
            if self._cancel.wait(max(0.0, self.left - fade_secs)):
                return
            get, put = self._vol()
            if fade_secs:
                self.state = "fading"
                self._v0 = get()
                while self.left > 0:
                    if self._cancel.wait(min(2.0, self.left)):
                        if self._v0 is not None:
                            put(self._v0)
                        return
                    if self._v0 is not None:
                        put(self._v0 * self.left / fade_secs)
            elif self._cancel.wait(self.left):
                return
            self.state = "done"
            self.app.ui(self.app.tool_status_changed, "sleeptimer")
            if self.action == "mute":
                return  # громкость осталась на нуле: тишина до утра
            if self.action == "shutdown" and self._v0 is not None:
                put(self._v0)
                time.sleep(0.3)
            do_power_action(self.action)
            if self.action != "shutdown" and self._v0 is not None:
                put(self._v0)  # проснулись — звук как был
        except Exception as err:  # noqa: BLE001
            log.exception("Таймер сна")
            self.state, self.error = "error", str(err)
        finally:
            self.app.ui(self.app.tool_status_changed, "sleeptimer")

    def status(self) -> str:
        names = {"sleep": "Сон", "hibernate": "Гибернация", "shutdown": "Выключение", "mute": "Тишина"}
        if self.state == "running":
            return f"{names.get(self.action, self.action)} через {self.left / 60:.0f} мин"
        if self.state == "fading":
            return f"Убавляю громкость… {names.get(self.action, self.action).lower()} через {self.left / 60:.1f} мин"
        if self.state == "done":
            return "Готово"
        if self.state == "error":
            return f"Ошибка: {self.error}"
        return "Отменено"
