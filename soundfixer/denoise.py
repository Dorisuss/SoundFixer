"""ИИ-шумоподавление: DeepFilterNet3 (по умолчанию) или RNNoise + умный гейт пауз.

Нейросети:
  * DeepFilterNet3 «низкая задержка» — лучшее качество, задержка 10 мс. Не «рвёт» голос,
    когда вы говорите и печатаете одновременно: на реальной записи печати искажение речи
    в 3–4 раза меньше, чем у RNNoise, а «провалов» голоса в 2–3 раза меньше.
  * DeepFilterNet3 лёгкая — почти то же качество, задержка 30 мс, вдвое меньше нагрузки.
  * RNNoise 0.2 — самая лёгкая, задержка 20 мс; запасной вариант для слабых ПК.

DeepFilterNet работает через deepfilter.dll — официальную потоковую реализацию libDF
(Rust + tract, лицензия MIT/Apache-2.0), собранную с C-интерфейсом. Модель (.tar.gz)
проверяется по SHA-256 до загрузки: повреждённый файл не может уронить программу.

Гейт между фразами: каждая нейросеть сообщает, есть ли в кадре речь (RNNoise — вероятность
голоса, DeepFilterNet — локальное отношение сигнал/шум). Обе видят звук чуть раньше, чем он
выходит, поэтому гейт открывается до начала слова и не съедает первые звуки.

«Запомнить мой шум»: 10 секунд записи вашего фона (печать, клики, движения) — порог гейта
поднимается ровно выше того, что даёт ваш шум. Профиль сохраняется в настройках.
"""

from __future__ import annotations

import ctypes
import hashlib
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
FRAME = 480            # 10 мс — размер кадра обеих нейросетей
BANDS = 24
N_FFT = 512
HOLD_FRAMES = 22       # гейт держится открытым 220 мс после последнего слова
RELEASE = float(np.exp(-FRAME / (0.045 * SAMPLE_RATE)))  # плавное закрытие (~45 мс)
ATTACK_SAMPLES = 64    # открытие за 1.3 мс — без щелчка
LEARN_SECONDS = 10
WARMUP_FRAMES = 15     # 150 мс после запуска гейт закрыт, пока нейросеть «прогревается»

PKG_DIR = Path(__file__).resolve().parent

# движок → (название, задержка мс, файл модели, sha256)
ENGINES = {
    "dfn_ll": ("DeepFilterNet3 (низкая задержка)", 10, "DeepFilterNet3_ll_onnx.tar.gz",
               "5998e58e8ba0e09bb76986ef97b84afa065a571ef282d4a1222f341e3251cf3a"),
    "dfn": ("DeepFilterNet3 лёгкая", 30, "DeepFilterNet3_onnx.tar.gz",
            "c94d91f70911001c946e0fabb4aa9adc37045f45a03b56008cb0c8244cb63616"),
    "rnnoise": ("RNNoise", 20, None, None),
}


class DenoiseError(RuntimeError):
    pass


def _search_dirs(sub: str) -> list[str]:
    dirs = []
    base = getattr(sys, "_MEIPASS", None)
    if base:
        dirs += [os.path.join(base, "soundfixer", sub), os.path.join(base, sub), base]
    dirs += [str(PKG_DIR / sub), str(PKG_DIR), str(PKG_DIR.parent)]
    return dirs


# =========================================================================== RNNoise
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
    dirs += [str(PKG_DIR), str(PKG_DIR.parent)]
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
    """Загружает RNNoise один раз (вызывать из потока управления, не из аудиопотока)."""
    global _lib, _lib_error, _tried
    if _lib is not None or (_tried and not force):
        return _lib
    _tried = True
    path = find_library()
    if path is None:
        _lib_error = "не установлен пакет pyrnnoise"
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
    return _lib is not None


class RNNoiseBackend:
    """Одно состояние RNNoise. Выход отстаёт от входа на 2 кадра (20 мс)."""

    kind = "rnnoise"
    delay = 2

    def __init__(self, lib=None):
        lib = lib or load_library()
        if lib is None:
            raise DenoiseError(library_error() or "RNNoise недоступен")
        self._lib = lib
        self._st = lib.rnnoise_create(None)
        if not self._st:
            raise DenoiseError("rnnoise_create вернул NULL")
        self._in = np.zeros(FRAME, np.float32)
        self._out = np.zeros(FRAME, np.float32)
        fp = ctypes.POINTER(ctypes.c_float)
        self._pin = self._in.ctypes.data_as(fp)
        self._pout = self._out.ctypes.data_as(fp)
        self._proc = lib.rnnoise_process_frame

    def process(self, frame: np.ndarray) -> tuple[np.ndarray, float]:
        """→ (очищенный кадр, вероятность голоса 0..1)."""
        np.multiply(frame, 32768.0, out=self._in)
        vad = self._proc(self._st, self._pout, self._pin)
        return self._out * (1.0 / 32768.0), float(vad)

    def set_strength(self, s: float) -> None:
        pass  # для RNNoise сила регулируется подмешиванием исходного сигнала (см. Denoiser)

    def close(self):
        if self._st:
            self._lib.rnnoise_destroy(self._st)
            self._st = None


# =========================================================================== DeepFilterNet
_df_lib = None
_df_error = ""


def find_deepfilter() -> str | None:
    env = os.environ.get("SOUNDFIXER_DEEPFILTER")
    if env and os.path.isfile(env):
        return env
    names = ["deepfilter.dll"] if sys.platform == "win32" else ["libdeepfilter.so", "libdf.so"]
    for d in _search_dirs("bin"):
        for n in names:
            p = os.path.join(d, n)
            if os.path.isfile(p):
                return p
    return None


def load_deepfilter():
    global _df_lib, _df_error
    if _df_lib is not None:
        return _df_lib
    path = find_deepfilter()
    if path is None:
        _df_error = "не найден файл deepfilter.dll (папка soundfixer/bin)"
        return None
    try:
        lib = ctypes.CDLL(path)
        lib.df_create.restype = ctypes.c_void_p
        lib.df_create.argtypes = [ctypes.c_char_p, ctypes.c_float, ctypes.c_char_p]
        lib.df_get_frame_length.restype = ctypes.c_size_t
        lib.df_get_frame_length.argtypes = [ctypes.c_void_p]
        lib.df_process_frame.restype = ctypes.c_float
        lib.df_process_frame.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_float),
                                         ctypes.POINTER(ctypes.c_float)]
        lib.df_set_atten_lim.restype = None
        lib.df_set_atten_lim.argtypes = [ctypes.c_void_p, ctypes.c_float]
        lib.df_free.restype = None
        lib.df_free.argtypes = [ctypes.c_void_p]
    except (OSError, AttributeError) as err:
        _df_error = f"не удалось загрузить {os.path.basename(path)}: {err}"
        log.error("DeepFilterNet: %s", _df_error)
        return None
    _df_lib = lib
    _df_error = ""
    log.info("DeepFilterNet загружен: %s", path)
    return lib


_verified: dict[str, tuple[int, float]] = {}


def model_path(engine: str) -> str:
    """Путь к модели после проверки SHA-256 (библиотека аварийно завершает процесс на битом файле)."""
    _title, _lat, fname, sha = ENGINES[engine]
    for d in _search_dirs("models"):
        p = os.path.join(d, fname)
        if not os.path.isfile(p):
            continue
        st = os.stat(p)
        sig = (st.st_size, st.st_mtime)
        if _verified.get(p) == sig:
            return p
        h = hashlib.sha256()
        with open(p, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        if h.hexdigest() != sha:
            raise DenoiseError(f"файл модели {fname} повреждён — скачайте программу заново")
        _verified[p] = sig
        return p
    raise DenoiseError(f"не найден файл модели {fname} (папка soundfixer/models)")


def atten_for_strength(s: float) -> float:
    """Сила 0..1 → ограничение подавления в дБ (100 — без ограничения)."""
    return 100.0 if s >= 0.99 else 6.0 + 54.0 * max(0.0, s)


class DeepFilterBackend:
    """Потоковый DeepFilterNet3. Выход отстаёт от входа на 1 кадр (10 мс) у модели «низкая
    задержка» и на 3 кадра (30 мс) у лёгкой. Создание занимает ~1 с — только вне аудиопотока."""

    def __init__(self, engine: str = "dfn_ll"):
        lib = load_deepfilter()
        if lib is None:
            raise DenoiseError(_df_error or "DeepFilterNet недоступен")
        path = model_path(engine)
        self.kind = engine
        self.delay = ENGINES[engine][1] // 10
        self._lib = lib
        self._st = lib.df_create(path.encode("utf-8"), 100.0, None)  # libDF ждёт UTF-8
        if not self._st:
            raise DenoiseError("не удалось создать DeepFilterNet")
        if lib.df_get_frame_length(self._st) != FRAME:
            raise DenoiseError("неожиданный размер кадра DeepFilterNet")
        self._in = np.zeros(FRAME, np.float32)
        self._out = np.zeros(FRAME, np.float32)
        fp = ctypes.POINTER(ctypes.c_float)
        self._pin = self._in.ctypes.data_as(fp)
        self._pout = self._out.ctypes.data_as(fp)
        self._proc = lib.df_process_frame
        self._atten = 100.0

    def process(self, frame: np.ndarray) -> tuple[np.ndarray, float]:
        """→ (очищенный кадр, локальное отношение сигнал/шум в дБ: −15…35)."""
        self._in[:] = frame
        lsnr = self._proc(self._st, self._pin, self._pout)
        return self._out.copy(), float(lsnr)

    def set_strength(self, s: float) -> None:
        atten = atten_for_strength(s)
        if atten != self._atten:
            self._atten = atten
            self._lib.df_set_atten_lim(self._st, atten)

    def close(self):
        if self._st:
            self._lib.df_free(self._st)
            self._st = None


def create_backend(engine: str):
    """Поток управления: создать нейросеть. Бросает DenoiseError с понятным текстом."""
    if engine == "rnnoise":
        return RNNoiseBackend()
    if engine in ("dfn", "dfn_ll"):
        return DeepFilterBackend(engine)
    raise DenoiseError(f"неизвестный движок {engine}")


# =========================================================================== анализ спектра
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


# =========================================================================== профиль шума
DFN_MAX_THRESHOLD = 6.0  # выше этого порог начинает съедать тихие слоги — речь важнее тишины в паузах


def dfn_threshold(lsnr_hi: float) -> float:
    """Порог гейта DeepFilterNet по обученному профилю: выше вашего шума, но не выше 6 дБ."""
    return min(DFN_MAX_THRESHOLD, max(0.0, lsnr_hi + 2.0))


def _family(kind: str) -> str:
    return "dfn" if kind.startswith("dfn") else "rnnoise"


class NoiseProfile:
    """То, что программа запомнила о вашем окружении (для той нейросети, с которой записано)."""

    def __init__(self, data: dict):
        self.backend = data.get("backend", "rnnoise")
        self.vad_hi = float(data.get("vad_hi", 0.3))
        self.lsnr_hi = float(data.get("lsnr_hi", -5.0))
        self.floor_db = float(data.get("floor_db", -80.0))
        self.click_db = float(data.get("click_db", -70.0))
        self.in_floor_db = float(data.get("in_floor_db", -70.0))
        cents = data.get("centroids") or []
        self.centroids = np.asarray(cents, np.float32).reshape(-1, BANDS) if cents else None
        self.radius = float(data.get("radius", 0.9))
        self.created = data.get("created", 0)

    def matches(self, kind: str) -> bool:
        return self.backend == _family(kind)

    @staticmethod
    def build(kind: str, scores, e_in, e_out, shapes) -> tuple[dict, str]:
        """Строит профиль по кадрам, записанным без голоса. → (данные, описание)."""
        scores = np.asarray(scores, np.float32)
        e_in = np.asarray(e_in, np.float32)
        e_out = np.asarray(e_out, np.float32)
        shapes = np.asarray(shapes, np.float32)
        n = len(scores)
        if n < 100:
            raise ValueError("слишком мало данных")
        family = _family(kind)
        voiced = float((scores > (10.0 if family == "dfn" else 0.85)).mean())
        if voiced > 0.2:
            raise ValueError("во время записи звучал голос — повторите, молча занимаясь своими делами "
                             "(печатайте, кликайте мышью, двигайтесь)")
        in_floor = float(np.percentile(e_in, 40))
        data = {
            "backend": family,
            "floor_db": float(np.percentile(e_out, 50)),
            "click_db": float(np.percentile(e_out, 99)),
            "in_floor_db": in_floor,
            "frames": n,
            "created": int(time.time()),
        }
        if family == "dfn":
            data["lsnr_hi"] = float(np.percentile(scores, 97))
            gate = dfn_threshold(data["lsnr_hi"])
            summary = (f"Изучено {n / 100:.0f} с: остаток шума после нейросети {data['floor_db']:.0f} дБ, "
                       f"ваш шум «похож на речь» максимум на {data['lsnr_hi']:.0f} дБ — порог гейта "
                       f"{gate:.0f} дБ")
        else:
            data["vad_hi"] = float(min(0.85, np.percentile(scores, 99)))
            loud = shapes[e_in > in_floor + 6.0]
            if len(loud) >= 20:
                k = int(min(8, max(2, len(loud) // 15)))
                cents, radius = _spherical_kmeans(loud, k)
                data["centroids"] = cents.round(5).tolist()
                data["radius"] = radius
            summary = (f"Изучено {n / 100:.0f} с: фон {data['floor_db']:.0f} дБ, щелчки после нейросети до "
                       f"{data['click_db']:.0f} дБ, ложные срабатывания голоса до {data['vad_hi']:.2f}, "
                       f"звуковых событий: {len(loud)}"
                       + (f", отпечатков: {len(data['centroids'])}" if "centroids" in data else ""))
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


# =========================================================================== шумодав
class Denoiser:
    """Поточный шумодав поверх выбранной нейросети. process() — только из аудиопотока."""

    def __init__(self, backend):
        self.backend = backend
        self.kind = backend.kind
        d = backend.delay
        self.strength = 1.0
        self.profile: NoiseProfile | None = None
        self.gate = True
        self.score = 0.0         # вероятность голоса (RNNoise) или отношение сигнал/шум (DeepFilterNet)
        self.open = False
        self._scores = deque([-100.0] * (d + 1), maxlen=d + 1)
        self._feat = deque([(-100.0, np.zeros(BANDS, np.float32), False)] * (d + 1), maxlen=d + 1)
        self._dry = deque([np.zeros(FRAME, np.float32)] * (d + 1), maxlen=d + 1)
        self._hold = 0
        self._warmup = WARMUP_FRAMES
        self._g = 0.0
        self._inbuf = np.zeros(0, np.float32)
        self._outbuf = np.zeros(0, np.float32)
        self._primed = False
        self._attack = np.linspace(0.0, 1.0, ATTACK_SAMPLES, dtype=np.float32)
        self._lin = np.linspace(0.0, 1.0, FRAME, dtype=np.float32)
        self._learn_left = 0
        self._learn: list | None = None

    @property
    def vad(self) -> float:
        """Для индикатора «Голос»: 0..1 для обеих нейросетей."""
        if self.kind == "rnnoise":
            return self.score
        return float(min(1.0, max(0.0, (self.score + 5.0) / 30.0)))

    # ---------- настройки (из аудиопотока через MicChain)
    def configure(self, strength: float, gate: bool, profile: NoiseProfile | None):
        self.strength = max(0.0, min(1.0, strength))
        self.backend.set_strength(self.strength)
        self.gate = gate
        self.profile = profile if profile is not None and profile.matches(self.kind) else None

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
        """После окончания записи → (kind, scores, e_in, e_out, shapes) или None, если ещё идёт."""
        if self._learn is None or self._learn_left > 0:
            return None
        rows = self._learn
        self._learn = None
        if not rows:
            return None
        scores, e_in, e_out, shapes = zip(*rows)
        return self.kind, scores, e_in, e_out, shapes

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
        wet, score = self.backend.process(x)
        self.score = score
        rnn = self.kind == "rnnoise"
        if rnn:  # спектральные «отпечатки» нужны только для RNNoise
            e_in, shape = frame_features(x)
            self._feat.append((e_in, shape, self._looks_like_noise(e_in, shape)))
            self._dry.append(x.copy())
        else:
            e_in, shape = 10.0 * np.log10(float(np.dot(x, x)) / FRAME + 1e-12), _ZERO_SHAPE
        self._scores.append(score)
        e_out = 10.0 * np.log10(float(np.dot(wet, wet)) / FRAME + 1e-12)
        learn = self._learn  # локальная ссылка: окно может отменить обучение в любой момент
        if learn is not None and self._learn_left > 0:
            learn.append((score, e_in, e_out, shape))
            self._learn_left -= 1

        s = self.strength
        if rnn and s < 0.99:  # сила RNNoise: подмешиваем исходный сигнал, выровненный по задержке
            a = 10.0 ** (-(12.0 + 48.0 * s) / 20.0)
            wet = wet * (1.0 - a) + self._dry[0] * a

        if not self.gate:
            self.open = True
            return wet
        speech = self._decide_rnnoise(e_out) if rnn else self._decide_dfn()
        target = 1.0 if speech else 10.0 ** (-(14.0 + 40.0 * s) / 20.0)
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

    def _hold_update(self, speech: bool) -> bool:
        if speech:
            self._hold = HOLD_FRAMES
        elif self._hold > 0:
            self._hold -= 1
        self.open = self._hold > 0
        return self.open

    def _decide_dfn(self) -> bool:
        """DeepFilterNet: отношение сигнал/шум; последний кадр — на 10 мс впереди выхода."""
        if self._warmup > 0:
            self._warmup -= 1
            self.open = False
            return False
        thr = dfn_threshold(self.profile.lsnr_hi) if self.profile is not None else 0.0
        return self._hold_update(max(self._scores) > thr)

    def _looks_like_noise(self, e_in: float, shape: np.ndarray) -> bool:
        """Похож ли входной кадр на один из запомненных звуков (щелчок клавиши, клик мыши…)."""
        prof = self.profile
        if prof is None or prof.centroids is None or e_in <= prof.in_floor_db + 6.0:
            return False
        return float(np.max(prof.centroids @ shape)) >= prof.radius

    def _decide_rnnoise(self, e_out: float) -> bool:
        """RNNoise: вероятность голоса; нейросеть видит на 2 кадра вперёд относительно выхода."""
        if self._warmup > 0:
            self._warmup -= 1
            self.open = False
            return False
        prof = self.profile
        scores = list(self._scores)              # [выровненный, +10 мс, +20 мс]
        feats = list(self._feat)
        v_al = scores[0]
        if prof is None:
            thr = 0.5
            noise_like = False
            v_ahead = max(scores[1:])
        else:
            thr = min(0.9, max(0.5, prof.vad_hi + 0.15))
            noise_like = feats[0][2] or e_out < prof.click_db + 3.0
            ahead = [v for v, f in zip(scores[1:], feats[1:]) if not f[2]]
            v_ahead = max(ahead) if ahead else 0.0
        speech_now = v_al >= thr and not (noise_like and v_al < 0.92)
        speech_soon = v_ahead >= max(thr, 0.8)
        return self._hold_update(speech_now or speech_soon)

    def close(self):
        self.backend.close()


_ZERO_SHAPE = np.zeros(BANDS, np.float32)
