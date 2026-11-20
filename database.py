"""
Инициализация базы данных и вспомогательные функции.
"""

from datetime import datetime, timezone
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker

from config import (
    DATABASE_PATH, DEFAULT_RETENTION_DAYS, MAX_TOTAL_STORAGE_GB,
    DEFAULT_MINERU_URL, DEFAULT_UNLIMITEDOCR_URL,
    DEFAULT_LLM_URL, DEFAULT_LLM_MODEL, DEFAULT_ENGINE_ENABLED,
    DEFAULT_ENGINE_GPU, DEFAULT_AUTH_REQUIRED, DEFAULT_SESSION_HOURS,
    GUEST_EMAIL, GUEST_FIRST_NAME, GUEST_LAST_NAME,
)
from models import Base, User, Setting

engine = create_engine(
    f"sqlite:///{DATABASE_PATH}", echo=False, connect_args={"check_same_thread": False}
)


@event.listens_for(engine, "connect")
def set_sqlite_pragma(dbapi_connection, connection_record):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def get_db():
    """Зависимость FastAPI для получения сессии БД."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    """Создание таблиц и заполнение начальными данными."""
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    Base.metadata.create_all(bind=engine)
    _run_migrations()
    _seed_settings()
    _ensure_guest_user()


def _ensure_guest_user():
    """Создать виртуальный гостевой аккаунт, если его ещё нет.

    Используется в «открытом» режиме (auth_required=false), когда на /
    можно зайти без логина. Все задачи привязываются к этому аккаунту.
    """
    db = SessionLocal()
    try:
        existing = db.query(User).filter(User.email == GUEST_EMAIL).first()
        if not existing:
            from auth import get_password_hash
            db.add(User(
                email=GUEST_EMAIL,
                first_name=GUEST_FIRST_NAME,
                last_name=GUEST_LAST_NAME,
                password_hash=get_password_hash("__guest_no_login__"),
                is_admin=False,
            ))
            db.commit()
    finally:
        db.close()


def _run_migrations():
    """Идемпотентное добавление новых колонок в существующие таблицы.

    SQLAlchemy create_all не обновляет структуру существующих таблиц,
    поэтому новые колонки добавляем через ALTER TABLE.
    """
    migrations = [
        # (table, column, sql-type, default)
        ("jobs", "total_pages", "INTEGER", "0"),
        ("job_files", "markdown_content", "TEXT", "NULL"),
        ("job_files", "page_count", "INTEGER", "0"),
    ]
    with engine.connect() as conn:
        for table, column, col_type, default in migrations:
            cols = conn.execute(text(f"PRAGMA table_info({table})")).fetchall()
            col_names = [row[1] for row in cols]
            if column not in col_names:
                conn.execute(
                    text(f"ALTER TABLE {table} ADD COLUMN {column} {col_type} DEFAULT {default}")
                )
                conn.commit()


def _seed_settings():
    """Заполнение настроек по умолчанию."""
    db = SessionLocal()
    try:
        defaults = {
            # URL'ы движков (можно переназначить в админке)
            "engine_url_mineru": DEFAULT_MINERU_URL,
            "engine_url_unlimitedocr": DEFAULT_UNLIMITEDOCR_URL,
            "engine_url_llm": DEFAULT_LLM_URL,
            "engine_llm_model": DEFAULT_LLM_MODEL,
            # Флаги включения движков
            "engine_enabled_mineru": str(DEFAULT_ENGINE_ENABLED["mineru"]).lower(),
            "engine_enabled_unlimitedocr": str(DEFAULT_ENGINE_ENABLED["unlimited-ocr"]).lower(),
            "engine_enabled_llm": str(DEFAULT_ENGINE_ENABLED["llm"]).lower(),
            # GPU для движков ("auto" | "0" | "1" | ...; применяется run_all
            # при запуске сервера движка через CUDA_VISIBLE_DEVICES)
            "engine_gpu_mineru": DEFAULT_ENGINE_GPU["mineru"],
            "engine_gpu_unlimitedocr": DEFAULT_ENGINE_GPU["unlimitedocr"],
            # Хранилище и очистка
            "retention_days": str(DEFAULT_RETENTION_DAYS),
            "storage_limit_gb": str(MAX_TOTAL_STORAGE_GB),
            # Режим авторизации (true = требовать вход, false = открытый режим)
            "auth_required": str(DEFAULT_AUTH_REQUIRED).lower(),
            "session_lifetime_hours": str(DEFAULT_SESSION_HOURS),
        }
        for key, value in defaults.items():
            existing = db.query(Setting).filter(Setting.key == key).first()
            if not existing:
                db.add(Setting(key=key, value=value))
        db.commit()
    finally:
        db.close()


def get_setting(key: str, default: str = "") -> str:
    """Получить значение настройки."""
    db = SessionLocal()
    try:
        setting = db.query(Setting).filter(Setting.key == key).first()
        return setting.value if setting else default
    finally:
        db.close()


def set_setting(key: str, value: str):
    """Установить значение настройки."""
    db = SessionLocal()
    try:
        setting = db.query(Setting).filter(Setting.key == key).first()
        if setting:
            setting.value = value
            setting.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
        else:
            db.add(Setting(key=key, value=value))
        db.commit()
    finally:
        db.close()


def get_total_storage_used() -> int:
    """Подсчитать общий объём загруженных файлов в байтах."""
    from config import UPLOADS_DIR, RESULTS_DIR
    total = 0
    for directory in [UPLOADS_DIR, RESULTS_DIR]:
        if directory.exists():
            for f in directory.rglob("*"):
                if f.is_file():
                    total += f.stat().st_size
    return total
