"""
Базовый класс OCR-движка.
Все движки реализуют единый интерфейс: принимают байты изображения (PNG/JPEG),
возвращают {text, markdown}. Реальная обработка происходит в отдельных сервисах
(см. ocr_servers/) либо в LM Studio (LLM-движок).
"""

from dataclasses import dataclass
from typing import Optional


@dataclass
class OCRResult:
    text: str           # чистый plain text
    markdown: Optional[str] = None  # опционально: разметка/список блоков


class OCRError(Exception):
    """Ошибка взаимодействия с OCR-движком."""


class OCREngine:
    """Базовый класс. Конкретная логика — в потомках."""

    #: код движка (mineru | llm)
    code: str = "base"
    #: человекочитаемое название
    name: str = "Base OCR"

    async def health(self) -> bool:
        """Проверить, что движок отвечает. По умолчанию — не реализовано."""
        return False

    async def ocr(self, image_bytes: bytes, filename: str = "image.png") -> OCRResult:
        """Распознать текст на изображении.

        image_bytes — байты изображения (PNG/JPEG).
        filename — исходное имя (для контекста/логов).
        """
        raise NotImplementedError

    @staticmethod
    def _empty() -> OCRResult:
        return OCRResult(text="")
