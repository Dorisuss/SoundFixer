"""Текст с экрана: обвести область мышью → распознанный текст в буфере обмена.

Как PowerToys Text Extractor / Capture2Text, но на встроенном распознавании Windows 10/11
(Windows.Media.Ocr) — без интернета и тяжёлых нейросетей. Языки — те, что установлены в
Windows (Параметры → Время и язык → Язык; у языка должно быть «Оптическое распознавание»).
"""

from __future__ import annotations

import asyncio
import logging
import re
import threading

from . import Tool, winapi

log = logging.getLogger(__name__)

LANG_SETTINGS_URI = "ms-settings:regionlanguage"


class OcrError(RuntimeError):
    pass


def text_score(text: str) -> float:
    """Чем «правдоподобнее» распознанный текст, тем больше: слова из смеси кириллицы и латиницы
    («НеІІо») — признак, что выбран не тот язык распознавания."""
    score = 0.0
    for word in re.findall(r"[^\W\d_]+", text):
        cyr = sum(1 for c in word if "Ѐ" <= c <= "ӿ")
        lat = sum(1 for c in word if c.isascii())
        if cyr and lat:
            score -= 2 * len(word)
        else:
            score += len(word)
    return score


def prepare(w: int, h: int, bgra: bytes, max_dim: int = 10000) -> tuple[int, int, bytes]:
    """Мелкий текст интерфейса распознаётся плохо — увеличиваем; альфа = 255 (GDI оставляет 0)."""
    from PIL import Image

    img = Image.frombuffer("RGB", (w, h), bgra, "raw", "BGRX", 0, 1)
    scale = 3 if h < 120 else 2 if h < 700 else 1
    scale = max(1, min(scale, max_dim // max(w, h, 1)))
    if scale > 1:
        img = img.resize((w * scale, h * scale), Image.LANCZOS)
    if max(img.size) > max_dim:  # огромная область — уменьшаем до допустимого
        k = max_dim / max(img.size)
        img = img.resize((max(1, int(img.width * k)), max(1, int(img.height * k))), Image.LANCZOS)
    return img.width, img.height, img.convert("RGBA").tobytes("raw", "BGRA")


def recognize(w: int, h: int, bgra: bytes, lang: str = "auto") -> tuple[str, str]:
    """→ (текст, язык распознавания). Вызывать не из GUI-потока."""
    try:
        from winrt.windows.globalization import Language
        from winrt.windows.graphics.imaging import BitmapPixelFormat, SoftwareBitmap
        from winrt.windows.media.ocr import OcrEngine
    except ImportError as err:
        raise OcrError("не установлены пакеты распознавания (winrt-Windows.Media.Ocr)") from err

    try:
        available = [lng.language_tag for lng in OcrEngine.available_recognizer_languages]
    except OSError as err:  # компонент распознавания не зарегистрирован (старая/урезанная Windows)
        raise OcrError("в этой Windows нет встроенного распознавания текста (нужна Windows 10 или 11)") from err
    if not available:
        raise OcrError("в Windows не установлен ни один язык с распознаванием текста")
    if lang != "auto":
        tags = [t for t in available if t.lower().split("-")[0] == lang.lower()]
        if not tags:
            raise OcrError(f"распознавание для языка «{lang}» не установлено в Windows")
    else:
        # язык профиля + русский + английский (что из них установлено), без повторов
        tags = []
        for want in ("ru", "en"):
            tags += [t for t in available if t.lower().startswith(want) and t not in tags][:1]
        tags = tags or available[:1]

    w, h, data = prepare(w, h, bgra, int(OcrEngine.max_image_dimension or 10000))
    # альфа уже 255, поэтому режим альфы по умолчанию не важен (вариант с 4 аргументами есть во всех версиях)
    bitmap = SoftwareBitmap.create_copy_from_buffer(data, BitmapPixelFormat.BGRA8, w, h)

    async def run():
        out = []
        for tag in tags:
            engine = OcrEngine.try_create_from_language(Language(tag))
            if engine is None:
                continue
            result = await engine.recognize_async(bitmap)
            text = "\n".join(line.text for line in result.lines)
            out.append((text, tag))
        return out

    results = asyncio.run(run())
    if not results:
        raise OcrError("не удалось запустить распознавание текста Windows")
    return max(results, key=lambda r: text_score(r[0]))


class ScreenText(Tool):
    key = "ocr"
    title = "Текст с экрана"

    def __init__(self, app):
        super().__init__(app)
        self.cfg: dict = {}
        self._busy = False
        self.last = ""

    def start(self, cfg):
        super().start(cfg)
        self.cfg = dict(cfg)

    def update(self, cfg):
        self.cfg = dict(cfg)

    def hotkeys(self, cfg):
        return {"grab": cfg.get("hotkey", "")}

    def on_hotkey(self, action):
        self.grab()

    def grab(self):
        """GUI-поток: затемнить экран и дать обвести область."""
        if self._busy:
            return
        self._busy = True
        from .popup import RegionSelector

        try:
            RegionSelector(self.app.root, self._selected, "Обведите текст мышью. Esc — отмена")
        except Exception:  # noqa: BLE001
            self._busy = False
            raise

    def _selected(self, box):
        if box is None:
            self._busy = False
            return
        threading.Thread(target=self._work, args=(box,), name="SoundFixer-ocr", daemon=True).start()

    def _work(self, box):
        try:
            shot = winapi.grab_region(*box)
            if shot is None:
                raise OcrError("не удалось сделать снимок экрана")
            text, tag = recognize(*shot, lang=self.cfg.get("lang", "auto"))
            text = text.strip()
            if not text:
                self.app.ui(self.app.notify, "Текст с экрана", "Текст не найден — попробуйте выделить крупнее")
                return
            winapi.set_clipboard_text(text)
            self.last = f"{len(text)} симв. ({tag})"
            self.app.ui(self._show, text, tag)
            self.app.ui(self.app.tool_status_changed, self.key)
        except OcrError as err:
            log.warning("OCR: %s", err)
            self.app.ui(self._error, str(err))
        except Exception as err:  # noqa: BLE001
            log.exception("OCR")
            self.app.ui(self._error, str(err))
        finally:
            self._busy = False

    def _error(self, text):
        self.last = f"Ошибка: {text}"
        self.app.notify("Текст с экрана", text[:200])
        self.app.tool_status_changed(self.key)

    def _show(self, text, tag):
        from .popup import Popup
        from .textutils import TranslateError, translate

        lang = tag.split("-")[0]

        def copy(p):
            winapi.set_clipboard_text(p.get_text())

        def do_translate(p):
            src = p.get_text()

            def work():
                try:
                    tr, _src, dst = translate(src, "auto")
                except TranslateError as err:
                    tr, dst = f"Перевод не удался: {err}", None
                self.app.ui(p.set_text, tr)
                if dst:
                    nonlocal lang
                    lang = dst

            threading.Thread(target=work, daemon=True).start()
            return True

        def speak(p):
            self.app.speak_local(p.get_text(), lang)
            return True

        Popup(self.app.root, "Текст скопирован — можно поправить", text=text, editable=True,
              buttons=[("Копировать", copy), ("Перевести", do_translate), ("Прочитать вслух", speak)])

    def status(self):
        return f"Последнее: {self.last}" if self.last else ""
