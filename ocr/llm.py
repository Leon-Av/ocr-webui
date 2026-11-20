"""
LLM-движок: обращается к локальному LM Studio (OpenAI-совместимый API)
по адресу http://localhost:1235/v1. Модель понимает изображения
(vision-capable LLM) — картинка передаётся как base64 в содержимом сообщения.
"""

import base64
import logging
from typing import Optional

import httpx

from config import ENGINE_REQUEST_TIMEOUT, ENGINE_HEALTH_TIMEOUT, DEFAULT_LLM_MODEL
from .base import OCREngine, OCRResult, OCRError

logger = logging.getLogger(__name__)

# Системный промпт: просим модель извлечь весь текст с документа как есть.
_SYSTEM_PROMPT = (
    "Ты — система распознавания текста (OCR). "
    "Внимательно изучи изображение документа и извлеки ВЕСЬ видимый текст. "
    "Сохрани порядок чтения и структуру (абзацы, заголовки, списки, таблицы). "
    "Не добавляй комментариев, не исправляй опечатки оригинала, не выдумывай содержимое. "
    "Если есть таблица — передай её в виде Markdown-таблицы. "
    "Отвечай ТОЛЬКО распознанным текстом в формате Markdown, без вступлений."
)


class LLMEngine(OCREngine):
    code = "llm"
    name = "LLM (LM Studio)"

    def __init__(self, base_url: str, model: str = DEFAULT_LLM_MODEL):
        self.base_url = base_url.rstrip("/")
        self.model = model

    async def health(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=ENGINE_HEALTH_TIMEOUT) as client:
                r = await client.get(f"{self.base_url}/models")
                return r.status_code == 200
        except Exception:
            return False

    async def ocr(self, image_bytes: bytes, filename: str = "image.png") -> OCRResult:
        url = f"{self.base_url}/chat/completions"
        b64 = base64.b64encode(image_bytes).decode("ascii")
        # MIME-тип по расширению (по умолчанию PNG).
        mime = "image/png"
        lower = filename.lower()
        if lower.endswith((".jpg", ".jpeg")):
            mime = "image/jpeg"
        elif lower.endswith(".gif"):
            mime = "image/gif"
        elif lower.endswith(".webp"):
            mime = "image/webp"
        data_url = f"data:{mime};base64,{b64}"

        payload = {
            "model": self.model,
            "temperature": 0.0,
            "max_tokens": 4096,
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Извлеки весь текст с этого изображения."},
                        {"type": "image_url", "image_url": {"url": data_url}},
                    ],
                },
            ],
        }

        try:
            async with httpx.AsyncClient(timeout=ENGINE_REQUEST_TIMEOUT) as client:
                r = await client.post(url, json=payload)
        except httpx.RequestError as e:
            raise OCRError(f"Не удалось подключиться к LM Studio ({self.base_url}): {e}")

        if r.status_code != 200:
            raise OCRError(f"LM Studio вернул HTTP {r.status_code}: {r.text[:300]}")

        try:
            data = r.json()
            markdown = data["choices"][0]["message"]["content"].strip()
        except (KeyError, IndexError, ValueError) as e:
            raise OCRError(f"Некорректный ответ LM Studio: {e}")

        return OCRResult(text=_markdown_to_text(markdown), markdown=markdown)


def _markdown_to_text(md: str) -> str:
    """Грубое преобразование Markdown -> plain text для .txt-вывода."""
    import re
    text = md or ""
    # Убираем изображения и ссылки, оставляя текст/alt
    text = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    # Убираем HTML-теги (например, таблицы как <table>)
    text = re.sub(r"<[^>]+>", "", text)
    # Убираем разметку заголовков/списков/цитат
    text = re.sub(r"^[#>\-\*\+]+\s*", "", text, flags=re.MULTILINE)
    # Убираем разделители таблиц
    text = re.sub(r"^\s*\|?[\s:|-]+\|?\s*$", "", text, flags=re.MULTILINE)
    # Убираем пару подряд идущих пустых строк
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()
