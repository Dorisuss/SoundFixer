"""Пресеты голоса и эквалайзера (без тяжёлых зависимостей — используются и в GUI, и в DSP)."""

VOICE_DEFAULTS = {"pitch": 0.0, "robot": 0, "distortion": 0, "chorus": 0, "echo": 0, "reverb": 0, "radio": False}

# ключ: (название, отличия от VOICE_DEFAULTS)
VOICE_PRESETS = {
    "normal": ("Обычный", {}),
    "deep": ("Низкий голос", {"pitch": -4.0}),
    "monster": ("Монстр", {"pitch": -8.0, "distortion": 25, "reverb": 25}),
    "demon": ("Демон", {"pitch": -12.0, "reverb": 45, "distortion": 15, "chorus": 30}),
    "high": ("Высокий голос", {"pitch": 4.0}),
    "chipmunk": ("Бурундук", {"pitch": 9.0}),
    "robot": ("Робот", {"robot": 85, "pitch": -1.0, "chorus": 20}),
    "alien": ("Инопланетянин", {"pitch": 3.0, "robot": 35, "chorus": 70}),
    "radio": ("Рация", {"radio": True, "distortion": 20}),
    "cave": ("Пещера", {"reverb": 75, "echo": 35}),
    "stadium": ("Стадион", {"echo": 55, "reverb": 50}),
    "custom": ("Свой", None),
}

# ключ: (название, усиление полос в дБ или None для «Свой»)
EQ_PRESETS = {
    "flat": ("Без эквалайзера", [0, 0, 0, 0, 0]),
    "bass": ("Больше баса", [7, 4, 0, 0, 1]),
    "bass_max": ("Максимум баса", [11, 6, -1, 0, 0]),
    "vocal": ("Голос / подкасты", [-3, -1, 3, 4, 1]),
    "treble": ("Больше высоких", [0, 0, 0, 3, 6]),
    "games": ("Игры (слышно шаги)", [-2, -1, 1, 5, 4]),
    "movie": ("Кино", [5, 2, 1, 2, 3]),
    "music": ("Музыка (V-образный)", [5, 2, -1, 2, 5]),
    "custom": ("Свой", None),
}


def voice_values(key: str) -> dict | None:
    """Полный набор параметров голоса для пресета (None для «Свой»)."""
    diff = VOICE_PRESETS.get(key, VOICE_PRESETS["normal"])[1]
    if diff is None:
        return None
    vals = dict(VOICE_DEFAULTS)
    vals.update(diff)
    return vals
