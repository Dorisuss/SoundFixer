"""Иконка и сведения о версии для SoundFixer.exe (вызывается из build.bat).

Сведения о версии и своя иконка — не украшение: антивирусы реже принимают за угрозу
программу, у которой они есть.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from soundfixer import APP_NAME, __version__  # noqa: E402
from soundfixer.icons import tray_icon  # noqa: E402


def main() -> int:
    out = ROOT / "build" / "assets"
    out.mkdir(parents=True, exist_ok=True)
    tray_icon(size=256).save(out / "SoundFixer.ico",
                             sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    nums = [int(p) for p in __version__.split(".") if p.isdigit()]
    v = tuple((nums + [0, 0, 0, 0])[:4])
    (out / "version_info.txt").write_text(f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers={v}, prodvers={v}, mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1,
                    subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('041904B0', [
      StringStruct('CompanyName', '{APP_NAME}'),
      StringStruct('FileDescription', '{APP_NAME}: звук, микрофон и мультитул'),
      StringStruct('FileVersion', '{__version__}'),
      StringStruct('InternalName', '{APP_NAME}'),
      StringStruct('OriginalFilename', '{APP_NAME}.exe'),
      StringStruct('ProductName', '{APP_NAME}'),
      StringStruct('ProductVersion', '{__version__}')])]),
    VarFileInfo([VarStruct('Translation', [1049, 1200])])
  ]
)
""", encoding="utf-8")
    print(f"Иконка и версия {__version__}: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
