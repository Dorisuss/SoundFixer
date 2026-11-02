"""Озвучка текста и звуки саундборда.

Два движка озвучки:
  * «Голоса Windows» (SAPI 5, офлайн) — встроенные голоса, включая голоса OneCore.
  * «Нейроголоса Microsoft» (edge-tts, нужен интернет) — естественные нейросетевые голоса
    (например, русские Светлана и Дмитрий).
Готовая озвучка кэшируется: повтор фразы и быстрые фразы по горячим клавишам звучат мгновенно.

Все методы вызываются из отдельного потока (media worker, COM MTA), чтобы синтез не
задерживал переключение устройств.
"""

from __future__ import annotations

import io
import logging
import os
import tempfile
import wave
from collections import OrderedDict
from xml.sax.saxutils import escape

import numpy as np

from .dsp import SAMPLE_RATE, resample
from .presets import EDGE_VOICES

log = logging.getLogger(__name__)

SAFT48kHz16BitMono = 38
SSFMCreateForWrite = 3
SVSF_IS_XML = 8
SVSF_IS_NOT_XML = 16
SAPI_CATEGORIES = (
    (r"HKEY_LOCAL_MACHINE\SOFTWARE\Microsoft\Speech\Voices", ""),
    # голоса OneCore (в т.ч. русские «Irina», «Pavel») — SAPI обычно умеет ими говорить
    (r"HKEY_LOCAL_MACHINE\SOFTWARE\Microsoft\Speech_OneCore\Voices", " (OneCore)"),
)



class TTSError(RuntimeError):
    pass


# --------------------------------------------------------------------------- SAPI
class SapiTTS:
    def __init__(self):
        self._voice = None
        self._tokens: dict[str, object] = {}

    def _engine(self):
        if self._voice is None:
            try:
                from comtypes.client import CreateObject
            except ImportError as err:
                raise TTSError("озвучка Windows недоступна: нет пакета comtypes") from err
            try:
                self._voice = CreateObject("SAPI.SpVoice")
            except Exception as err:  # noqa: BLE001
                raise TTSError(f"не удалось запустить голоса Windows (SAPI): {err}") from err
        return self._voice

    def voices(self) -> list[str]:
        eng = self._engine()  # понятная ошибка, если SAPI/comtypes недоступны
        from comtypes.client import CreateObject

        self._tokens.clear()
        try:  # основной способ — голоса, которые видит сам SpVoice
            tokens = eng.GetVoices()
            for i in range(tokens.Count):
                tok = tokens.Item(i)
                self._tokens.setdefault(tok.GetDescription(), tok)
        except Exception as err:  # noqa: BLE001
            log.warning("SAPI GetVoices: %s", err)
        for path, suffix in SAPI_CATEGORIES:
            try:
                cat = CreateObject("SAPI.SpObjectTokenCategory")
                cat.SetId(path, False)
                tokens = cat.EnumerateTokens()
                for i in range(tokens.Count):
                    tok = tokens.Item(i)
                    name = tok.GetDescription()
                    if name not in self._tokens and name + suffix not in self._tokens:
                        self._tokens[name + suffix] = tok
            except Exception as err:  # noqa: BLE001 — категории может не быть
                log.debug("Голоса %s: %s", path, err)
        return list(self._tokens)

    def synth(self, text: str, voice: str, rate: int, pitch: int) -> np.ndarray:
        eng = self._engine()
        from comtypes.client import CreateObject

        if voice and not self._tokens:
            self.voices()
        if voice and voice in self._tokens:
            try:
                eng.Voice = self._tokens[voice]
            except Exception as err:  # noqa: BLE001
                raise TTSError(f"голос «{voice}» не поддерживается SAPI: {err}") from err
        eng.Rate = max(-10, min(10, int(rate)))
        eng.Volume = 100

        fd, path = tempfile.mkstemp(suffix=".wav", prefix="sf_tts_")
        os.close(fd)
        try:
            stream = CreateObject("SAPI.SpFileStream")
            try:
                fmt = CreateObject("SAPI.SpAudioFormat")
                fmt.Type = SAFT48kHz16BitMono
                stream.Format = fmt
            except Exception as err:  # noqa: BLE001 — формат прочитаем из WAV
                log.debug("SAPI формат: %s", err)
            stream.Open(path, SSFMCreateForWrite, False)
            try:
                eng.AudioOutputStream = stream
                if pitch:
                    eng.Speak(f'<pitch absmiddle="{max(-10, min(10, int(pitch)))}">{escape(text)}</pitch>',
                              SVSF_IS_XML)
                else:
                    eng.Speak(text, SVSF_IS_NOT_XML)
            finally:
                stream.Close()
            data = read_wav(path)
        except TTSError:
            raise
        except Exception as err:  # noqa: BLE001
            raise TTSError(f"ошибка голосов Windows: {err}") from err
        finally:
            try:
                os.remove(path)
            except OSError:
                pass
        if not len(data):
            raise TTSError("голос Windows вернул пустой звук")
        return data


# --------------------------------------------------------------------------- Edge (нейроголоса)
class EdgeTTS:
    @staticmethod
    def available() -> bool:
        import importlib.util

        return importlib.util.find_spec("edge_tts") is not None

    @staticmethod
    def voices() -> list[tuple[str, str]]:
        """Полный список нейроголосов с сервера → [(ShortName, описание)]."""
        import asyncio

        import edge_tts

        try:
            raw = asyncio.run(edge_tts.list_voices())
        except Exception as err:  # noqa: BLE001
            raise TTSError(f"не удалось получить список нейроголосов: {err}") from err
        known = dict(EDGE_VOICES)
        out = []
        for v in sorted(raw, key=lambda v: (not v["Locale"].startswith(("ru", "uk", "kk", "be")), v["ShortName"])):
            name = v["ShortName"]
            gender = {"Female": "женский", "Male": "мужской"}.get(v.get("Gender", ""), v.get("Gender", ""))
            out.append((name, known.get(name) or f"{name} — {v['Locale']}, {gender}"))
        return out

    @staticmethod
    def synth(text: str, voice: str, rate: int, pitch: int) -> np.ndarray:
        import asyncio

        try:
            import edge_tts
        except ImportError as err:
            raise TTSError("нейроголоса не установлены (пакет edge-tts)") from err

        async def run() -> bytes:
            comm = edge_tts.Communicate(
                text, voice or "ru-RU-SvetlanaNeural",
                rate=f"{int(rate) * 10:+d}%", pitch=f"{int(pitch) * 5:+d}Hz",
                connect_timeout=8, receive_timeout=30,
            )
            buf = bytearray()
            async for chunk in comm.stream():
                if chunk["type"] == "audio":
                    buf += chunk["data"]
            return bytes(buf)

        try:
            mp3 = asyncio.run(run())
        except Exception as err:  # noqa: BLE001
            msg = str(err) or type(err).__name__
            if "Cannot connect" in msg or "getaddrinfo" in msg or "timeout" in msg.lower():
                msg = "нет связи с сервисом (проверьте интернет)"
            raise TTSError(f"нейроголос: {msg}") from err
        if not mp3:
            raise TTSError("нейроголос: сервис не вернул звук")
        return decode_bytes(mp3)


# --------------------------------------------------------------------------- менеджер
# голос для текста на другом языке (перевод, текст с экрана), если выбранный голос его не знает
LANG_EDGE_VOICES = {"en": "en-US-AriaNeural", "ru": "ru-RU-SvetlanaNeural", "uk": "uk-UA-PolinaNeural",
                    "de": "de-DE-KatjaNeural", "fr": "fr-FR-DeniseNeural", "es": "es-ES-ElviraNeural",
                    "zh": "zh-CN-XiaoxiaoNeural", "ja": "ja-JP-NanamiNeural", "kk": "kk-KZ-AigulNeural"}
LANG_SAPI_WORDS = {"en": "english", "ru": "russian", "uk": "ukrainian", "de": "german", "fr": "french",
                   "es": "spanish", "zh": "chinese", "ja": "japanese", "kk": "kazakh"}


class TTS:
    """Выбирает движок, кэширует результат (одинаковая фраза не синтезируется заново)."""

    CACHE = 40

    def __init__(self):
        self.sapi = SapiTTS()
        self.edge = EdgeTTS()
        self._cache: OrderedDict[tuple, np.ndarray] = OrderedDict()

    def voice_for(self, engine: str, voice: str, lang: str | None) -> str:
        lang = (lang or "").split("-")[0].lower()
        if not lang:
            return voice
        if engine == "edge":
            if voice and voice.lower().startswith(lang + "-"):
                return voice
            return LANG_EDGE_VOICES.get(lang, voice)
        word = LANG_SAPI_WORDS.get(lang)
        if not word or word in (voice or "").lower():
            return voice
        try:
            names = list(self.sapi._tokens) or self.sapi.voices()
        except TTSError:
            return voice
        return next((n for n in names if word in n.lower()), voice)

    def synth(self, text: str, cfg: dict) -> np.ndarray:
        engine = cfg.get("engine", "sapi")
        voice = cfg.get("edge_voice") if engine == "edge" else cfg.get("voice", "")
        voice = self.voice_for(engine, voice, cfg.get("lang"))
        key = (engine, voice, int(cfg.get("rate", 0)), int(cfg.get("pitch", 0)), text)
        if key in self._cache:
            self._cache.move_to_end(key)
            return self._cache[key]
        if engine == "edge":
            data = self.edge.synth(text, voice, key[2], key[3])
        else:
            data = self.sapi.synth(text, voice, key[2], key[3])
        data = _trim_silence(data)
        self._cache[key] = data
        while len(self._cache) > self.CACHE:
            self._cache.popitem(last=False)
        return data


def _trim_silence(data: np.ndarray, threshold: float = 0.003) -> np.ndarray:
    """Срезает тишину в начале (у MP3/SAPI бывает 30–100 мс) — озвучка звучит сразу."""
    idx = np.flatnonzero(np.abs(data) > threshold)
    if not len(idx):
        return data
    start = max(0, idx[0] - 240)
    end = min(len(data), idx[-1] + 2400)
    return np.ascontiguousarray(data[start:end])


# --------------------------------------------------------------------------- файлы
def read_wav(path: str) -> np.ndarray:
    with wave.open(path, "rb") as wf:
        ch, width, rate = wf.getnchannels(), wf.getsampwidth(), wf.getframerate()
        raw = wf.readframes(wf.getnframes())
    if width == 2:
        data = np.frombuffer(raw, "<i2").astype(np.float32) / 32768.0
    elif width == 1:
        data = (np.frombuffer(raw, np.uint8).astype(np.float32) - 128.0) / 128.0
    elif width == 4:
        data = np.frombuffer(raw, "<i4").astype(np.float32) / 2147483648.0
    else:
        raise ValueError(f"неподдерживаемый WAV: {width * 8} бит")
    if ch > 1:
        data = data.reshape(-1, ch).mean(axis=1)
    return resample(data, rate, SAMPLE_RATE)


def decode_bytes(data: bytes) -> np.ndarray:
    from pedalboard.io import AudioFile

    with AudioFile(io.BytesIO(data)).resampled_to(SAMPLE_RATE) as f:
        audio = f.read(f.frames)
    return np.ascontiguousarray(audio.mean(axis=0) if audio.shape[0] > 1 else audio[0], np.float32)


class SoundCache:
    """Декодирует mp3/wav/ogg/flac один раз и держит в памяти (LRU, до ~80 МБ)."""

    LIMIT = 80 * 1024 * 1024

    def __init__(self):
        self._items: OrderedDict[str, np.ndarray] = OrderedDict()
        self._size = 0

    def get(self, path: str) -> np.ndarray:
        key = os.path.normcase(os.path.abspath(path))
        if key in self._items:
            self._items.move_to_end(key)
            return self._items[key]
        if not os.path.isfile(path):
            raise FileNotFoundError(f"файл не найден: {path}")
        data = self._decode(path)
        self._items[key] = data
        self._size += data.nbytes
        while self._size > self.LIMIT and len(self._items) > 1:
            _, old = self._items.popitem(last=False)
            self._size -= old.nbytes
        return data

    @staticmethod
    def _decode(path: str) -> np.ndarray:
        if path.lower().endswith(".wav"):
            try:
                return read_wav(path)
            except (wave.Error, ValueError, EOFError):
                pass  # например float WAV — пусть декодирует pedalboard
        from pedalboard.io import AudioFile

        try:
            with AudioFile(path).resampled_to(SAMPLE_RATE) as f:
                data = f.read(f.frames)
        except Exception as err:  # noqa: BLE001
            raise ValueError(f"не удалось открыть {os.path.basename(path)}: {err}") from err
        return np.ascontiguousarray(data.mean(axis=0) if data.shape[0] > 1 else data[0], np.float32)
