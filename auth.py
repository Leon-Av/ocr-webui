"""
Аутентификация и авторизация.
JWT-токены для пользователей и администратора.
"""

from datetime import datetime, timedelta, timezone
from typing import Optional

from jose import JWTError, jwt
import bcrypt
from sqlalchemy.orm import Session

from config import SECRET_KEY, ALGORITHM, ACCESS_TOKEN_EXPIRE_MINUTES, ADMIN_USERNAME, ADMIN_PASSWORD
from database import SessionLocal
from models import User


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Проверить пароль против хеша. Совместимо с bcrypt >=4.1."""
    if isinstance(plain_password, str):
        plain_password = plain_password.encode("utf-8")
    if isinstance(hashed_password, str):
        hashed_password = hashed_password.encode("utf-8")
    try:
        return bcrypt.checkpw(plain_password, hashed_password)
    except (ValueError, TypeError):
        return False


def get_password_hash(password: str) -> str:
    """Получить хеш пароля. Совместимо с bcrypt >=4.1."""
    if isinstance(password, str):
        password = password.encode("utf-8")
    return bcrypt.hashpw(password, bcrypt.gensalt()).decode("utf-8")


def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    to_encode = data.copy()
    expire = datetime.now(timezone.utc).replace(tzinfo=None) + (
        expires_delta or timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    )
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


def decode_access_token(token: str) -> Optional[dict]:
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except JWTError:
        return None


def get_current_user_from_request(request) -> Optional[User]:
    """Получить текущего пользователя из cookie. Создаёт свою сессию БД."""
    token = request.cookies.get("access_token")
    if not token:
        return None
    payload = decode_access_token(token)
    if not payload:
        return None
    if payload.get("type") == "admin":
        return None
    user_id = payload.get("sub")
    if user_id is None:
        return None
    try:
        uid = int(user_id)
    except (ValueError, TypeError):
        return None
    db = SessionLocal()
    try:
        return db.query(User).filter(User.id == uid).first()
    finally:
        db.close()


def get_current_user_with_db(request, db: Session) -> Optional[User]:
    """Получить текущего пользователя, используя существующую сессию БД."""
    token = request.cookies.get("access_token")
    if not token:
        return None
    payload = decode_access_token(token)
    if not payload:
        return None
    if payload.get("type") == "admin":
        return None
    user_id = payload.get("sub")
    if user_id is None:
        return None
    try:
        uid = int(user_id)
    except (ValueError, TypeError):
        return None
    return db.query(User).filter(User.id == uid).first()


def authenticate_user(email: str, password: str, db: Session) -> Optional[User]:
    """Аутентификация пользователя по email и паролю."""
    user = db.query(User).filter(User.email == email).first()
    if not user:
        return None
    if not verify_password(password, user.password_hash):
        return None
    user.last_login = datetime.now(timezone.utc).replace(tzinfo=None)
    user.login_count = (user.login_count or 0) + 1
    db.commit()
    return user


def authenticate_admin(username: str, password: str) -> bool:
    """Аутентификация администратора по кредам из конфига."""
    return username == ADMIN_USERNAME and password == ADMIN_PASSWORD


def check_admin_token(request) -> Optional[dict]:
    """Проверить авторизацию администратора. Возвращает payload или None."""
    token = request.cookies.get("admin_token")
    if not token:
        return None
    payload = decode_access_token(token)
    if not payload or payload.get("type") != "admin":
        return None
    return payload


# ====================================================================
#  Режим авторизации пользователей (auth_required)
# ====================================================================

def is_auth_required() -> bool:
    """Требуется ли вход по email/паролю (читается из settings).

    False = «открытый» режим: пользователь попадает на / без логина,
    все задачи привязываются к гостевому аккаунту.
    """
    from config import DEFAULT_AUTH_REQUIRED
    from database import get_setting
    val = get_setting("auth_required", str(DEFAULT_AUTH_REQUIRED).lower())
    return val.lower() in ("1", "true", "yes", "on")


def get_guest_user() -> Optional[User]:
    """Вернуть виртуальный гостевой аккаунт (для открытого режима)."""
    from config import GUEST_EMAIL
    db = SessionLocal()
    try:
        return db.query(User).filter(User.email == GUEST_EMAIL).first()
    finally:
        db.close()


def get_or_create_session_user(request):
    """В открытом режиме: вернуть (или создать) уникального гостя по сессии.

    Каждая сессия = отдельный аккаунт (session_<uuid>@local), чтобы истории и
    файлы разных пользователей были изолированы. Сессия живёт DEFAULT_SESSION_HOURS;
    по истечении cleanup.py удаляет гостя и его файлы.

    Возвращает (user, response_cookies) — response_cookies нужно установить на ответ.
    """
    import uuid
    from datetime import datetime, timezone, timedelta
    from config import (GUEST_EMAIL, GUEST_FIRST_NAME, GUEST_LAST_NAME,
                        DEFAULT_SESSION_HOURS, SESSION_GUEST_EMAIL_PREFIX)
    from database import get_setting, SessionLocal

    lifetime_hours = int(get_setting("session_lifetime_hours", str(DEFAULT_SESSION_HOURS)))
    cookie_name = "session_id"
    session_id = request.cookies.get(cookie_name)
    now = datetime.now(timezone.utc).replace(tzinfo=None)

    db = SessionLocal()
    new_cookie_value = None
    try:
        user = None
        if session_id:
            # Ищем сессионного гостя по email session_<id>@local.
            email = f"{SESSION_GUEST_EMAIL_PREFIX}{session_id}@local"
            user = db.query(User).filter(User.email == email).first()
            if user:
                # Продлеваем срок жизни при активности.
                user.last_login = now
                db.commit()
        if not user:
            # Создаём нового сессионного гостя.
            session_id = uuid.uuid4().hex
            email = f"{SESSION_GUEST_EMAIL_PREFIX}{session_id}@local"
            user = User(
                email=email,
                first_name=GUEST_FIRST_NAME,
                last_name=GUEST_LAST_NAME,
                password_hash=get_password_hash("__session_no_login__"),
                is_admin=False,
                last_login=now,
            )
            db.add(user)
            db.commit()
            db.refresh(user)
            new_cookie_value = session_id
        # Возвращаем user в отдельной сессии через id ( detached ).
        user_id = user.id
    finally:
        db.close()

    # Перечитываем в свежей сессии, чтобы вернуть «живой» объект.
    db = SessionLocal()
    try:
        u = db.query(User).filter(User.id == user_id).first()
        return u, new_cookie_value
    finally:
        db.close()


def get_session_user(request) -> Optional[User]:
    """Получить «текущего» пользователя с учётом режима авторизации.

    • auth_required=True  — обычная проверка cookie access_token.
    • auth_required=False — сессионный гость из request.state (создаётся в middleware)
                            или поиск по cookie session_id.
    """
    if not is_auth_required():
        # В открытом режиме middleware создаёт гостя и кладёт в request.state.session_user.
        cached = getattr(request.state, "session_user", None)
        if cached is not None:
            return cached
        # Фолбэк: если middleware не отработал (напр. статика) — ищем по cookie.
        from config import SESSION_GUEST_EMAIL_PREFIX
        session_id = request.cookies.get("session_id")
        if session_id:
            db = SessionLocal()
            try:
                return db.query(User).filter(
                    User.email == f"{SESSION_GUEST_EMAIL_PREFIX}{session_id}@local"
                ).first()
            finally:
                db.close()
        return None
    return get_current_user_from_request(request)


def generate_random_password(length: int = 12) -> str:
    """Генерация случайного пароля."""
    import secrets
    import string
    alphabet = string.ascii_letters + string.digits + "!@#$%"
    password = ''.join(secrets.choice(alphabet) for _ in range(length))
    if not any(c.isalpha() for c in password):
        password = password[:-1] + secrets.choice(string.ascii_letters)
    if not any(c.isdigit() for c in password):
        password = password[:-1] + secrets.choice(string.digits)
    return password
