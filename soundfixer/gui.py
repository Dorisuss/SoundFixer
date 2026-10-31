"""Главное окно (tkinter/ttk, тёмная тема).

Создаётся лениво при первом открытии; закрытие окна прячет его в трей.
Индикаторы уровня обновляются только пока окно видно и открыта нужная вкладка.
"""

from __future__ import annotations

import os
import sys
import tkinter as tk
import webbrowser
from tkinter import filedialog, messagebox, simpledialog, ttk

from . import APP_NAME, __version__, config
from .config import EQ_BANDS
from .hotkeys import PTT_KEYS, parse as parse_hotkey
from .presets import EQ_PRESETS, VOICE_PRESETS, voice_values

BG = "#17181c"
PANEL = "#202228"
FIELD = "#2a2d35"
FG = "#e8e9ed"
MUTED_FG = "#9aa0ad"
ACCENT = "#4f8cff"
GOOD = "#2ec484"
WARN = "#f0b429"
BAD = "#eb4848"

VB_CABLE_URL = "https://vb-audio.com/Cable/"


def pct_label(p: float) -> str:
    import math

    db = 20 * math.log10(max(p, 1) / 100)
    return f"{p:.0f}%  ({db:+.1f} дБ)"


class MainWindow:
    def __init__(self, app):
        self.app = app
        self.cfg = app.cfg
        root = self.root = app.root
        root.title(f"{APP_NAME} {__version__}")
        root.geometry("640x720")
        root.minsize(560, 560)
        root.configure(bg=BG)
        root.protocol("WM_DELETE_WINDOW", self.hide)
        self._set_icon()
        self._style()
        self._meter_job = None
        self._updating = False  # подавляет обратные колбэки при программном обновлении виджетов
        self._dev_maps = {"in": {}, "out": {}, "vmic": {}, "vspk": {}}
        self._sync_vars: list = []  # (var, getter) — для refresh()

        nb = self.nb = ttk.Notebook(root)
        nb.pack(fill="both", expand=True, padx=8, pady=(8, 0))
        self.status_bar = ttk.Label(root, style="Status.TLabel", anchor="w")
        self.status_bar.pack(fill="x", padx=10, pady=(2, 6))

        self._tab_devices()
        self._tab_mic()
        self._tab_spk()
        self._tab_speech()
        self._tab_settings()
        nb.bind("<<NotebookTabChanged>>", lambda _e: self._start_meters())
        self.refresh_devices()
        self.refresh()

    # ================================================================ оформление
    def _set_icon(self):
        try:
            from PIL import ImageTk

            from .icons import tray_icon

            self._icon_img = ImageTk.PhotoImage(tray_icon())
            self.root.iconphoto(True, self._icon_img)
        except Exception:  # noqa: BLE001
            pass

    def _style(self):
        st = ttk.Style(self.root)
        st.theme_use("clam")
        font = ("Segoe UI", 10)
        st.configure(".", background=BG, foreground=FG, fieldbackground=FIELD, font=font,
                     bordercolor=PANEL, lightcolor=PANEL, darkcolor=PANEL, troughcolor=FIELD,
                     focuscolor=ACCENT, selectbackground=ACCENT, selectforeground="#fff",
                     insertcolor=FG, arrowcolor=FG)
        st.configure("TNotebook", background=BG, borderwidth=0, tabmargins=(0, 0, 0, 0))
        st.configure("TNotebook.Tab", background=PANEL, foreground=MUTED_FG, padding=(12, 6), borderwidth=0)
        st.map("TNotebook.Tab", background=[("selected", FIELD)], foreground=[("selected", FG)])
        st.configure("TFrame", background=BG)
        st.configure("Card.TFrame", background=PANEL)
        st.configure("TLabelframe", background=PANEL, bordercolor=FIELD, relief="solid", borderwidth=1)
        st.configure("TLabelframe.Label", background=PANEL, foreground=ACCENT, font=("Segoe UI Semibold", 10))
        st.configure("TLabel", background=PANEL)
        st.configure("Bg.TLabel", background=BG)
        st.configure("Hint.TLabel", background=PANEL, foreground=MUTED_FG, font=("Segoe UI", 9))
        st.configure("Status.TLabel", background=BG, foreground=MUTED_FG, font=("Segoe UI", 9))
        st.configure("Value.TLabel", background=PANEL, foreground=FG, font=("Consolas", 10))
        st.configure("TCheckbutton", background=PANEL, foreground=FG)
        st.map("TCheckbutton", background=[("active", PANEL)], indicatorcolor=[("selected", ACCENT), ("", FIELD)])
        st.configure("Big.TCheckbutton", font=("Segoe UI Semibold", 11))
        st.configure("TButton", background=FIELD, foreground=FG, padding=(10, 4), borderwidth=0)
        st.map("TButton", background=[("active", "#353945"), ("pressed", ACCENT)])
        st.configure("Accent.TButton", background=ACCENT, foreground="#fff")
        st.map("Accent.TButton", background=[("active", "#6a9dff"), ("pressed", "#3a74e0")])
        st.configure("TCombobox", fieldbackground=FIELD, background=FIELD, foreground=FG, padding=4)
        st.map("TCombobox", fieldbackground=[("readonly", FIELD)], foreground=[("readonly", FG)],
               selectbackground=[("readonly", FIELD)], selectforeground=[("readonly", FG)])
        st.configure("TEntry", fieldbackground=FIELD, foreground=FG, padding=4)
        for orient in ("Horizontal", "Vertical"):
            st.configure(f"{orient}.TScale", background=ACCENT, troughcolor=FIELD, bordercolor=PANEL,
                         lightcolor=ACCENT, darkcolor=ACCENT, sliderthickness=14, gripcount=0)
            st.map(f"{orient}.TScale", background=[("active", "#7aa8ff")])
        st.configure("Vertical.TScrollbar", background=FIELD, troughcolor=BG, bordercolor=BG,
                     lightcolor=FIELD, darkcolor=FIELD, arrowcolor=MUTED_FG, gripcount=0)
        st.map("Vertical.TScrollbar", background=[("active", "#353945")])
        self.root.option_add("*TCombobox*Listbox.background", FIELD)
        self.root.option_add("*TCombobox*Listbox.foreground", FG)
        self.root.option_add("*TCombobox*Listbox.selectBackground", ACCENT)

    # ================================================================ помощники виджетов
    def _page(self, title: str) -> ttk.Frame:
        """Вкладка с вертикальной прокруткой."""
        outer = ttk.Frame(self.nb)
        self.nb.add(outer, text=title)
        canvas = tk.Canvas(outer, bg=BG, highlightthickness=0, borderwidth=0)
        bar = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        inner = ttk.Frame(canvas)
        win = canvas.create_window((0, 0), window=inner, anchor="nw")
        inner.bind("<Configure>", lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(win, width=e.width))
        canvas.configure(yscrollcommand=bar.set)
        canvas.pack(side="left", fill="both", expand=True)
        bar.pack(side="right", fill="y")

        def wheel(e):
            if canvas.bbox("all")[3] > canvas.winfo_height():
                canvas.yview_scroll(int(-e.delta / 120), "units")

        inner.bind("<Enter>", lambda _e: canvas.bind_all("<MouseWheel>", wheel))
        inner.bind("<Leave>", lambda _e: canvas.unbind_all("<MouseWheel>"))
        inner.page_name = title
        return inner

    def _card(self, parent, title: str) -> ttk.Labelframe:
        f = ttk.Labelframe(parent, text=f" {title} ", padding=(10, 6))
        f.pack(fill="x", padx=6, pady=6)
        f.columnconfigure(0, minsize=150)
        f.columnconfigure(1, weight=1)
        return f

    def _hint(self, parent, text: str, row=None, wrap=560):
        lbl = ttk.Label(parent, text=text, style="Hint.TLabel", wraplength=wrap, justify="left")
        if row is None:
            lbl.grid(column=0, columnspan=3, sticky="w", pady=(2, 4))
        else:
            lbl.grid(row=row, column=0, columnspan=3, sticky="w", pady=(2, 4))
        return lbl

    def _next_row(self, parent) -> int:
        return parent.grid_size()[1]

    def _check(self, parent, text, section, key, devices=False, hotkeys=False, style="TCheckbutton", cmd=None,
               sync=False):
        var = tk.BooleanVar(value=self._get(section, key))

        def changed():
            if self._updating:
                return
            self._set(section, key, var.get())
            if cmd:
                cmd()
            self.app.settings_changed(devices=devices, hotkeys=hotkeys, sync=sync)

        cb = ttk.Checkbutton(parent, text=text, variable=var, command=changed, style=style)
        cb.grid(row=self._next_row(parent), column=0, columnspan=3, sticky="w", pady=2)
        self._sync_vars.append((var, lambda: self._get(section, key)))
        return var

    def _slider(self, parent, text, section, key, lo, hi, fmt=str, step=1.0, cmd=None, devices=False, sync=False):
        row = self._next_row(parent)
        ttk.Label(parent, text=text).grid(row=row, column=0, sticky="w", padx=(0, 8))
        var = tk.DoubleVar(value=self._get(section, key))
        val = ttk.Label(parent, style="Value.TLabel", width=18, anchor="e")
        val.grid(row=row, column=2, sticky="e")

        def show(v):
            val.configure(text=fmt(v))

        def changed(_=None):
            v = round(float(var.get()) / step) * step
            if isinstance(self._get(section, key), int) and step >= 1:
                v = int(v)
            show(v)
            if self._updating or v == self._get(section, key):
                return
            self._set(section, key, v)
            if cmd:
                cmd()
            self.app.settings_changed(devices=devices, sync=sync)

        sc = ttk.Scale(parent, from_=lo, to=hi, variable=var, command=changed)
        sc.grid(row=row, column=1, sticky="ew", pady=3)
        sc.bind("<Double-Button-1>", lambda _e: self._reset_slider(section, key, var, changed))
        show(self._get(section, key))
        self._sync_vars.append((var, lambda: self._get(section, key)))
        var.trace_add("write", lambda *_: show(round(float(var.get()) / step) * step))
        return var

    def _reset_slider(self, section, key, var, changed):
        default = config.DEFAULTS[section][key] if section else config.DEFAULTS[key]
        var.set(default)
        changed()

    def _get(self, section, key):
        return self.cfg[section][key] if section else self.cfg[key]

    def _set(self, section, key, value):
        if section:
            self.cfg[section][key] = value
        else:
            self.cfg[key] = value

    def _meter(self, parent, label) -> tk.Canvas:
        row = self._next_row(parent)
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w")
        c = tk.Canvas(parent, height=12, bg=FIELD, highlightthickness=0)
        c.grid(row=row, column=1, columnspan=2, sticky="ew", pady=4)
        c.bar = c.create_rectangle(0, 0, 0, 12, fill=GOOD, width=0)
        return c

    def _status_label(self, parent) -> ttk.Label:
        lbl = ttk.Label(parent, style="Hint.TLabel", wraplength=560, justify="left")
        lbl.grid(row=self._next_row(parent), column=0, columnspan=3, sticky="w", pady=(0, 4))
        return lbl

    def _hotkey_entry(self, parent, text, section, key, hotkeys=True):
        row = self._next_row(parent)
        ttk.Label(parent, text=text).grid(row=row, column=0, sticky="w", padx=(0, 8))
        var = tk.StringVar(value=self._get(section, key))
        ent = ttk.Entry(parent, textvariable=var, width=22)
        ent.grid(row=row, column=1, sticky="w", pady=3)
        ttk.Label(parent, text="кликните и нажмите сочетание", style="Hint.TLabel").grid(row=row, column=2, sticky="e")

        def on_key(e):
            combo = keysym_to_combo(e)
            if combo is None:
                return "break"
            var.set(combo)
            self._set(section, key, combo)
            self.app.settings_changed(hotkeys=hotkeys)
            return "break"

        ent.bind("<KeyPress>", on_key)
        self._sync_vars.append((var, lambda: self._get(section, key)))
        return var

    # ================================================================ вкладка «Устройства»
    def _tab_devices(self):
        page = self._page("Устройства")

        card = self._card(page, "Профиль")
        self.profile_var = tk.StringVar()
        self.profile_box = ttk.Combobox(card, textvariable=self.profile_var, state="readonly")
        self.profile_box.grid(row=0, column=0, columnspan=2, sticky="ew", pady=3)
        self.profile_box.bind("<<ComboboxSelected>>", lambda _e: self.app.switch_profile(self.profile_var.get()))
        btns = ttk.Frame(card, style="Card.TFrame")
        btns.grid(row=0, column=2, sticky="e", padx=(6, 0))
        ttk.Button(btns, text="Новый", command=self._profile_new).pack(side="left", padx=2)
        ttk.Button(btns, text="Имя", command=self._profile_rename).pack(side="left", padx=2)
        ttk.Button(btns, text="Удалить", command=self._profile_delete).pack(side="left", padx=2)

        ttk.Label(card, text="Микрофон").grid(row=1, column=0, sticky="w", pady=(8, 0))
        self.in_var = tk.StringVar()
        self.in_box = ttk.Combobox(card, textvariable=self.in_var, state="readonly")
        self.in_box.grid(row=2, column=0, columnspan=3, sticky="ew", pady=3)
        self.in_box.bind("<<ComboboxSelected>>", lambda _e: self._device_picked("in"))

        ttk.Label(card, text="Наушники / динамики").grid(row=3, column=0, sticky="w", pady=(8, 0))
        self.out_var = tk.StringVar()
        self.out_box = ttk.Combobox(card, textvariable=self.out_var, state="readonly")
        self.out_box.grid(row=4, column=0, columnspan=3, sticky="ew", pady=3)
        self.out_box.bind("<<ComboboxSelected>>", lambda _e: self._device_picked("out"))

        row = ttk.Frame(card, style="Card.TFrame")
        row.grid(row=5, column=0, columnspan=3, sticky="ew", pady=(8, 2))
        ttk.Button(row, text="Применить сейчас", style="Accent.TButton", command=self.app.apply_now).pack(side="left")
        ttk.Button(row, text="Обновить список", command=self.app.apply_now).pack(side="left", padx=6)
        self.defaults_lbl = self._status_label(card)

        card = self._card(page, "Автоматика")
        self.autostart_var = tk.BooleanVar(value=self.app.autostart_enabled)
        ttk.Checkbutton(card, text="Запускать вместе с Windows (в трее)", variable=self.autostart_var,
                        command=self.app.toggle_autostart).grid(row=0, column=0, columnspan=3, sticky="w", pady=2)
        self._check(card, "Следить за устройствами и сразу возвращать выбранные", None, "enforce_defaults",
                    devices=True)
        self._hint(card, "Если Windows или программа (игра, Discord, подключение гарнитуры) поменяет "
                         "устройство по умолчанию — SoundFixer вернёт выбранное в профиле. Работает по "
                         "событиям Windows, без постоянного опроса.")
        self._check(card, "Показывать уведомления", None, "notifications")

        card = self._card(page, "Микрофон в Windows")
        self._check(card, "Фиксировать громкость микрофона (запретить программам её менять)", "mic",
                    "lock_level", sync=True)
        self._slider(card, "Уровень", "mic", "lock_level_value", 0, 100, lambda v: f"{v:.0f}%", sync=True)
        self._hint(card, "Полезно против автонастройки громкости в Discord, Zoom, Skype, Teams.")

    def _profile_new(self):
        name = simpledialog.askstring(APP_NAME, "Название нового профиля:", parent=self.root)
        if not name or not name.strip():
            return
        name = name.strip()
        if name in self.cfg["profiles"]:
            messagebox.showwarning(APP_NAME, "Профиль с таким именем уже есть.", parent=self.root)
            return
        self.cfg["profiles"][name] = dict(self.app.profile)
        self.app.switch_profile(name)
        self.refresh()

    def _profile_rename(self):
        old = self.cfg["active_profile"]
        name = simpledialog.askstring(APP_NAME, "Новое имя профиля:", initialvalue=old, parent=self.root)
        if not name or not name.strip() or name.strip() == old:
            return
        name = name.strip()
        if name in self.cfg["profiles"]:
            messagebox.showwarning(APP_NAME, "Профиль с таким именем уже есть.", parent=self.root)
            return
        self.cfg["profiles"] = {(name if k == old else k): v for k, v in self.cfg["profiles"].items()}
        self.cfg["active_profile"] = name
        self.app.settings_changed()
        self.refresh()

    def _profile_delete(self):
        if len(self.cfg["profiles"]) <= 1:
            messagebox.showinfo(APP_NAME, "Нельзя удалить единственный профиль.", parent=self.root)
            return
        name = self.cfg["active_profile"]
        if not messagebox.askyesno(APP_NAME, f"Удалить профиль «{name}»?", parent=self.root):
            return
        del self.cfg["profiles"][name]
        self.app.switch_profile(next(iter(self.cfg["profiles"])))
        self.refresh()

    def _device_picked(self, kind):
        var = self.in_var if kind == "in" else self.out_var
        dev_id = self._dev_maps[kind].get(var.get())
        if dev_id is not None:
            self.app.set_device(kind, dev_id or None)

    # ================================================================ вкладка «Микрофон»
    def _tab_mic(self):
        page = self.mic_page = self._page("Микрофон")
        card = self._card(page, "Обработка микрофона")
        self.mic_enabled = self._check(card, "Включить (громкость > 100%, шумодав, смена голоса, озвучка)",
                                       "mic", "enabled", devices=True, style="Big.TCheckbutton")
        self.mic_status = self._status_label(card)
        self.mic_meter = self._meter(card, "Уровень входа")
        self.mute_btn = ttk.Button(card, text="Выключить микрофон", command=self.app.toggle_mute)
        self.mute_btn.grid(row=self._next_row(card), column=0, sticky="w", pady=4)

        card = self._card(page, "Громкость и чистота")
        self._slider(card, "Громкость", "mic", "gain", 0, 1000, pct_label, step=5)
        self._check(card, "Убирать низкий гул (фильтр < 90 Гц)", "mic", "hpf")
        self._check(card, "Шумодав (тишина, когда вы молчите)", "mic", "gate")
        self._slider(card, "Порог шумодава", "mic", "gate_db", -80, -20, lambda v: f"{v:.0f} дБ")
        self._check(card, "Выравнивать громкость (компрессор)", "mic", "compressor")
        self._hint(card, "Двойной клик по ползунку — сброс к значению по умолчанию.")

        card = self._card(page, "Смена голоса")
        ttk.Label(card, text="Пресет").grid(row=0, column=0, sticky="w")
        self.preset_var = tk.StringVar()
        box = ttk.Combobox(card, textvariable=self.preset_var, state="readonly",
                           values=[n for n, _ in VOICE_PRESETS.values()])
        box.grid(row=0, column=1, columnspan=2, sticky="ew", pady=3)
        box.bind("<<ComboboxSelected>>", lambda _e: self._preset_picked())
        custom = lambda: self._mark_custom("mic", "preset")  # noqa: E731
        self._voice_vars = {
            "pitch": self._slider(card, "Высота тона", "mic", "pitch", -12, 12,
                                  lambda v: f"{v:+.1f} полутона", step=0.5, cmd=custom),
            "robot": self._slider(card, "Робот", "mic", "robot", 0, 100, lambda v: f"{v:.0f}%", cmd=custom),
            "distortion": self._slider(card, "Хрип / перегруз", "mic", "distortion", 0, 100,
                                       lambda v: f"{v:.0f}%", cmd=custom),
            "chorus": self._slider(card, "Хорус", "mic", "chorus", 0, 100, lambda v: f"{v:.0f}%", cmd=custom),
            "echo": self._slider(card, "Эхо", "mic", "echo", 0, 100, lambda v: f"{v:.0f}%", cmd=custom),
            "reverb": self._slider(card, "Реверберация", "mic", "reverb", 0, 100, lambda v: f"{v:.0f}%",
                                   cmd=custom),
            "radio": self._check(card, "Рация / телефон", "mic", "radio", cmd=custom),
        }

        card = self._card(page, "Слышать себя")
        self._check(card, "Выводить свой голос в наушники (проверка эффектов)", "mic", "monitor", devices=True)
        self._slider(card, "Громкость контроля", "mic", "monitor_volume", 0, 100, lambda v: f"{v:.0f}%")

        card = self._card(page, "Выключение микрофона")
        self._hotkey_entry(card, "Горячая клавиша", "mic", "mute_hotkey")
        self._check(card, "Push-to-talk (микрофон работает, только пока зажата клавиша)", "mic", "ptt",
                    hotkeys=True)
        row = self._next_row(card)
        ttk.Label(card, text="Клавиша PTT").grid(row=row, column=0, sticky="w")
        self.ptt_var = tk.StringVar(value=self.cfg["mic"]["ptt_key"])
        ptt = ttk.Combobox(card, textvariable=self.ptt_var, values=PTT_KEYS, width=20)
        ptt.grid(row=row, column=1, sticky="w", pady=3)

        def ptt_changed(_e=None):
            key = self.ptt_var.get().strip().lower()
            if parse_hotkey(key):
                self.cfg["mic"]["ptt_key"] = key
                self.app.settings_changed(hotkeys=True)

        ptt.bind("<<ComboboxSelected>>", ptt_changed)
        ptt.bind("<FocusOut>", ptt_changed)
        self._hint(card, "Без обработки микрофона кнопка выключает сам микрофон в Windows, с обработкой — "
                         "выключает его мгновенно внутри SoundFixer. Подходят и боковые кнопки мыши (mouse4/5).")

    def _preset_picked(self):
        label = self.preset_var.get()
        key = next((k for k, (n, _) in VOICE_PRESETS.items() if n == label), "normal")
        self.cfg["mic"]["preset"] = key
        vals = voice_values(key)
        if vals is not None:
            self.cfg["mic"].update(vals)
            self._refresh_vars()
        self.app.settings_changed()

    def _mark_custom(self, section, key):
        if self.cfg[section][key] != "custom":
            self.cfg[section][key] = "custom"
            self._refresh_combos()

    # ================================================================ вкладка «Наушники»
    def _tab_spk(self):
        page = self.spk_page = self._page("Наушники")
        card = self._card(page, "Обработка звука")
        self.spk_enabled = self._check(card, "Включить (громкость > 100%, эквалайзер, эффекты)", "spk",
                                       "enabled", devices=True, style="Big.TCheckbutton")
        self.spk_status = self._status_label(card)
        self.spk_meter = self._meter(card, "Уровень выхода")
        self._slider(card, "Громкость", "spk", "gain", 0, 500, pct_label, step=5)
        self._hint(card, "Звук всей системы проходит через виртуальный кабель, усиливается и выводится в "
                         "наушники. Лимитер не даёт звуку хрипеть даже на 500%.")

        card = self._card(page, "Эквалайзер")
        ttk.Label(card, text="Пресет").grid(row=0, column=0, sticky="w")
        self.eq_var = tk.StringVar()
        box = ttk.Combobox(card, textvariable=self.eq_var, state="readonly", values=[n for n, _ in EQ_PRESETS.values()])
        box.grid(row=0, column=1, columnspan=2, sticky="ew", pady=3)
        box.bind("<<ComboboxSelected>>", lambda _e: self._eq_preset_picked())
        bands = ttk.Frame(card, style="Card.TFrame")
        bands.grid(row=1, column=0, columnspan=3, sticky="ew", pady=6)
        self.eq_vars = []
        for i, f in enumerate(EQ_BANDS):
            bands.columnconfigure(i, weight=1)
            var = tk.DoubleVar(value=self.cfg["spk"]["eq"][i])
            val = ttk.Label(bands, style="Value.TLabel", width=6, anchor="center")
            val.grid(row=0, column=i)
            sc = ttk.Scale(bands, from_=12, to=-12, orient="vertical", variable=var, length=130,
                           command=lambda _v, i=i: self._eq_changed(i))
            sc.grid(row=1, column=i, pady=2)
            sc.bind("<Double-Button-1>", lambda _e, i=i: self._eq_reset(i))
            ttk.Label(bands, text=f"{f / 1000:g} кГц" if f >= 1000 else f"{f} Гц",
                      style="Hint.TLabel").grid(row=2, column=i)
            var.trace_add("write", lambda *_a, v=var, l=val: l.configure(text=f"{round(v.get()):+d}"))
            val.configure(text=f"{round(var.get()):+d}")
            self.eq_vars.append(var)

        card = self._card(page, "Пространство")
        self._slider(card, "Ширина стерео", "spk", "width", 0, 200,
                     lambda v: "моно" if v == 0 else f"{v:.0f}%", step=5)
        self._slider(card, "Баланс Л/П", "spk", "balance", -100, 100,
                     lambda v: "центр" if v == 0 else (f"Л {-v:.0f}%" if v < 0 else f"П {v:.0f}%"), step=5)
        self._check(card, "Кроссфид (мягче стерео в наушниках, меньше устают уши)", "spk", "crossfeed")
        self._check(card, "Ночной режим (тихие звуки громче, взрывы тише)", "spk", "night")

    def _eq_changed(self, i):
        if self._updating:
            return
        v = float(round(self.eq_vars[i].get()))
        if self.cfg["spk"]["eq"][i] == v:
            return
        self.cfg["spk"]["eq"][i] = v
        self._mark_custom("spk", "eq_preset")
        self.app.settings_changed()

    def _eq_reset(self, i):
        self.eq_vars[i].set(0)
        self._eq_changed(i)

    def _eq_preset_picked(self):
        label = self.eq_var.get()
        key, vals = next(((k, v) for k, (n, v) in EQ_PRESETS.items() if n == label), ("flat", [0] * 5))
        self.cfg["spk"]["eq_preset"] = key
        if vals is not None:
            self.cfg["spk"]["eq"] = [float(v) for v in vals]
            self._refresh_vars()
        self.app.settings_changed()

    # ================================================================ вкладка «Озвучка и звуки»
    def _tab_speech(self):
        page = self._page("Озвучка и звуки")
        card = self._card(page, "Озвучка текста в микрофон")
        self.tts_text = tk.Text(card, height=4, wrap="word", bg=FIELD, fg=FG, insertbackground=FG,
                                relief="flat", font=("Segoe UI", 10), padx=6, pady=4,
                                highlightthickness=1, highlightbackground=FIELD, highlightcolor=ACCENT)
        self.tts_text.grid(row=0, column=0, columnspan=3, sticky="ew", pady=3)
        self.tts_text.bind("<Control-Return>", lambda _e: (self._speak(), "break")[1])
        row = ttk.Frame(card, style="Card.TFrame")
        row.grid(row=1, column=0, columnspan=3, sticky="ew")
        ttk.Button(row, text="Сказать  (Ctrl+Enter)", style="Accent.TButton", command=self._speak).pack(side="left")
        ttk.Button(row, text="Стоп", command=self.app.stop_sounds).pack(side="left", padx=6)

        ttk.Label(card, text="Голос").grid(row=2, column=0, sticky="w", pady=(8, 0))
        self.voice_var = tk.StringVar(value=self.cfg["tts"]["voice"] or "по умолчанию")
        self.voice_box = ttk.Combobox(card, textvariable=self.voice_var, state="readonly")
        self.voice_box.grid(row=2, column=1, columnspan=2, sticky="ew", pady=(8, 3))
        self.voice_box.bind("<<ComboboxSelected>>", lambda _e: self._voice_picked())
        self.voice_box.bind("<Button-1>", lambda _e: self._load_voices(), add="+")
        self._slider(card, "Скорость", "tts", "rate", -10, 10, lambda v: f"{v:+.0f}")
        self._slider(card, "Громкость", "tts", "volume", 0, 100, lambda v: f"{v:.0f}%")
        self._check(card, "Пропускать через эффекты голоса", "tts", "through_fx")
        self._check(card, "Слышать озвучку в наушниках", "tts", "monitor", devices=True)

        ttk.Label(card, text="Недавние фразы (двойной клик — повторить)").grid(
            row=self._next_row(card), column=0, columnspan=3, sticky="w", pady=(8, 0))
        self.history = tk.Listbox(card, height=4, bg=FIELD, fg=FG, selectbackground=ACCENT, relief="flat",
                                  highlightthickness=0, activestyle="none")
        self.history.grid(row=self._next_row(card), column=0, columnspan=3, sticky="ew", pady=3)
        self.history.bind("<Double-Button-1>", lambda _e: self._replay())

        card = self._card(page, "Саундборд (звуки в микрофон)")
        self.sounds_list = tk.Listbox(card, height=6, bg=FIELD, fg=FG, selectbackground=ACCENT, relief="flat",
                                      highlightthickness=0, activestyle="none")
        self.sounds_list.grid(row=0, column=0, columnspan=3, sticky="ew", pady=3)
        self.sounds_list.bind("<Double-Button-1>", lambda _e: self._play_selected())
        row = ttk.Frame(card, style="Card.TFrame")
        row.grid(row=1, column=0, columnspan=3, sticky="ew")
        ttk.Button(row, text="▶ Играть", style="Accent.TButton", command=self._play_selected).pack(side="left")
        ttk.Button(row, text="Добавить…", command=self._add_sounds).pack(side="left", padx=4)
        ttk.Button(row, text="Клавиша…", command=self._sound_hotkey).pack(side="left", padx=4)
        ttk.Button(row, text="Удалить", command=self._remove_sound).pack(side="left", padx=4)
        ttk.Button(row, text="Стоп", command=self.app.stop_sounds).pack(side="left", padx=4)
        self._slider(card, "Громкость", "soundboard", "volume", 0, 200, lambda v: f"{v:.0f}%", step=5)
        self._check(card, "Слышать звуки в наушниках", "soundboard", "monitor", devices=True)
        self._hotkey_entry(card, "Остановить все звуки", "soundboard", "stop_hotkey")
        self._hint(card, "Форматы: mp3, wav, ogg, flac. Звуки загружаются в память один раз и "
                         "играют мгновенно.")
        self.speech_status = self._status_label(card)

    def _speak(self):
        text = self.tts_text.get("1.0", "end").strip()
        if text:
            self.app.speak(text)
            self.tts_text.delete("1.0", "end")
            self._refresh_lists()

    def _replay(self):
        sel = self.history.curselection()
        if sel:
            self.app.speak(self.history.get(sel[0]))

    def _load_voices(self):
        if getattr(self, "_voices_loaded", False):
            return
        self._voices_loaded = True

        def done(voices):
            self.voice_box.configure(values=["по умолчанию"] + voices)

        self.app.list_voices(done)

    def _voice_picked(self):
        v = self.voice_var.get()
        self.cfg["tts"]["voice"] = "" if v == "по умолчанию" else v
        self.app.settings_changed()

    def _add_sounds(self):
        paths = filedialog.askopenfilenames(
            parent=self.root, title="Добавить звуки",
            filetypes=[("Аудио", "*.mp3 *.wav *.ogg *.flac *.aiff *.m4a"), ("Все файлы", "*.*")])
        for p in paths:
            name = os.path.splitext(os.path.basename(p))[0]
            self.cfg["soundboard"]["sounds"].append({"path": p, "name": name, "hotkey": ""})
        if paths:
            self.app.settings_changed()
            self._refresh_lists()

    def _selected_sound(self):
        sel = self.sounds_list.curselection()
        return sel[0] if sel else None

    def _play_selected(self):
        i = self._selected_sound()
        if i is not None:
            self.app.play_sound(i)

    def _remove_sound(self):
        i = self._selected_sound()
        if i is not None:
            del self.cfg["soundboard"]["sounds"][i]
            self.app.settings_changed(hotkeys=True)
            self._refresh_lists()

    def _sound_hotkey(self):
        i = self._selected_sound()
        if i is None:
            return
        snd = self.cfg["soundboard"]["sounds"][i]
        combo = simpledialog.askstring(APP_NAME, "Горячая клавиша (например ctrl+alt+1, f13, num 5).\n"
                                                 "Пусто — убрать:", initialvalue=snd.get("hotkey", ""),
                                       parent=self.root)
        if combo is None:
            return
        combo = combo.strip().lower()
        if combo and not parse_hotkey(combo):
            messagebox.showwarning(APP_NAME, "Не понимаю такое сочетание.", parent=self.root)
            return
        snd["hotkey"] = combo
        self.app.settings_changed(hotkeys=True)
        self._refresh_lists()

    # ================================================================ вкладка «Настройки»
    def _tab_settings(self):
        page = self._page("Настройки")
        card = self._card(page, "Виртуальные кабели")
        self._hint(card, "Чтобы собеседники слышали обработанный голос, а звук системы можно было усилить "
                         "выше 100%, нужен виртуальный аудиокабель (бесплатный VB-Audio Virtual Cable). "
                         "Для микрофона и наушников одновременно нужны два кабеля (например, VB-Cable и "
                         "VB-Cable A/B, или VoiceMeeter).", row=0)
        ttk.Label(card, text="Кабель для микрофона").grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.vmic_var = tk.StringVar()
        self.vmic_box = ttk.Combobox(card, textvariable=self.vmic_var, state="readonly")
        self.vmic_box.grid(row=2, column=0, columnspan=3, sticky="ew", pady=3)
        self.vmic_box.bind("<<ComboboxSelected>>", lambda _e: self._virtual_picked("vmic", "virtual_mic"))
        self._check(card, "Делать виртуальный микрофон устройством Windows по умолчанию", "mic",
                    "set_virtual_default", devices=True)
        ttk.Label(card, text="Кабель для звука системы (наушники)").grid(
            row=self._next_row(card), column=0, columnspan=3, sticky="w", pady=(6, 0))
        self.vspk_var = tk.StringVar()
        self.vspk_box = ttk.Combobox(card, textvariable=self.vspk_var, state="readonly")
        self.vspk_box.grid(row=self._next_row(card), column=0, columnspan=3, sticky="ew", pady=3)
        self.vspk_box.bind("<<ComboboxSelected>>", lambda _e: self._virtual_picked("vspk", "virtual_spk"))
        row = ttk.Frame(card, style="Card.TFrame")
        row.grid(row=self._next_row(card), column=0, columnspan=3, sticky="w", pady=4)
        ttk.Button(row, text="Скачать VB-Cable", command=lambda: webbrowser.open(VB_CABLE_URL)).pack(side="left")
        ttk.Button(row, text="Звуковые устройства Windows",
                   command=lambda: os.startfile("mmsys.cpl") if hasattr(os, "startfile") else None).pack(
            side="left", padx=6)

        card = self._card(page, "Производительность")
        ttk.Label(card, text="Размер буфера").grid(row=0, column=0, sticky="w")
        self.block_var = tk.StringVar()
        self._blocks = {"256 (5 мс, минимальная задержка)": 256, "480 (10 мс, рекомендуется)": 480,
                        "960 (20 мс, для слабых ПК)": 960}
        box = ttk.Combobox(card, textvariable=self.block_var, state="readonly", values=list(self._blocks))
        box.grid(row=0, column=1, columnspan=2, sticky="ew", pady=3)
        box.bind("<<ComboboxSelected>>", lambda _e: self._block_picked())
        self._hint(card, "Обработка звука включается только по требованию: без неё программа не загружает "
                         "звуковой движок вообще и почти не занимает памяти и CPU.")

        card = self._card(page, "О программе")
        ttk.Label(card, text=f"{APP_NAME} {__version__}").grid(row=0, column=0, sticky="w")
        row = ttk.Frame(card, style="Card.TFrame")
        row.grid(row=1, column=0, columnspan=3, sticky="w", pady=4)
        ttk.Button(row, text="Папка настроек и логов",
                   command=lambda: os.startfile(config.CONFIG_DIR) if hasattr(os, "startfile") else None).pack(
            side="left")
        ttk.Button(row, text="Выход из программы", command=self.app.quit).pack(side="left", padx=6)

    def _virtual_picked(self, kind, key):
        var = self.vmic_var if kind == "vmic" else self.vspk_var
        name = self._dev_maps[kind].get(var.get(), "")
        self.cfg[key] = name
        self.app.settings_changed(devices=True)

    def _block_picked(self):
        self.cfg["block_size"] = self._blocks.get(self.block_var.get(), 480)
        self.app.settings_changed(devices=True)

    # ================================================================ обновление
    def refresh_devices(self):
        """Заполняет списки устройств (вызывается при изменении устройств в системе)."""
        app = self.app
        prof = app.profile

        def fill(box, var, kind, devices, current_id, allow_none_label):
            names, mapping, seen = [allow_none_label], {allow_none_label: ""}, {}
            for d in devices:
                label = d.name
                seen[label] = seen.get(label, 0) + 1
                if seen[label] > 1:
                    label = f"{label} #{seen[label]}"
                mapping[label] = d.id
                names.append(label)
            current = next((n for n, i in mapping.items() if i and current_id and i.lower() == current_id.lower()),
                           None)
            if current is None and current_id:
                current = "(не подключено) — сохранённое устройство"
                names.append(current)
                mapping[current] = current_id
            box.configure(values=names)
            var.set(current or allow_none_label)
            self._dev_maps[kind] = mapping

        fill(self.in_box, self.in_var, "in", app.inputs, prof["input_id"], "— не менять —")
        fill(self.out_box, self.out_var, "out", app.outputs, prof["output_id"], "— не менять —")

        virt = sorted(app.outputs, key=lambda d: not d.is_virtual)
        for kind, box, var, key in (("vmic", self.vmic_box, self.vmic_var, "virtual_mic"),
                                    ("vspk", self.vspk_box, self.vspk_var, "virtual_spk")):
            mapping = {"— не использовать —": ""}
            for d in virt:
                mapping[d.name + ("" if d.is_virtual else "   (не виртуальное!)")] = d.name
            cur = self.cfg[key]
            label = next((lbl for lbl, n in mapping.items() if n and n == cur), None)
            if label is None and cur:
                label = f"{cur} (не найден)"
                mapping[label] = cur
            box.configure(values=list(mapping))
            var.set(label or "— не использовать —")
            self._dev_maps[kind] = mapping

        names = {d.id: d.name for d in app.inputs + app.outputs}
        from .winaudio import CAPTURE, RENDER

        cur_in = names.get(app.defaults.get(CAPTURE) or "", "—")
        cur_out = names.get(app.defaults.get(RENDER) or "", "—")
        self.defaults_lbl.configure(text=f"Сейчас в Windows — микрофон: {cur_in}\nзвук: {cur_out}")

    def refresh(self):
        """Синхронизирует виджеты с конфигом (после смены профиля, из трея и т.п.)."""
        self.cfg = self.app.cfg
        self.profile_box.configure(values=list(self.cfg["profiles"]))
        self.profile_var.set(self.cfg["active_profile"])
        self.autostart_var.set(self.app.autostart_enabled)
        self._refresh_vars()
        self._refresh_lists()
        self.refresh_devices()
        self.refresh_status()

    def _refresh_vars(self):
        self._updating = True
        try:
            for var, getter in self._sync_vars:
                v = getter()
                if var.get() != v:
                    var.set(v)
            for i, var in enumerate(self.eq_vars):
                var.set(self.cfg["spk"]["eq"][i])
            self.ptt_var.set(self.cfg["mic"]["ptt_key"])
            self.block_var.set(next((k for k, v in self._blocks.items() if v == self.cfg["block_size"]),
                                    list(self._blocks)[1]))
            self._refresh_combos()
        finally:
            self._updating = False

    def _refresh_combos(self):
        self.preset_var.set(VOICE_PRESETS.get(self.cfg["mic"]["preset"], VOICE_PRESETS["custom"])[0])
        self.eq_var.set(EQ_PRESETS.get(self.cfg["spk"]["eq_preset"], EQ_PRESETS["custom"])[0])

    def _refresh_lists(self):
        self.history.delete(0, "end")
        for h in self.cfg["tts"]["history"]:
            self.history.insert("end", h)
        self.sounds_list.delete(0, "end")
        for s in self.cfg["soundboard"]["sounds"]:
            hk = f"   [{s['hotkey']}]" if s.get("hotkey") else ""
            missing = "" if os.path.exists(s["path"]) else "   (файл не найден)"
            self.sounds_list.insert("end", f"{s.get('name') or os.path.basename(s['path'])}{hk}{missing}")

    def refresh_status(self):
        st = self.app.status

        def paint(lbl, text, enabled):
            color = MUTED_FG
            if enabled and text.startswith("Работает"):
                color = GOOD
            elif enabled and text:
                color = WARN
            lbl.configure(text=text or ("Выключено" if not enabled else "Запуск…"), foreground=color)

        paint(self.mic_status, st.get("mic", ""), self.cfg["mic"]["enabled"])
        paint(self.spk_status, st.get("spk", ""), self.cfg["spk"]["enabled"])
        self.mute_btn.configure(text="Включить микрофон" if self.app.muted else "Выключить микрофон")
        eng = self.app.engine
        self.speech_status.configure(text="" if eng is not None and eng.mic_running else st.get("mic", ""))
        if self.root.state() != "withdrawn":
            self._start_meters()
        bar = f"Профиль: {self.cfg['active_profile']}"
        if self.app.muted:
            bar += "   •   МИКРОФОН ВЫКЛЮЧЕН"
        if st.get("devices"):
            bar += f"   •   {st['devices']}"
        hk = self.app.hotkeys.failed
        if hk:
            bar += f"   •   занятые клавиши: {', '.join(hk)}"
        self.status_bar.configure(text=bar, foreground=BAD if self.app.muted else MUTED_FG)

    # ================================================================ показ/скрытие, индикаторы
    def show(self):
        self.refresh()
        self.root.deiconify()
        self.root.lift()
        self.root.attributes("-topmost", True)
        self.root.after(150, lambda: self.root.attributes("-topmost", False))
        self.root.focus_force()
        self._start_meters()

    def hide(self):
        self.root.withdraw()
        self._stop_meters()

    def _start_meters(self):
        if self._meter_job is None:
            self._tick()

    def _stop_meters(self):
        if self._meter_job is not None:
            self.root.after_cancel(self._meter_job)
            self._meter_job = None

    def _tick(self):
        self._meter_job = None
        if self.root.state() == "withdrawn":
            return
        eng = self.app.engine
        tab = self.nb.index("current")
        if eng is None or tab not in (1, 2):
            return  # индикаторы нужны только на вкладках «Микрофон»/«Наушники»
        if tab == 1:
            self._draw_meter(self.mic_meter, eng.mic_level if eng.mic_running else 0.0)
        else:
            self._draw_meter(self.spk_meter, eng.spk_level if eng.spk_running else 0.0)
        self._meter_job = self.root.after(50, self._tick)

    @staticmethod
    def _draw_meter(canvas, level):
        import math

        w = canvas.winfo_width()
        db = 20 * math.log10(max(level, 1e-5))
        frac = max(0.0, min(1.0, (db + 60) / 60))
        color = GOOD if db < -9 else (WARN if db < -1 else BAD)
        canvas.coords(canvas.bar, 0, 0, int(w * frac), 12)
        canvas.itemconfigure(canvas.bar, fill=color)


# ---------------------------------------------------------------- клавиатура tkinter → сочетание
_KEYSYM_MAP = {
    "prior": "page up", "next": "page down", "caps_lock": "caps lock", "scroll_lock": "scroll lock",
    "num_lock": "num lock", "return": "enter", "grave": "`", "minus": "-", "equal": "=",
    "bracketleft": "[", "bracketright": "]", "semicolon": ";", "apostrophe": "'", "comma": ",",
    "period": ".", "slash": "/", "backslash": "\\",
}
_MODIFIER_KEYSYMS = {"control_l", "control_r", "alt_l", "alt_r", "shift_l", "shift_r", "win_l", "win_r",
                     "super_l", "super_r", "meta_l", "meta_r"}


def keysym_to_combo(event) -> str | None:
    """Событие tkinter → строка вида 'ctrl+alt+m'. Backspace/Delete — очистить."""
    ks = event.keysym.lower()
    if ks in _MODIFIER_KEYSYMS:
        return None
    if ks in ("backspace", "delete", "escape"):
        return ""
    if ks.startswith("kp_"):
        rest = ks[3:]
        ks = f"num {rest}" if rest.isdigit() else None
        if ks is None:
            return None
    else:
        ks = _KEYSYM_MAP.get(ks, ks)
    # кириллическая раскладка: берём клавишу по коду
    if len(ks) > 1 and not parse_hotkey(ks) and 0x41 <= event.keycode <= 0x5A:
        ks = chr(event.keycode).lower()
    mods = []
    if event.state & 0x4:
        mods.append("ctrl")
    if event.state & (0x20000 if sys.platform == "win32" else 0x8):
        mods.append("alt")
    if event.state & 0x1:
        mods.append("shift")
    combo = "+".join(mods + [ks])
    return combo if parse_hotkey(combo) else None
