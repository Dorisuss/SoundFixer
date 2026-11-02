"""Хранение настроек: JSON в %APPDATA%\\SoundFixer, атомарная запись, миграция со старой версии."""

from __future__ import annotations

import copy
import json
import logging
import os
import threading
from pathlib import Path

from . import APP_NAME

log = logging.getLogger(__name__)

CONFIG_DIR = Path(os.environ.get("APPDATA") or Path.home()) / APP_NAME
CONFIG_FILE = CONFIG_DIR / "config.json"
LOG_FILE = CONFIG_DIR / "soundfixer.log"

DEFAULT_PROFILE = "Основной"
EQ_BANDS = (60, 230, 910, 3600, 12000)  # Гц

DEFAULTS: dict = {
    "version": 3,
    "active_profile": DEFAULT_PROFILE,
    "profiles": {DEFAULT_PROFILE: {"input_id": None, "output_id": None}},
    # Следить за устройствами по умолчанию и возвращать их (подключение гарнитуры,
    # программы, меняющие устройство и т.д.)
    "enforce_defaults": True,
    # Виртуальные кабели (имя playback-устройства, например "CABLE Input (VB-Audio Virtual Cable)")
    "virtual_mic": "",
    "virtual_spk": "",
    "block_size": 480,  # сэмплов при 48 кГц: 480 = 10 мс (кадр нейросети шумоподавления)
    "notifications": True,
    "mic": {
        "enabled": False,          # обработка микрофона (через виртуальный кабель)
        "set_virtual_default": True,  # делать виртуальный микрофон устройством Windows по умолчанию
        "gain": 100,               # % (100 = без изменений, до 1000)
        "hpf": True,               # срез низкого гула
        "denoise": "ai",           # off | gate (простой порог) | ai (нейросеть + умный гейт)
        "denoise_engine": "dfn_ll",  # dfn_ll — DeepFilterNet3 10 мс | dfn — лёгкая 30 мс | rnnoise
        "denoise_strength": 100,   # сила ИИ-шумоподавления, %
        "noise_learn": True,       # использовать запомненный профиль шума
        "noise_profile": None,     # что программа запомнила о вашем окружении
        "gate_db": -50,            # порог простого шумодава
        "compressor": True,
        "preset": "normal",
        "pitch": 0.0,              # полутона
        "robot": 0,                # 0..100
        "distortion": 0,
        "chorus": 0,
        "echo": 0,
        "reverb": 0,
        "radio": False,
        "monitor": False,          # слышать себя
        "monitor_volume": 60,
        "mute_hotkey": "ctrl+alt+m",
        "ptt": False,
        "ptt_key": "caps lock",
        "lock_level": False,       # не давать программам менять громкость микрофона в Windows
        "lock_level_value": 100,
    },
    "spk": {
        "enabled": False,
        "gain": 100,               # % (до 500)
        "eq": [0.0] * len(EQ_BANDS),
        "eq_preset": "flat",
        "width": 100,              # 0 = моно, 100 = стерео, 200 = широко
        "balance": 0,              # -100..100
        "crossfeed": False,        # мягкое смешение каналов для наушников
        "night": False,            # компрессор: тихое громче, громкое тише
    },
    "tts": {
        "engine": "sapi",          # sapi — голоса Windows (офлайн) | edge — нейроголоса Microsoft (интернет)
        "voice": "",               # голос Windows
        "edge_voice": "ru-RU-SvetlanaNeural",
        "rate": 0,                 # -10..10
        "pitch": 0,                # -10..10
        "volume": 100,             # %, до 200
        "through_fx": False,       # пропускать озвучку через эффекты голоса
        "monitor": True,           # слышать озвучку самому
        "repeat_hotkey": "",       # повторить последнюю фразу
        "history": [],
    },
    "tools": {  # мультитул: каждый инструмент включается отдельно, выключенный не загружается
        "layout": {"enabled": False, "hotkey": "pause"},
        "games": {"enabled": False, "block_win": True, "no_sticky": True, "english": True,
                  "fullscreen": True, "list": []},
        "ping": {"enabled": False, "host": "1.1.1.1", "warn_ms": 150, "warn_loss": 5, "notify": True},
        "windows": {"enabled": False, "topmost_hotkey": "ctrl+alt+t", "fade_more": "ctrl+alt+page down",
                    "fade_less": "ctrl+alt+page up"},
        "awake": {"enabled": False, "display": True},
        "download": {"action": "sleep", "kbps": 300, "minutes": 3},
        # текст
        "translate": {"enabled": False, "hotkey": "ctrl+alt+e", "replace_hotkey": "", "target": "auto"},
        "plainpaste": {"enabled": False, "hotkey": "win+alt+v", "join_lines": False},
        "qr": {"enabled": False, "hotkey": "ctrl+alt+q"},
        "ocr": {"enabled": False, "hotkey": "ctrl+alt+o", "lang": "auto"},
        # окна и экран
        "snap": {"enabled": False, "left": "ctrl+alt+left", "right": "ctrl+alt+right", "top": "ctrl+alt+up",
                 "bottom": "ctrl+alt+down", "center": "ctrl+alt+c", "maximize": "ctrl+alt+enter",
                 "monitor": "ctrl+alt+n"},
        "color": {"enabled": False, "hotkey": "win+shift+c", "format": "hex"},
        "night": {"enabled": False, "mode": "schedule", "start": "21:00", "end": "07:00", "transition": 30,
                  "temperature": 4000, "dim": 100, "pause_games": True, "hotkey": "ctrl+alt+l"},
        "theme": {"enabled": False, "light": "07:00", "dark": "19:00", "apps": True, "system": True,
                  "hotkey": ""},
        "brightness": {"enabled": False, "up": "ctrl+alt+]", "down": "ctrl+alt+[", "off": "", "step": 10},
        # звук
        "apps": {"enabled": False, "up": "ctrl+alt+=", "down": "ctrl+alt+-", "mute": "", "mixer": "",
                 "step": 10, "play": "", "next": "", "prev": ""},
        "micguard": {"enabled": False, "overlay": "muted", "corner": "top-right", "size": 100, "osd": True,
                     "talk_alert": True, "alert_every": 30},
        "autoprofile": {"enabled": False, "rules": [], "cycle_hotkey": "ctrl+alt+p"},
        # система
        "breaks": {"enabled": False, "every": 20, "seconds": 20, "long": True, "long_every": 60,
                   "long_minutes": 5, "idle_reset": 5, "skip_fullscreen": True},
        "power": {"enabled": False, "game": "", "battery": ""},
        "sleeptimer": {"minutes": 60, "action": "sleep", "fade": True},
    },
    "soundboard": {
        "volume": 80,
        "monitor": True,
        "stop_hotkey": "ctrl+alt+s",
        "exclusive": True,         # повторное нажатие перезапускает звук, а не накладывает
        "sounds": [],              # [{"path": ... | "text": ..., "name": ..., "hotkey": ...}]
    },
}


def _merge(base: dict, data: dict) -> dict:
    """Рекурсивно накладывает сохранённые значения на значения по умолчанию.

    Неизвестные ключи отбрасываются, типы проверяются — битый конфиг не роняет программу.
    """
    out = copy.deepcopy(base)
    for key, default in base.items():
        if key not in data:
            continue
        value = data[key]
        if isinstance(default, dict) and key != "profiles":
            if isinstance(value, dict):
                out[key] = _merge(default, value)
        elif isinstance(default, bool):
            if isinstance(value, bool):
                out[key] = value
        elif isinstance(default, (int, float)):
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                out[key] = type(default)(value) if isinstance(default, float) else value
        elif type(value) is type(default) or default is None:
            out[key] = value
    return out


def _migrate(data: dict) -> dict:
    """Конфиг первой версии (input_device_id/output_device_id) и версии 2 (mic.gate)."""
    if "version" not in data:
        profile = {"input_id": data.get("input_device_id"), "output_id": data.get("output_device_id")}
        return {"version": 3, "profiles": {DEFAULT_PROFILE: profile}, "active_profile": DEFAULT_PROFILE}
    mic = data.get("mic")
    if isinstance(mic, dict) and "denoise" not in mic and "gate" in mic:
        mic["denoise"] = "ai" if mic.get("gate") else "off"
    if data.get("block_size") not in (480, 960):
        data["block_size"] = 480
    return data


def load() -> dict:
    try:
        with CONFIG_FILE.open(encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            raise ValueError("config root is not an object")
    except FileNotFoundError:
        return copy.deepcopy(DEFAULTS)
    except (OSError, ValueError) as err:
        log.warning("Не удалось прочитать конфиг (%s), используются значения по умолчанию", err)
        return copy.deepcopy(DEFAULTS)

    cfg = _merge(DEFAULTS, _migrate(data))
    profiles = cfg.get("profiles")
    if not isinstance(profiles, dict) or not profiles:
        profiles = {DEFAULT_PROFILE: {"input_id": None, "output_id": None}}
    cfg["profiles"] = {
        str(name): {"input_id": p.get("input_id"), "output_id": p.get("output_id")}
        for name, p in profiles.items()
        if isinstance(p, dict)
    } or {DEFAULT_PROFILE: {"input_id": None, "output_id": None}}
    if cfg["active_profile"] not in cfg["profiles"]:
        cfg["active_profile"] = next(iter(cfg["profiles"]))
    try:
        cfg["spk"]["eq"] = [float(g) for g in cfg["spk"]["eq"]]
    except (TypeError, ValueError):
        cfg["spk"]["eq"] = []
    if len(cfg["spk"]["eq"]) != len(EQ_BANDS):
        cfg["spk"]["eq"] = [0.0] * len(EQ_BANDS)
    if cfg["mic"]["denoise"] not in ("off", "gate", "ai"):
        cfg["mic"]["denoise"] = "ai"
    if cfg["mic"]["denoise_engine"] not in ("dfn_ll", "dfn", "rnnoise"):
        cfg["mic"]["denoise_engine"] = "dfn_ll"
    if cfg["tts"]["engine"] not in ("sapi", "edge"):
        cfg["tts"]["engine"] = "sapi"
    _validate_tools(cfg["tools"])
    if not isinstance(cfg["mic"]["noise_profile"], dict):
        cfg["mic"]["noise_profile"] = None
    cfg["soundboard"]["sounds"] = [
        s for s in cfg["soundboard"]["sounds"]
        if isinstance(s, dict) and (isinstance(s.get("path"), str) or isinstance(s.get("text"), str))
    ]
    return cfg


_CHOICES = {
    ("translate", "target"): ("auto", "ru", "en", "uk", "de", "fr", "es", "zh-CN", "ja", "kk"),
    ("ocr", "lang"): ("auto", "ru", "en", "uk", "de", "fr", "es", "zh", "ja"),
    ("color", "format"): ("hex", "rgb", "hsl"),
    ("night", "mode"): ("schedule", "always"),
    ("micguard", "overlay"): ("muted", "always", "off"),
    ("micguard", "corner"): ("top-right", "top-left", "bottom-right", "bottom-left"),
    ("sleeptimer", "action"): ("sleep", "hibernate", "shutdown", "mute"),
    ("download", "action"): ("sleep", "hibernate", "shutdown"),
}
_TIMES = (("night", "start"), ("night", "end"), ("theme", "light"), ("theme", "dark"))


def _valid_time(text: str) -> bool:
    try:
        h, m = text.split(":")
        return 0 <= int(h) < 24 and 0 <= int(m) < 60
    except (ValueError, AttributeError):
        return False


def _validate_tools(tools: dict) -> None:
    defaults = DEFAULTS["tools"]
    for (tool, key), allowed in _CHOICES.items():
        if tools[tool][key] not in allowed:
            tools[tool][key] = defaults[tool][key]
    for tool, key in _TIMES:
        if not _valid_time(tools[tool][key]):
            tools[tool][key] = defaults[tool][key]
    tools["autoprofile"]["rules"] = [
        {"exe": r["exe"].strip().lower(), "profile": r["profile"]}
        for r in tools["autoprofile"]["rules"]
        if isinstance(r, dict) and isinstance(r.get("exe"), str) and isinstance(r.get("profile"), str)
        and r["exe"].strip()
    ]
    tools["games"]["list"] = [g for g in tools["games"]["list"] if isinstance(g, str)]


_save_lock = threading.Lock()


def save(cfg: dict) -> None:
    with _save_lock:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        tmp = CONFIG_FILE.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(cfg, fh, indent=2, ensure_ascii=False)
        os.replace(tmp, CONFIG_FILE)
