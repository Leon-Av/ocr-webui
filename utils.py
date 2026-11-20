"""
Утилиты для шаблонов: серверное время и форматирование.

Дата/время в БД хранится в UTC (naive). При показе на сайте конвертируем
в локальный часовой пояс сервера, чтобы пользователь видел «своё» время.
"""

from datetime import datetime, timezone
from typing import Optional


def to_local(dt: Optional[datetime]) -> Optional[datetime]:
    """Конвертировать naive-UTC datetime в локальное время сервера."""
    if dt is None:
        return None
    # В БД время хранится как naive UTC. Помечаем зону и конвертируем.
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone()


def fmt_dt(dt: Optional[datetime], fmt: str = "%d.%m.%Y %H:%M") -> str:
    """Форматировать datetime в локальном времени (по умолчанию 'дд.мм.гггг чч:мм')."""
    local = to_local(dt)
    if local is None:
        return "—"
    return local.strftime(fmt)


def fmt_date(dt: Optional[datetime]) -> str:
    """Только дата: 'дд.мм.гггг'."""
    return fmt_dt(dt, "%d.%m.%Y")


def fmt_time(dt: Optional[datetime]) -> str:
    """Только время: 'чч:мм'."""
    return fmt_dt(dt, "%H:%M")
