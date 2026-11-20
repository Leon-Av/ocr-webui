"""
HTTP-движок: общий адаптер для внешних OCR-серверов (MinerU).

Каждый такой сервер реализует единый контракт:
    POST /ocr   multipart-form: image=<байты>, filename=<имя>
                -> {"text": "...", "markdown": "..."}
    GET  /health -> {"status": "ok"}
"""

import logging
from typing import Optional

import httpx

from config import ENGINE_REQUEST_TIMEOUT, ENGINE_HEALTH_TIMEOUT, ENGINE_MAX_RETRIES
from .base import OCREngine, OCRResult, OCRError

logger = logging.getLogger(__name__)


class HTTPEngine(OCREngine):
    """Адаптер к внешнему OCR-сервису по HTTP."""

    code = "http"
    name = "HTTP OCR"

    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")

    async def health(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=ENGINE_HEALTH_TIMEOUT) as client:
                r = await client.get(f"{self.base_url}/health")
                return r.status_code == 200
        except Exception:
            return False

    async def ocr(self, image_bytes: bytes, filename: str = "image.png") -> OCRResult:
        url = f"{self.base_url}/ocr"
        last_err: Optional[Exception] = None

        for attempt in range(1, ENGINE_MAX_RETRIES + 2):
            try:
                async with httpx.AsyncClient(timeout=ENGINE_REQUEST_TIMEOUT) as client:
                    files = {"image": (filename, image_bytes)}
                    data = {"filename": filename}
                    r = await client.post(url, files=files, data=data)
                if r.status_code == 200:
                    payload = r.json()
                    return OCRResult(
                        text=payload.get("text", "") or "",
                        markdown=payload.get("markdown"),
                    )
                last_err = OCRError(f"Движок вернул HTTP {r.status_code}: {r.text[:200]}")
                # 4xx не повторяем
                if 400 <= r.status_code < 500:
                    raise last_err
            except httpx.RequestError as e:
                last_err = OCRError(f"Сетевая ошибка при запросе к движку: {e}")
            except OCRError:
                raise
            except Exception as e:
                last_err = OCRError(f"Непредвиденная ошибка движка: {e}")

            logger.warning(
                f"[{self.code}] попытка {attempt}/{ENGINE_MAX_RETRIES + 1} не удалась: {last_err}"
            )

        raise last_err or OCRError("Неизвестная ошибка движка")
