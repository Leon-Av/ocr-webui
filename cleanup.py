"""
Фоновая очистка устаревших задач по retention_days.
Удаляет загруженные файлы и результаты, переводит задачу в status=expired.
"""

import asyncio
import logging
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

from config import (CLEANUP_INTERVAL_SECONDS, UPLOADS_DIR, RESULTS_DIR,
                    DEFAULT_SESSION_HOURS, SESSION_GUEST_EMAIL_PREFIX)
from database import SessionLocal, get_setting
from models import Job, JobFile, User
from worker import _job_events

logger = logging.getLogger(__name__)


def _delete_expired_session_users(db):
    """Удалить сессионных гостей с истёкшим TTL и их задачи/файлы.

    В открытом режиме каждый посетитель = отдельный сессионный гость
    (email session_<id>@local). По истечении session_lifetime_hours
    удаляем гостя, его задачи и файлы, чтобы история была изолирована
    и не копилась бесконечно.
    """
    from datetime import datetime, timezone, timedelta
    lifetime_hours = int(get_setting("session_lifetime_hours", str(DEFAULT_SESSION_HOURS)))
    threshold = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=lifetime_hours)

    # Сессионные гости — те, чей email начинается с префикса.
    guests = (
        db.query(User)
        .filter(User.email.like(f"{SESSION_GUEST_EMAIL_PREFIX}%@local"))
        .all()
    )
    removed = 0
    for g in guests:
        # last_login обновляется при каждой активности сессии.
        last_seen = g.last_login or g.created_at
        if last_seen < threshold:
            # Удаляем файлы всех задач этого гостя.
            jobs = db.query(Job).filter(Job.user_id == g.id).all()
            for job in jobs:
                _delete_job_files(job)
            # Удаляем самого гостя (каскадно — задачи через FK CASCADE).
            db.delete(g)
            removed += 1
    if removed:
        db.commit()
        logger.info(f"Cleanup: удалено {removed} просроченных сессионных гостей.")
    return removed


def get_expiration_info(job: Job) -> dict:
    """Информация о сроке жизни задачи (для UI-предупреждений)."""
    retention = int(get_setting("retention_days", "60"))
    base = job.completed_at or job.created_at
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    expires_at = base + timedelta(days=retention)
    delta = expires_at - now
    days_remaining = max(0, delta.days)
    return {
        "is_warning": days_remaining <= 7 and job.status == "completed",
        "is_expired": delta.total_seconds() <= 0,
        "expires_at": expires_at,
        "days_remaining": days_remaining,
    }


def _delete_job_files(job: Job):
    if job.result_dir:
        rp = Path(job.result_dir)
        if rp.exists():
            shutil.rmtree(rp, ignore_errors=True)
    for f in job.files:
        up = UPLOADS_DIR / f.stored_filename
        if up.exists():
            try:
                up.unlink()
            except OSError:
                pass


async def cleanup_loop():
    """Периодически помечает истёкшие задачи как expired и чистит файлы."""
    logger.info("Cleanup-воркер запущен.")
    while True:
        try:
            await asyncio.sleep(CLEANUP_INTERVAL_SECONDS)
            retention = int(get_setting("retention_days", "60"))
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            threshold = now - timedelta(days=retention)

            db = SessionLocal()
            try:
                # 1. Удаляем просроченных сессионных гостей (открытый режим).
                _delete_expired_session_users(db)

                # 2. Помечаем истёкшие задачи как expired.
                candidates = (
                    db.query(Job)
                    .filter(Job.status.in_(["completed", "failed"]))
                    .all()
                )
                expired_count = 0
                for job in candidates:
                    base = job.completed_at or job.created_at
                    if base and base < threshold:
                        _delete_job_files(job)
                        job.status = "expired"
                        job.result_dir = None
                        expired_count += 1
                db.commit()
                if expired_count:
                    logger.info(f"Cleanup: помечено как expired — {expired_count} задач.")
            finally:
                db.close()
        except Exception as e:
            logger.error(f"Ошибка в cleanup_loop: {e}", exc_info=True)
            await asyncio.sleep(60)
