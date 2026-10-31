"""Исправление раскладки: «ghbdtn» → «привет» (и обратно) по горячей клавише.

Как работает: копируем выделенный текст (если ничего не выделено — выделяем слово перед
курсором), меняем символы по расположению клавиш QWERTY ↔ ЙЦУКЕН, вставляем обратно,
возвращаем прежнее содержимое буфера обмена и переключаем раскладку окна на нужную.
Без постоянного перехвата клавиатуры — всё происходит только в момент нажатия.
"""

from __future__ import annotations

import logging
import threading
import time

from . import Tool, winapi

log = logging.getLogger(__name__)

_EN = ("`qwertyuiop[]asdfghjkl;'zxcvbnm,./"
       "~QWERTYUIOP{}ASDFGHJKL:\"ZXCVBNM<>?"
       "@#$^&")
_RU = ("ёйцукенгшщзхъфывапролджэячсмитьбю."
       "ЁЙЦУКЕНГШЩЗХЪФЫВАПРОЛДЖЭЯЧСМИТЬБЮ,"
       "\"№;:?")
EN_TO_RU = dict(zip(_EN, _RU))
RU_TO_EN = {r: e for e, r in zip(_EN, _RU)}
# «.» и «,» в русской раскладке стоят на месте «/» и «?», а «;» «:» «?» «"» — на цифрах;
# при обратном переводе берём основное значение клавиши
RU_TO_EN.update({".": "/", ",": "?", ";": "$", ":": "^", "?": "&", "\"": "@", "№": "#"})


def _is_latin(c: str) -> bool:
    return "a" <= c.lower() <= "z"


def _is_cyrillic(c: str) -> bool:
    return "а" <= c.lower() <= "я" or c in "ёЁ"


def convert_layout(text: str) -> tuple[str, int]:
    """→ (исправленный текст, язык результата LANG_RUSSIAN/LANG_ENGLISH)."""
    latin = sum(_is_latin(c) for c in text)
    cyr = sum(_is_cyrillic(c) for c in text)
    if latin >= cyr:
        return "".join(EN_TO_RU.get(c, c) for c in text), winapi.LANG_RUSSIAN
    return "".join(RU_TO_EN.get(c, c) for c in text), winapi.LANG_ENGLISH


class LayoutFixer(Tool):
    key = "layout"
    title = "Исправление раскладки"

    def __init__(self, app):
        super().__init__(app)
        self._busy = threading.Lock()
        self.last = ""

    def hotkeys(self, cfg):
        return {"fix": cfg.get("hotkey", "pause")}

    def on_hotkey(self, action):
        if action == "fix" and self._busy.acquire(blocking=False):
            threading.Thread(target=self._fix_safe, name="SoundFixer-layout", daemon=True).start()

    def _fix_safe(self):
        try:
            self._fix()
        except Exception:  # noqa: BLE001
            log.exception("Исправление раскладки")
        finally:
            self._busy.release()

    def _copy(self, timeout: float = 0.35) -> bool:
        seq = winapi.clipboard_sequence()
        winapi.send_keys(winapi.combo(winapi.VK_CONTROL, ord("C")))
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if winapi.clipboard_sequence() != seq:
                return True
            time.sleep(0.01)
        return False

    def _fix(self):
        winapi.wait_modifiers_released()
        hwnd = winapi.foreground_window()
        saved = winapi.get_clipboard_text()
        if not self._copy():
            # ничего не выделено — выделяем слово перед курсором
            winapi.send_keys(winapi.combo(winapi.VK_CONTROL, winapi.VK_SHIFT, winapi.VK_LEFT))
            time.sleep(0.03)
            if not self._copy():
                return
        text = winapi.get_clipboard_text()
        if not text or len(text) > 5000:
            return
        fixed, lang = convert_layout(text)
        if fixed != text:
            winapi.set_clipboard_text(fixed)
            winapi.send_keys(winapi.combo(winapi.VK_CONTROL, ord("V")))
            time.sleep(0.25)  # программа читает буфер асинхронно — не подменяем его сразу
            self.last = f"{text[:30]} → {fixed[:30]}"
        winapi.switch_layout(hwnd, lang)
        if saved is not None:
            winapi.set_clipboard_text(saved)
        else:
            time.sleep(0.05)
