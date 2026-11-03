@echo off
chcp 65001 >nul
cd /d "%~dp0"
rem Сборка SoundFixer: папка dist\SoundFixer, внутри SoundFixer.exe

where python >nul 2>nul
if errorlevel 1 (
    echo Python не найден. Установите Python 3.10+ с python.org, галочка "Add to PATH".
    pause
    exit /b 1
)

rem Запущенный SoundFixer держит свой exe: сборка не сможет его заменить
tasklist /fi "imagename eq SoundFixer.exe" 2>nul | find /i "SoundFixer.exe" >nul
if not errorlevel 1 (
    echo Закрываю запущенный SoundFixer...
    taskkill /f /im SoundFixer.exe >nul 2>&1
    timeout /t 2 /nobreak >nul
)

echo Устанавливаю зависимости...
python -m pip install --upgrade -r requirements.txt pyinstaller
if errorlevel 1 goto :pipfail
python -m pip install --upgrade --no-deps pyrnnoise

set "RNNOISE="
for /f "usebackq delims=" %%D in (`python -c "import importlib.util, os; print(os.path.join(importlib.util.find_spec('pyrnnoise').submodule_search_locations[0], 'rnnoise.dll'))" 2^>nul`) do set "RNNOISE=%%D"
set "ADDBIN="
if defined RNNOISE if exist "%RNNOISE%" set "ADDBIN=--add-binary "%RNNOISE%;pyrnnoise""
if not defined ADDBIN echo Не найден rnnoise.dll: запасная нейросеть RNNoise не войдёт в сборку, основная DeepFilterNet3 войдёт.

python scripts\build_assets.py
if errorlevel 1 goto :fail

rem Старые сборки: одиночный exe прошлых версий и прошлая папка
if exist "dist\SoundFixer.exe" del /f /q "dist\SoundFixer.exe" >nul 2>&1
if exist "dist\SoundFixer.exe" goto :locked
if exist "dist\SoundFixer" rmdir /s /q "dist\SoundFixer" >nul 2>&1
if exist "dist\SoundFixer\SoundFixer.exe" goto :locked

python -m PyInstaller --noconfirm --clean --onedir --windowed --noupx --name SoundFixer ^
    --icon build\assets\SoundFixer.ico --version-file build\assets\version_info.txt ^
    --collect-all pedalboard --collect-submodules edge_tts --hidden-import pystray._win32 ^
    --collect-submodules soundfixer --collect-submodules qrcode --hidden-import winrt.windows.media.ocr ^
    --add-binary "soundfixer\bin\deepfilter.dll;soundfixer\bin" --add-data "soundfixer\models;soundfixer\models" ^
    %ADDBIN% ^
    --exclude-module matplotlib --exclude-module scipy --exclude-module pandas ^
    SoundFixer.pyw
if errorlevel 1 goto :fail
if not exist "dist\SoundFixer\SoundFixer.exe" goto :fail

echo.
echo Готово: dist\SoundFixer\SoundFixer.exe
echo Папку dist\SoundFixer переносите целиком: exe работает только рядом с папкой _internal.
start "" explorer "dist\SoundFixer"
pause
exit /b 0

:locked
echo.
echo Не удаётся удалить старую сборку в папке dist: файл занят.
echo Закройте SoundFixer через значок в трее, пункт "Выход", и запустите build.bat снова.
echo Если не помогает, файл проверяет антивирус: подождите минуту или добавьте папку в исключения.
pause
exit /b 1

:pipfail
echo.
echo Не удалось установить зависимости, подробности выше. Проверьте интернет.
pause
exit /b 1

:fail
echo.
echo Сборка НЕ удалась, подробности выше.
pause
exit /b 1
