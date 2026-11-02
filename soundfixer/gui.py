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
from .presets import EDGE_VOICES, EQ_PRESETS, VOICE_PRESETS, voice_values

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
DENOISE_MODES = {
    "ai": "ИИ — нейросеть (рекомендуется)",
    "gate": "Простое — глушить всё тише порога",
    "off": "Выключено",
}
DENOISE_ENGINES = {
    "dfn_ll": "DeepFilterNet3 — лучшее качество, 10 мс",
    "dfn": "DeepFilterNet3 лёгкая — 30 мс, вдвое меньше нагрузки",
    "rnnoise": "RNNoise — самая лёгкая, 20 мс (хуже при печати)",
}
TTS_ENGINES = {
    "sapi": "Голоса Windows (без интернета)",
    "edge": "Нейроголоса Microsoft (естественные, нужен интернет)",
}


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
        self._tab_tools()
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

    def _slot(self, parent) -> ttk.Frame:
        """Строка-контейнер для виджетов, которые то показываются, то скрываются."""
        box = ttk.Frame(parent, style="Card.TFrame")
        box.grid(row=self._next_row(parent), column=0, columnspan=3, sticky="ew")
        return box

    def _check(self, parent, text, section, key, devices=False, hotkeys=False, style="TCheckbutton", cmd=None,
               sync=False, tools=False):
        var = tk.BooleanVar(value=self._get(section, key))

        def changed():
            if self._updating:
                return
            self._set(section, key, var.get())
            if cmd:
                cmd()
            self.app.settings_changed(devices=devices, hotkeys=hotkeys, sync=sync, tools=tools)
            if tools:
                self.refresh_tool(section[1] if isinstance(section, tuple) else "")

        cb = ttk.Checkbutton(parent, text=text, variable=var, command=changed, style=style)
        cb.grid(row=self._next_row(parent), column=0, columnspan=3, sticky="w", pady=2)
        self._sync_vars.append((var, lambda: self._get(section, key)))
        return var

    def _slider(self, parent, text, section, key, lo, hi, fmt=str, step=1.0, cmd=None, devices=False, sync=False,
                tools=False):
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
            self.app.settings_changed(devices=devices, sync=sync, tools=tools)

        sc = ttk.Scale(parent, from_=lo, to=hi, variable=var, command=changed)
        sc.grid(row=row, column=1, sticky="ew", pady=3)
        sc.bind("<Double-Button-1>", lambda _e: self._reset_slider(section, key, var, changed))
        show(self._get(section, key))
        self._sync_vars.append((var, lambda: self._get(section, key)))
        var.trace_add("write", lambda *_: show(round(float(var.get()) / step) * step))
        return var

    def _reset_slider(self, section, key, var, changed):
        d = config.DEFAULTS
        for part in (section if isinstance(section, tuple) else (section,) if section else ()):
            d = d[part]
        default = d[key]
        var.set(default)
        changed()

    def _section(self, section):
        if not section:
            return self.cfg
        if isinstance(section, tuple):  # вложенный раздел, например ("tools", "ping")
            d = self.cfg
            for part in section:
                d = d[part]
            return d
        return self.cfg[section]

    def _get(self, section, key):
        return self._section(section)[key]

    def _set(self, section, key, value):
        self._section(section)[key] = value

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
            self.app.settings_changed(hotkeys=hotkeys, tools=isinstance(section, tuple))
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
        self.mic_enabled = self._check(card, "Включить (шумодав, громкость > 100%, эффекты, озвучка)",
                                       "mic", "enabled", devices=True, style="Big.TCheckbutton")
        self.mic_status = self._status_label(card)
        self.mic_meter = self._meter(card, "Уровень входа")
        self.mute_btn = ttk.Button(card, text="Выключить микрофон", command=self.app.toggle_mute)
        self.mute_btn.grid(row=self._next_row(card), column=0, sticky="w", pady=4)

        # ---------- шумоподавление
        card = self.dn_card = self._card(page, "Шумоподавление")
        ttk.Label(card, text="Режим").grid(row=0, column=0, sticky="w")
        self.dn_var = tk.StringVar()
        box = ttk.Combobox(card, textvariable=self.dn_var, state="readonly", values=list(DENOISE_MODES.values()))
        box.grid(row=0, column=1, columnspan=2, sticky="ew", pady=3)
        box.bind("<<ComboboxSelected>>", lambda _e: self._denoise_picked())
        ttk.Label(card, text="Нейросеть").grid(row=1, column=0, sticky="w")
        self.dn_engine_var = tk.StringVar()
        box = ttk.Combobox(card, textvariable=self.dn_engine_var, state="readonly",
                           values=list(DENOISE_ENGINES.values()))
        box.grid(row=1, column=1, columnspan=2, sticky="ew", pady=3)
        box.bind("<<ComboboxSelected>>", lambda _e: self._denoise_engine_picked())
        self.dn_status = self._status_label(card)
        box = self._slot(card)
        self.dn_install = ttk.Button(box, text="Установить RNNoise (pyrnnoise, ~14 МБ)",
                                     style="Accent.TButton", command=self._install_denoise)
        self.dn_strength = self._slider(card, "Сила подавления", "mic", "denoise_strength", 0, 100,
                                        lambda v: f"{v:.0f}%", step=5)
        self.vad_meter = self._meter(card, "Голос (по мнению ИИ)")
        self._slider(card, "Порог (простой режим)", "mic", "gate_db", -80, -20, lambda v: f"{v:.0f} дБ")

        sub = ttk.Frame(card, style="Card.TFrame")
        sub.grid(row=self._next_row(card), column=0, columnspan=3, sticky="ew", pady=(8, 2))
        self.learn_btn = ttk.Button(sub, text="Запомнить мой шум (10 с)", command=self._learn_start)
        self.learn_btn.pack(side="left")
        self.learn_cancel = ttk.Button(sub, text="Отмена", command=self._learn_cancel)
        ttk.Button(sub, text="Забыть", command=self._learn_forget).pack(side="right")
        self.learn_bar = ttk.Progressbar(self._slot(card), mode="determinate", maximum=1.0)
        self.learn_lbl = self._status_label(card)
        self._check(card, "Использовать запомненный шум", "mic", "noise_learn")
        self._hint(card, "DeepFilterNet3 оставляет голос и убирает клавиатуру, клики, вентилятор, улицу — "
                         "и не «рвёт» речь, когда вы говорите и печатаете одновременно (с этим плохо "
                         "справляется RNNoise). «Запомнить мой шум»: 10 секунд молчите и шумите как обычно — "
                         "порог гейта поднимется ровно выше вашего шума, и в паузах будет полная тишина. "
                         "Профиль записывается для выбранной нейросети.")

        card = self._card(page, "Громкость и чистота")
        self._slider(card, "Громкость", "mic", "gain", 0, 1000, pct_label, step=5)
        self._check(card, "Убирать низкий гул (фильтр < 90 Гц)", "mic", "hpf")
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
        self._check(card, "Выводить свой голос в наушники (проверка эффектов и шумодава)", "mic", "monitor",
                    devices=True)
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

    def _denoise_engine_picked(self):
        label = self.dn_engine_var.get()
        self.cfg["mic"]["denoise_engine"] = next((k for k, v in DENOISE_ENGINES.items() if v == label), "dfn_ll")
        self.app.settings_changed(devices=True)
        self.refresh_status()

    def _denoise_picked(self):
        label = self.dn_var.get()
        self.cfg["mic"]["denoise"] = next((k for k, v in DENOISE_MODES.items() if v == label), "ai")
        self.app.settings_changed(devices=True)
        self.refresh_status()

    def _install_denoise(self):
        self.dn_install.configure(state="disabled", text="Устанавливаю… (до минуты)")

        def done(ok, out):
            self.dn_install.configure(state="normal", text="Установить RNNoise (pyrnnoise, ~14 МБ)")
            if ok:
                self.app.report("denoise", "Установлено, запускаю нейросеть…", None)
                self.app.reload_denoise()
            else:
                self.app.report("denoise", "Не удалось установить: " + out.strip().splitlines()[-1][:200]
                                if out.strip() else "Не удалось установить", False)

        self.app.install_package(["--no-deps", "pyrnnoise"], done)

    # ---------- обучение на шуме
    def _learn_start(self):
        err = self.app.start_noise_learning()
        if err:
            self.learn_lbl.configure(text=err, foreground=WARN)
            return
        self.learn_btn.configure(state="disabled")
        self.learn_cancel.pack(side="left", padx=6)
        self.learn_bar.pack(fill="x", pady=4)
        self.learn_bar["value"] = 0
        self.learn_lbl.configure(text="Слушаю… Молчите и шумите как обычно: печатайте, кликайте мышью, "
                                      "двигайтесь, скрипите стулом.", foreground=ACCENT)
        self.root.after(100, self._learn_poll)

    def _learn_poll(self):
        p = self.app.learning_progress()
        if p is None:
            self._learn_ui_done()
            return
        self.learn_bar["value"] = p
        if p >= 1.0 and self.app.finish_learning(self._learn_result):
            self.learn_lbl.configure(text="Анализирую…", foreground=ACCENT)
            return
        self.root.after(100, self._learn_poll)

    def _learn_result(self, data, text):
        self._learn_ui_done()
        if data is None:
            self.learn_lbl.configure(text=f"Не получилось: {text}", foreground=WARN)
            return
        self.cfg["mic"]["noise_profile"] = data
        self.cfg["mic"]["noise_learn"] = True
        self.app.settings_changed()
        self._refresh_vars()
        self.learn_lbl.configure(text="✔ " + text, foreground=GOOD)

    def _learn_cancel(self):
        self.app.cancel_learning()
        self._learn_ui_done()
        self.learn_lbl.configure(text="Обучение отменено", foreground=MUTED_FG)

    def _learn_ui_done(self):
        self.learn_btn.configure(state="normal")
        self.learn_cancel.pack_forget()
        self.learn_bar.pack_forget()

    def _learn_forget(self):
        self.cfg["mic"]["noise_profile"] = None
        self.app.settings_changed()
        self.learn_lbl.configure(text="Запомненный шум удалён", foreground=MUTED_FG)

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
        page = self._page("Озвучка")
        card = self._card(page, "Состояние")
        self.route_lbl = self._status_label(card)
        self.speech_status = self._status_label(card)

        card = self._card(page, "Озвучка текста")
        self.tts_text = tk.Text(card, height=3, wrap="word", bg=FIELD, fg=FG, insertbackground=FG,
                                relief="flat", font=("Segoe UI", 10), padx=6, pady=4,
                                highlightthickness=1, highlightbackground=FIELD, highlightcolor=ACCENT)
        self.tts_text.grid(row=0, column=0, columnspan=3, sticky="ew", pady=3)
        self.tts_text.bind("<Control-Return>", lambda _e: (self._speak(), "break")[1])
        self.tts_text.bind("<Return>", lambda _e: (self._speak(), "break")[1])
        self.tts_text.bind("<Shift-Return>", lambda _e: None)
        row = ttk.Frame(card, style="Card.TFrame")
        row.grid(row=1, column=0, columnspan=3, sticky="ew")
        ttk.Button(row, text="Сказать  (Enter)", style="Accent.TButton", command=self._speak).pack(side="left")
        ttk.Button(row, text="Повторить последнее", command=self.app.repeat_last).pack(side="left", padx=6)
        ttk.Button(row, text="Стоп", command=self.app.stop_sounds).pack(side="left")

        ttk.Label(card, text="Движок").grid(row=2, column=0, sticky="w", pady=(8, 0))
        self.engine_var = tk.StringVar()
        eng_box = ttk.Combobox(card, textvariable=self.engine_var, state="readonly", values=list(TTS_ENGINES.values()))
        eng_box.grid(row=2, column=1, columnspan=2, sticky="ew", pady=(8, 3))
        eng_box.bind("<<ComboboxSelected>>", lambda _e: self._engine_picked())
        ttk.Label(card, text="Голос").grid(row=3, column=0, sticky="w")
        vrow = ttk.Frame(card, style="Card.TFrame")
        vrow.grid(row=3, column=1, columnspan=2, sticky="ew", pady=3)
        vrow.columnconfigure(0, weight=1)
        self.voice_var = tk.StringVar()
        self.voice_box = ttk.Combobox(vrow, textvariable=self.voice_var, state="readonly")
        self.voice_box.grid(row=0, column=0, sticky="ew")
        self.voice_box.bind("<<ComboboxSelected>>", lambda _e: self._voice_picked())
        self.voices_btn = ttk.Button(vrow, text="Обновить", command=self._load_voices)
        self.voices_btn.grid(row=0, column=1, padx=(6, 0))
        self.edge_install = ttk.Button(self._slot(card), text="Установить нейроголоса (edge-tts)",
                                       style="Accent.TButton", command=self._install_edge)
        self._slider(card, "Скорость", "tts", "rate", -10, 10, lambda v: f"{v:+.0f}")
        self._slider(card, "Высота", "tts", "pitch", -10, 10, lambda v: f"{v:+.0f}")
        self._slider(card, "Громкость", "tts", "volume", 0, 200, lambda v: f"{v:.0f}%", step=5)
        self._check(card, "Пропускать через эффекты голоса (робот, эхо…)", "tts", "through_fx")
        self._check(card, "Слышать озвучку в наушниках", "tts", "monitor")
        self._hotkey_entry(card, "Повторить последнее", "tts", "repeat_hotkey")

        ttk.Label(card, text="Недавние фразы (двойной клик — сказать ещё раз)").grid(
            row=self._next_row(card), column=0, columnspan=3, sticky="w", pady=(8, 0))
        self.history = tk.Listbox(card, height=5, bg=FIELD, fg=FG, selectbackground=ACCENT, relief="flat",
                                  highlightthickness=0, activestyle="none")
        self.history.grid(row=self._next_row(card), column=0, columnspan=3, sticky="ew", pady=3)
        self.history.bind("<Double-Button-1>", lambda _e: self._replay())
        hrow = ttk.Frame(card, style="Card.TFrame")
        hrow.grid(row=self._next_row(card), column=0, columnspan=3, sticky="w")
        ttk.Button(hrow, text="В быстрые фразы", command=self._history_to_board).pack(side="left")
        ttk.Button(hrow, text="Очистить историю", command=self._clear_history).pack(side="left", padx=6)

        card = self._card(page, "Саундборд и быстрые фразы")
        self.sounds_list = tk.Listbox(card, height=7, bg=FIELD, fg=FG, selectbackground=ACCENT, relief="flat",
                                      highlightthickness=0, activestyle="none")
        self.sounds_list.grid(row=0, column=0, columnspan=3, sticky="ew", pady=3)
        self.sounds_list.bind("<Double-Button-1>", lambda _e: self._play_selected())
        row = ttk.Frame(card, style="Card.TFrame")
        row.grid(row=1, column=0, columnspan=3, sticky="ew")
        ttk.Button(row, text="▶ Играть", style="Accent.TButton", command=self._play_selected).pack(side="left")
        ttk.Button(row, text="+ Звук…", command=self._add_sounds).pack(side="left", padx=(6, 0))
        ttk.Button(row, text="+ Фраза…", command=self._add_phrase).pack(side="left", padx=(6, 0))
        ttk.Button(row, text="Клавиша…", command=self._sound_hotkey).pack(side="left", padx=(6, 0))
        ttk.Button(row, text="Имя…", command=self._rename_sound).pack(side="left", padx=(6, 0))
        ttk.Button(row, text="Удалить", command=self._remove_sound).pack(side="left", padx=(6, 0))
        self._slider(card, "Громкость", "soundboard", "volume", 0, 200, lambda v: f"{v:.0f}%", step=5)
        self._check(card, "Слышать звуки в наушниках", "soundboard", "monitor")
        self._check(card, "Повторное нажатие перезапускает звук (не накладывать)", "soundboard", "exclusive")
        self._hotkey_entry(card, "Остановить все звуки", "soundboard", "stop_hotkey")
        self._hint(card, "Звуки: mp3, wav, ogg, flac — загружаются в память один раз и играют мгновенно. "
                         "Быстрые фразы озвучиваются выбранным голосом и тоже запоминаются.")

    def _speak(self):
        text = self.tts_text.get("1.0", "end").strip()
        if text:
            self.app.speak(text)
            self.tts_text.delete("1.0", "end")
            self._refresh_lists()

    def _replay(self):
        sel = self.history.curselection()
        if sel:
            self.app.speak(self.cfg["tts"]["history"][sel[0]])
            self._refresh_lists()

    def _history_to_board(self):
        sel = self.history.curselection()
        if not sel:
            messagebox.showinfo(APP_NAME, "Выберите фразу в списке недавних.", parent=self.root)
            return
        text = self.cfg["tts"]["history"][sel[0]]
        self.cfg["soundboard"]["sounds"].append({"text": text, "name": text[:40], "hotkey": ""})
        self.app.settings_changed()
        self._refresh_lists()

    def _clear_history(self):
        self.cfg["tts"]["history"] = []
        self.app.settings_changed()
        self._refresh_lists()

    # ---------- голоса
    def _engine_picked(self):
        label = self.engine_var.get()
        self.cfg["tts"]["engine"] = next((k for k, v in TTS_ENGINES.items() if v == label), "sapi")
        self.app.settings_changed()
        self._fill_voices()

    def _fill_voices(self, items=None):
        tts = self.cfg["tts"]
        if tts["engine"] == "edge":
            self._voice_items = items or getattr(self, "_edge_items", None) or list(EDGE_VOICES)
            self._edge_items = self._voice_items
            current = tts["edge_voice"]
            if current not in dict(self._voice_items):
                self._voice_items = [(current, current)] + self._voice_items
            self.voices_btn.configure(text="Все голоса")
        else:
            self._voice_items = [("", "по умолчанию")] + (items or getattr(self, "_sapi_items", []))
            if items:
                self._sapi_items = items
            current = tts["voice"]
            if current and current not in dict(self._voice_items):
                self._voice_items.append((current, current))
            self.voices_btn.configure(text="Обновить")
        labels = dict(self._voice_items)
        self.voice_box.configure(values=[lbl for _, lbl in self._voice_items])
        self.voice_var.set(labels.get(current, current or "по умолчанию"))
        need_edge = tts["engine"] == "edge" and not self.app.edge_available()
        if need_edge:
            self.edge_install.pack(anchor="w", pady=4)
        else:
            self.edge_install.pack_forget()

    def _load_voices(self):
        engine = self.cfg["tts"]["engine"]
        self.voices_btn.configure(state="disabled")

        def done(items, err):
            self.voices_btn.configure(state="normal")
            if items is None:
                self.app.report("speech", f"Голоса: {err}", False)
                return
            if engine == self.cfg["tts"]["engine"]:
                self._fill_voices(items)
                self.app.report("speech", f"Голосов найдено: {len(items)}", True)

        self.app.list_voices(engine, done)

    def _voice_picked(self):
        label = self.voice_var.get()
        vid = next((i for i, lbl in self._voice_items if lbl == label), "")
        key = "edge_voice" if self.cfg["tts"]["engine"] == "edge" else "voice"
        self.cfg["tts"][key] = vid
        self.app.settings_changed()

    def _install_edge(self):
        self.edge_install.configure(state="disabled", text="Устанавливаю…")

        def done(ok, out):
            self.edge_install.configure(state="normal", text="Установить нейроголоса (edge-tts)")
            if ok:
                self.app.report("speech", "Нейроголоса установлены", True)
            else:
                self.app.report("speech", "Не удалось установить edge-tts: "
                                + (out.strip().splitlines()[-1][:200] if out.strip() else ""), False)
            self._fill_voices()

        self.app.install_package(["edge-tts"], done)

    # ---------- саундборд
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

    def _add_phrase(self):
        text = simpledialog.askstring(APP_NAME, "Текст быстрой фразы (будет озвучен выбранным голосом):",
                                      parent=self.root)
        if text and text.strip():
            text = text.strip()
            self.cfg["soundboard"]["sounds"].append({"text": text, "name": text[:40], "hotkey": ""})
            self.app.settings_changed()
            self._refresh_lists()

    def _selected_sound(self):
        sel = self.sounds_list.curselection()
        return sel[0] if sel else None

    def _play_selected(self):
        i = self._selected_sound()
        if i is None:
            if self.cfg["soundboard"]["sounds"]:
                self.app.report("speech", "Выберите звук в списке", None)
            return
        self.app.play_sound(i)

    def _remove_sound(self):
        i = self._selected_sound()
        if i is not None:
            del self.cfg["soundboard"]["sounds"][i]
            self.app.settings_changed(hotkeys=True)
            self._refresh_lists()

    def _rename_sound(self):
        i = self._selected_sound()
        if i is None:
            return
        snd = self.cfg["soundboard"]["sounds"][i]
        name = simpledialog.askstring(APP_NAME, "Название:", initialvalue=snd.get("name", ""), parent=self.root)
        if name and name.strip():
            snd["name"] = name.strip()
            self.app.settings_changed()
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

    # ================================================================ вкладка «Инструменты»
    def _tab_tools(self):
        page = self._page("Инструменты")
        self.tool_status: dict[str, ttk.Label] = {}
        self._hint(self._card(page, "Мультитул"),
                   "Каждый инструмент включается отдельно. Выключенный не загружается и не тратит ни память, "
                   "ни процессор. Горячие клавиши задаются кликом по полю и нажатием сочетания.", row=0)

        card = self._card(page, "Исправление раскладки")
        self._check(card, "Включить", ("tools", "layout"), "enabled", tools=True, style="Big.TCheckbutton")
        self._hotkey_entry(card, "Клавиша", ("tools", "layout"), "hotkey")
        self.tool_status["layout"] = self._status_label(card)
        self._hint(card, "Набрали «ghbdtn» вместо «привет»? Нажмите клавишу — выделенный текст (или слово перед "
                         "курсором) исправится, а раскладка переключится сама. Работает в обе стороны.")

        card = self._card(page, "Игры")
        self._check(card, "Включить", ("tools", "games"), "enabled", tools=True, style="Big.TCheckbutton")
        self._check(card, "Клавиша Win не сворачивает игру", ("tools", "games"), "block_win", tools=True)
        self._check(card, "Не показывать «Залипание клавиш» (5× Shift) и похожие окна", ("tools", "games"),
                    "no_sticky", tools=True)
        self._check(card, "В игре включать английскую раскладку", ("tools", "games"), "english", tools=True)
        self._check(card, "Считать игрой любое полноэкранное окно (кроме браузеров и плееров)",
                    ("tools", "games"), "fullscreen", tools=True)
        ttk.Label(card, text="Мои игры (если не распознаются сами)").grid(
            row=self._next_row(card), column=0, columnspan=3, sticky="w", pady=(6, 0))
        self.games_list = tk.Listbox(card, height=4, bg=FIELD, fg=FG, selectbackground=ACCENT, relief="flat",
                                     highlightthickness=0, activestyle="none")
        self.games_list.grid(row=self._next_row(card), column=0, columnspan=3, sticky="ew", pady=3)
        row = ttk.Frame(card, style="Card.TFrame")
        row.grid(row=self._next_row(card), column=0, columnspan=3, sticky="w")
        ttk.Button(row, text="+ Из запущенных…", command=self._game_add_running).pack(side="left")
        ttk.Button(row, text="+ Файл…", command=self._game_add_file).pack(side="left", padx=6)
        ttk.Button(row, text="Удалить", command=self._game_remove).pack(side="left")
        self.tool_status["games"] = self._status_label(card)
        self._hint(card, "Игры из Steam, Epic, GOG, Riot, Battle.net, Xbox, Ubisoft, EA и папок «Games» "
                         "распознаются автоматически.")

        card = self._card(page, "Интернет")
        self._check(card, "Пинг в трее и предупреждение о потерях", ("tools", "ping"), "enabled", tools=True,
                    style="Big.TCheckbutton")
        row_n = self._next_row(card)
        ttk.Label(card, text="Адрес").grid(row=row_n, column=0, sticky="w")
        self.ping_host = tk.StringVar(value=self.cfg["tools"]["ping"]["host"])
        ent = ttk.Entry(card, textvariable=self.ping_host, width=24)
        ent.grid(row=row_n, column=1, sticky="w", pady=3)
        ttk.Label(card, text="1.1.1.1, 8.8.8.8 или сервер игры", style="Hint.TLabel").grid(
            row=row_n, column=2, sticky="e")
        ent.bind("<FocusOut>", lambda _e: self._ping_host_changed())
        ent.bind("<Return>", lambda _e: self._ping_host_changed())
        self._slider(card, "Предупреждать, если пинг выше", ("tools", "ping"), "warn_ms", 30, 500,
                     lambda v: f"{v:.0f} мс", step=10, tools=True)
        self._slider(card, "…или потери выше", ("tools", "ping"), "warn_loss", 1, 50,
                     lambda v: f"{v:.0f}%", tools=True)
        self.tool_status["ping"] = self._status_label(card)

        sub = ttk.Labelframe(card, text=" Выключить после загрузки ", padding=(8, 4))
        sub.grid(row=self._next_row(card), column=0, columnspan=3, sticky="ew", pady=(8, 2))
        sub.columnconfigure(1, weight=1)
        ttk.Label(sub, text="Действие").grid(row=0, column=0, sticky="w")
        self.dl_action = tk.StringVar()
        self._dl_actions = {"sleep": "Сон", "hibernate": "Гибернация", "shutdown": "Выключение"}
        box = ttk.Combobox(sub, textvariable=self.dl_action, state="readonly", values=list(self._dl_actions.values()),
                           width=16)
        box.grid(row=0, column=1, sticky="w", pady=3)
        box.bind("<<ComboboxSelected>>", lambda _e: self._dl_action_picked())
        self._slider(sub, "Загрузка закончилась, когда скорость ниже", ("tools", "download"), "kbps", 50, 5000,
                     lambda v: f"{v:.0f} КБ/с", step=50)
        self._slider(sub, "…в течение", ("tools", "download"), "minutes", 1, 15, lambda v: f"{v:.0f} мин")
        self.dl_btn = ttk.Button(sub, text="Запустить ожидание", style="Accent.TButton", command=self._dl_toggle)
        self.dl_btn.grid(row=self._next_row(sub), column=0, sticky="w", pady=4)
        self.tool_status["download"] = self._status_label(sub)

        card = self._card(page, "Окна")
        self._check(card, "Включить", ("tools", "windows"), "enabled", tools=True, style="Big.TCheckbutton")
        self._hotkey_entry(card, "Поверх всех", ("tools", "windows"), "topmost_hotkey")
        self._hotkey_entry(card, "Прозрачнее", ("tools", "windows"), "fade_more")
        self._hotkey_entry(card, "Плотнее", ("tools", "windows"), "fade_less")
        self.tool_status["windows"] = self._status_label(card)
        self._hint(card, "Действует на активное окно: закрепите видео или чат поверх игры и сделайте "
                         "полупрозрачным. Окна, запущенные от администратора, Windows менять не даёт.")

        card = self._card(page, "Не засыпать")
        self._check(card, "Компьютер не уходит в сон (загрузки, фильмы, презентации)", ("tools", "awake"),
                    "enabled", tools=True, style="Big.TCheckbutton")
        self._check(card, "И экран не гаснет", ("tools", "awake"), "display", tools=True)
        self.tool_status["awake"] = self._status_label(card)

    def _ping_host_changed(self):
        host = self.ping_host.get().strip()
        if host and host != self.cfg["tools"]["ping"]["host"]:
            self.cfg["tools"]["ping"]["host"] = host
            self.app.settings_changed(tools=True)

    def _dl_action_picked(self):
        label = self.dl_action.get()
        self.cfg["tools"]["download"]["action"] = next((k for k, v in self._dl_actions.items() if v == label),
                                                       "sleep")
        self.app.settings_changed()

    def _dl_toggle(self):
        if self.app.download_active:
            self.app.cancel_download_watch()
        else:
            self.app.start_download_watch()
        self.refresh_tool("download")

    def _game_add_running(self):
        from .tools import winapi

        progs = winapi.visible_programs()
        if not progs:
            messagebox.showinfo(APP_NAME, "Не нашёл запущенных программ с окнами.", parent=self.root)
            return
        win = tk.Toplevel(self.root)
        win.title("Выберите игру")
        win.configure(bg=BG)
        win.geometry("520x360")
        lb = tk.Listbox(win, bg=FIELD, fg=FG, selectbackground=ACCENT, relief="flat", highlightthickness=0)
        lb.pack(fill="both", expand=True, padx=8, pady=8)
        for exe, title in progs:
            lb.insert("end", f"{exe}   —   {title[:50]}")

        def add():
            sel = lb.curselection()
            if sel:
                exe = progs[sel[0]][0]
                games = self.cfg["tools"]["games"]["list"]
                if exe not in games:
                    games.append(exe)
                    self.app.settings_changed(tools=True)
                    self._refresh_games()
            win.destroy()

        lb.bind("<Double-Button-1>", lambda _e: add())
        ttk.Button(win, text="Добавить", style="Accent.TButton", command=add).pack(pady=(0, 8))

    def _game_add_file(self):
        path = filedialog.askopenfilename(parent=self.root, title="Исполняемый файл игры",
                                          filetypes=[("Программы", "*.exe"), ("Все файлы", "*.*")])
        if path:
            exe = os.path.basename(path).lower()
            games = self.cfg["tools"]["games"]["list"]
            if exe not in games:
                games.append(exe)
                self.app.settings_changed(tools=True)
                self._refresh_games()

    def _game_remove(self):
        sel = self.games_list.curselection()
        if sel:
            del self.cfg["tools"]["games"]["list"][sel[0]]
            self.app.settings_changed(tools=True)
            self._refresh_games()

    def _refresh_games(self):
        self.games_list.delete(0, "end")
        for exe in self.cfg["tools"]["games"]["list"]:
            self.games_list.insert("end", exe)

    def refresh_tool(self, key: str):
        """Обновить строку состояния инструмента (вызывается по событию инструмента)."""
        lbl = self.tool_status.get(key)
        if lbl is None:
            return
        if key == "download":
            d = self.app.tools.download
            active = self.app.download_active
            self.dl_btn.configure(text="Отменить" if active else "Запустить ожидание")
            text = d.status() if d is not None else ""
            lbl.configure(text=text, foreground=WARN if d is not None and d.state == "countdown" else
                          (GOOD if active else MUTED_FG))
            return
        tool = self.app.tools.get(key)
        text = tool.status() if tool is not None and tool.running else ""
        enabled = self.cfg["tools"].get(key, {}).get("enabled")
        if enabled and not text:
            text = "Работает" if key != "ping" else "Замеряю…"
        bad = any(w in text for w in ("нет ответа", "не удалось", "Ошибка"))
        lbl.configure(text=text if enabled else "Выключено",
                      foreground=(WARN if bad else GOOD) if enabled else MUTED_FG)

    # ================================================================ вкладка «Настройки»
    def _tab_settings(self):
        page = self._page("Настройки")
        card = self._card(page, "Проверка звука")
        self._hint(card, "Если что-то не звучит — запустите проверку: она проверит устройства, вывод в "
                         "наушники (короткий писк), микрофон, нейросеть шумоподавления и голоса озвучки.", row=0)
        self.diag_btn = ttk.Button(card, text="Проверить всё", style="Accent.TButton", command=self._diagnostics)
        self.diag_btn.grid(row=1, column=0, sticky="w", pady=4)

        card = self._card(page, "Виртуальные кабели")
        self._hint(card, "Чтобы собеседники слышали обработанный голос, озвучку и звуки, а звук системы можно "
                         "было усилить выше 100%, нужен виртуальный аудиокабель (бесплатный VB-Audio Virtual "
                         "Cable). Для микрофона и наушников одновременно нужны два кабеля (например, VB-Cable и "
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
        self._blocks = {"480 (10 мс, рекомендуется)": 480, "960 (20 мс, для слабых ПК)": 960}
        box = ttk.Combobox(card, textvariable=self.block_var, state="readonly", values=list(self._blocks))
        box.grid(row=0, column=1, columnspan=2, sticky="ew", pady=3)
        box.bind("<<ComboboxSelected>>", lambda _e: self._block_picked())
        self._hint(card, "Обработка звука включается только по требованию: без неё программа не загружает "
                         "звуковой движок вообще и почти не занимает памяти и CPU. Выход в наушники для "
                         "озвучки и звуков открывается при воспроизведении и закрывается после простоя.")

        card = self._card(page, "О программе")
        ttk.Label(card, text=f"{APP_NAME} {__version__}").grid(row=0, column=0, sticky="w")
        row = ttk.Frame(card, style="Card.TFrame")
        row.grid(row=1, column=0, columnspan=3, sticky="w", pady=4)
        ttk.Button(row, text="Папка настроек и логов",
                   command=lambda: os.startfile(config.CONFIG_DIR) if hasattr(os, "startfile") else None).pack(
            side="left")
        ttk.Button(row, text="Выход из программы", command=self.app.quit).pack(side="left", padx=6)

    def _diagnostics(self):
        self.diag_btn.configure(state="disabled", text="Проверяю… (несколько секунд)")

        def done(text):
            self.diag_btn.configure(state="normal", text="Проверить всё")
            win = tk.Toplevel(self.root)
            win.title("Проверка звука — SoundFixer")
            win.configure(bg=BG)
            win.geometry("620x420")
            txt = tk.Text(win, wrap="word", bg=FIELD, fg=FG, relief="flat", font=("Segoe UI", 10), padx=10, pady=8)
            txt.pack(fill="both", expand=True, padx=8, pady=(8, 4))
            txt.insert("1.0", text)
            txt.tag_configure("bad", foreground=BAD)
            txt.tag_configure("good", foreground=GOOD)
            for i, line in enumerate(text.splitlines(), start=1):
                if line.startswith("✖"):
                    txt.tag_add("bad", f"{i}.0", f"{i}.end")
                elif line.startswith("✔"):
                    txt.tag_add("good", f"{i}.0", f"{i}.1")
            txt.configure(state="disabled")
            row = ttk.Frame(win)
            row.pack(fill="x", padx=8, pady=(0, 8))

            def copy_text():
                self.root.clipboard_clear()
                self.root.clipboard_append(text)

            ttk.Button(row, text="Копировать", command=copy_text).pack(side="left")
            ttk.Button(row, text="Закрыть", command=win.destroy).pack(side="right")

        self.app.run_diagnostics(done)

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
        self._refresh_games()
        self.ping_host.set(self.cfg["tools"]["ping"]["host"])
        self.dl_action.set(self._dl_actions.get(self.cfg["tools"]["download"]["action"], "Сон"))
        for key in self.tool_status:
            self.refresh_tool(key)
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
                                    list(self._blocks)[0]))
            self.dn_var.set(DENOISE_MODES.get(self.cfg["mic"]["denoise"], DENOISE_MODES["ai"]))
            self.dn_engine_var.set(DENOISE_ENGINES.get(self.cfg["mic"]["denoise_engine"],
                                                       DENOISE_ENGINES["dfn_ll"]))
            self.engine_var.set(TTS_ENGINES.get(self.cfg["tts"]["engine"], TTS_ENGINES["sapi"]))
            self._fill_voices()
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
            if s.get("text"):
                self.sounds_list.insert("end", f"💬 {s.get('name') or s['text'][:40]}{hk}")
            else:
                missing = "" if os.path.exists(s["path"]) else "   (файл не найден)"
                self.sounds_list.insert("end", f"♪ {s.get('name') or os.path.basename(s['path'])}{hk}{missing}")
        prof = self.cfg["mic"]["noise_profile"]
        if prof and not self.learn_lbl.cget("text"):
            import time as _t

            when = _t.strftime("%d.%m.%Y %H:%M", _t.localtime(prof.get("created", 0)))
            self.learn_lbl.configure(text=f"Запомнен {when}. {prof.get('summary', '')}", foreground=MUTED_FG)

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
        self._refresh_denoise_status()
        self._refresh_speech_status()
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

    def _refresh_denoise_status(self):
        mic = self.cfg["mic"]
        mode = mic["denoise"]
        eng = self.app.engine
        ok = self.app.status_ok.get("denoise")
        show_install = False
        if mode == "ai":
            dn = eng.mic_chain.denoiser if eng is not None else None
            note = self.app.status.get("denoise") or ""
            info = self.app.denoise_info
            if ok is False and dn is None:
                text, color = note or "ИИ-шумоподавление недоступно", BAD
                show_install = mic["denoise_engine"] == "rnnoise" and not getattr(sys, "frozen", False)
                if mic["enabled"]:
                    text += " — пока работает простой шумодав"
            elif not mic["enabled"]:
                text, color = "Нейросеть включится вместе с обработкой микрофона", MUTED_FG
            elif dn is not None and info is not None:
                prof_data = mic["noise_profile"] if mic["noise_learn"] else None
                prof = ""
                if prof_data:
                    used = "dfn" if dn.kind.startswith("dfn") else "rnnoise"
                    prof = (" • учитывает ваш шум" if prof_data.get("backend", "rnnoise") == used
                            else " • профиль шума записан для другой нейросети — запишите заново")
                text = (f"{info[0]} работает • задержка {info[1]} мс • обработка {eng.proc_ms:.1f} мс "
                        f"на блок 10 мс{prof}")
                if eng.underruns:
                    text += f"\nЗвук не успевал {eng.underruns} раз — запас буфера увеличен до {eng.buffer_ms:.0f} мс"
                if note:
                    text = note + "\n" + text
                color = WARN if note else GOOD
            else:
                text, color = note or "Запуск нейросети…", MUTED_FG
        elif mode == "gate":
            text, color = "Простой шумодав: глушит всё, что тише порога (без ИИ)", MUTED_FG
        else:
            text, color = "Шумоподавление выключено", MUTED_FG
        self.dn_status.configure(text=text, foreground=color)
        if show_install:
            self.dn_install.pack(anchor="w", pady=4)
        else:
            self.dn_install.pack_forget()

    def _refresh_speech_status(self):
        eng = self.app.engine
        mic_on = eng is not None and eng.mic_running
        cable = eng is not None and eng.cable_running
        if mic_on and cable:
            route, color = ("Озвучку и звуки слышат собеседники (через виртуальный микрофон) и вы — "
                            "если включено «Слышать…»."), GOOD
        elif mic_on:
            route, color = ("Озвучку и звуки слышите только вы: виртуальный кабель не выбран "
                            "(вкладка «Настройки»)."), WARN
        else:
            route, color = ("Озвучку и звуки слышите вы в наушниках. Чтобы их слышали собеседники — включите "
                            "«Обработку микрофона» на вкладке «Микрофон» и виртуальный кабель."), WARN
        self.route_lbl.configure(text=route, foreground=color)
        text = self.app.status.get("speech", "")
        ok = self.app.status_ok.get("speech")
        self.speech_status.configure(text=text, foreground=GOOD if ok else (BAD if ok is False else FG))

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
            dn = eng.mic_chain.denoiser if eng.mic_running else None
            self._draw_vad(dn.vad if dn is not None else 0.0, dn is not None and dn.open)
            self._ticks = getattr(self, "_ticks", 0) + 1
            if self._ticks % 10 == 0:
                self._refresh_denoise_status()
        else:
            self._draw_meter(self.spk_meter, eng.spk_level if eng.spk_running else 0.0)
        self._meter_job = self.root.after(50, self._tick)

    def _draw_vad(self, vad: float, open_: bool):
        c = self.vad_meter
        w = c.winfo_width()
        c.coords(c.bar, 0, 0, int(w * max(0.0, min(1.0, vad))), 12)
        c.itemconfigure(c.bar, fill=GOOD if open_ else MUTED_FG)

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
