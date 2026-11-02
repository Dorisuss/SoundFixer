"""Текст: перевод выделенного, вставка без форматирования, QR-код буфера обмена.

Идеи из открытых проектов: Crow Translate / STranslate (перевод по горячей клавише),
PowerToys Advanced Paste и PureText (вставка простым текстом).
"""

from __future__ import annotations

import json
import logging
import re
import threading
import urllib.parse
import urllib.request

from . import Tool, winapi

log = logging.getLogger(__name__)

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) SoundFixer"
LANGS = {"auto": "Авто (русский ↔ английский)", "ru": "Русский", "en": "English", "uk": "Українська",
         "de": "Deutsch", "fr": "Français", "es": "Español", "zh-CN": "中文", "ja": "日本語", "kk": "Қазақша"}


class TranslateError(RuntimeError):
    pass


def _cyrillic_share(text: str) -> float:
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return 0.0
    return sum("а" <= c.lower() <= "я" or c in "ёЁіїєґІЇЄҐ" for c in letters) / len(letters)


def pick_target(text: str, target: str) -> str:
    if target != "auto":
        return target
    return "en" if _cyrillic_share(text) > 0.5 else "ru"


def parse_google(data) -> tuple[str, str]:
    """Ответ translate_a/single (client=gtx) → (перевод, язык оригинала)."""
    parts = [seg[0] for seg in (data[0] or []) if isinstance(seg, list) and seg and seg[0]]
    if not parts:
        raise TranslateError("пустой ответ сервиса перевода")
    src = data[2] if len(data) > 2 and isinstance(data[2], str) else "?"
    return "".join(parts), src


def _get(url: str, timeout: float = 7.0):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def split_chunks(text: str, limit: int = 1200) -> list[str]:
    """Длинный текст → куски по строкам/предложениям: слишком длинный адрес запроса сервис отклоняет."""
    chunks: list[str] = []
    cur = ""
    for part in re.split(r"(\n+)", text):
        while len(part) > limit:
            cut = max(part.rfind(". ", 0, limit), part.rfind(" ", 0, limit))
            cut = cut + 1 if cut > 0 else limit
            if cur:
                chunks.append(cur)
                cur = ""
            chunks.append(part[:cut])
            part = part[cut:]
        if cur and len(cur) + len(part) > limit:
            chunks.append(cur)
            cur = ""
        cur += part
    if cur:
        chunks.append(cur)
    return chunks


def translate(text: str, target: str = "auto") -> tuple[str, str, str]:
    """→ (перевод, язык оригинала, язык перевода). Google, при ошибке — MyMemory."""
    text = text.strip()[:4500]
    if not text:
        raise TranslateError("нет текста")
    dst = pick_target(text, target)
    errors = []
    try:
        out, src = [], "?"
        for chunk in split_chunks(text):
            if not chunk.strip():
                out.append(chunk)
                continue
            q = urllib.parse.urlencode({"client": "gtx", "sl": "auto", "tl": dst, "dt": "t", "q": chunk})
            tr, lang = parse_google(_get("https://translate.googleapis.com/translate_a/single?" + q))
            out.append(tr)
            src = lang if src == "?" else src
        return "".join(out), src, dst
    except Exception as err:  # noqa: BLE001
        errors.append(f"Google: {err}")
    try:
        src = "ru" if _cyrillic_share(text) > 0.5 else "en"
        if src == dst:
            src = "en" if dst == "ru" else "ru"
        q = urllib.parse.urlencode({"q": text[:500], "langpair": f"{src}|{dst}"})
        data = _get("https://api.mymemory.translated.net/get?" + q)
        tr = (data.get("responseData") or {}).get("translatedText") or ""
        if tr:
            return tr, src, dst
        errors.append("MyMemory: пустой ответ")
    except Exception as err:  # noqa: BLE001
        errors.append(f"MyMemory: {err}")
    raise TranslateError("; ".join(errors))


class TranslateTool(Tool):
    key = "translate"
    title = "Перевод выделенного"

    def __init__(self, app):
        super().__init__(app)
        self.cfg: dict = {}
        self._busy = threading.Lock()
        self.last = ""

    def start(self, cfg):
        super().start(cfg)
        self.cfg = dict(cfg)

    def update(self, cfg):
        self.cfg = dict(cfg)

    def hotkeys(self, cfg):
        return {"show": cfg.get("hotkey", ""), "replace": cfg.get("replace_hotkey", "")}

    def on_hotkey(self, action):
        if self._busy.acquire(blocking=False):
            threading.Thread(target=self._work, args=(action == "replace",), name="SoundFixer-translate",
                             daemon=True).start()

    def _work(self, replace: bool):
        try:
            hwnd = winapi.foreground_window()
            text, saved = winapi.grab_selection(select_word=False)
            if saved is not None:
                winapi.set_clipboard_text(saved)
            if not text or not text.strip():
                self.app.ui(self.app.notify, "Перевод", "Сначала выделите текст")
                return
            try:
                tr, src, dst = translate(text, self.cfg.get("target", "auto"))
            except TranslateError as err:
                self.app.ui(self.app.notify, "Перевод не удался", str(err)[:200])
                return
            self.last = f"{src} → {dst}: {tr[:60]}"
            if replace:
                winapi.paste_text(tr, restore=saved, hwnd=hwnd)
            else:
                self.app.ui(self._show, text, tr, src, dst, hwnd, saved)
            self.app.ui(self.app.tool_status_changed, self.key)
        except Exception:  # noqa: BLE001
            log.exception("Перевод")
        finally:
            self._busy.release()

    def _show(self, text, tr, src, dst, hwnd, saved):
        from .popup import Popup

        def copy(_p):
            winapi.set_clipboard_text(tr)

        def replace(_p):
            threading.Thread(target=winapi.paste_text, args=(tr, saved, hwnd), daemon=True).start()

        def speak(_p):
            self.app.speak_local(tr, dst)
            return True  # окно не закрываем

        Popup(self.app.root, f"Перевод  {src} → {dst}", text=tr, sub=text[:300],
              buttons=[("Копировать", copy), ("Заменить выделенное", replace), ("Озвучить", speak)])

    def status(self):
        return f"Последний: {self.last}" if self.last else ""


class PlainPaste(Tool):
    """Вставка без форматирования: из буфера уходят шрифты, цвета, таблицы и картинки."""

    key = "plainpaste"
    title = "Вставка простым текстом"

    def __init__(self, app):
        super().__init__(app)
        self.cfg: dict = {}

    def start(self, cfg):
        super().start(cfg)
        self.cfg = dict(cfg)

    def update(self, cfg):
        self.cfg = dict(cfg)

    def hotkeys(self, cfg):
        return {"paste": cfg.get("hotkey", "")}

    def on_hotkey(self, action):
        threading.Thread(target=self._paste, name="SoundFixer-plainpaste", daemon=True).start()

    def _paste(self):
        text = winapi.get_clipboard_text()
        if not text:
            return
        if self.cfg.get("join_lines"):
            text = join_wrapped_lines(text)
        winapi.paste_text(text, restore=None)


def join_wrapped_lines(text: str) -> str:
    """Склеивает строки, разорванные при копировании из PDF, сохраняя абзацы и переносы со знаком «-»."""
    text = text.replace("\r\n", "\n")
    paras = re.split(r"\n\s*\n", text)
    out = []
    for p in paras:
        p = re.sub(r"(\w)-\n(\w)", r"\1\2", p)       # перенос слова
        p = re.sub(r"[ \t]*\n[ \t]*", " ", p)
        out.append(re.sub(r" {2,}", " ", p).strip())
    return "\n\n".join(out)


class QRTool(Tool):
    """QR-код того, что в буфере обмена: ссылку или текст удобно перекинуть на телефон."""

    key = "qr"
    title = "QR-код буфера"

    def hotkeys(self, cfg):
        return {"show": cfg.get("hotkey", "")}

    def on_hotkey(self, action):
        self.show()

    def show(self):
        text = winapi.get_clipboard_text() if winapi.IS_WINDOWS else None
        if not text or not text.strip():
            self.app.notify("QR-код", "В буфере обмена нет текста")
            return
        text = text.strip()[:1500]
        try:
            img = make_qr(text)
        except Exception as err:  # noqa: BLE001
            self.app.notify("QR-код", f"Не удалось: {err}")
            return
        from PIL import ImageTk

        from .popup import Popup

        photo = ImageTk.PhotoImage(img)
        Popup(self.app.root, "Наведите камеру телефона", image=photo, sub=text[:160], width=360)


def make_qr(text: str):
    import qrcode

    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=6, border=3)
    qr.add_data(text)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white").convert("RGB")
    side = 320
    if img.width > side:
        from PIL import Image

        img = img.resize((side, side), Image.NEAREST)
    return img
