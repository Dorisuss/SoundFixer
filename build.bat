@echo off
chcp 65001 >nul
cd /d "%~dp0"
rem Сборка одного SoundFixer.exe (папка dist)
python -m pip install --upgrade -r requirements.txt pyinstaller
python -m pip install --upgrade --no-deps pyrnnoise
for /f "usebackq delims=" %%D in (`python -c "import importlib.util, os; print(os.path.join(importlib.util.find_spec('pyrnnoise').submodule_search_locations[0], 'rnnoise.dll'))"`) do set "RNNOISE=%%D"
if not exist "%RNNOISE%" (
    echo Не найден rnnoise.dll — ИИ-шумоподавление не войдёт в сборку.
    set "ADDBIN="
) else (
    set "ADDBIN=--add-binary "%RNNOISE%;pyrnnoise""
)
python -m PyInstaller --noconfirm --clean --onefile --windowed --name SoundFixer ^
    --collect-all pedalboard --collect-submodules edge_tts --hidden-import pystray._win32 ^
    %ADDBIN% ^
    --exclude-module matplotlib --exclude-module scipy --exclude-module pandas ^
    SoundFixer.pyw
echo.
echo Готово: dist\SoundFixer.exe
pause
