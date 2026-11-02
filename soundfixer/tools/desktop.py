"""Окна и питание: «поверх всех», прозрачность, «не засыпать»."""

from __future__ import annotations

import logging

from . import Tool, winapi

log = logging.getLogger(__name__)


class WindowTools(Tool):
    key = "windows"
    title = "Окна"

    def hotkeys(self, cfg):
        return {"topmost": cfg.get("topmost_hotkey", ""), "more": cfg.get("fade_more", ""),
                "less": cfg.get("fade_less", "")}

    def on_hotkey(self, action):
        hwnd = winapi.foreground_window()
        if not hwnd:
            return
        title = winapi.window_title(hwnd)[:40] or "окно"
        if action == "topmost":
            state = winapi.toggle_topmost(hwnd)
            if state is None:
                self.app.notify("Окна", "Не удалось: окно запущено от имени администратора?")
            else:
                self.app.notify("Окна", f"{'Поверх всех' if state else 'Обычное окно'}: {title}")
        elif action in ("more", "less"):
            alpha = winapi.change_opacity(hwnd, -26 if action == "more" else 26)
            if alpha is None:
                return
            self.last = f"{title}: {round(alpha / 255 * 100)}%"
            self.app.tool_status_changed(self.key)

    last = ""

    def status(self):
        return f"Последнее: {self.last}" if self.running and self.last else ""


class KeepAwake(Tool):
    key = "awake"
    title = "Не засыпать"

    def start(self, cfg):
        super().start(cfg)
        self._display = bool(cfg.get("display", True))
        winapi.keep_awake(True, self._display)  # вызывается в GUI-потоке — состояние держится, пока он жив

    def update(self, cfg):
        if bool(cfg.get("display", True)) != self._display:
            self.start(cfg)

    def stop(self):
        super().stop()
        winapi.keep_awake(False)

    def status(self):
        if not self.running:
            return ""
        return "Компьютер не уснёт" + (" и экран не погаснет" if self._display else " (экран может погаснуть)")

    def tray_text(self):
        return "не засыпать"


# --------------------------------------------------------------------------- раскладка окон
def snap_rect(work: tuple[int, int, int, int], action: str, step: int,
              size: tuple[int, int] | None = None) -> tuple[int, int, int, int]:
    """Прямоугольник (x, y, w, h) в рабочей области монитора.

    Повторное нажатие той же клавиши меняет ширину по кругу: ½ → ⅓ → ⅔ (как Rectangle/Spectacle).
    """
    left, top, right, bottom = work
    ww, wh = right - left, bottom - top
    if action in ("left", "right"):
        frac = (1 / 2, 1 / 3, 2 / 3)[step % 3]
        w = round(ww * frac)
        x = left if action == "left" else right - w
        return x, top, w, wh
    if action in ("top", "bottom"):
        h = wh // 2
        return left, top if action == "top" else bottom - h, ww, h
    if action == "center":
        if step % 2 == 0 and size:
            w, h = min(size[0], ww), min(size[1], wh)
        else:
            w, h = round(ww * 2 / 3), round(wh * 3 / 4)
        return left + (ww - w) // 2, top + (wh - h) // 2, w, h
    raise ValueError(action)


def move_to_monitor(rect: tuple[int, int, int, int], src: tuple[int, int, int, int],
                    dst: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    """Перенос окна на другой монитор с сохранением относительного положения и размера."""
    x, y, w, h = rect
    sw, sh = src[2] - src[0], src[3] - src[1]
    dw, dh = dst[2] - dst[0], dst[3] - dst[1]
    kx, ky = dw / max(sw, 1), dh / max(sh, 1)
    nw, nh = min(dw, round(w * kx)), min(dh, round(h * ky))
    nx = dst[0] + round((x - src[0]) * kx)
    ny = dst[1] + round((y - src[1]) * ky)
    nx = min(max(dst[0], nx), dst[2] - nw)
    ny = min(max(dst[1], ny), dst[3] - nh)
    return nx, ny, nw, nh


class WindowSnap(Tool):
    key = "snap"
    title = "Раскладка окон"
    ACTIONS = ("left", "right", "top", "bottom", "center", "maximize", "monitor")

    def __init__(self, app):
        super().__init__(app)
        self._last = (0, "", 0.0)  # (окно, действие, время)
        self._step = 0

    def hotkeys(self, cfg):
        return {a: cfg.get(a, "") for a in self.ACTIONS}

    def on_hotkey(self, action):
        import time

        hwnd = winapi.foreground_window()
        if not hwnd or winapi.is_shell_window(hwnd):
            return
        now = time.monotonic()
        last_hwnd, last_action, last_t = self._last
        self._step = self._step + 1 if (last_hwnd, last_action) == (hwnd, action) and now - last_t < 2.0 else 0
        self._last = (hwnd, action, now)
        mon = winapi.window_monitor(hwnd)
        if mon is None:
            return
        if action == "maximize":
            winapi.toggle_maximize(hwnd)
            return
        if action == "monitor":
            mons = winapi.monitors()
            if len(mons) < 2:
                return
            i = next((k for k, m in enumerate(mons) if m.handle == mon.handle), 0)
            dst = mons[(i + 1) % len(mons)]
            rect = winapi.visible_rect(hwnd)
            if rect is None:
                return
            was_max = winapi.is_maximized(hwnd)
            winapi.place_window(hwnd, *move_to_monitor(rect, mon.work, dst.work))
            if was_max:
                winapi.toggle_maximize(hwnd)
            return
        rect = winapi.visible_rect(hwnd)
        size = (rect[2], rect[3]) if rect else None
        if not winapi.place_window(hwnd, *snap_rect(mon.work, action, self._step, size)):
            self.app.notify("Раскладка окон", "Не удалось: окно запущено от имени администратора?")


# --------------------------------------------------------------------------- пипетка
def color_formats(rgb: tuple[int, int, int]) -> dict[str, str]:
    import colorsys

    r, g, b = rgb
    h, l, s = colorsys.rgb_to_hls(r / 255, g / 255, b / 255)
    return {"hex": f"#{r:02X}{g:02X}{b:02X}", "rgb": f"rgb({r}, {g}, {b})",
            "hsl": f"hsl({round(h * 360)}, {round(s * 100)}%, {round(l * 100)}%)"}


class ColorPicker(Tool):
    """Цвет точки под курсором: нажали клавишу — код цвета уже в буфере обмена."""

    key = "color"
    title = "Пипетка"

    def __init__(self, app):
        super().__init__(app)
        self.cfg: dict = {}
        self.last = ""

    def start(self, cfg):
        super().start(cfg)
        self.cfg = dict(cfg)

    def update(self, cfg):
        self.cfg = dict(cfg)

    def hotkeys(self, cfg):
        return {"pick": cfg.get("hotkey", "")}

    def on_hotkey(self, action):
        x, y = winapi.cursor_pos()
        rgb = winapi.pixel_color(x, y)
        if rgb is None:
            return
        fmts = color_formats(rgb)
        fmt = self.cfg.get("format", "hex")
        winapi.set_clipboard_text(fmts.get(fmt, fmts["hex"]))
        self.last = fmts["hex"]
        self.app.tool_status_changed(self.key)
        self._show(x, y, rgb, fmts)

    def _show(self, x, y, rgb, fmts):
        from .popup import Popup

        image = None
        shot = winapi.grab_region(x - 5, y - 5, x + 6, y + 6)
        try:
            from PIL import Image, ImageDraw, ImageTk

            if shot is not None:
                w, h, data = shot
                img = Image.frombuffer("RGB", (w, h), data, "raw", "BGRX", 0, 1).resize((w * 11, h * 11),
                                                                                        Image.NEAREST)
            else:
                img = Image.new("RGB", (121, 121), rgb)
            d = ImageDraw.Draw(img)
            c = 5 * 11
            d.rectangle((c - 1, c - 1, c + 11, c + 11), outline=(255, 255, 255))
            d.rectangle((c - 2, c - 2, c + 12, c + 12), outline=(0, 0, 0))
            swatch = Image.new("RGB", (img.width, 36), rgb)
            full = Image.new("RGB", (img.width, img.height + 40), (32, 34, 40))
            full.paste(img, (0, 0))
            full.paste(swatch, (0, img.height + 4))
            image = ImageTk.PhotoImage(full)
        except Exception:  # noqa: BLE001 — без Pillow просто покажем коды
            image = None

        def copier(key):
            return lambda _p: winapi.set_clipboard_text(fmts[key]) and False

        Popup(self.app.root, "Цвет скопирован", image=image,
              text=f"{fmts['hex']}\n{fmts['rgb']}\n{fmts['hsl']}",
              buttons=[("HEX", copier("hex")), ("RGB", copier("rgb")), ("HSL", copier("hsl"))], width=300)

    def status(self):
        return f"Последний цвет: {self.last}" if self.last else ""
