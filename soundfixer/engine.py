"""Аудиодвижок: потоки WASAPI (sounddevice/PortAudio) + цепочки обработки.

Микрофон:  физический микрофон → ИИ-шумодав → эффекты → виртуальный кабель (его «выход»
           программы видят как микрофон).
Наушники:  Windows играет в виртуальный кабель → SpeakerChain → настоящие наушники.
           Так можно поднять громкость выше 100% и включить эквалайзер для всей системы.
Контроль:  отдельный выход в наушники — «слышать себя», озвучка и звуки саундборда.
           Работает и без обработки микрофона: тогда озвучку/звуки слышите только вы.

Модуль импортируется лениво — только когда нужен звук, поэтому в режиме «только профили
устройств» numpy/pedalboard/PortAudio вообще не загружаются.
Управляющие методы вызываются только из потока управления.
"""

from __future__ import annotations

import logging
import time

import numpy as np
import sounddevice as sd

from .dsp import SAMPLE_RATE, ClipMixer, MicChain, Ring, SpeakerChain

log = logging.getLogger(__name__)


class EngineError(RuntimeError):
    pass


def _peak(x: np.ndarray) -> float:
    return float(np.max(np.abs(x))) if x.size else 0.0


def _close(stream) -> None:
    if stream is not None:
        try:
            stream.close(ignore_errors=True)
        except Exception:  # noqa: BLE001
            pass


class _Output:
    """Выходной поток; source(buf) заполняет буфер формы (frames, src_channels)."""

    def __init__(self, device: int, block: int, settings, src_channels: int, source):
        info = sd.query_devices(device)
        self.channels = max(1, min(2, int(info["max_output_channels"])))
        self.source = source
        self.tmp = np.zeros((block, src_channels), np.float32)
        self.stream = sd.OutputStream(
            device=device, channels=self.channels, samplerate=SAMPLE_RATE, blocksize=block,
            dtype="float32", latency="low", extra_settings=settings, callback=self._cb,
        )

    def _cb(self, outdata, frames, _time, _status):
        try:
            tmp = self.tmp
            if len(tmp) != frames:
                tmp = self.tmp = np.zeros((frames, tmp.shape[1]), np.float32)
            self.source(tmp)
            if tmp.shape[1] > outdata.shape[1]:
                outdata[:, 0] = tmp.mean(axis=1)
            else:
                outdata[:] = tmp  # моно → стерео через broadcast
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
        self.proc_ms = 0.0            # среднее время обработки блока микрофона
        self._rec = None              # [исходный, обработанный, позиция] — тест «как меня слышат»
        self.last_activity = time.monotonic()
        self.monitor_device: str | None = None
        self._mic_in = None
        self._mic_active = False
        self._cable: _Output | None = None
        self._cable_ring: Ring | None = None
        self._mon: _Output | None = None
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
        raise EngineError(f"устройство не найдено: {name}")

    def reset(self) -> None:
        """Закрыть все потоки и перечитать устройства: PortAudio кэширует их список, и после
        подключения нового устройства без этого его не увидеть."""
        self.stop_mic()
        self.stop_spk()
        self.close_monitor()
        if hasattr(sd, "_terminate"):
            sd._terminate()
            sd._initialize()

    @property
    def mic_running(self) -> bool:
        return self._mic_in is not None

    @property
    def spk_running(self) -> bool:
        return self._spk_in is not None

    @property
    def monitor_running(self) -> bool:
        return self._mon is not None

    @property
    def cable_running(self) -> bool:
        return self._cable is not None

    def healthy(self) -> bool:
        streams = [self._mic_in, self._spk_in]
        streams += [o.stream for o in (self._cable, self._mon, self._spk_out) if o is not None]
        return all(s.active for s in streams if s is not None)

    @property
    def underruns(self) -> int:
        """Сколько раз звук не успел (опустошения буферов микрофона и контроля)."""
        return sum(r.underruns for r in (self._cable_ring, self._mon_ring) if r is not None)

    @property
    def buffer_ms(self) -> float:
        r = self._cable_ring or self._mon_ring
        return r.target / 48.0 if r is not None else 0.0

    def idle_for(self) -> float:
        return time.monotonic() - self.last_activity

    # ---------------------------------------------------------------- микрофон
    def start_mic(self, mic: str, cable: str | None) -> None:
        self.stop_mic()
        b = self.block
        try:
            src = self.find(mic, output=False)
            in_ch = max(1, min(2, int(sd.query_devices(src)["max_input_channels"])))
            if cable:
                self._cable_ring = Ring(1, b)
                self._cable = _Output(self.find(cable, True), b, self._settings, 1, self._cable_ring.read)
            self._mic_in = sd.InputStream(
                device=src, channels=in_ch, samplerate=SAMPLE_RATE, blocksize=b, dtype="float32",
                latency="low", extra_settings=self._settings, callback=self._mic_cb,
            )
            if self._cable:
                self._cable.stream.start()
            self._mic_active = True
            self._mic_in.start()
        except Exception as err:
            self.stop_mic()
            raise EngineError(f"не удалось запустить обработку микрофона: {err}") from err
        log.info("Микрофон: %s → %s", mic, cable)

    def _mic_cb(self, indata, frames, _time, _status):
        try:
            t0 = time.perf_counter()
            x = indata[:, 0].copy() if indata.shape[1] == 1 else indata.mean(axis=1, dtype=np.float32)
            self.mic_level = _peak(x)
            pre, post, mon = self.mixer.render(frames)
            if pre is not None:
                x += pre
            raw = x.copy() if self._rec is not None else None
            v = self.mic_chain.process(x)
            if self.muted:
                v = np.zeros(frames, np.float32)
            y = v + post if post is not None else v
            np.clip(y, -1.0, 1.0, out=y)
            if self._cable_ring is not None:
                self._cable_ring.write(y.reshape(-1, 1))
            rec = self._rec
            if rec is not None and raw is not None:
                pos = rec[2]
                m = min(frames, len(rec[0]) - pos)
                rec[0][pos:pos + m] = raw[:m]
                rec[1][pos:pos + m] = y[:m]
                rec[2] = pos + m
            ring = self._mon_ring
            if ring is not None:
                m = v * self.monitor_gain if self.monitor_voice else None
                if mon is not None:
                    m = mon if m is None else m + mon
                if m is None:
                    m = np.zeros(frames, np.float32)
                else:
                    self.last_activity = time.monotonic()
                ring.write(np.clip(m, -1.0, 1.0).reshape(-1, 1))
            dt = (time.perf_counter() - t0) * 1000.0
            self.proc_ms += (dt - self.proc_ms) * 0.05
        except Exception:  # noqa: BLE001
            self._log_once("mic")

    # ---------------------------------------------------------------- тест «как меня слышат»
    def start_recording(self, seconds: float = 6.0) -> None:
        n = int(SAMPLE_RATE * seconds)
        self._rec = [np.zeros(n, np.float32), np.zeros(n, np.float32), 0]

    @property
    def recording_progress(self) -> float | None:
        rec = self._rec
        return None if rec is None else rec[2] / len(rec[0])

    def take_recording(self):
        """→ (исходный звук, то, что слышат другие) после окончания записи, иначе None."""
        rec = self._rec
        if rec is None or rec[2] < len(rec[0]):
            return None
        self._rec = None
        return rec[0], rec[1]

    def cancel_recording(self) -> None:
        self._rec = None

    def stop_mic(self) -> None:
        self._mic_active = False
        _close(self._mic_in)
        if self._cable is not None:
            _close(self._cable.stream)
        self._mic_in = None
        self._cable = None
        self._cable_ring = None
        self.mic_level = 0.0
        if self._mon_ring is not None:  # контроль переключается на прямое воспроизведение
            self._mon_ring = Ring(1, self.block)

    # ---------------------------------------------------------------- контроль (наушники)
    def open_monitor(self, device: str) -> None:
        if self._mon is not None and self.monitor_device == device and self._mon.stream.active:
            return
        self.close_monitor()
        try:
            self._mon_ring = Ring(1, self.block)
            self._mon = _Output(self.find(device, True), self.block, self._settings, 1, self._monitor_fill)
            self._mon.stream.start()
        except Exception as err:
            self.close_monitor()
            raise EngineError(f"не удалось открыть наушники для контроля: {err}") from err
        self.monitor_device = device
        self.last_activity = time.monotonic()
        log.info("Контроль в наушники: %s", device)

    def _monitor_fill(self, buf: np.ndarray) -> None:
        if self._mic_active:  # звук готовит колбэк микрофона — синхронно с тем, что слышат другие
            self._mon_ring.read(buf)
            return
        _, _, mon = self.mixer.render(len(buf), monitor_only=True)
        if mon is None:
            buf.fill(0)
        else:
            np.clip(mon, -1.0, 1.0, out=buf[:, 0])
            self.last_activity = time.monotonic()

    def close_monitor(self) -> None:
        if self._mon is not None:
            _close(self._mon.stream)
        self._mon = None
        self._mon_ring = None
        self.monitor_device = None

    # ---------------------------------------------------------------- наушники (звук системы)
    def start_spk(self, cable_capture: str, headphones: str) -> None:
        self.stop_spk()
        b = self.block
        try:
            src = self.find(cable_capture, output=False)
            in_ch = max(1, min(2, int(sd.query_devices(src)["max_input_channels"])))
            self._spk_ring = Ring(2, b)
            self._spk_out = _Output(self.find(headphones, True), b, self._settings, 2, self._spk_ring.read)
            self._spk_in = sd.InputStream(
                device=src, channels=in_ch, samplerate=SAMPLE_RATE, blocksize=b, dtype="float32",
                latency="low", extra_settings=self._settings, callback=self._spk_cb,
            )
            self._spk_out.stream.start()
            self._spk_in.start()
        except Exception as err:
            self.stop_spk()
            raise EngineError(f"не удалось запустить обработку звука наушников: {err}") from err
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
        _close(self._spk_in)
        if self._spk_out is not None:
            _close(self._spk_out.stream)
        self._spk_in = self._spk_out = self._spk_ring = None
        self.spk_level = 0.0

    def stop(self) -> None:
        self.stop_mic()
        self.stop_spk()
        self.close_monitor()
        self.mixer.stop()

    # ---------------------------------------------------------------- диагностика
    def test_tone(self, device: str, seconds: float = 0.4) -> None:
        """Короткий сигнал в устройство (проверка вывода звука)."""
        t = np.arange(int(SAMPLE_RATE * seconds)) / SAMPLE_RATE
        tone = (0.2 * np.sin(2 * np.pi * 660 * t) * np.minimum(1, np.minimum(t, seconds - t) * 40)).astype(np.float32)
        self.open_monitor(device)
        self.mixer.play(tone, 1.0, to_mic=False, monitor=True, tag="test")

    def measure_input(self, device: str, seconds: float = 0.7) -> float:
        """Пиковый уровень микрофона за seconds (проверка записи). → дБ."""
        idx = self.find(device, output=False)
        ch = max(1, min(2, int(sd.query_devices(idx)["max_input_channels"])))
        peak = [0.0]

        def cb(indata, frames, _t, _s):
            peak[0] = max(peak[0], _peak(indata))

        with sd.InputStream(device=idx, channels=ch, samplerate=SAMPLE_RATE, blocksize=self.block,
                            dtype="float32", extra_settings=self._settings, callback=cb):
            time.sleep(seconds)
        return 20 * np.log10(max(peak[0], 1e-6))
