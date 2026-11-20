"""
Очередь обработки и worker.
Обеспечивает последовательную обработку задач, по одной за раз.
Поддержка отмены и live-прогресса через SSE.
"""

import asyncio
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from sqlalchemy.orm import Session

from config import UPLOADS_DIR, RESULTS_DIR
from database import SessionLocal
from models import Job, JobFile, ProcessingStat
from ocr import get_engine
from ocr.base import OCRError
from ocr.pdf_utils import rasterize_pdf, is_pdf, normalize_image_to_png
import doc_builder

logger = logging.getLogger(__name__)

# Человекочитаемый суффикс движка для имён выходных файлов.
# Напр. отчёт.pdf + MinerU -> отчёт_MinerU.docx / отчёт_MinerU.txt.
ENGINE_SUFFIX = {
    "mineru": "MinerU",
    "unlimited-ocr": "Unlimited-OCR",
    "llm": "LLM",
}


def _engine_suffix(code: str) -> str:
    return ENGINE_SUFFIX.get((code or "").strip().lower(), code or "OCR")

# Глобальная очередь задач
job_queue: asyncio.Queue = asyncio.Queue()

# Текущий статус обработки для SSE
_current_status: dict = {
    "job_id": None,
    "status": "idle",
    "progress": 0,
    "message": "Ожидание задач...",
    "current_file": None,
    "current_page": None,
    "total_pages": None,
    "user_jobs": [],
}

# События завершения задач (для синхронного ожидания)
_job_events: dict[int, asyncio.Event] = {}

# Множество отменённых задач
_cancelled_jobs: set[int] = set()


def get_current_status() -> dict:
    """Получить текущий статус обработки (копия)."""
    return _current_status.copy()


def get_job_event(job_id: int) -> asyncio.Event:
    if job_id not in _job_events:
        _job_events[job_id] = asyncio.Event()
    return _job_events[job_id]


def is_job_cancelled(job_id: int) -> bool:
    return job_id in _cancelled_jobs


def cancel_job(job_id: int) -> bool:
    """Отменить задачу. Возвращает True если задача была отменена."""
    db = SessionLocal()
    try:
        job = db.query(Job).filter(Job.id == job_id).first()
        if not job:
            return False
        if job.status in ("completed", "failed", "expired"):
            return False

        if job.status == "processing":
            _cancelled_jobs.add(job_id)
            logger.info(f"Задача {job_id} помечена для отмены (обрабатывается)")
            return True

        if job.status == "queued":
            job.status = "failed"
            job.error_message = "Задача отменена пользователем"
            job.completed_at = datetime.now(timezone.utc).replace(tzinfo=None)
            db.commit()
            _cleanup_job_files(job)
            _cancelled_jobs.add(job_id)
            get_job_event(job_id).set()
            logger.info(f"Задача {job_id} отменена (была в очереди)")
            return True
        return False
    finally:
        db.close()


def _cleanup_job_files(job: Job):
    """Удалить файлы задачи (загруженные + результаты)."""
    if job.result_dir:
        rp = Path(job.result_dir)
        if rp.exists():
            import shutil
            shutil.rmtree(rp, ignore_errors=True)
    for f in job.files:
        up = UPLOADS_DIR / f.stored_filename
        if up.exists():
            try:
                up.unlink()
            except OSError:
                pass


async def enqueue_job(job_id: int):
    event = asyncio.Event()
    _job_events[job_id] = event
    await job_queue.put(job_id)
    logger.info(f"Задача {job_id} добавлена в очередь. В очереди: {job_queue.qsize()}")


def get_queue_position(job_id: int) -> Optional[int]:
    """Позиция задачи в очереди (0 = обрабатывается)."""
    db = SessionLocal()
    try:
        job = db.query(Job).filter(Job.id == job_id).first()
        if not job or job.status != "queued":
            return None
        count = (
            db.query(Job)
            .filter(Job.status == "queued", Job.created_at < job.created_at)
            .count()
        )
        return count + 1
    finally:
        db.close()


def _set_status(job: Job, **kwargs):
    _current_status.update({
        "job_id": job.id,
        "user_id": job.user_id,
        **kwargs,
    })


async def _ocr_pages(job: Job, file: JobFile, engine, progress_cb=None) -> list[doc_builder.Page]:
    """Растеризовать (если PDF) и распознать все страницы файла. Возвращает список Page.

    progress_cb(done_pages, total_pages) — опциональный колбэк для обновления
    прогресс-бара постранично (чинит «застывание на 10%»).
    """
    file_path = UPLOADS_DIR / file.stored_filename

    # Подготовить список (имя, байты) страниц/изображений.
    if is_pdf(file_path):
        pages = rasterize_pdf(file_path)
    else:
        with open(file_path, "rb") as fh:
            pages = [(file_path.name, fh.read())]

    total_file_pages = len(pages)
    result_pages: list[doc_builder.Page] = []
    for idx, (label, img_bytes) in enumerate(pages, start=1):
        if job.id in _cancelled_jobs:
            raise OCRError("Задача отменена пользователем")

        _set_status(
            job,
            status="processing",
            current_file=file.original_filename,
            current_page=f"{idx}/{total_file_pages}",
            message=f"OCR: {file.original_filename} — страница {idx} из {total_file_pages}",
        )

        # Нормализуем картинку к PNG для совместимости с движками.
        send_bytes = img_bytes
        send_name = label
        try:
            send_bytes = normalize_image_to_png(img_bytes)
            if not label.lower().endswith(".png"):
                send_name = Path(label).stem + ".png"
        except Exception:
            pass  # если не получилось — шлём как есть

        try:
            res = await engine.ocr(send_bytes, filename=send_name)
            page = doc_builder.Page(
                label=Path(label).stem,
                text=res.text or "",
                markdown=res.markdown,
            )
        except OCRError as e:
            page = doc_builder.Page(label=Path(label).stem, text="", markdown=None)
            logger.warning(f"[job {job.id}] OCR не удалась для {file.original_filename}/{label}: {e}")
            # Сохраняем ошибку на уровне файла (в памяти + в БД),
            # но продолжаем по следующим страницам.
            err = str(e)
            if file.error_message:
                file.error_message += f"; {label}: {err}"
            else:
                file.error_message = f"{label}: {err}"
            _persist_file_error(file.id, f"{label}: {err}")

        result_pages.append(page)
        # Обновляем прогресс постранично, чтобы бар не «застывал».
        if progress_cb is not None:
            try:
                progress_cb(idx, total_file_pages)
            except Exception:
                pass
    return result_pages


def _persist_file_error(file_id: int, message: str):
    """Сразу сохранить сообщение об ошибке на странице в БД (своя сессия)."""
    db = SessionLocal()
    try:
        f = db.query(JobFile).filter(JobFile.id == file_id).first()
        if f:
            if f.error_message:
                f.error_message += f"; {message}"
            else:
                f.error_message = message
            db.commit()
    finally:
        db.close()


async def _process_file(job: Job, file: JobFile, engine, progress_cb=None):
    """Обработать один файл задачи: OCR всех страниц, сохранить текст в БД.

    progress_cb(done_pages_in_file, total_pages_in_file) — колбэк для прогресса.
    """
    db = SessionLocal()
    try:
        # Перечитываем file в этой сессии для коммита.
        f = db.query(JobFile).filter(JobFile.id == file.id).first()
        f.status = "processing"
        db.commit()
    finally:
        db.close()

    try:
        pages = await _ocr_pages(job, file, engine, progress_cb=progress_cb)

        db = SessionLocal()
        try:
            f = db.query(JobFile).filter(JobFile.id == file.id).first()
            all_text = "\n\n".join((p.text or "") for p in pages if (p.text or "").strip())
            all_md = "\n\n".join(
                (p.markdown or p.text or "") for p in pages if (p.markdown or p.text or "").strip()
            )
            f.text_content = all_text
            f.markdown_content = all_md
            f.page_count = len(pages)
            f.status = "completed" if all_text.strip() else "failed"
            if not all_text.strip():
                # f.error_message уже содержит реальные ошибки со страниц
                # (сохранённые через _persist_file_error); фолбэк — только если их не было.
                if not f.error_message:
                    f.error_message = "Не распознано ни одного символа"
            db.commit()
            # Скопируем обратно в объект file (для использования в основной сессии).
            file.page_count = f.page_count
            file.status = f.status
            file.text_content = f.text_content
            file.markdown_content = f.markdown_content
            file.error_message = f.error_message
        finally:
            db.close()
    except OCRError as e:
        db = SessionLocal()
        try:
            f = db.query(JobFile).filter(JobFile.id == file.id).first()
            f.status = "failed"
            f.error_message = str(e)
            db.commit()
        finally:
            db.close()
        file.status = "failed"
        file.error_message = str(e)
        raise


async def process_job(job_id: int):
    """Обработка одной задачи."""
    db = SessionLocal()
    try:
        job = db.query(Job).filter(Job.id == job_id).first()
        if not job:
            logger.error(f"Задача {job_id} не найдена.")
            return

        if job_id in _cancelled_jobs:
            _mark_failed(job, db, "Задача отменена пользователем")
            _cleanup_job_files(job)
            _cancelled_jobs.discard(job_id)
            return

        job.status = "processing"
        job.started_at = datetime.now(timezone.utc).replace(tzinfo=None)
        db.commit()

        _set_status(
            job,
            status="processing",
            progress=5,
            message=f"Запуск движка: {job.engine_display}",
            current_file=None, current_page=None,
        )

        start_time = time.time()
        result_dir = RESULTS_DIR / str(job_id)

        try:
            engine = get_engine(job.engine)

            # Считаем общее число «листов» (страниц) для прогресса.
            ordered_files = list(job.files)
            total_pages_est = 0
            # Сколько страниц уже обработано в файлах ДО текущего (для глобального счёта).
            pages_before: dict[int, int] = {}
            running = 0
            for f in ordered_files:
                pages_before[f.id] = running
                fp = UPLOADS_DIR / f.stored_filename
                if is_pdf(fp):
                    from ocr.pdf_utils import get_pdf_page_count
                    n = get_pdf_page_count(fp) or 1
                else:
                    n = 1
                running += n
            total_pages_est = running
            _set_status(job, total_pages=total_pages_est, progress=10)

            # Глобальный счётчик обработанных страниц (обновляется постранично).
            pages_done = {"n": 0}

            def make_progress_cb(file_id: int):
                """Колбэк: (done_in_file, total_in_file) -> обновить глобальный прогресс."""
                base = pages_before.get(file_id, 0)
                def _cb(done_in_file, total_in_file):
                    pages_done["n"] = base + done_in_file
                    total = max(total_pages_est, 1)
                    # 10% (старт) .. 90% (весь OCR готов), пропорционально страницам.
                    pct = 10 + int(80 * pages_done["n"] / total)
                    _set_status(job, progress=min(pct, 90))
                return _cb

            for f in ordered_files:
                if job.id in _cancelled_jobs:
                    raise OCRError("Задача отменена пользователем")
                await _process_file(job, f, engine, progress_cb=make_progress_cb(f.id))
                _set_status(
                    job,
                    message=f"Готово: {f.original_filename}",
                )
            # Финальный апдейт после всех файлов.
            pages_done["n"] = total_pages_est
            _set_status(job, progress=90)

            # Перечитаем файлы с уже сохранёнными результатами.
            db.expire_all()
            files = db.query(JobFile).filter(JobFile.job_id == job_id).order_by(JobFile.order_index).all()

            # Сборка выходных документов.
            _set_status(job, progress=92, message="Сборка документов...")
            suffix = _engine_suffix(job.engine)
            parts = []
            for f in files:
                pages = _split_to_pages(f)
                parts.append(doc_builder.DocPart(
                    name=f"{Path(f.original_filename).stem}_{suffix}",
                    pages=pages,
                    error=f.error_message if f.status == "failed" else None,
                ))

            if job.output_mode == "per_file":
                for part in parts:
                    doc_builder.write_per_file_outputs(part, result_dir)
            else:
                # combined: один документ, имя = result_<Engine>.
                doc_builder.write_combined_outputs(parts, result_dir, name=f"result_{suffix}")

            job.total_pages = sum((f.page_count or 0) for f in files)
            failed_count = sum(1 for f in files if f.status == "failed")
            job.completed_at = datetime.now(timezone.utc).replace(tzinfo=None)
            job.processing_time = time.time() - start_time
            job.result_dir = str(result_dir)
            # Все файлы провалились → задача failed; иначе completed
            # (с частичными ошибками, отражёнными в JobFile.error_message).
            if files and failed_count == len(files):
                job.status = "failed"
                job.error_message = (
                    (files[0].error_message or "Ошибка OCR")
                    if len(files) == 1
                    else f"Все {failed_count} файл(ов) не распознаны"
                )
            else:
                job.status = "completed"
            db.commit()

            # Статистика — только для успешно завершённых/частично успешных задач.
            if job.status == "completed":
                db.add(ProcessingStat(
                    job_id=job.id,
                    engine=job.engine,
                    pages=job.total_pages,
                    processing_time=job.processing_time,
                ))
                db.commit()

            _set_status(
                job, progress=100, status="completed",
                message=f"Завершено: {len(files)} файл(ов), {job.total_pages} стр.",
            )
            logger.info(f"Задача {job_id} завершена за {job.processing_time:.1f} сек.")

        except OCRError as e:
            _mark_failed(job, db, str(e))
        except Exception as e:
            logger.error(f"Ошибка обработки задачи {job_id}: {e}", exc_info=True)
            _mark_failed(job, db, f"Внутренняя ошибка: {e}")

    except Exception as e:
        logger.error(f"Критическая ошибка в process_job({job_id}): {e}", exc_info=True)
    finally:
        db.close()
        if job_id in _job_events:
            _job_events[job_id].set()
        _cancelled_jobs.discard(job_id)


def _split_to_pages(f: JobFile) -> list[doc_builder.Page]:
    """Восстановить список Page из сохранённых text/markdown файла (одна «страница»)."""
    text = f.text_content or ""
    md = f.markdown_content or None
    if not text and not md:
        return []
    return [doc_builder.Page(label="content", text=text, markdown=md)]


def _mark_failed(job: Job, db: Session, message: str):
    job.status = "failed"
    job.error_message = message
    job.completed_at = datetime.now(timezone.utc).replace(tzinfo=None)
    db.commit()
    _current_status.update({
        "job_id": job.id,
        "status": "failed",
        "progress": 0,
        "message": message,
    })


async def worker_loop():
    """Основной цикл worker'а — обрабатывает задачи по очереди."""
    logger.info("Worker запущен, ожидание задач...")
    while True:
        try:
            job_id = await job_queue.get()
            logger.info(f"Worker начал обработку задачи {job_id}")
            await process_job(job_id)
            job_queue.task_done()

            if job_queue.empty():
                _current_status.update({
                    "job_id": None,
                    "status": "idle",
                    "progress": 0,
                    "message": "Ожидание задач...",
                    "current_file": None,
                    "current_page": None,
                    "total_pages": None,
                })
            else:
                _current_status.update({
                    "status": "queued",
                    "message": f"В очереди: {job_queue.qsize()} задач",
                })
        except Exception as e:
            logger.error(f"Ошибка в worker_loop: {e}", exc_info=True)
            await asyncio.sleep(1)


async def requeue_pending_jobs():
    """При запуске: вернуть незавершённые задачи в очередь."""
    db = SessionLocal()
    try:
        # Зависшие в processing — считаем упавшими.
        stuck = db.query(Job).filter(Job.status == "processing").all()
        for job in stuck:
            job.status = "failed"
            job.error_message = "Сервер перезапущен во время обработки"
            job.completed_at = datetime.now(timezone.utc).replace(tzinfo=None)

        queued = (
            db.query(Job)
            .filter(Job.status == "queued")
            .order_by(Job.created_at.asc())
            .all()
        )
        for job in queued:
            await job_queue.put(job.id)
            _job_events[job.id] = asyncio.Event()

        db.commit()
        logger.info(
            f"В очередь возвращено {len(queued)} задач, {len(stuck)} задач помечены как failed."
        )
    finally:
        db.close()
