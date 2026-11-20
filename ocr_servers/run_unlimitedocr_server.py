"""
Сервер OCR-движка Unlimited-OCR (Baidu ERNIE Team).

Модель: baidu/Unlimited-OCR-2 (HuggingFace) — VLM ~3.3B, BF16, ~6.7 ГБ весов.
PyTorch + Transformers (trust_remote_code), БЕЗ paddlepaddle. Динамический
тайлинг ("gundam mode") — модель сама режет страницу на тайлы.
Модель грузится ОДИН РАЗ при старте (preload), каждый запрос /ocr отдаёт
изображение во временный файл и вызывает model.infer(...) — на выходе Markdown.

Требования: GPU с ~10+ ГБ VRAM (BF16), Python 3.10-3.12.
Первый запуск скачивает веса с HuggingFace (~6.7 ГБ).

Контракт: POST /ocr (multipart image) -> {"text", "markdown"}.
Веб-приложение ходит сюда по умолчанию на http://localhost:8503.
"""

import logging
import os
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from server_common import build_app, parse_args, apply_gpu_pin  # noqa: E402

# Имя модели на HF (можно переопределить: v1 = baidu/Unlimited-OCR)
MODEL_NAME = os.environ.get("UNLIMITED_OCR_MODEL", "baidu/Unlimited-OCR-2")

_state = {"model": None, "tokenizer": None}


def _load():
    import logging
    log = logging.getLogger("unlimited_server")

    import torch
    if not torch.cuda.is_available():
        raise RuntimeError(
            "CUDA недоступна: Unlimited-OCR — тяжёлая VLM, на CPU не работает. "
            "Проверьте драйвер и torch с CUDA."
        )
    log.info("GPU: %s", torch.cuda.get_device_name(0))

    from transformers import AutoModel, AutoTokenizer

    log.info("Загрузка модели %s (BF16). Первый запуск скачивает ~6.7 ГБ с HuggingFace...",
             MODEL_NAME)
    tokenizer = AutoTokenizer.from_pretrained(MODEL_NAME, trust_remote_code=True)
    model = AutoModel.from_pretrained(
        MODEL_NAME,
        trust_remote_code=True,
        use_safetensors=True,
        torch_dtype=torch.bfloat16,
    ).eval().cuda()
    _state["model"] = model
    _state["tokenizer"] = tokenizer
    log.info("Модель Unlimited-OCR загружена и готова.")


def _markdown_to_text(md: str) -> str:
    """Markdown (с разметкой таблиц/заголовков) -> читаемый plain text."""
    if not md:
        return ""
    text = md
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"^[#>\-\*\+]+\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"^\s*\|?[\s:|-]+\|?\s*$", "", text, flags=re.MULTILINE)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def run_ocr(img):
    """img — PIL.Image. Возвращает (text, markdown)."""
    model, tokenizer = _state["model"], _state["tokenizer"]
    # Модель принимает путь к файлу изображения; PIL -> временный PNG
    with tempfile.TemporaryDirectory(prefix="unlimited_ocr_") as td:
        img_path = Path(td) / "page.png"
        img.save(img_path, format="PNG")
        text = model.infer(
            tokenizer,
            image_file=str(img_path),
            output_path=str(Path(td) / "out"),
            save_results=False,
        )
    markdown = (text or "").strip()
    # Модель может помечать страницы <PAGE> — на одиночную страницу маркера быть
    # не должно, но на всякий случай убираем
    markdown = markdown.replace("<PAGE>", "").strip()
    return _markdown_to_text(markdown), (markdown or None)


def main():
    args = parse_args(default_port=8503)
    # Пиннинг GPU (при запуске вручную): до загрузки torch в _load().
    # При запуске через run_all env уже выставлен им самим.
    apply_gpu_pin(args.gpu)
    import uvicorn
    app = build_app("Unlimited-OCR (Baidu)", run_ocr, on_startup=_load)
    print(f"Unlimited-OCR сервер: host={args.host}, port={args.port}, model={MODEL_NAME}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
