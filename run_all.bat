@echo off
REM Launch all OCR-service components (Windows).
REM
REM   run_all.bat            start web + OCR servers whose venv exists
REM   run_all.bat --all      start everything supported on Windows
REM   run_all.bat --stop     stop everything
REM   run_all.bat --status   show status and what still needs installing
REM   run_all.bat --only web,mineru   only the listed services
REM
REM First-time setup: run install_all.bat

chcp 65001 >nul
setlocal
cd /d "%~dp0"

REM Force UTF-8 for Python (fixes UnicodeEncodeError on non-UTF Windows).
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8

REM Activate the web-app venv if present.
if exist "venv\Scripts\activate.bat" (
    call "venv\Scripts\activate.bat"
) else (
    echo [WARNING] Root venv not found. Run install_all.bat to set up.
    echo            Falling back to system python.
)

python -X utf8 run_all.py %*

endlocal
