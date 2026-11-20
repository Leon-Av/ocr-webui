"""
Общий каркас OCR-сервера.

Каждый конкретный сервер (run_mineru_server.py и т.д.) реализует функцию
`run_ocr(image: PIL.Image) -> (text, markdown)` и вызывает build_app().
Это даёт единый HTTP-контракт, ожидаемый основным веб-приложением:
    POST /ocr   multipart-form: image=<байты>, filename=<имя>
                -> {"text": "...", "markdown": "..."}
    GET  /health -> {"status": "ok"}
"""

import argparse
import logging
import os
from io import BytesIO
from typing import Callable, Optional, Tuple

from fastapi import FastAPI, UploadFile, File
from fastapi.responses import JSONResponse
from PIL import Image

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("ocr_server")


def build_app(
    name: str,
    run_ocr: Callable[[Image.Image], Tuple[str, Optional[str]]],
    on_startup: Optional[Callable[[], None]] = None,
):
    """
    Собрать FastAPI-приложение OCR-сервера.

    name        — имя движка (для логов).
    run_ocr     — функция (PIL.Image) -> (text, markdown|None).
    on_startup  — опциональная инициализация (загрузка модели).
    """
    app = FastAPI(title=f"{name} OCR Server")
    state = {"ready": on_startup is None}

    @app.on_event("startup")
    async def _startup():
        if on_startup:
            logger.info(f"[{name}] инициализация модели...")
            try:
                on_startup()
                state["ready"] = True
                logger.info(f"[{name}] готов к работе.")
            except Exception as e:
                logger.error(f"[{name}] ошибка инициализации: {e}", exc_info=True)
                raise

    @app.get("/health")
    async def health():
        if not state["ready"]:
            return JSONResponse({"status": "loading"}, status_code=503)
        return {"status": "ok"}

    @app.post("/ocr")
    async def ocr(image: UploadFile = File(...)):
        if not state["ready"]:
            return JSONResponse(
                {"detail": "Модель ещё загружается, попробуйте позже"},
                status_code=503,
            )
        raw = await image.read()
        if not raw:
            return JSONResponse({"detail": "Пустое изображение"}, status_code=400)
        try:
            img = Image.open(BytesIO(raw))
            img.load()
        except Exception as e:
            return JSONResponse(
                {"detail": f"Не удалось открыть изображение: {e}"}, status_code=400
            )
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        try:
            text, markdown = run_ocr(img)
        except Exception as e:
            logger.error(f"[{name}] OCR не удалась: {e}", exc_info=True)
            return JSONResponse({"detail": str(e)}, status_code=500)
        return {"text": text or "", "markdown": markdown}

    return app


def parse_args(default_port: int):
    p = argparse.ArgumentParser(description="OCR-сервер")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=default_port)
    p.add_argument("--gpu", default=None,
                   help="закрепить GPU по номеру CUDA (0, 1, ...); "
                        "по умолчанию не менять (все видимые GPU)")
    return p.parse_args()


def apply_gpu_pin(gpu):
    """Закрепить GPU до импорта torch: CUDA_VISIBLE_DEVICES + PCI-порядок.

    Вызывать в main() сервера ДО загрузки модели. При запуске через run_all
    env уже выставлен им (настройка GPU из админки) — тогда gpu=None и
    ничего не трогаем.
    """
    if gpu is None or str(gpu).strip() in ("", "auto"):
        return
    os.environ["CUDA_VISIBLE_DEVICES"] = str(gpu).strip()
    os.environ.setdefault("CUDA_DEVICE_ORDER", "PCI_BUS_ID")
    logger.info("GPU закреплён: CUDA-устройство %s (PCI-порядок, как в nvidia-smi)",
                gpu)
