"""
SQLAlchemy модели базы данных.
"""

from datetime import datetime, timezone
from sqlalchemy import (
    Column, Integer, String, Float, Boolean, DateTime, Text, ForeignKey
)
from sqlalchemy.orm import relationship, DeclarativeBase


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    email = Column(String(255), unique=True, nullable=False, index=True)
    password_hash = Column(String(255), nullable=False)
    first_name = Column(String(100), nullable=False)
    last_name = Column(String(100), nullable=False)
    is_admin = Column(Boolean, default=False)
    created_at = Column(DateTime, default=_now)
    last_login = Column(DateTime, nullable=True)
    login_count = Column(Integer, default=0)
    notifications_enabled = Column(Boolean, default=True)
    sound_enabled = Column(Boolean, default=True)

    jobs = relationship("Job", back_populates="user", cascade="all, delete-orphan")

    @property
    def full_name(self):
        return f"{self.first_name} {self.last_name}"


class Job(Base):
    """Одна загрузка пользователя (1..N файлов) = одна задача."""
    __tablename__ = "jobs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    engine = Column(String(50), nullable=False)        # mineru|llm (легаси: paddleocr-vl)
    output_mode = Column(String(20), default="combined")  # combined|per_file
    status = Column(String(20), default="queued")      # queued|processing|completed|failed|expired
    total_pages = Column(Integer, default=0)            # обработано страниц
    created_at = Column(DateTime, default=_now, index=True)
    started_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)
    processing_time = Column(Float, nullable=True)      # секунды
    error_message = Column(Text, nullable=True)
    result_dir = Column(String(500), nullable=True)

    user = relationship("User", back_populates="jobs")
    files = relationship(
        "JobFile", back_populates="job", cascade="all, delete-orphan",
        order_by="JobFile.order_index"
    )

    @property
    def status_display(self):
        statuses = {
            "queued": "В очереди",
            "processing": "Обработка",
            "completed": "Завершено",
            "failed": "Ошибка",
            "expired": "Удалено (срок истёк)",
        }
        return statuses.get(self.status, self.status)

    @property
    def engine_display(self):
        from config import ENGINES
        # Легаси-имена движков, удалённых из сервиса (для старых записей истории).
        legacy = {"paddleocr-vl": "PaddleOCR-VL-1.5", "olmocr": "olmOCR-2-7B"}
        for e in ENGINES:
            if e["code"] == self.engine:
                return e["name"]
        return legacy.get(self.engine, self.engine)

    @property
    def output_mode_display(self):
        if self.output_mode == "per_file":
            return "По файлам"
        return "Объединённый"

    @property
    def processing_time_display(self):
        if self.processing_time is None:
            return "—"
        total = int(self.processing_time)
        h, rem = divmod(total, 3600)
        m, s = divmod(rem, 60)
        return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


class JobFile(Base):
    """Один загруженный файл внутри задачи."""
    __tablename__ = "job_files"

    id = Column(Integer, primary_key=True, autoincrement=True)
    job_id = Column(Integer, ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, index=True)
    order_index = Column(Integer, default=0)
    original_filename = Column(String(500), nullable=False)
    stored_filename = Column(String(500), nullable=False)
    file_size = Column(Integer, nullable=False)
    is_pdf = Column(Boolean, default=False)
    page_count = Column(Integer, default=0)             # для PDF — число страниц
    status = Column(String(20), default="queued")       # queued|processing|completed|failed
    error_message = Column(Text, nullable=True)
    text_content = Column(Text, nullable=True)          # распознанный plain text
    markdown_content = Column(Text, nullable=True)      # при наличии

    job = relationship("Job", back_populates="files")

    @property
    def file_size_display(self):
        size = self.file_size
        if size < 1024:
            return f"{size} Б"
        elif size < 1024 ** 2:
            return f"{size / 1024:.1f} КБ"
        elif size < 1024 ** 3:
            return f"{size / 1024 ** 2:.1f} МБ"
        else:
            return f"{size / 1024 ** 3:.2f} ГБ"


class ProcessingStat(Base):
    """Статистика обработки для графиков админки / ETE."""
    __tablename__ = "processing_stats"

    id = Column(Integer, primary_key=True, autoincrement=True)
    job_id = Column(Integer, ForeignKey("jobs.id", ondelete="SET NULL"), nullable=True)
    engine = Column(String(50), nullable=False)
    pages = Column(Integer, nullable=False, default=0)
    processing_time = Column(Float, nullable=False)
    created_at = Column(DateTime, default=_now)


class Setting(Base):
    __tablename__ = "settings"

    key = Column(String(100), primary_key=True)
    value = Column(Text, nullable=False)
    updated_at = Column(DateTime, default=_now, onupdate=_now)
