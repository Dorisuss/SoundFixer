@echo off
chcp 65001 >nul
cd /d "%~dp0"
rem Сборка одного SoundFixer.exe (папка dist)
python -m pip install --upgrade -r requirements.txt pyinstaller
python -m PyInstaller --noconfirm --clean --onefile --windowed --name SoundFixer ^
    --collect-all pedalboard --hidden-import pystray._win32 ^
    --exclude-module matplotlib --exclude-module scipy --exclude-module pandas ^
    SoundFixer.pyw
echo.
echo Готово: dist\SoundFixer.exe
pause
