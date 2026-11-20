@echo off
chcp 65001 >nul
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
cd /d "%~dp0"

REM Master installer: web app + MinerU.
REM All UI text lives in install.py (UTF-8 native); this .bat is ASCII-only.
python install.py all
pause
