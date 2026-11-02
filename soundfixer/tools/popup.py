"""Всплывающие окна инструментов (только GUI-поток): окно у курсора и выделение области экрана.

Используются обычные tk-виджеты с явными цветами — выглядят одинаково, даже если главное
окно программы ещё ни разу не открывалось (стили ttk создаются вместе с ним).
"""

from __future__ import annotations

import tkinter as tk

from . import winapi

BG = "#17181c"
PANEL = "#202228"
FIELD = "#2a2d35"
FG = "#e8e9ed"
MUTED = "#9aa0ad"
ACCENT = "#4f8cff"
FONT = ("Segoe UI", 10)


def _button(parent, text, cmd, accent=False):
    return tk.Button(parent, text=text, command=cmd, relief="flat", bd=0, padx=10, pady=4, cursor="hand2",
                     font=FONT, bg=ACCENT if accent else FIELD, fg="#fff" if accent else FG,
                     activebackground="#6a9dff" if accent else "#353945", activeforeground="#fff")


_current = None  # открытое окно: новое закрывает предыдущее, чтобы окна не копились


class Popup:
    """Окно у курсора: заголовок, текст и/или картинка, кнопки. Esc или клик мимо — закрыть."""

    def __init__(self, root, title: str, *, text: str | None = None, sub: str | None = None, image=None,
                 buttons=(), width: int = 440, autoclose_ms: int | None = None, editable: bool = False):
        global _current
        if _current is not None:
            _current.close()
        _current = self
        self.win = win = tk.Toplevel(root)
        win.withdraw()
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        win.configure(bg=ACCENT, padx=1, pady=1)  # тонкая рамка акцентного цвета
        body = tk.Frame(win, bg=PANEL, padx=12, pady=10)
        body.pack(fill="both", expand=True)
        head = tk.Frame(body, bg=PANEL)
        head.pack(fill="x")
        tk.Label(head, text=title, bg=PANEL, fg=ACCENT, font=("Segoe UI Semibold", 10)).pack(side="left")
        close = tk.Label(head, text="✕", bg=PANEL, fg=MUTED, cursor="hand2", font=FONT)
        close.pack(side="right")
        close.bind("<Button-1>", lambda _e: self.close())
        self._img = image
        self.editable = editable
        if image is not None:
            tk.Label(body, image=image, bg=PANEL).pack(pady=(8, 4))
        self.text = None
        if text is not None:
            lines = min(14, max(2, text.count("\n") + len(text) // 55 + 1))
            self.text = t = tk.Text(body, height=lines, width=width // 9, wrap="word", bg=FIELD, fg=FG,
                                    relief="flat", font=("Segoe UI", 11), padx=8, pady=6, insertbackground=FG,
                                    highlightthickness=0)
            t.insert("1.0", text)
            if not editable:
                t.configure(state="disabled")
            t.pack(fill="both", expand=True, pady=(8, 4))
        if sub:
            tk.Label(body, text=sub, bg=PANEL, fg=MUTED, font=("Segoe UI", 9), wraplength=width,
                     justify="left").pack(anchor="w")
        if buttons:
            row = tk.Frame(body, bg=PANEL)
            row.pack(fill="x", pady=(8, 0))
            for i, (label, cmd) in enumerate(buttons):
                _button(row, label, lambda c=cmd: self._run(c), accent=(i == 0)).pack(side="left", padx=(0, 6))
        win.bind("<Escape>", lambda _e: self.close())
        win.bind("<FocusOut>", self._focus_out)
        self._place(width)
        win.deiconify()
        win.lift()
        win.focus_force()
        if autoclose_ms:
            win.after(autoclose_ms, self.close)

    def _place(self, width: int):
        win = self.win
        win.update_idletasks()
        w, h = max(win.winfo_reqwidth(), 200), win.winfo_reqheight()
        x, y = winapi.cursor_pos()
        sx, sy, sw, sh = winapi.virtual_screen()
        x = min(max(sx, x + 12), sx + sw - w - 8)
        y = min(max(sy, y + 16), sy + sh - h - 8)
        win.geometry(f"+{x}+{y}")

    def _run(self, cmd):
        keep = cmd(self) if cmd else None
        if not keep:
            self.close()

    def _focus_out(self, _e):
        # закрываем, только если фокус ушёл из всего окна (а не на его кнопку)
        self.win.after(150, lambda: self.win.winfo_exists() and self.win.focus_displayof() is None and self.close())

    def set_text(self, text: str):
        if self.text is not None:
            self.text.configure(state="normal")
            self.text.delete("1.0", "end")
            self.text.insert("1.0", text)
            if not self.editable:
                self.text.configure(state="disabled")

    def get_text(self) -> str:
        return self.text.get("1.0", "end-1c") if self.text is not None else ""

    def close(self):
        global _current
        if _current is self:
            _current = None
        try:
            self.win.destroy()
        except tk.TclError:
            pass


class RegionSelector:
    """Затемняет экран; пользователь обводит область мышью → callback((x1, y1, x2, y2)) в пикселях экрана."""

    def __init__(self, root, callback, hint: str = "Обведите область мышью. Esc — отмена"):
        self.callback = callback
        self.root = root
        x, y, w, h = winapi.virtual_screen()
        self.ox, self.oy = x, y
        self.win = win = tk.Toplevel(root)
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        win.attributes("-alpha", 0.35)
        win.geometry(f"{w}x{h}+{x}+{y}")
        self.cv = cv = tk.Canvas(win, bg="black", highlightthickness=0, cursor="crosshair")
        cv.pack(fill="both", expand=True)
        cv.create_text(w // 2, 40, text=hint, fill="white", font=("Segoe UI Semibold", 16))
        self.start = None
        self.rect = None
        cv.bind("<ButtonPress-1>", self._press)
        cv.bind("<B1-Motion>", self._drag)
        cv.bind("<ButtonRelease-1>", self._release)
        win.bind("<Escape>", lambda _e: self._finish(None))
        win.focus_force()

    def _press(self, e):
        self.start = (e.x, e.y)
        self.rect = self.cv.create_rectangle(e.x, e.y, e.x, e.y, outline=ACCENT, width=2, fill="white")

    def _drag(self, e):
        if self.start:
            self.cv.coords(self.rect, *self.start, e.x, e.y)

    def _release(self, e):
        if not self.start:
            return
        x1, y1 = self.start
        x2, y2 = e.x, e.y
        box = (min(x1, x2) + self.ox, min(y1, y2) + self.oy, max(x1, x2) + self.ox, max(y1, y2) + self.oy)
        self._finish(box if box[2] - box[0] > 4 and box[3] - box[1] > 4 else None)

    def _finish(self, box):
        try:
            self.win.destroy()
        except tk.TclError:
            pass
        # даём окну исчезнуть с экрана, прежде чем делать снимок
        if box:
            self.root.after(150, lambda: self.callback(box))
        else:
            self.callback(None)
