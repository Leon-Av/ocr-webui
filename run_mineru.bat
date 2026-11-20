@echo off
REM Start the OCR service: web + MinerU.
REM
REM   run_mineru.bat            start web + MinerU
REM   run_mineru.bat --stop     stop everything (same as run_all.bat --stop)
REM   run_mineru.bat --status   show status
REM
REM LLM (LM Studio) is external and always started separately.
REM First-time setup: install_all.bat + install_mineru.bat

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

python -X utf8 run_all.py --only web,mineru %*

endlocal
