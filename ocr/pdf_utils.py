"""
Растеризация PDF в изображения (PNG) для последующего OCR.
Используется PyMuPDF (fitz) — он надёжен и не требует внешних бинарников.
"""

import logging
from io import BytesIO
from pathlib import Path
from typing import List, Tuple

import fitz  # PyMuPDF

from config import PDF_RENDER_DPI, PDF_RENDER_FORMAT

logger = logging.getLogger(__name__)

# PyMuPDF использует zoom = DPI/72 (72 точек на дюйм — базовый масштаб).
_ZOOM = PDF_RENDER_DPI / 72.0


def is_pdf(path: Path) -> bool:
    return path.suffix.lower() == ".pdf"


def get_pdf_page_count(path) -> int:
    """Вернуть число страниц в PDF (path может быть str/Path/bytes)."""
    src = path if isinstance(path, (str, Path)) else None
    try:
        doc = fitz.open(src) if src else fitz.open(stream=path, filetype="pdf")
        try:
            return doc.page_count
        finally:
            doc.close()
    except Exception as e:
        logger.warning(f"Не удалось прочитать PDF {path}: {e}")
        return 0


def rasterize_pdf(path, fmt: str = PDF_RENDER_FORMAT, dpi: int = PDF_RENDER_DPI) -> List[Tuple[str, bytes]]:
    """Растеризовать PDF в список (имя_страницы, байты_изображения).

    Возвращает список кортежей, по одному на страницу.
    Изображения в формате PNG или JPEG в зависимости от fmt.
    """
    zoom = dpi / 72.0
    matrix = fitz.Matrix(zoom, zoom)
    pix_fmt = fitz.csRGB

    src = path if isinstance(path, (str, Path)) else None
    doc = fitz.open(src) if src else fitz.open(stream=path, filetype="pdf")

    pages: List[Tuple[str, bytes]] = []
    try:
        for i in range(doc.page_count):
            page = doc.load_page(i)
            pix = page.get_pixmap(matrix=matrix, colorspace=pix_fmt, alpha=False)

            ext = "png" if fmt == "png" else "jpeg"
            buf = BytesIO()
            if ext == "png":
                pix.pil_save(buf, format="PNG")
            else:
                pix.pil_save(buf, format="JPEG", quality=90)
            pages.append((f"page_{i + 1:03d}.{ext}", buf.getvalue()))
    finally:
        doc.close()

    return pages


def normalize_image_to_png(image_bytes: bytes) -> bytes:
    """Перекодировать произвольное изображение в PNG (для совместимости движков).

    Если вход уже корректен — возвращаем как есть, чтобы не тратить время.
    """
    from PIL import Image
    try:
        img = Image.open(BytesIO(image_bytes))
        img.load()
    except Exception:
        return image_bytes

    # Конвертируем палитру/режимы без альфы в RGB для единообразия.
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")

    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
