@echo off
chcp 65001 >nul
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
    echo Python не найден. Установите Python 3.10+ с python.org ^(галочка "Add to PATH"^).
    pause
    exit /b 1
)

python -c "import pycaw, pystray, PIL, numpy, sounddevice, pedalboard" 2>nul
if errorlevel 1 (
    echo Установка зависимостей...
    python -m pip install --upgrade -r requirements.txt
    if errorlevel 1 (
        echo Не удалось установить зависимости.
        pause
        exit /b 1
    )
)

where pythonw >nul 2>nul
if errorlevel 1 (
    start "" python "%~dp0SoundFixer.pyw" %*
) else (
    start "" pythonw "%~dp0SoundFixer.pyw" %*
)
