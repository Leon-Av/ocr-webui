"""
Реестр OCR-движков.

get_engine(code) возвращает адаптер, сконфигурированный текущими URL'ами
из таблицы settings (админка может их менять в реальном времени).
"""

from typing import Dict, List

from config import ENGINES
from database import get_setting
from .base import OCREngine, OCRResult, OCRError
from .http_engine import HTTPEngine
from .llm import LLMEngine

# Настройки по умолчанию (используются, если в БД значения ещё нет).
_DEFAULT_URLS = {
    "engine_url_mineru": "http://localhost:8501",
    "engine_url_unlimitedocr": "http://localhost:8503",
    "engine_url_llm": "http://localhost:1235/v1",
}


def _setting(key: str) -> str:
    return get_setting(key, _DEFAULT_URLS.get(key, ""))


class MinerUEngine(HTTPEngine):
    code = "mineru"
    name = "MinerU 2.5-Pro-1.2B"


class UnlimitedOCREngine(HTTPEngine):
    code = "unlimited-ocr"
    name = "Unlimited-OCR (Baidu)"


def get_engine(code: str) -> OCREngine:
    code = (code or "").strip().lower()
    if code == "mineru":
        return MinerUEngine(_setting("engine_url_mineru"))
    if code == "unlimited-ocr":
        return UnlimitedOCREngine(_setting("engine_url_unlimitedocr"))
    if code == "llm":
        model = get_setting("engine_llm_model", "local-llm")
        return LLMEngine(_setting("engine_url_llm"), model=model)
    raise OCRError(f"Неизвестный движок: {code}")


def is_engine_enabled(code: str) -> bool:
    """Включён ли движок для пользователей (читается из settings)."""
    key = f"engine_enabled_{code.replace('-', '_')}"
    val = get_setting(key, "true")
    return val.lower() in ("1", "true", "yes", "on")


def available_engines() -> List[Dict]:
    """Список включённых движков с названиями (для UI)."""
    return [
        {"code": e["code"], "name": e["name"]}
        for e in ENGINES
        if is_engine_enabled(e["code"])
    ]


__all__ = [
    "OCREngine", "OCRResult", "OCRError",
    "get_engine", "is_engine_enabled", "available_engines",
    "HTTPEngine", "LLMEngine",
    "MinerUEngine", "UnlimitedOCREngine",
]
