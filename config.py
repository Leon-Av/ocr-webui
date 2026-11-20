"""
Конфигурация OCR-сервиса.
Все настраиваемые параметры собраны в этом файле.

Часть параметров дублируется в таблице `settings` БД и редактируется
через админку (URL'ы движков, флаги включения, retention, лимит хранилища).
Значения ниже — это значения по умолчанию.
"""

import os
from pathlib import Path

# === Сервер ===
HOST = "0.0.0.0"
PORT = 2002
SECRET_KEY = os.environ.get("SECRET_KEY", "CHANGE-ME-in-production")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24 * 7  # 7 дней

# === Администратор (логинится через эти креды, не через БД) ===
ADMIN_USERNAME = "admin"
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin")  # задайте через переменную окружения

# === Режим авторизации пользователей ===
# True  — требовать вход по email/паролю (классический режим, как в example).
# False — «открытый» режим: на / можно зайти без логина, все задачи
#         привязываются к виртуальному гостевому аккаунту. Удобно для
#         локального/интернет-экспоната. Переключается в админке (Настройки).
DEFAULT_AUTH_REQUIRED = os.environ.get("AUTH_REQUIRED", "true").lower() == "true"
# Email и имя виртуального гостевого аккаунта (создаётся при инициализации БД).
GUEST_EMAIL = "guest@local"
GUEST_FIRST_NAME = "Гость"
GUEST_LAST_NAME = ""
# Время жизни сессионного гостя в открытом режиме (часы).
# По истечении — файлы и история сессии автоматически удаляются (cleanup.py).
DEFAULT_SESSION_HOURS = int(os.environ.get("SESSION_HOURS", "48"))
# Префикс email для сессионных гостей (напр. session_<uuid>@local).
SESSION_GUEST_EMAIL_PREFIX = "session_"

# === OCR-движки ===
# Каждый движок работает как отдельный HTTP-сервис и реализует
# единый контракт: POST /ocr (multipart image) -> {"text", "markdown"}.
# URL'ы ниже можно переназначить в админке (Настройки).
DEFAULT_MINERU_URL = os.environ.get("MINERU_URL", "http://localhost:8501")
DEFAULT_UNLIMITEDOCR_URL = os.environ.get("UNLIMITEDOCR_URL", "http://localhost:8503")
# LLM — это локальный LM Studio с OpenAI-совместимым API.
DEFAULT_LLM_URL = os.environ.get("LLM_URL", "http://localhost:1235/v1")
# Имя модели в LM Studio (берётся из списка загруженных моделей).
DEFAULT_LLM_MODEL = os.environ.get("LLM_MODEL", "local-llm")

# По умолчанию все движки включены (видны пользователям).
# Реальная доступность определяется пингом /health при запросе страницы.
DEFAULT_ENGINE_ENABLED = {
    "mineru": True,
    "unlimited-ocr": True,
    "llm": True,
}

# GPU для OCR-движков (применяется при запуске сервисов через run_all):
#   "auto" — не закреплять (движок видит все GPU и берёт первый);
#   "0"/"1"/... — закрепить CUDA-устройство с этим номером (номера как
#                 в nvidia-smi, т.к. при пиннинге задаётся PCI-порядок).
# Значения хранятся в settings (engine_gpu_<имя сервиса>), редактируются
# в админке (Настройки → OCR-движки). LLM не входит: GPU для него
# выбирается в самом LM Studio.
DEFAULT_ENGINE_GPU = {
    "mineru": os.environ.get("MINERU_GPU", "auto"),
    "unlimitedocr": os.environ.get("UNLIMITEDOCR_GPU", "auto"),
}

# Реестр движков: ключ -> человекочитаемое название (для UI).
ENGINES = [
    {"code": "mineru", "name": "MinerU 2.5-Pro-1.2B", "url_setting": "engine_url_mineru"},
    {"code": "unlimited-ocr", "name": "Unlimited-OCR (Baidu)", "url_setting": "engine_url_unlimitedocr"},
    {"code": "llm", "name": "LLM (LM Studio)", "url_setting": "engine_url_llm"},
]

# Таймаут на запрос к движку (секунды). OCR VLM может думать подолго.
ENGINE_REQUEST_TIMEOUT = float(os.environ.get("ENGINE_REQUEST_TIMEOUT", "600"))
ENGINE_HEALTH_TIMEOUT = float(os.environ.get("ENGINE_HEALTH_TIMEOUT", "3"))
# Число повторов при сетевой ошибке / 5xx движка.
ENGINE_MAX_RETRIES = int(os.environ.get("ENGINE_MAX_RETRIES", "2"))

# === Растеризация PDF ===
PDF_RENDER_DPI = int(os.environ.get("PDF_RENDER_DPI", "200"))
PDF_RENDER_FORMAT = "png"  # png | jpeg

# === Режимы вывода результатов ===
OUTPUT_MODES = [
    {"code": "combined", "name": "Объединить все файлы в один документ"},
    {"code": "per_file", "name": "Каждый файл — отдельный результат"},
]

# === Хранилище ===
# STORAGE_ROOT — корневой каталог для хранения файлов.
# Можно указать другой диск, например: "/mnt/data" или "D:/ocr_storage".
BASE_DIR = Path(__file__).parent
STORAGE_ROOT = Path(os.environ.get("STORAGE_ROOT", "D:/ocr_storage"))
UPLOADS_DIR = STORAGE_ROOT / "uploads"
RESULTS_DIR = STORAGE_ROOT / "results"
DATABASE_PATH = BASE_DIR / "ocr_service.db"

MAX_TOTAL_STORAGE_GB = 60
MAX_TOTAL_STORAGE_BYTES = MAX_TOTAL_STORAGE_GB * 1024 ** 3

# Лимит размера одного загружаемого файла (МБ).
MAX_FILE_SIZE_MB = int(os.environ.get("MAX_FILE_SIZE_MB", "200"))
MAX_FILE_SIZE_BYTES = MAX_FILE_SIZE_MB * 1024 ** 2

# === Поддерживаемые форматы ===
SUPPORTED_IMAGE_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tiff", ".tif"
}
SUPPORTED_PDF_EXTENSIONS = {".pdf"}
SUPPORTED_EXTENSIONS = SUPPORTED_IMAGE_EXTENSIONS | SUPPORTED_PDF_EXTENSIONS

# === Настройки по умолчанию ===
DEFAULT_RETENTION_DAYS = 60
EXPIRATION_WARNING_DAYS = 7  # За сколько дней предупреждать об удалении

# === Очистка ===
CLEANUP_INTERVAL_SECONDS = 3600  # Проверка каждый час

# === SMTP / Email (необязательно; по умолчанию выключено) ===
EMAIL_ENABLED = os.environ.get("EMAIL_ENABLED", "false").lower() == "true"
SMTP_HOST = os.environ.get("SMTP_HOST", "mail.example.ru")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "no-reply@example.ru")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
SMTP_FROM_EMAIL = os.environ.get("SMTP_FROM_EMAIL", "no-reply@example.ru")
SMTP_FROM_NAME = os.environ.get("SMTP_FROM_NAME", "OCR Сервис")
SMTP_USE_TLS = os.environ.get("SMTP_USE_TLS", "true").lower() == "true"

# === URL сервиса (для ссылок в письмах) ===
SERVICE_URL = os.environ.get("SERVICE_URL", "http://localhost:2002")
