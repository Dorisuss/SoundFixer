"""Иконки трея рисуются один раз и кэшируются."""

from __future__ import annotations

from functools import lru_cache

from PIL import Image, ImageDraw

ACCENT = (79, 140, 255, 255)
ACTIVE = (46, 196, 132, 255)
MUTED = (235, 72, 72, 255)
WHITE = (255, 255, 255, 255)


@lru_cache(maxsize=8)
def tray_icon(active: bool = False, muted: bool = False, size: int = 64) -> Image.Image:
    s = size
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([2, 2, s - 3, s - 3], radius=s // 4, fill=ACTIVE if active else ACCENT)
    # эквалайзер-«волна» из пяти столбиков
    heights = (0.35, 0.65, 0.95, 0.6, 0.3)
    bw = s // 11
    gap = (s - 2 * (s // 6) - 5 * bw) // 4
    x = s // 6
    for h in heights:
        bh = int((s - s // 3) * h)
        top = (s - bh) // 2
        d.rounded_rectangle([x, top, x + bw, top + bh], radius=bw // 2, fill=WHITE)
        x += bw + gap
    if muted:
        r = s // 3
        d.ellipse([s - r - 1, s - r - 1, s - 1, s - 1], fill=MUTED, outline=WHITE, width=2)
        pad = r // 3
        d.line([s - r + pad - 1, s - r + pad - 1, s - pad - 1, s - pad - 1], fill=WHITE, width=3)
    return img
