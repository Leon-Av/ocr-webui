"""
SMTP-отправка писем (необязательная).
Если EMAIL_ENABLED=False — функции молча ничего не делают.
"""

import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from config import (
    EMAIL_ENABLED, SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD,
    SMTP_FROM_EMAIL, SMTP_FROM_NAME, SERVICE_URL,
)

logger = logging.getLogger(__name__)


def _send(to_email: str, subject: str, body: str):
    if not EMAIL_ENABLED:
        logger.info(f"[email disabled] письмо для {to_email}: {subject}")
        return
    msg = MIMEMultipart("alternative")
    msg["From"] = f"{SMTP_FROM_NAME} <{SMTP_FROM_EMAIL}>"
    msg["To"] = to_email
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain", "utf-8"))
    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as s:
            s.ehlo()
            if SMTP_USE_TLS:
                s.starttls()
                s.ehlo()
            if SMTP_USER and SMTP_PASSWORD:
                s.login(SMTP_USER, SMTP_PASSWORD)
            s.sendmail(SMTP_FROM_EMAIL, [to_email], msg.as_string())
    except Exception as e:
        logger.error(f"SMTP-отправка не удалась ({to_email}): {e}")
        raise


def send_new_account_email(user, password: str):
    subject = "Аккаунт создан — OCR Сервис"
    body = (
        f"Здравствуйте, {user.first_name}!\n\n"
        f"Для вас создан аккаунт в OCR-сервисе.\n"
        f"Сайт: {SERVICE_URL}\n"
        f"Email: {user.email}\n"
        f"Пароль: {password}\n\n"
        f"Рекомендуем сменить пароль после первого входа.\n"
    )
    _send(user.email, subject, body)


def send_password_reset_email(user, new_password: str):
    subject = "Новый пароль — OCR Сервис"
    body = (
        f"Здравствуйте, {user.first_name}!\n\n"
        f"Ваш пароль был сброшен.\n"
        f"Сайт: {SERVICE_URL}\n"
        f"Email: {user.email}\n"
        f"Новый пароль: {new_password}\n\n"
        f"Рекомендуем сменить пароль после входа.\n"
    )
    _send(user.email, subject, body)
