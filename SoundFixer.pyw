"""Запуск SoundFixer без окна консоли (двойной клик или pythonw SoundFixer.pyw).

Если программа не может запуститься, ошибка не теряется молча: она записывается в
%APPDATA%\\SoundFixer\\crash.log и показывается в окне сообщения.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def _fatal(details: str) -> None:
    folder = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), "SoundFixer")
    path = os.path.join(folder, "crash.log")
    try:
        import datetime

        os.makedirs(folder, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(f"==== {datetime.datetime.now():%Y-%m-%d %H:%M:%S}  Python {sys.version.split()[0]}"
                     f"  {'exe' if getattr(sys, 'frozen', False) else 'script'}\n{details}\n")
    except OSError:
        path = "(не удалось записать файл)"
    last = details.strip().splitlines()[-1] if details.strip() else "неизвестная ошибка"
    text = f"SoundFixer не смог запуститься.\n\n{last}\n\nПодробности: {path}"
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.user32.MessageBoxW(None, text, "SoundFixer", 0x10 | 0x10000)  # MB_ICONERROR | MB_SETFOREGROUND
            return
        except Exception:  # noqa: BLE001
            pass
    sys.stderr and sys.stderr.write(text + "\n")


if __name__ == "__main__":
    try:
        from soundfixer.app import main

        code = main()
    except SystemExit:
        raise
    except BaseException:  # noqa: BLE001 — любая ошибка запуска должна быть видна пользователю
        import traceback

        _fatal(traceback.format_exc())
        code = 1
    sys.exit(code)
