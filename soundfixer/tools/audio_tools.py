"""Звук: громкость отдельных программ, медиаклавиши, индикатор микрофона, автопрофили.

Идеи из открытых проектов: EarTrumpet (громкость программ), MicMute (значок и
предупреждение «вы говорите в выключенный микрофон»), SoundSwitch (переключение
устройств горячей клавишей и по программам).
"""

from __future__ import annotations

import logging
import ntpath
import threading
import time

from . import Tool, winapi

log = logging.getLogger(__name__)

VK_MEDIA = {"play": 0xB3, "next": 0xB0, "prev": 0xB1, "stop": 0xB2}
SYSTEM_SOUNDS = "Системные звуки"


# =========================================================================== громкость программ
class AppSession:
    """Снимок аудиосессии (для окна микшера)."""

    def __init__(self, key: str, name: str, exe: str, pid: int, volume: float, muted: bool, active: bool):
        self.key, self.name, self.exe, self.pid = key, name, exe, pid
        self.volume, self.muted, self.active = volume, muted, active

    def __repr__(self):
        return f"AppSession({self.name!r}, {self.volume:.2f}, muted={self.muted})"


def _sessions_raw():
    """Только в потоке управления (COM)."""
    from pycaw.pycaw import AudioUtilities

    return AudioUtilities.GetAllSessions()


def nice_name(exe: str, title: str = "") -> str:
    known = {"chrome.exe": "Chrome", "msedge.exe": "Edge", "firefox.exe": "Firefox", "opera.exe": "Opera",
             "browser.exe": "Яндекс Браузер", "discord.exe": "Discord", "spotify.exe": "Spotify",
             "telegram.exe": "Telegram", "steam.exe": "Steam", "vlc.exe": "VLC", "obs64.exe": "OBS",
             "teams.exe": "Teams", "ms-teams.exe": "Teams", "zoom.exe": "Zoom", "skype.exe": "Skype",
             "yandexmusic.exe": "Яндекс Музыка", "aimp.exe": "AIMP", "foobar2000.exe": "foobar2000"}
    if exe in known:
        return known[exe]
    if title:
        return title
    return exe[:-4].capitalize() if exe.endswith(".exe") else exe or "?"


def list_sessions(raw=None) -> list[AppSession]:
    """Сессии, объединённые по программе (у браузеров звук идёт из нескольких процессов)."""
    out: dict[str, AppSession] = {}
    for s in (raw if raw is not None else _sessions_raw()):
        try:
            pid = int(s.ProcessId)
            if pid == 0:
                exe, name = "", SYSTEM_SOUNDS
            else:
                exe = ntpath.basename(winapi.process_path(pid) or (s.Process.name() if s.Process else "")).lower()
                if not exe:
                    continue
                name = nice_name(exe, s.DisplayName if not str(s.DisplayName).startswith("@") else "")
            vol = s.SimpleAudioVolume
            level, muted = float(vol.GetMasterVolume()), bool(vol.GetMute())
            active = int(s.State) == 1  # AudioSessionStateActive
        except Exception as err:  # noqa: BLE001 — процесс мог завершиться
            log.debug("Сессия: %s", err)
            continue
        key = exe or "system"
        cur = out.get(key)
        if cur is None:
            out[key] = AppSession(key, name, exe, pid, level, muted, active)
        else:
            cur.active = cur.active or active
            if active:
                cur.volume, cur.muted = level, muted
    return sorted(out.values(), key=lambda a: (a.key == "system", not a.active, a.name.lower()))


def set_app_volume(key: str, volume: float | None = None, mute: bool | None = None, delta: float | None = None,
                   raw=None) -> float | None:
    """Громкость всех сессий программы (key — имя exe или "system"). → новая громкость.

    Шаг (delta) считается от громкости играющей сессии: у браузера их несколько, и тихая
    фоновая не должна сбивать уровень.
    """
    matched = []
    for s in (raw if raw is not None else _sessions_raw()):
        try:
            pid = int(s.ProcessId)
            exe = "system" if pid == 0 else ntpath.basename(winapi.process_path(pid) or "").lower()
            if exe == key:
                matched.append((s, s.SimpleAudioVolume, int(s.State) == 1))
        except Exception as err:  # noqa: BLE001 — процесс мог завершиться
            log.debug("Сессия %s: %s", key, err)
    if not matched:
        return None
    playing = [m for m in matched if m[2]] or matched
    current = max(float(v.GetMasterVolume()) for _s, v, _a in playing)
    result = current
    if delta is not None:
        result = max(0.0, min(1.0, current + delta))
    if volume is not None:
        result = max(0.0, min(1.0, volume))
    for _s, vol, _a in matched:
        try:
            if delta is not None or volume is not None:
                vol.SetMasterVolume(result, None)
                if delta is not None and result > 0 and vol.GetMute():
                    vol.SetMute(False, None)
            if mute is not None:
                vol.SetMute(bool(mute), None)
        except Exception as err:  # noqa: BLE001
            log.debug("Громкость %s: %s", key, err)
    return result


class AppVolume(Tool):
    key = "apps"
    title = "Громкость программ"

    def __init__(self, app):
        super().__init__(app)
        self.cfg: dict = {}
        self.window = None
        self.last = ""

    def start(self, cfg):
        super().start(cfg)
        self.cfg = dict(cfg)

    def update(self, cfg):
        self.cfg = dict(cfg)

    def stop(self):
        super().stop()
        if self.window is not None:
            self.window.close()

    def hotkeys(self, cfg):
        return {k: cfg.get(k, "") for k in ("up", "down", "mute", "mixer", "play", "next", "prev")}

    def on_hotkey(self, action):
        if action in ("play", "next", "prev"):
            threading.Thread(target=self._media, args=(action,), daemon=True).start()
        elif action == "mixer":
            self.open_mixer()
        else:
            info = winapi.window_info(winapi.foreground_window())
            if info is None or not info.exe:
                return
            step = int(self.cfg.get("step", 10)) / 100
            exe = info.exe

            def job():
                try:
                    if action == "mute":
                        level = set_app_volume(exe, mute=not self._is_muted(exe))
                    else:
                        level = set_app_volume(exe, delta=step if action == "up" else -step)
                except Exception as err:  # noqa: BLE001 — служба звука недоступна
                    log.warning("Громкость программы: %s", err)
                    level = None
                self._after(exe, info.title, action, level)

            self.app.worker.submit(job)

    @staticmethod
    def _is_muted(exe):
        return any(a.muted for a in list_sessions() if a.key == exe)

    def _after(self, exe, title, action, level):
        name = nice_name(exe)
        if level is None:
            self.app.ui(self._osd, f"{name}: сейчас не выводит звук", None, False)
            return
        try:
            muted = action == "mute" and self._is_muted(exe)
        except Exception:  # noqa: BLE001
            muted = False
        self.last = f"{name} {round(level * 100)}%" + (" (без звука)" if muted else "")
        self.app.ui(self._osd, f"{name} {round(level * 100)}%" if not muted else f"{name}: без звука",
                    level, muted)

    def _osd(self, text, level, muted):
        self.app.tool_status_changed(self.key)
        osd = getattr(self.app, "osd", None)
        if osd is not None:
            osd.show(text, value=level if level is not None and not muted else None, icon="note",
                     accent=(235, 72, 72, 255) if muted else (79, 140, 255, 255))

    @staticmethod
    def _media(action):
        winapi.wait_modifiers_released(0.6)
        winapi.send_keys(winapi.combo(VK_MEDIA[action]))

    def open_mixer(self):
        if self.window is not None and self.window.alive:
            self.window.lift()
            return
        self.window = MixerWindow(self.app)

    def status(self):
        return f"Последнее: {self.last}" if self.last else ""


class MixerWindow:
    """Окно «Громкость программ» (как микшер Windows, но с усилением порядка и без лишнего)."""

    def __init__(self, app):
        import tkinter as tk

        from .popup import ACCENT, FG, MUTED, PANEL, _button

        self.app = app
        self.alive = True
        self.rows: dict[str, tuple] = {}
        win = self.win = tk.Toplevel(app.root)
        win.title("Громкость программ — SoundFixer")
        win.configure(bg=PANEL)
        win.attributes("-topmost", True)
        win.resizable(False, False)
        head = tk.Frame(win, bg=PANEL)
        head.pack(fill="x", padx=12, pady=(10, 4))
        tk.Label(head, text="Громкость программ", bg=PANEL, fg=ACCENT, font=("Segoe UI Semibold", 11)).pack(
            side="left")
        _button(head, "Обновить", self.refresh).pack(side="right")
        self.body = tk.Frame(win, bg=PANEL)
        self.body.pack(fill="both", expand=True, padx=12, pady=4)
        self.hint = tk.Label(win, text="Загружаю…", bg=PANEL, fg=MUTED, font=("Segoe UI", 9))
        self.hint.pack(fill="x", padx=12, pady=(0, 10))
        self._fg = FG
        win.protocol("WM_DELETE_WINDOW", self.close)
        win.bind("<Escape>", lambda _e: self.close())
        x, y = winapi.cursor_pos()
        win.geometry(f"+{max(0, x - 200)}+{max(0, y - 300)}")
        self.refresh()

    def lift(self):
        self.win.deiconify()
        self.win.lift()
        self.win.focus_force()
        self.refresh()

    def refresh(self):
        def job():
            try:
                items, err = list_sessions(), ""
            except Exception as e:  # noqa: BLE001
                items, err = [], str(e)
            self.app.ui(self._show, items, err)

        self.app.worker.submit(job)

    def _show(self, items, err):
        import tkinter as tk
        from tkinter import ttk

        if not self.alive:
            return
        from .popup import FIELD, MUTED, PANEL

        for child in self.body.winfo_children():
            child.destroy()
        self.rows.clear()
        if err:
            self.hint.configure(text=f"Ошибка: {err}")
            return
        self.hint.configure(text="Сейчас играют — сверху. Колёсико мыши над ползунком — точная настройка."
                            if items else "Ни одна программа сейчас не выводит звук.")
        for i, a in enumerate(items):
            fg = self._fg if a.active else MUTED
            tk.Label(self.body, text=a.name[:28], bg=PANEL, fg=fg, font=("Segoe UI", 10), anchor="w",
                     width=22).grid(row=i, column=0, sticky="w", pady=3)
            var = tk.DoubleVar(value=round(a.volume * 100))
            val = tk.Label(self.body, text=f"{round(a.volume * 100)}%", bg=PANEL, fg=fg, width=5,
                           font=("Consolas", 10))
            scale = ttk.Scale(self.body, from_=0, to=100, variable=var, length=220,
                              command=lambda v, k=a.key, lbl=val: self._set(k, float(v), lbl))
            scale.grid(row=i, column=1, padx=8)
            scale.bind("<MouseWheel>", lambda e, var=var, k=a.key, lbl=val: self._wheel(e, var, k, lbl))
            val.grid(row=i, column=2)
            mute = tk.BooleanVar(value=a.muted)
            cb = tk.Checkbutton(self.body, text="без звука", variable=mute, bg=PANEL, fg=MUTED, selectcolor=FIELD,
                                activebackground=PANEL, activeforeground=self._fg, font=("Segoe UI", 9),
                                command=lambda k=a.key, m=mute: self._mute(k, m.get()))
            cb.grid(row=i, column=3, padx=(4, 0))
            self.rows[a.key] = (var, val, mute)

    def _set(self, key, value, label):
        label.configure(text=f"{round(value)}%")
        self.app.worker.submit(lambda: set_app_volume(key, volume=value / 100), delay=0.03, key=f"appvol:{key}")

    def _wheel(self, e, var, key, label):
        v = max(0.0, min(100.0, var.get() + (2 if e.delta > 0 else -2)))
        var.set(v)
        self._set(key, v, label)
        return "break"

    def _mute(self, key, muted):
        self.app.worker.submit(lambda: set_app_volume(key, mute=muted))

    def close(self):
        self.alive = False
        try:
            self.win.destroy()
        except Exception:  # noqa: BLE001
            pass


# =========================================================================== индикатор микрофона
CORNERS = ("top-right", "top-left", "bottom-right", "bottom-left")


def corner_position(work, size, corner: str, margin: int) -> tuple[int, int]:
    l, t, r, b = work
    w, h = size
    x = l + margin if corner.endswith("left") else r - w - margin
    y = t + margin if corner.startswith("top") else b - h - margin
    return x, y


def voice_now(engine) -> bool:
    """Говорит ли пользователь прямо сейчас (по нейросети шумоподавления или по уровню)."""
    if engine is None or not engine.mic_running:
        return False
    dn = engine.mic_chain.denoiser
    if dn is not None:
        return bool(dn.open)
    return engine.mic_level > 0.06


class MicGuard(Tool):
    """Значок «микрофон выключен» поверх игр и предупреждение, если говорите в выключенный микрофон."""

    key = "micguard"
    title = "Индикатор микрофона"
    POLL_MS = 250

    def __init__(self, app):
        super().__init__(app)
        self.cfg: dict = {}
        self.badge = None
        self._poll_job = None
        self._talk = 0
        self._last_alert = 0.0
        self.alerts = 0

    def start(self, cfg):
        super().start(cfg)
        self.cfg = dict(cfg)
        self.refresh()

    def update(self, cfg):
        self.cfg = dict(cfg)
        self.refresh()

    def stop(self):
        super().stop()
        if self._poll_job is not None:
            self.app.root.after_cancel(self._poll_job)
            self._poll_job = None
        if self.badge is not None:
            self.badge.destroy()
            self.badge = None

    def on_mute(self, muted: bool, announce: bool = True):
        # push-to-talk меняет состояние на каждое нажатие — крупную подсказку не показываем
        if announce and self.cfg.get("osd", True):
            osd = getattr(self.app, "osd", None)
            if osd is not None:
                hk = self.app.cfg["mic"]["mute_hotkey"]
                osd.show("Микрофон выключен" if muted else "Микрофон включён",
                         sub=f"{hk} — {'включить' if muted else 'выключить'}" if hk else "", icon="mic",
                         muted=muted, accent=(235, 72, 72, 255) if muted else (46, 196, 132, 255))
        self.refresh()

    def refresh(self):
        """Значок в углу и опрос «говорит ли» — только пока нужны."""
        if not self.running:
            return
        muted = self.app.muted
        mode = self.cfg.get("overlay", "muted")
        show = mode == "always" or (mode == "muted" and muted)
        if show:
            self._show_badge(muted)
        elif self.badge is not None:
            self.badge.hide()
        want_poll = muted and self.cfg.get("talk_alert", True)
        if want_poll and self._poll_job is None:
            self._talk = 0
            self._poll_job = self.app.root.after(self.POLL_MS, self._poll)
        elif not want_poll and self._poll_job is not None:
            self.app.root.after_cancel(self._poll_job)
            self._poll_job = None

    def _show_badge(self, muted: bool):
        from PIL import Image, ImageDraw

        from .overlay import LayeredWindow, dpi_scale, draw_mic

        k = dpi_scale() * float(self.cfg.get("size", 100)) / 100
        side = round(34 * k)
        img = Image.new("RGBA", (side, side), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        color = (235, 72, 72, 255) if muted else (46, 196, 132, 255)
        d.rounded_rectangle((0, 0, side - 1, side - 1), radius=round(9 * k), fill=(23, 24, 28, 215))
        pad = round(7 * k)
        draw_mic(d, (pad, pad, side - pad, side - pad), color, muted, max(2, round(2 * k)))
        mons = winapi.monitors()
        primary = next((m for m in mons if m.rect[0] == 0 and m.rect[1] == 0), mons[0] if mons else None)
        work = primary.work if primary else (0, 0, 1920, 1080)
        x, y = corner_position(work, img.size, self.cfg.get("corner", "top-right"), round(12 * k))
        if self.badge is None:
            self.badge = LayeredWindow()
        self.badge.show(img, x, y)

    def _poll(self):
        self._poll_job = None
        if not self.running or not self.app.muted:
            return
        if voice_now(self.app.engine):
            self._talk += 1
        else:
            self._talk = max(0, self._talk - 1)
        if self._talk >= 4 and time.monotonic() - self._last_alert > float(self.cfg.get("alert_every", 30)):
            self._last_alert = time.monotonic()
            self._talk = 0
            self.alerts += 1
            self.alert()
        self._poll_job = self.app.root.after(self.POLL_MS, self._poll)

    def alert(self):
        osd = getattr(self.app, "osd", None)
        hk = self.app.cfg["mic"]["mute_hotkey"]
        if osd is not None:
            osd.show("Вы говорите, но микрофон выключен", sub=f"{hk} — включить" if hk else "", icon="mic",
                     muted=True, accent=(235, 72, 72, 255), ms=2500)
        else:
            self.app.notify("Микрофон выключен", "Похоже, вы говорите, но вас не слышно")
        self.app.tool_status_changed(self.key)

    def status(self):
        if not self.running:
            return ""
        eng = self.app.engine
        note = "" if eng is not None and eng.mic_running else \
            " Предупреждение «говорите в выключенный микрофон» работает при включённой обработке микрофона."
        return (f"Предупреждений: {self.alerts}." if self.alerts else "Работает.") + note


# =========================================================================== автопрофили
def running_exes() -> set[str]:
    try:
        import psutil
    except ImportError:
        return set()
    out = set()
    for p in psutil.process_iter(["name"]):
        name = p.info.get("name")
        if name:
            out.add(name.lower())
    return out


def pick_profile(rules: list[dict], running: set[str], profiles) -> str | None:
    for rule in rules:
        exe = str(rule.get("exe", "")).lower().strip()
        prof = rule.get("profile")
        if exe and prof in profiles and exe in running:
            return prof
    return None


class AutoProfiles(Tool):
    """Профиль устройств по запущенной программе + горячая клавиша «следующий профиль»."""

    key = "autoprofile"
    title = "Автопрофили"
    PERIOD = 3.0

    def __init__(self, app, scan=None):
        super().__init__(app)
        self.cfg: dict = {}
        self._scan = scan or running_exes
        self._stop = threading.Event()
        self._thread = None
        self.matched: str | None = None   # профиль, включённый правилом
        self.base: str | None = None      # профиль до автопереключения
        self._last_want: str | None = None

    def start(self, cfg):
        super().start(cfg)
        self.cfg = dict(cfg)
        self._stop.clear()
        if self.cfg.get("rules"):
            self._start_thread()

    def update(self, cfg):
        self.cfg = dict(cfg)
        if self.cfg.get("rules") and (self._thread is None or not self._thread.is_alive()):
            self._start_thread()

    def stop(self):
        super().stop()
        self._stop.set()
        self._thread = None
        if self.matched and self.base and self.app.cfg["active_profile"] == self.matched:
            self.app.switch_profile(self.base, quiet=True)
        self.matched = None

    def _start_thread(self):
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="SoundFixer-autoprofile", daemon=True)
        self._thread.start()

    def _loop(self):
        while not self._stop.is_set():
            try:
                running = self._scan()
                self.app.ui(self.apply, running)
            except Exception:  # noqa: BLE001
                log.exception("Автопрофили")
            self._stop.wait(self.PERIOD)

    def apply(self, running: set[str]):
        """GUI-поток: решить, какой профиль нужен. Срабатывает только при смене набора программ,
        поэтому ручной выбор профиля (трей, горячая клавиша) правила не перебивают."""
        if not self.running:
            return
        cfg = self.app.cfg
        want = pick_profile(self.cfg.get("rules", []), running, cfg["profiles"])
        if want == self._last_want:
            return
        prev, self._last_want = self._last_want, want
        active = cfg["active_profile"]
        if want is not None:
            if prev is None:
                self.base = active
            self.matched = want
            if active != want:
                log.info("Автопрофиль: %s", want)
                self.app.switch_profile(want, quiet=True)
                self._osd(f"Профиль: {want}")
        else:
            self.matched = None
            # возвращаем, только если пользователь сам не выбрал другой профиль
            if self.base and active == prev and self.base in cfg["profiles"]:
                self.app.switch_profile(self.base, quiet=True)
                self._osd(f"Профиль: {self.base}")
        self.app.tool_status_changed(self.key)

    def _osd(self, text):
        osd = getattr(self.app, "osd", None)
        if osd is not None:
            osd.show(text, icon="note", ms=1500)

    def hotkeys(self, cfg):
        return {"next": cfg.get("cycle_hotkey", "")}

    def on_hotkey(self, action):
        names = list(self.app.cfg["profiles"])
        if len(names) < 2:
            self._osd("Создайте второй профиль во вкладке «Устройства»")
            return
        i = names.index(self.app.cfg["active_profile"]) if self.app.cfg["active_profile"] in names else -1
        nxt = names[(i + 1) % len(names)]
        self.matched = None  # ручной выбор важнее правил до следующей смены программ
        self.app.switch_profile(nxt, quiet=True)
        self._osd(f"Профиль: {nxt}")

    def status(self):
        if not self.running:
            return ""
        if self.matched:
            return f"Включён по правилу: {self.matched}"
        return "Правила не сработали — профиль как выбран вручную" if self.cfg.get("rules") else \
            "Правил нет — работает только горячая клавиша"
