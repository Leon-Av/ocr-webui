@echo off
chcp 65001 >nul
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
cd /d "%~dp0"

REM Install MinerU 2.5-Pro-1.2B. ASCII-only; UI text is in install.py.
python install.py mineru
pause
