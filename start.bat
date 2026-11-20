@echo off
chcp 65001 >nul
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
REM Launch the OCR web service only (Windows).
REM First-time setup: pip install -r requirements.txt  (or run install_all.bat)

setlocal
cd /d "%~dp0"

REM Activate venv if present, else use system Python.
if exist "venv\Scripts\activate.bat" (
    call "venv\Scripts\activate.bat"
)

echo Starting OCR service at http://localhost:2002 ...
python main.py

endlocal
