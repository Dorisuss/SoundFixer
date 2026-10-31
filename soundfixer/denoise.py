"""ИИ-шумоподавление: нейросеть RNNoise 0.2 (Xiph) + умный гейт, обучаемый на шуме пользователя.

Как это работает:
  1. RNNoise — рекуррентная нейросеть, обученная отделять речь от любых других звуков
     (вентиляторы, улица, клавиатура, клики). Работает кадрами по 10 мс, задержка 20 мс
     (у модели 0.2 есть свёрточный слой с заглядыванием на один кадр вперёд).
  2. Умный гейт между фразами: использует вероятность речи от нейросети. Нейросеть «видит»
     на 2 кадра вперёд относительно выходного звука, поэтому гейт открывается до начала слова
     и не съедает первые звуки — без какой-либо дополнительной задержки.
  3. Обучение («запомнить шум»): 10 секунд вы молчите, а вокруг звучит обычный фон — печать,
     клики, движения. Программа запоминает:
       * громкость остаточного шума после нейросети (уровень, ниже которого — точно не голос);
       * как часто ваш шум «обманывает» нейросеть (и поднимает порог голоса ровно настолько);
       * спектральные «отпечатки» ваших щелчков клавиатуры/мыши (кластеризация k-means),
         чтобы узнавать именно эти звуки и не пропускать их.
     Профиль сохраняется в настройках и используется после перезапуска.

Нагрузка: RNNoise ~0.2–0.6 мс на кадр 10 мс, анализ гейта ~0.05 мс — около 3–6% одного ядра.
"""

from __future__ import annotations

import ctypes
import importlib.util
import logging
import os
import sys
import time
from collections import deque
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)

SAMPLE_RATE = 48000
FRAME = 480            # 10 мс — размер кадра RNNoise
DELAY_FRAMES = 2       # выход RNNoise 0.2 отстаёт от входа на 960 сэмплов (20 мс)
BANDS = 24
N_FFT = 512
HOLD_FRAMES = 22       # гейт держится открытым 220 мс после последнего слова
RELEASE = float(np.exp(-FRAME / (0.045 * SAMPLE_RATE)))  # плавное закрытие (~45 мс)
ATTACK_SAMPLES = 64    # открытие за 1.3 мс — без щелчка
LEARN_SECONDS = 10
WARMUP_FRAMES = 15     # 150 мс после запуска гейт закрыт, пока нейросеть «прогревается»


# --------------------------------------------------------------------------- библиотека
def find_library() -> str | None:
    """Ищет rnnoise.dll: рядом с exe (PyInstaller), рядом с программой, в пакете pyrnnoise.

    Пакет pyrnnoise только находится, но не импортируется — его __init__ тянет тяжёлые
    зависимости (matplotlib, audiolab), а нам нужна лишь сама библиотека.
    """
    if sys.platform == "win32":
        names = ["rnnoise.dll"]
    elif sys.platform == "darwin":
        names = ["librnnoise.dylib"]
    else:
        names = ["librnnoise.so"]
    dirs: list[str] = []
    env = os.environ.get("SOUNDFIXER_RNNOISE")
    if env:
        if os.path.isfile(env):
            return env
        dirs.append(env)
    base = getattr(sys, "_MEIPASS", None)
    if base:
        dirs += [os.path.join(base, "pyrnnoise"), base]
    app_dir = Path(__file__).resolve().parent
    dirs += [str(app_dir), str(app_dir.parent)]
    try:
        spec = importlib.util.find_spec("pyrnnoise")
    except (ImportError, ValueError):
        spec = None
    if spec and spec.submodule_search_locations:
        dirs += list(spec.submodule_search_locations)
    for d in dirs:
        for n in names:
            p = os.path.join(d, n)
            if os.path.isfile(p):
                return p
    return None


_lib = None
_lib_error = ""
_tried = False


def load_library(force: bool = False):
    """Загружает RNNoise один раз (вызывать из потока управления, не из аудиопотока).

    Неудачная попытка запоминается, чтобы аудиопоток не искал файл на диске при каждом
    изменении настроек; force=True — повторить (например, после установки пакета).
    """
    global _lib, _lib_error, _tried
    if _lib is not None or (_tried and not force):
        return _lib
    _tried = True
    path = find_library()
    if path is None:
        _lib_error = "не установлен пакет pyrnnoise (ИИ-шумоподавление)"
        return None
    try:
        lib = ctypes.CDLL(path)
        lib.rnnoise_create.restype = ctypes.c_void_p
        lib.rnnoise_create.argtypes = [ctypes.c_void_p]
        lib.rnnoise_destroy.argtypes = [ctypes.c_void_p]
        lib.rnnoise_destroy.restype = None
        lib.rnnoise_process_frame.restype = ctypes.c_float
        lib.rnnoise_process_frame.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_float),
                                              ctypes.POINTER(ctypes.c_float)]
        lib.rnnoise_get_frame_size.restype = ctypes.c_int
        if lib.rnnoise_get_frame_size() != FRAME:
            raise OSError(f"неожиданный размер кадра {lib.rnnoise_get_frame_size()}")
    except (OSError, AttributeError) as err:
        _lib_error = f"не удалось загрузить {os.path.basename(path)}: {err}"
        log.error("RNNoise: %s", _lib_error)
        return None
    _lib = lib
    _lib_error = ""
    log.info("RNNoise загружен: %s", path)
    return lib


def library_error() -> str:
    return _lib_error


def library_ready() -> bool:
    """Без загрузки и обращения к диску — безопасно вызывать из аудиопотока."""
    return _lib is not None


class RNNoise:
    """Одно состояние нейросети (моно). Буферы выделены заранее — без аллокаций в аудиопотоке."""

    def __init__(self, lib):
        self._lib = lib
        self._st = lib.rnnoise_create(None)
        if not self._st:
            raise MemoryError("rnnoise_create вернул NULL")
        self._in = np.zeros(FRAME, np.float32)
        self._out = np.zeros(FRAME, np.float32)
        fp = ctypes.POINTER(ctypes.c_float)
        self._pin = self._in.ctypes.data_as(fp)
        self._pout = self._out.ctypes.data_as(fp)
        self._proc = lib.rnnoise_process_frame

    def process(self, frame: np.ndarray) -> tuple[np.ndarray, float]:
        """frame: 480 float32 в диапазоне [-1, 1] → (очищенный кадр (новый массив), вероятность речи)."""
        np.multiply(frame, 32768.0, out=self._in)
        vad = self._proc(self._st, self._pout, self._pin)
        return self._out * (1.0 / 32768.0), float(vad)

    def close(self):
        if self._st:
            self._lib.rnnoise_destroy(self._st)
            self._st = None

    def __del__(self):
        try:
            self.close()
        except Exception:  # noqa: BLE001
            pass


# --------------------------------------------------------------------------- анализ спектра
def _band_matrix() -> np.ndarray:
    freqs = np.fft.rfftfreq(N_FFT, 1.0 / SAMPLE_RATE)

    def mel(f):
        return 2595.0 * np.log10(1.0 + f / 700.0)

    def imel(m):
        return 700.0 * (10.0 ** (m / 2595.0) - 1.0)

    edges = imel(np.linspace(mel(80.0), mel(18000.0), BANDS + 2))
    m = np.zeros((BANDS, len(freqs)), np.float32)
    for b in range(BANDS):
        lo, c, hi = edges[b], edges[b + 1], edges[b + 2]
        m[b] = np.clip(np.minimum((freqs - lo) / (c - lo), (hi - freqs) / (hi - c)), 0.0, None)
    m /= m.sum(axis=1, keepdims=True) + 1e-9
    return m


_BANDS = _band_matrix()
_WINDOW = np.hanning(FRAME).astype(np.float32)


def frame_features(x: np.ndarray) -> tuple[float, np.ndarray]:
    """→ (энергия кадра в дБ, нормированный «отпечаток» спектра — единичный вектор из 24 полос)."""
    spec = np.fft.rfft(x * _WINDOW, N_FFT)
    power = spec.real * spec.real + spec.imag * spec.imag
    log_bands = 10.0 * np.log10(_BANDS @ power + 1e-12)
    energy = 10.0 * np.log10(float(np.dot(x, x)) / len(x) + 1e-12)
    shape = log_bands - log_bands.mean()
    norm = float(np.sqrt(np.dot(shape, shape)))
    return energy, (shape / norm if norm > 1e-6 else shape)


# --------------------------------------------------------------------------- профиль шума
class NoiseProfile:
    """То, что программа запомнила о вашем окружении."""

    def __init__(self, data: dict):
        self.vad_hi = float(data.get("vad_hi", 0.3))
        self.floor_db = float(data.get("floor_db", -80.0))
        self.click_db = float(data.get("click_db", -70.0))
        self.in_floor_db = float(data.get("in_floor_db", -70.0))
        cents = data.get("centroids") or []
        self.centroids = np.asarray(cents, np.float32).reshape(-1, BANDS) if cents else None
        self.radius = float(data.get("radius", 0.9))
        self.created = data.get("created", 0)

    @staticmethod
    def build(vads, e_in, e_out, shapes) -> tuple[dict, str]:
        """Строит профиль по кадрам, записанным в тишине (без голоса). → (данные, описание/предупреждение)."""
        vads = np.asarray(vads, np.float32)
        e_in = np.asarray(e_in, np.float32)
        e_out = np.asarray(e_out, np.float32)
        shapes = np.asarray(shapes, np.float32)
        n = len(vads)
        if n < 100:
            raise ValueError("слишком мало данных")
        voiced = float((vads > 0.85).mean())
        if voiced > 0.2:
            raise ValueError("во время записи звучал голос — повторите, молча занимаясь своими делами "
                             "(печатайте, кликайте мышью, двигайтесь)")
        in_floor = float(np.percentile(e_in, 40))
        data = {
            "vad_hi": float(min(0.85, np.percentile(vads, 99))),
            "floor_db": float(np.percentile(e_out, 50)),
            "click_db": float(np.percentile(e_out, 99)),
            "in_floor_db": in_floor,
            "frames": n,
            "created": int(time.time()),
        }
        # «отпечатки» звонких событий: щелчки клавиш и мыши, стуки, шорохи
        loud = shapes[e_in > in_floor + 6.0]
        n_events = len(loud)
        if n_events >= 20:
            k = int(min(8, max(2, n_events // 15)))
            cents, radius = _spherical_kmeans(loud, k)
            data["centroids"] = cents.round(5).tolist()
            data["radius"] = radius
        summary = (f"Изучено {n / 100:.0f} с: фон {data['floor_db']:.0f} дБ, щелчки после нейросети до "
                   f"{data['click_db']:.0f} дБ, ложные срабатывания голоса до {data['vad_hi']:.2f}, "
                   f"звуковых событий: {n_events}"
                   + (f", запомнено отпечатков: {len(data['centroids'])}" if "centroids" in data else ""))
        data["summary"] = summary
        return data, summary


def _spherical_kmeans(x: np.ndarray, k: int, iters: int = 20) -> tuple[np.ndarray, float]:
    rng = np.random.default_rng(1)
    cents = [x[rng.integers(len(x))]]
    for _ in range(1, k):  # k-means++ по косинусному расстоянию
        sims = np.max(x @ np.array(cents).T, axis=1).astype(np.float64)
        d = np.clip(1.0 - sims, 0, None) ** 2
        p = d / d.sum() if d.sum() > 1e-12 else None
        cents.append(x[rng.choice(len(x), p=p)])
    c = np.array(cents, np.float32)
    for _ in range(iters):
        labels = np.argmax(x @ c.T, axis=1)
        for j in range(k):
            members = x[labels == j]
            if len(members):
                v = members.sum(axis=0)
                c[j] = v / (np.linalg.norm(v) + 1e-9)
    sims = np.max(x @ c.T, axis=1)
    radius = float(np.clip(np.percentile(sims, 15), 0.72, 0.97))
    return c, radius


# --------------------------------------------------------------------------- шумодав
class Denoiser:
    """Поточный шумодав. process() вызывается только из аудиопотока."""

    def __init__(self, lib):
        self._rnn = RNNoise(lib)
        self.strength = 1.0
        self.profile: NoiseProfile | None = None
        self.gate = True
        self.vad = 0.0           # для индикатора в окне
        self.open = False
        self._vads = deque([0.0] * (DELAY_FRAMES + 1), maxlen=DELAY_FRAMES + 1)
        self._feat = deque([(-100.0, np.zeros(BANDS, np.float32), False)] * (DELAY_FRAMES + 1),
                           maxlen=DELAY_FRAMES + 1)
        self._dry = deque([np.zeros(FRAME, np.float32)] * (DELAY_FRAMES + 1), maxlen=DELAY_FRAMES + 1)
        self._hold = 0
        self._warmup = WARMUP_FRAMES
        self._g = 0.0
        self._inbuf = np.zeros(0, np.float32)
        self._outbuf = np.zeros(0, np.float32)
        self._primed = False
        self._attack = np.linspace(0.0, 1.0, ATTACK_SAMPLES, dtype=np.float32)
        self._lin = np.linspace(0.0, 1.0, FRAME, dtype=np.float32)
        # обучение
        self._learn_left = 0
        self._learn: list | None = None

    # ---------- настройки (вызываются из аудиопотока через MicChain._apply_pending)
    def configure(self, strength: float, gate: bool, profile: NoiseProfile | None):
        self.strength = max(0.0, min(1.0, strength))
        self.gate = gate
        self.profile = profile

    # ---------- обучение
    def start_learning(self, seconds: float = LEARN_SECONDS):
        self._learn = []
        self._learn_left = int(seconds * SAMPLE_RATE / FRAME)

    @property
    def learning_progress(self) -> float | None:
        if self._learn is None:
            return None
        total = len(self._learn) + self._learn_left
        return len(self._learn) / total if total else 1.0

    def take_learned(self):
        """После окончания записи → (vads, e_in, e_out, shapes) или None, если ещё идёт."""
        if self._learn is None or self._learn_left > 0:
            return None
        rows = self._learn
        self._learn = None
        if not rows:
            return None
        vads, e_in, e_out, shapes = zip(*rows)
        return vads, e_in, e_out, shapes

    def cancel_learning(self):
        self._learn = None
        self._learn_left = 0

    # ---------- обработка
    def process(self, x: np.ndarray) -> np.ndarray:
        """Любой размер блока; при блоке, кратном 480, дополнительной задержки нет."""
        n = len(x)
        if n == FRAME:
            return self._frame(x)
        buf = np.concatenate((self._inbuf, x)) if len(self._inbuf) else x
        out = [self._outbuf] if len(self._outbuf) else []
        pos = 0
        while len(buf) - pos >= FRAME:
            out.append(self._frame(buf[pos:pos + FRAME]))
            pos += FRAME
        self._inbuf = buf[pos:].copy()
        res = np.concatenate(out) if out else np.zeros(0, np.float32)
        if not self._primed and n % FRAME:
            res = np.concatenate((np.zeros(FRAME, np.float32), res))  # постоянная задержка 1 кадр
            self._primed = True
        if len(res) < n:
            res = np.concatenate((np.zeros(n - len(res), np.float32), res))
        self._outbuf = res[n:]
        return res[:n]

    def _frame(self, x: np.ndarray) -> np.ndarray:
        x = np.ascontiguousarray(x, np.float32)
        wet, vad = self._rnn.process(x)
        e_in, shape = frame_features(x)
        self._vads.append(vad)
        self._feat.append((e_in, shape, self._looks_like_noise(e_in, shape)))
        self._dry.append(x.copy())
        self.vad = vad

        e_out = 10.0 * np.log10(float(np.dot(wet, wet)) / FRAME + 1e-12)
        learn = self._learn  # локальная ссылка: окно может отменить обучение в любой момент
        if learn is not None and self._learn_left > 0:
            learn.append((vad, e_in, e_out, shape))
            self._learn_left -= 1

        s = self.strength
        if s < 0.99:  # «сила»: сколько исходного сигнала подмешать (выровнено по задержке нейросети)
            a = 10.0 ** (-(12.0 + 48.0 * s) / 20.0)
            wet = wet * (1.0 - a) + self._dry[0] * a

        if not self.gate:
            self.open = True
            return wet
        target = 1.0 if self._decide(e_out) else 10.0 ** (-(14.0 + 40.0 * s) / 20.0)
        g0 = self._g
        if target >= g0:
            if target == g0:
                return wet * g0 if g0 != 1.0 else wet
            ramp = np.full(FRAME, target, np.float32)
            ramp[:ATTACK_SAMPLES] = g0 + (target - g0) * self._attack
            self._g = target
        else:
            g1 = target + (g0 - target) * RELEASE
            ramp = g0 + (g1 - g0) * self._lin
            self._g = g1 if g1 - target > 1e-4 else target
        return wet * ramp

    def _looks_like_noise(self, e_in: float, shape: np.ndarray) -> bool:
        """Похож ли входной кадр на один из запомненных звуков (щелчок клавиши, клик мыши…)."""
        prof = self.profile
        if prof is None or prof.centroids is None or e_in <= prof.in_floor_db + 6.0:
            return False
        return float(np.max(prof.centroids @ shape)) >= prof.radius

    def _decide(self, e_out: float) -> bool:
        """Есть ли речь в выходном кадре?

        Нейросеть «видит» на 2 кадра вперёд относительно выходного звука: гейт открывается
        заранее и не съедает начало слова. С профилем шума кадры, похожие на ваши щелчки,
        не могут открыть гейт, а порог голоса поднимается выше ложных срабатываний.
        """
        if self._warmup > 0:  # первые 150 мс состояние нейросети ещё не устоялось
            self._warmup -= 1
            self.open = False
            return False
        prof = self.profile
        vads = list(self._vads)                    # [выровненный, +10 мс, +20 мс]
        feats = list(self._feat)
        v_al = vads[0]
        if prof is None:
            thr = 0.5
            noise_like = False
            v_ahead = max(vads[1], vads[2])
        else:
            # обучение может только ужесточить порог: если ваш шум «обманывает» нейросеть,
            # порог поднимается выше самых сильных ложных срабатываний
            thr = min(0.9, max(0.5, prof.vad_hi + 0.15))
            noise_like = feats[0][2] or e_out < prof.click_db + 3.0
            ahead = [v for v, f in zip(vads[1:], feats[1:]) if not f[2]]
            v_ahead = max(ahead) if ahead else 0.0
        speech_now = v_al >= thr and not (noise_like and v_al < 0.92)
        speech_soon = v_ahead >= max(thr, 0.8)
        if speech_now or speech_soon:
            self._hold = HOLD_FRAMES
        elif self._hold > 0:
            self._hold -= 1
        self.open = self._hold > 0
        return self.open

    def close(self):
        self._rnn.close()
