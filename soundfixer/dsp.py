"""Обработка звука в реальном времени.

Всё векторизовано на numpy, тяжёлые фильтры/эффекты — pedalboard (C++/JUCE).
Обработка блока 10 мс занимает десятки микросекунд, поэтому нагрузка на CPU
практически нулевая. Параметры меняются «на лету» без пересоздания цепочки,
а изменения применяются в аудиопотоке в начале блока — без блокировок и щелчков.
"""

from __future__ import annotations

import math
import threading

import numpy as np
from pedalboard import (
    Chorus,
    Compressor,
    Delay,
    Distortion,
    Gain,
    HighpassFilter,
    HighShelfFilter,
    Limiter,
    LowpassFilter,
    LowShelfFilter,
    NoiseGate,
    PeakFilter,
    Pedalboard,
    Reverb,
)

from .config import EQ_BANDS

SAMPLE_RATE = 48000


def pct_to_db(percent: float) -> float:
    return 20.0 * math.log10(max(percent, 1.0) / 100.0)


# --------------------------------------------------------------------------- буферы
class Ring:
    """Кольцевой буфер между входным и выходным потоками разных устройств.

    Поглощает дрейф тактовой частоты: при переполнении отбрасывает старое,
    при опустошении выдаёт тишину и заново набирает запас (target).
    """

    def __init__(self, channels: int, target: int, capacity: int | None = None):
        self.target = target
        self.capacity = capacity or max(target * 16, 8192)
        self.buf = np.zeros((self.capacity, channels), np.float32)
        self.r = 0
        self.w = 0
        self.primed = False
        self.lock = threading.Lock()

    def write(self, data: np.ndarray) -> None:
        n = len(data)
        if n > self.capacity:
            data = data[-self.capacity:]
            n = self.capacity
        with self.lock:
            if self.w - self.r + n > self.capacity:
                self.r = self.w + n - self.target
            i = self.w % self.capacity
            first = min(n, self.capacity - i)
            self.buf[i:i + first] = data[:first]
            if first < n:
                self.buf[:n - first] = data[first:]
            self.w += n

    def read(self, out: np.ndarray) -> None:
        n = len(out)
        with self.lock:
            fill = self.w - self.r
            if not self.primed:
                if fill < self.target + n:
                    out.fill(0)
                    return
                self.primed = True
            if fill < n:  # опустошение: отдаём что есть и набираем запас заново
                self._copy_out(out[:fill], fill)
                out[fill:] = 0
                self.primed = False
                return
            self._copy_out(out, n)
            if fill - n > self.target * 4:  # слишком большая задержка — догоняем
                self.r = self.w - self.target

    def _copy_out(self, out: np.ndarray, n: int) -> None:
        if n <= 0:
            return
        i = self.r % self.capacity
        first = min(n, self.capacity - i)
        out[:first] = self.buf[i:i + first]
        if first < n:
            out[first:n] = self.buf[:n - first]
        self.r += n


# --------------------------------------------------------------------------- эффекты
class PitchShifter:
    """Сдвиг высоты тона на двух скользящих линиях задержки с кроссфейдом sin².

    Задержка ~ window/2 (около 20 мс), без «растягивания» во времени, O(n) на блок.
    """

    def __init__(self, window: int = 1920, size: int = 1 << 14):
        self.window = window
        self.mask = size - 1
        self.buf = np.zeros(size, np.float32)
        self.w = 0
        self.phase = 0.0
        self.ratio = 1.0

    def set_semitones(self, st: float) -> None:
        self.ratio = 2.0 ** (st / 12.0)

    def process(self, x: np.ndarray) -> np.ndarray:
        n = len(x)
        pos = self.w + np.arange(n)
        self.buf[pos & self.mask] = x
        step = (1.0 - self.ratio) / self.window
        phases = np.mod(self.phase + step * np.arange(1, n + 1), 1.0)
        self.phase = float(phases[-1])
        out = self._tap(pos, phases) * np.sin(np.pi * phases) ** 2
        ph2 = np.mod(phases + 0.5, 1.0)
        out += self._tap(pos, ph2) * np.sin(np.pi * ph2) ** 2
        self.w += n
        return out.astype(np.float32, copy=False)

    def _tap(self, pos: np.ndarray, phases: np.ndarray) -> np.ndarray:
        rd = pos - phases * self.window
        i0 = np.floor(rd)
        frac = rd - i0
        i0 = i0.astype(np.int64)
        a = self.buf[i0 & self.mask]
        b = self.buf[(i0 + 1) & self.mask]
        return a + (b - a) * frac


class RingModulator:
    """«Робот»: кольцевая модуляция низкой частотой."""

    def __init__(self, freq: float = 55.0):
        self.freq = freq
        self.depth = 0.0
        self.phase = 0.0

    def process(self, x: np.ndarray) -> np.ndarray:
        n = len(x)
        inc = 2.0 * np.pi * self.freq / SAMPLE_RATE
        ph = self.phase + inc * np.arange(1, n + 1)
        self.phase = float(ph[-1] % (2.0 * np.pi))
        mod = (1.0 - self.depth) + self.depth * np.sin(ph)
        return (x * mod).astype(np.float32, copy=False)


# --------------------------------------------------------------------------- цепочки
class _Chain:
    """База: структура цепочки зависит от включённых эффектов; параметры
    обновляются на месте, а пересборка происходит только при смене структуры."""

    def __init__(self):
        self._pending = None
        self._sig = None

    def configure(self, cfg: dict) -> None:
        self._pending = dict(cfg)  # атомарно; применится в аудиопотоке

    def _apply_pending(self) -> None:
        cfg = self._pending
        if cfg is None:
            return
        self._pending = None
        sig = self._signature(cfg)
        if sig != self._sig:
            self._build(cfg)
            self._sig = sig
        self._update(cfg)


class MicChain(_Chain):
    """Микрофон (моно): срез гула → шумодав → голосовые эффекты → компрессор → усиление → лимитер."""

    def __init__(self):
        super().__init__()
        self.pitch = PitchShifter()
        self.robot = RingModulator()
        self.pre = Pedalboard()
        self.post = Pedalboard()
        self.p: dict = {}
        self.use_pitch = self.use_robot = False

    @staticmethod
    def _signature(c):
        return (
            c.get("hpf"), c.get("gate"), c.get("radio"), c.get("compressor"),
            c.get("pitch", 0) != 0, c.get("robot", 0) > 0, c.get("distortion", 0) > 0,
            c.get("chorus", 0) > 0, c.get("echo", 0) > 0, c.get("reverb", 0) > 0,
            c.get("gain", 100) != 100,
        )

    def _build(self, c):
        p = self.p = {}
        pre = []
        if c.get("hpf"):
            pre.append(HighpassFilter(90))
        if c.get("gate"):
            p["gate"] = NoiseGate(threshold_db=-50, ratio=12, attack_ms=1, release_ms=180)
            pre.append(p["gate"])
        post = []
        if c.get("radio"):
            post += [HighpassFilter(450), LowpassFilter(3000)]
        if c.get("distortion", 0) > 0:
            p["dist"] = Distortion(10)
            p["dist_comp"] = Gain(0)
            post += [p["dist"], p["dist_comp"]]
        if c.get("chorus", 0) > 0:
            p["chorus"] = Chorus(rate_hz=1.3, depth=0.35, centre_delay_ms=8, mix=0.5)
            post.append(p["chorus"])
        if c.get("echo", 0) > 0:
            p["echo"] = Delay(delay_seconds=0.22, feedback=0.35, mix=0.3)
            post.append(p["echo"])
        if c.get("reverb", 0) > 0:
            p["reverb"] = Reverb(room_size=0.6, damping=0.5, wet_level=0.3, dry_level=0.8, width=0.0)
            post.append(p["reverb"])
        if c.get("compressor"):
            post.append(Compressor(threshold_db=-22, ratio=3.5, attack_ms=4, release_ms=120))
        if c.get("gain", 100) != 100:
            p["gain"] = Gain(0)
            post.append(p["gain"])
        post.append(Limiter(threshold_db=-1.0, release_ms=80))  # защита от перегруза
        self.pre = Pedalboard(pre)
        self.post = Pedalboard(post)

    def _update(self, c):
        p = self.p
        if "gate" in p:
            p["gate"].threshold_db = float(c.get("gate_db", -50))
        st = float(c.get("pitch", 0))
        self.use_pitch = st != 0
        self.pitch.set_semitones(st)
        self.robot.depth = min(1.0, c.get("robot", 0) / 100.0)
        self.use_robot = self.robot.depth > 0
        if "dist" in p:
            drive = 3 + c["distortion"] * 0.32  # до ~35 дБ
            p["dist"].drive_db = drive
            p["dist_comp"].gain_db = -drive * 0.55
        if "chorus" in p:
            p["chorus"].mix = min(1.0, c["chorus"] / 100.0)
        if "echo" in p:
            v = c["echo"] / 100.0
            p["echo"].mix = 0.15 + 0.4 * v
            p["echo"].feedback = 0.2 + 0.5 * v
        if "reverb" in p:
            v = c["reverb"] / 100.0
            p["reverb"].room_size = 0.35 + 0.6 * v
            p["reverb"].wet_level = 0.6 * v
            p["reverb"].dry_level = 1.0 - 0.45 * v
        if "gain" in p:
            p["gain"].gain_db = pct_to_db(c.get("gain", 100))

    def process(self, x: np.ndarray) -> np.ndarray:
        """x — моно float32, возвращает новый массив той же длины."""
        self._apply_pending()
        n = len(x)
        y = self.pre.process(x.reshape(1, n), SAMPLE_RATE, reset=False)[0] if len(self.pre) else x
        if self.use_pitch:
            y = self.pitch.process(y)
        if self.use_robot:
            y = self.robot.process(y)
        return self.post.process(np.ascontiguousarray(y, np.float32).reshape(1, n), SAMPLE_RATE, reset=False)[0]


class SpeakerChain(_Chain):
    """Наушники (стерео): усиление → эквалайзер → ночной режим → ширина/баланс/кроссфид → лимитер."""

    def __init__(self):
        super().__init__()
        self.board = Pedalboard()
        self.cross = Pedalboard()
        self.p: dict = {}
        self.width = 1.0
        self.gl = self.gr = 1.0
        self.use_cross = False

    @staticmethod
    def _signature(c):
        eq = tuple(bool(g) for g in c.get("eq", ()))
        return (c.get("gain", 100) != 100, eq, c.get("night"), c.get("crossfeed"))

    def _build(self, c):
        p = self.p = {}
        board = []
        if c.get("gain", 100) != 100:
            p["gain"] = Gain(0)
            board.append(p["gain"])
        for i, g in enumerate(c.get("eq", ())):
            if not g:
                continue
            f = EQ_BANDS[i]
            if i == 0:
                flt = LowShelfFilter(f, 0, 0.7)
            elif i == len(EQ_BANDS) - 1:
                flt = HighShelfFilter(f, 0, 0.7)
            else:
                flt = PeakFilter(f, 0, 1.0)
            p[f"eq{i}"] = flt
            board.append(flt)
        if c.get("night"):
            board += [Compressor(threshold_db=-28, ratio=4, attack_ms=8, release_ms=250), Gain(8)]
        board.append(Limiter(threshold_db=-0.8, release_ms=60))
        self.board = Pedalboard(board)
        self.use_cross = bool(c.get("crossfeed"))
        self.cross = Pedalboard([LowpassFilter(700)]) if self.use_cross else Pedalboard()

    def _update(self, c):
        p = self.p
        if "gain" in p:
            p["gain"].gain_db = pct_to_db(c.get("gain", 100))
        for i, g in enumerate(c.get("eq", ())):
            if f"eq{i}" in p:
                p[f"eq{i}"].gain_db = float(g)
        self.width = c.get("width", 100) / 100.0
        b = max(-1.0, min(1.0, c.get("balance", 0) / 100.0))
        self.gl, self.gr = min(1.0, 1.0 - b), min(1.0, 1.0 + b)

    def process(self, x: np.ndarray) -> np.ndarray:
        """x — float32 (frames, 2) → новый массив (frames, 2)."""
        self._apply_pending()
        s = np.ascontiguousarray(x.T)  # (2, n) для pedalboard
        if self.width != 1.0:
            mid = (s[0] + s[1]) * 0.5
            side = (s[0] - s[1]) * (0.5 * self.width)
            s[0] = mid + side
            s[1] = mid - side
        if self.use_cross:
            lp = self.cross.process(s[::-1].copy(), SAMPLE_RATE, reset=False)
            s = (s + 0.35 * lp) * (1 / 1.2)
        if self.gl != 1.0:
            s[0] *= self.gl
        if self.gr != 1.0:
            s[1] *= self.gr
        return self.board.process(np.ascontiguousarray(s, np.float32), SAMPLE_RATE, reset=False).T


# --------------------------------------------------------------------------- микшер
class ClipMixer:
    """Подмешивает озвучку текста и звуки саундборда в микрофон (и в наушники для контроля)."""

    def __init__(self):
        self._clips: list[list] = []  # [data, pos, gain, through_fx, monitor]
        self._lock = threading.Lock()

    @property
    def busy(self) -> bool:
        return bool(self._clips)

    def play(self, data: np.ndarray, gain: float = 1.0, through_fx: bool = False, monitor: bool = True) -> None:
        with self._lock:
            self._clips.append([data, 0, float(gain), through_fx, monitor])

    def stop(self) -> None:
        with self._lock:
            self._clips.clear()

    def render(self, n: int):
        """→ (pre_fx, post_fx, monitor) — массивы длины n или None."""
        if not self._clips:
            return None, None, None
        pre = post = mon = None
        with self._lock:
            alive = []
            for clip in self._clips:
                data, pos, gain, through_fx, monitor = clip
                chunk = data[pos:pos + n]
                m = len(chunk)
                if m:
                    seg = np.zeros(n, np.float32)
                    seg[:m] = chunk * gain
                    if through_fx:
                        pre = seg if pre is None else pre + seg
                    else:
                        post = seg if post is None else post + seg
                    if monitor:
                        mon = seg.copy() if mon is None else mon + seg
                clip[1] = pos + n
                if clip[1] < len(data):
                    alive.append(clip)
            self._clips = alive
        return pre, post, mon


def resample(data: np.ndarray, src_rate: int, dst_rate: int = SAMPLE_RATE) -> np.ndarray:
    if src_rate == dst_rate or len(data) == 0:
        return data.astype(np.float32, copy=False)
    n = int(round(len(data) * dst_rate / src_rate))
    x = np.linspace(0, len(data) - 1, n)
    return np.interp(x, np.arange(len(data)), data).astype(np.float32)
