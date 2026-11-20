"""
Сборка выходных документов: DOCX, TXT, ZIP.

Все функции принимают структурированный «скелет» результата:
    DocPart(name, pages: list[Page], error: str | None)
где Page(label, text, markdown).

DOCX собирается с настоящим форматированием: заголовки, таблицы, списки,
жирный/курсив — markdown конвертируется в HTML, затем в объекты Word
(через markdown + htmldocx). Формулы LaTeX сохраняются как читаемый текст.
"""

import re
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from typing import List, Optional
from zipfile import ZIP_DEFLATED, ZipFile

from docx import Document
from docx.shared import Pt

# Конвертеры markdown → HTML → DOCX (ленивый импорт, чтобы веб-приложение
# запускалось даже если этих пакетов нет — упадёт только при сборке DOCX).
_MD_EXTENSIONS = ["tables", "fenced_code", "sane_lists", "nl2br"]


@dataclass
class Page:
    label: str                       # напр. "page_001" или "image"
    text: str = ""
    markdown: Optional[str] = None


@dataclass
class DocPart:
    """Один логический документ-источник (загруженный файл)."""
    name: str                        # имя файла без расширения
    pages: List[Page] = field(default_factory=list)
    error: Optional[str] = None


def _safe_text(s: str) -> str:
    return (s or "").strip()


# ----------------------------------------------------------------------
#  Предобработка markdown перед конвертацией.
# ----------------------------------------------------------------------

def _strip_images(md: str) -> str:
    """Убрать картинки ![alt](path) — оставив alt-текст в скобках, если есть."""
    # ![alt text](url) -> (alt text) или пусто.
    def repl(m):
        alt = m.group(1).strip()
        return f"({alt})" if alt else ""
    return re.sub(r"!\[([^\]]*)\]\([^)]*\)", repl, md)


def _normalize_formulas(md: str) -> str:
    """Привести формулы к читаемому виду.

    $$ ... $$ и $ ... $ оставляем как есть для DOCX (MathML слишком сложен),
    но убираем тройные-dollar обёртки MinerU и экранирование.
    """
    # MinerU иногда оборачивает в $$\[ ... \]$$ — упростим.
    md = re.sub(r"\$\$\\\[\s*", "$$", md)
    md = re.sub(r"\s*\\\]\$\$", "$$", md)
    return md


def _preprocess_markdown(md: str) -> str:
    """Очистить markdown MinerU перед подачей в конвертер."""
    if not md:
        return ""
    md = _strip_images(md)
    md = _normalize_formulas(md)
    return md


# ----------------------------------------------------------------------
#  DOCX.
# ----------------------------------------------------------------------

def _apply_table_borders(doc: Document):
    """Назначить всем таблицам чёрные границы (стиль Table Grid + tblBorders).

    Без этого Word и Google Docs показывают таблицы без границ (стиль по
    умолчанию TableNormal). htmldocx создаёт таблицы без указания стиля/границ,
    поэтому правим XML напрямую: добавляем w:tblBorders в tblPr каждой таблицы
    и ссылку на стиль TableGrid (создаём его, если нет).
    """
    from docx.oxml.ns import qn
    from docx.oxml import OxmlElement

    # 1. Гарантируем стиль "TableGrid" в документе.
    styles = doc.styles
    if "Table Grid" not in [s.name for s in styles]:
        # python-docx умеет только add_style; для таблиц используем WD_STYLE_TYPE.TABLE.
        from docx.enum.style import WD_STYLE_TYPE
        try:
            grid = styles.add_style("Table Grid", WD_STYLE_TYPE.TABLE)
        except Exception:
            grid = None
    else:
        grid = styles["Table Grid"]

    # 2. Каждой таблице — границы (надёжнее, чем стиль, работает везде).
    for tbl in doc.tables:
        tblPr = tbl._tbl.tblPr
        if tblPr is None:
            continue
        # Удалим старые границы, если есть.
        for old in tblPr.findall(qn("w:tblBorders")):
            tblPr.remove(old)
        borders = OxmlElement("w:tblBorders")
        for edge in ("top", "left", "bottom", "right", "insideH", "insideV"):
            el = OxmlElement(f"w:{edge}")
            el.set(qn("w:val"), "single")     # стиль линии: single/dashed/...
            el.set(qn("w:sz"), "4")           # толщина (1/8 pt) → 0.5pt
            el.set(qn("w:space"), "0")
            el.set(qn("w:color"), "000000")   # чёрный
            borders.append(el)
        tblPr.append(borders)


def _md_to_docx(doc: Document, md_body: str):
    """Добавить в doc содержимое из markdown с настоящим форматированием."""
    import markdown
    from htmldocx import HtmlToDocx

    html = markdown.markdown(md_body, extensions=_MD_EXTENSIONS)
    # HtmlToDocx добавляет параграфы/таблицы/списки прямо в документ.
    parser = HtmlToDocx()
    parser.add_html_to_document(html, doc)
    # Назначаем границы всем таблицам (Word/Google Docs иначе рисуют без границ).
    _apply_table_borders(doc)


def _write_docx_part(doc: Document, part: DocPart, with_name_heading: bool = True):
    if with_name_heading:
        doc.add_heading(part.name, level=1)
    if part.error:
        p = doc.add_paragraph()
        run = p.add_run(f"Ошибка обработки: {part.error}")
        run.italic = True
        return
    for page in part.pages:
        # Предпочитаем markdown (там таблицы/формулы), иначе plain text.
        body = _safe_text(page.markdown) if page.markdown else _safe_text(page.text)
        if not body:
            continue
        # Если страниц больше одной — подзаголовок-метка страницы.
        if len(part.pages) > 1 and page.label:
            doc.add_heading(page.label, level=2)
        body = _preprocess_markdown(body) if page.markdown else body
        try:
            _md_to_docx(doc, body)
        except Exception:
            # Фолбэк: если markdown/htmldocx недоступны или упали —
            # пишем как обычный текст, чтобы документ всё равно собрался.
            for para in body.split("\n"):
                if para.strip():
                    doc.add_paragraph(para)
                else:
                    doc.add_paragraph("")


def build_docx(parts: List[DocPart], combined: bool) -> bytes:
    """Собрать .docx из списка частей.

    combined=True  — все части в одном документе (с заголовками по файлам).
    combined=False — ожидается одна часть; вернёт документ для неё.
    """
    doc = Document()
    # Базовый стиль шрифта.
    style = doc.styles["Normal"]
    style.font.name = "Calibri"
    style.font.size = Pt(11)

    target_parts = parts if combined else parts[:1]
    for part in target_parts:
        _write_docx_part(doc, part, with_name_heading=combined or len(parts) > 1)

    buf = BytesIO()
    doc.save(buf)
    return buf.getvalue()


def part_to_text(part: DocPart) -> str:
    """Слить страницы части в один plain-text блок."""
    if part.error:
        return f"[Ошибка обработки: {part.error}]"
    chunks = [_safe_text(p.text) for p in part.pages if _safe_text(p.text)]
    return "\n\n".join(chunks)


def part_to_markdown(part: DocPart) -> str:
    """Слить страницы части в Markdown (если был), иначе в plain text."""
    if part.error:
        return f"_[Ошибка обработки: {part.error}]_"
    chunks = []
    for p in part.pages:
        body = _safe_text(p.markdown) if p.markdown else _safe_text(p.text)
        if body:
            chunks.append(body)
    return "\n\n".join(chunks)


def build_txt(parts: List[DocPart], combined: bool) -> bytes:
    target_parts = parts if combined else parts[:1]
    if combined:
        blocks = []
        for part in target_parts:
            text = part_to_text(part)
            header = f"===== {part.name} ====="
            blocks.append(f"{header}\n\n{text}" if text else header)
        body = "\n\n\n".join(blocks)
    else:
        body = part_to_text(target_parts[0]) if target_parts else ""
    return body.encode("utf-8")


def write_per_file_outputs(part: DocPart, out_dir: Path):
    """Записать .docx и .txt для одной части (режим per_file)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    docx_bytes = build_docx([part], combined=False)
    txt_bytes = build_txt([part], combined=False)
    (out_dir / f"{_safe_filename(part.name)}.docx").write_bytes(docx_bytes)
    (out_dir / f"{_safe_filename(part.name)}.txt").write_bytes(txt_bytes)


def write_combined_outputs(parts: List[DocPart], out_dir: Path, name: str = "result"):
    """Записать единые .docx и .txt (режим combined)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    docx_bytes = build_docx(parts, combined=True)
    txt_bytes = build_txt(parts, combined=True)
    (out_dir / f"{_safe_filename(name)}.docx").write_bytes(docx_bytes)
    (out_dir / f"{_safe_filename(name)}.txt").write_bytes(txt_bytes)


def build_zip(out_dir: Path) -> bytes:
    """Собрать все файлы в каталоге в один ZIP (in-memory)."""
    buf = BytesIO()
    with ZipFile(buf, "w", ZIP_DEFLATED) as zf:
        for path in sorted(out_dir.rglob("*")):
            if path.is_file():
                zf.write(path, arcname=path.name)
    return buf.getvalue()


def _safe_filename(name: str) -> str:
    """Удалить символы, недопустимые в именах файлов."""
    bad = '<>:"/\\|?*\x00'
    cleaned = "".join("_" if c in bad else c for c in (name or "result"))
    cleaned = cleaned.strip(". ").strip() or "result"
    return cleaned[:120]
