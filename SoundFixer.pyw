"""Запуск SoundFixer без окна консоли (двойной клик или pythonw SoundFixer.pyw)."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from soundfixer.app import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
