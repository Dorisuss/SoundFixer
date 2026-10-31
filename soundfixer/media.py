"""Озвучка текста (Windows SAPI) и загрузка звуков для саундборда.

Вызывается из потока управления (COM MTA). Синтез идёт в WAV во временный файл,
затем звук подмешивается в виртуальный микрофон — собеседники в Discord/играх
слышат озвученный текст.
"""

from __future__ import annotations

import logging
import os
import tempfile
import wave
from collections import OrderedDict

import numpy as np

from .dsp import SAMPLE_RATE, resample

log = logging.getLogger(__name__)

SAFT48kHz16BitMono = 38
SSFMCreateForWrite = 3
VOICE_CATEGORIES = (
    r"HKEY_LOCAL_MACHINE\SOFTWARE\Microsoft\Speech\Voices",
    # голоса OneCore (в т.ч. русские «Irina», «Pavel») — SAPI умеет ими говорить
    r"HKEY_LOCAL_MACHINE\SOFTWARE\Microsoft\Speech_OneCore\Voices",
)


class TextToSpeech:
    def __init__(self):
        self._voice = None
        self._tokens: dict[str, object] = {}

    def _engine(self):
        if self._voice is None:
            from comtypes.client import CreateObject

            self._voice = CreateObject("SAPI.SpVoice")
        return self._voice

    def voices(self) -> list[str]:
        from comtypes.client import CreateObject

        self._engine()
        self._tokens.clear()
        for path in VOICE_CATEGORIES:
            try:
                cat = CreateObject("SAPI.SpObjectTokenCategory")
                cat.SetId(path, False)
                tokens = cat.EnumerateTokens()
                for i in range(tokens.Count):
                    tok = tokens.Item(i)
                    name = tok.GetDescription()
                    self._tokens.setdefault(name, tok)
            except Exception as err:  # noqa: BLE001 — категории может не быть
                log.debug("Голоса %s: %s", path, err)
        return list(self._tokens)

    def synth(self, text: str, voice: str = "", rate: int = 0, volume: int = 100) -> np.ndarray:
        from comtypes.client import CreateObject

        eng = self._engine()
        if voice and not self._tokens:
            self.voices()
        if voice in self._tokens:
            eng.Voice = self._tokens[voice]
        eng.Rate = max(-10, min(10, int(rate)))
        eng.Volume = max(0, min(100, int(volume)))

        fd, path = tempfile.mkstemp(suffix=".wav", prefix="sf_tts_")
        os.close(fd)
        try:
            stream = CreateObject("SAPI.SpFileStream")
            try:
                fmt = CreateObject("SAPI.SpAudioFormat")
                fmt.Type = SAFT48kHz16BitMono
                stream.Format = fmt
            except Exception:  # noqa: BLE001 — формат прочитаем из WAV
                pass
            stream.Open(path, SSFMCreateForWrite, False)
            eng.AudioOutputStream = stream
            try:
                eng.Speak(text, 0)
            finally:
                stream.Close()
            return read_wav(path)
        finally:
            try:
                os.remove(path)
            except OSError:
                pass


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
        raise ValueError(f"Неподдерживаемый WAV: {width * 8} бит")
    if ch > 1:
        data = data.reshape(-1, ch).mean(axis=1)
    return resample(data, rate, SAMPLE_RATE)


class SoundCache:
    """Декодирует mp3/wav/ogg/flac один раз и держит в памяти (LRU, до ~60 МБ)."""

    LIMIT = 60 * 1024 * 1024

    def __init__(self):
        self._items: OrderedDict[str, np.ndarray] = OrderedDict()
        self._size = 0

    def get(self, path: str) -> np.ndarray:
        key = os.path.normcase(os.path.abspath(path))
        if key in self._items:
            self._items.move_to_end(key)
            return self._items[key]
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
            except (wave.Error, ValueError):
                pass  # например float WAV — пусть декодирует pedalboard
        from pedalboard.io import AudioFile

        with AudioFile(path).resampled_to(SAMPLE_RATE) as f:
            data = f.read(f.frames)
        return np.ascontiguousarray(data.mean(axis=0) if data.shape[0] > 1 else data[0], np.float32)
