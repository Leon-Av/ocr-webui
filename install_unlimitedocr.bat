@echo off
chcp 65001 >nul
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
cd /d "%~dp0"

REM Install Unlimited-OCR (Baidu) engine: VLM ~3.3B, PyTorch+Transformers.
REM Model (~6.7 GB) downloads from HuggingFace on first server start.
python install.py unlimited
pause
