"""Аудиодвижок: потоки WASAPI (sounddevice/PortAudio) + цепочки обработки.

Микрофон:  физический микрофон → MicChain → виртуальный кабель (его «выход»
           программы видят как микрофон) [+ контроль в наушники].
Наушники:  Windows играет в виртуальный кабель → SpeakerChain → настоящие наушники.
           Так можно поднять громкость выше 100% и включить эквалайзер для всей системы.

Модуль импортируется лениво — только если включена обработка звука, поэтому в режиме
«только профили устройств» numpy/pedalboard/PortAudio вообще не загружаются.
Управляющие методы вызываются только из потока управления.
"""

from __future__ import annotations

import logging

import numpy as np
import sounddevice as sd

from .dsp import SAMPLE_RATE, ClipMixer, MicChain, Ring, SpeakerChain

log = logging.getLogger(__name__)


class EngineError(RuntimeError):
    pass


def _peak(x: np.ndarray) -> float:
    return float(np.max(np.abs(x))) if x.size else 0.0


class _Output:
    """Выходной поток, читающий из кольцевого буфера."""

    def __init__(self, device: int, ring: Ring, block: int, settings, name: str):
        info = sd.query_devices(device)
        self.channels = max(1, min(2, int(info["max_output_channels"])))
        self.ring = ring
        self.tmp = np.zeros((block, ring.buf.shape[1]), np.float32)
        self.stream = sd.OutputStream(
            device=device, channels=self.channels, samplerate=SAMPLE_RATE, blocksize=block,
            dtype="float32", latency="low", extra_settings=settings, callback=self._cb,
        )
        self.name = name

    def _cb(self, outdata, frames, _time, _status):
        try:
            if len(self.tmp) != frames:
                self.tmp = np.zeros((frames, self.tmp.shape[1]), np.float32)
            self.ring.read(self.tmp)
            outdata[:] = self.tmp  # (n,1) → (n,2) через broadcast
        except Exception:  # noqa: BLE001 — исключение остановило бы поток
            outdata.fill(0)


class Engine:
    def __init__(self, block: int = 480):
        self.block = block
        self.mic_chain = MicChain()
        self.spk_chain = SpeakerChain()
        self.mixer = ClipMixer()
        self.muted = False
        self.monitor_voice = False
        self.monitor_gain = 0.6
        self.mic_level = 0.0
        self.spk_level = 0.0
        self._mic_in = None
        self._mic_outs: list[_Output] = []
        self._mic_ring: Ring | None = None
        self._mon_ring: Ring | None = None
        self._spk_in = None
        self._spk_out: _Output | None = None
        self._spk_ring: Ring | None = None
        self._settings = self._wasapi_settings()
        self._logged: set[str] = set()

    def _log_once(self, where: str) -> None:
        # колбэк вызывается 100 раз в секунду — не засоряем лог одинаковыми ошибками
        if where not in self._logged:
            self._logged.add(where)
            log.exception("Ошибка обработки звука (%s)", where)

    # ---------------------------------------------------------------- устройства
    @staticmethod
    def _wasapi_settings():
        try:
            return sd.WasapiSettings(auto_convert=True)
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    def _wasapi_index() -> int:
        for i, api in enumerate(sd.query_hostapis()):
            if "wasapi" in api["name"].lower():
                return i
        raise EngineError("WASAPI недоступен")

    def find(self, name: str, output: bool) -> int:
        """Ищет устройство WASAPI по имени Windows (точно, затем без учёта регистра/по началу)."""
        api = self._wasapi_index()
        key = "max_output_channels" if output else "max_input_channels"
        cands = [(i, d["name"]) for i, d in enumerate(sd.query_devices())
                 if d["hostapi"] == api and d[key] > 0 and "[loopback]" not in d["name"].lower()]
        low = name.lower()
        for test in (lambda n: n == name, lambda n: n.lower() == low,
                     lambda n: n.lower().startswith(low[:31]) or low.startswith(n.lower())):
            for i, n in cands:
                if test(n):
                    return i
        raise EngineError(f"Устройство не найдено: {name}")

    @staticmethod
    def rescan() -> None:
        """PortAudio кэширует список устройств — после подключения нового его надо перечитать."""
        if hasattr(sd, "_terminate"):
            sd._terminate()
            sd._initialize()

    @property
    def mic_running(self) -> bool:
        return self._mic_in is not None

    @property
    def spk_running(self) -> bool:
        return self._spk_in is not None

    def healthy(self) -> bool:
        streams = [s for s in (self._mic_in, self._spk_in) if s is not None]
        streams += [o.stream for o in self._mic_outs]
        if self._spk_out:
            streams.append(self._spk_out.stream)
        return all(s.active for s in streams)

    # ---------------------------------------------------------------- микрофон
    def start_mic(self, mic: str, cable: str | None, monitor: str | None) -> None:
        self.stop_mic()
        b = self.block
        try:
            src = self.find(mic, output=False)
            info = sd.query_devices(src)
            in_ch = max(1, min(2, int(info["max_input_channels"])))
            outs = []
            if cable:
                self._mic_ring = Ring(1, b)
                outs.append(_Output(self.find(cable, True), self._mic_ring, b, self._settings, "cable"))
            if monitor:
                self._mon_ring = Ring(1, b)
                outs.append(_Output(self.find(monitor, True), self._mon_ring, b, self._settings, "monitor"))
            self._mic_outs = outs
            self._mic_in = sd.InputStream(
                device=src, channels=in_ch, samplerate=SAMPLE_RATE, blocksize=b, dtype="float32",
                latency="low", extra_settings=self._settings, callback=self._mic_cb,
            )
            for o in outs:
                o.stream.start()
            self._mic_in.start()
        except Exception as err:
            self.stop_mic()
            raise EngineError(f"Не удалось запустить обработку микрофона: {err}") from err
        log.info("Микрофон: %s → %s (контроль: %s)", mic, cable, monitor)

    def _mic_cb(self, indata, frames, _time, _status):
        try:
            x = indata[:, 0].copy() if indata.shape[1] == 1 else indata.mean(axis=1, dtype=np.float32)
            self.mic_level = _peak(x)
            pre, post, mon = self.mixer.render(frames)
            if pre is not None:
                x += pre
            y = self.mic_chain.process(x)
            if self.muted:
                y = np.zeros(frames, np.float32)
            if post is not None:
                y = y + post
            np.clip(y, -1.0, 1.0, out=y)
            if self._mic_ring is not None:
                self._mic_ring.write(y.reshape(-1, 1))
            if self._mon_ring is not None:
                if self.monitor_voice:  # озвучка и звуки уже внутри y
                    m = y * self.monitor_gain
                elif mon is not None:
                    m = mon * self.monitor_gain
                else:
                    m = np.zeros(frames, np.float32)
                self._mon_ring.write(m.reshape(-1, 1))
        except Exception:  # noqa: BLE001
            self._log_once("mic")

    def stop_mic(self) -> None:
        for s in [self._mic_in] + [o.stream for o in self._mic_outs]:
            if s is not None:
                try:
                    s.close(ignore_errors=True)
                except Exception:  # noqa: BLE001
                    pass
        self._mic_in = None
        self._mic_outs = []
        self._mic_ring = self._mon_ring = None
        self.mic_level = 0.0

    # ---------------------------------------------------------------- наушники
    def start_spk(self, cable_capture: str, headphones: str) -> None:
        self.stop_spk()
        b = self.block
        try:
            src = self.find(cable_capture, output=False)
            info = sd.query_devices(src)
            in_ch = max(1, min(2, int(info["max_input_channels"])))
            self._spk_ring = Ring(2, b)
            self._spk_out = _Output(self.find(headphones, True), self._spk_ring, b, self._settings, "spk")
            self._spk_in = sd.InputStream(
                device=src, channels=in_ch, samplerate=SAMPLE_RATE, blocksize=b, dtype="float32",
                latency="low", extra_settings=self._settings, callback=self._spk_cb,
            )
            self._spk_out.stream.start()
            self._spk_in.start()
        except Exception as err:
            self.stop_spk()
            raise EngineError(f"Не удалось запустить обработку звука наушников: {err}") from err
        log.info("Наушники: %s → %s", cable_capture, headphones)

    def _spk_cb(self, indata, frames, _time, _status):
        try:
            x = indata if indata.shape[1] == 2 else np.repeat(indata[:, :1], 2, axis=1)
            y = np.clip(self.spk_chain.process(x), -1.0, 1.0)
            self.spk_level = _peak(y)
            self._spk_ring.write(y)
        except Exception:  # noqa: BLE001
            self._log_once("spk")

    def stop_spk(self) -> None:
        for s in (self._spk_in, self._spk_out.stream if self._spk_out else None):
            if s is not None:
                try:
                    s.close(ignore_errors=True)
                except Exception:  # noqa: BLE001
                    pass
        self._spk_in = self._spk_out = self._spk_ring = None
        self.spk_level = 0.0

    def stop(self) -> None:
        self.stop_mic()
        self.stop_spk()
        self.mixer.stop()
