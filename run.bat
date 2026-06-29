@echo off
chcp 65001 >nul
cd /d "%~dp0"
python -c "import pystray, PIL, pycaw" 2>nul
if errorlevel 1 (
    echo Installing dependencies...
    python -m pip install -r requirements.txt -q
)
for /f "tokens=*" %%P in ('python -c "import sys; print(sys.executable)"') do set PYTHON=%%P
set PYTHONW=%PYTHON:python.exe=pythonw.exe%
if not exist "%PYTHONW%" set PYTHONW=%PYTHON%
start "" "%PYTHONW%" "%~dp0soundfixer.py"
