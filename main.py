"""
Главный файл приложения FastAPI.
Маршруты для пользователей и администратора, API загрузки/обработки, SSE.
"""

import asyncio
import json
import logging
import uuid
import zipfile
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path

from fastapi import (
    FastAPI, Request, Depends, HTTPException, UploadFile, File, Form, Query
)
from fastapi.responses import (
    HTMLResponse, RedirectResponse, FileResponse, StreamingResponse, JSONResponse
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, desc
from sqlalchemy.orm import Session

from config import (
    HOST, PORT, UPLOADS_DIR, RESULTS_DIR, SUPPORTED_EXTENSIONS,
    SUPPORTED_IMAGE_EXTENSIONS, SUPPORTED_PDF_EXTENSIONS,
    MAX_TOTAL_STORAGE_BYTES, MAX_TOTAL_STORAGE_GB, MAX_FILE_SIZE_BYTES,
    ENGINES, OUTPUT_MODES,
)
from database import (
    get_db, init_db, get_setting, set_setting, get_total_storage_used, SessionLocal
)
from models import User, Job, JobFile, ProcessingStat, Setting
from auth import (
    authenticate_user, authenticate_admin, create_access_token,
    get_current_user_from_request, get_current_user_with_db,
    generate_random_password, get_password_hash, check_admin_token,
    is_auth_required, get_session_user,
)
from worker import (
    enqueue_job, get_current_status, get_queue_position,
    worker_loop, requeue_pending_jobs, cancel_job,
)
from cleanup import cleanup_loop, get_expiration_info
from ocr import available_engines, is_engine_enabled, get_engine

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger(__name__)

UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
RESULTS_DIR.mkdir(parents=True, exist_ok=True)

BASE_PATH = Path(__file__).parent
STATIC_DIR = str(BASE_PATH / "static")
TEMPLATES_DIR = str(BASE_PATH / "templates")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    await requeue_pending_jobs()
    asyncio.create_task(worker_loop())
    asyncio.create_task(cleanup_loop())
    logger.info("OCR-сервис запущен.")
    yield
    logger.info("OCR-сервис останавливается.")


app = FastAPI(title="OCR Сервис", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

templates = Jinja2Templates(directory=TEMPLATES_DIR)

# Шаблонные фильтры для серверного (локального) времени.
from utils import fmt_dt, fmt_date  # noqa: E402
templates.env.filters["fmt_dt"] = fmt_dt
templates.env.filters["fmt_date"] = fmt_date


# ---------------------------------------------------------------------
#  Middleware: сессионная изоляция в открытом режиме.
#  В открытом режиме каждый посетитель получает уникального гостя по cookie
#  session_id (TTL из settings.session_lifetime_hours). Задачи и история
#  изолированы между сессиями. По истечении TTL cleanup удаляет гостя и файлы.
# ---------------------------------------------------------------------
from starlette.middleware.base import BaseHTTPMiddleware  # noqa: E402
from starlette.requests import Request as StarletteRequest  # noqa: E402


class SessionMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        from auth import is_auth_required, get_or_create_session_user
        # Только в открытом режиме и только для HTTP-маршрутов (не статики).
        if is_auth_required() or request.url.path.startswith("/static"):
            return await call_next(request)
        try:
            user, new_cookie = get_or_create_session_user(request)
            request.state.session_user = user
        except Exception as e:
            logger.warning(f"SessionMiddleware: не удалось создать сессию: {e}")
            request.state.session_user = None
            new_cookie = None
        response = await call_next(request)
        if new_cookie:
            from database import get_setting
            from config import DEFAULT_SESSION_HOURS
            hours = int(get_setting("session_lifetime_hours", str(DEFAULT_SESSION_HOURS)))
            response.set_cookie(
                "session_id", new_cookie,
                httponly=True, samesite="lax",
                max_age=hours * 3600,
            )
        return response


app.add_middleware(SessionMiddleware)

@app.get("/favicon.ico")
async def favicon():
    return FileResponse(str(BASE_PATH / "static" / "favicon.svg"), media_type="image/svg+xml")


def render(request: Request, template_name: str, **context) -> HTMLResponse:
    return templates.TemplateResponse(request, template_name, context=context)


# ====================================================================
#  Вспомогательные функции
# ====================================================================

def _is_authorized_user(request: Request) -> bool:
    return get_current_user_from_request(request) is not None


async def _engines_with_availability() -> list:
    """Список включённых движков + флаг жив ли их сервер."""
    result = []
    for code in [e["code"] for e in ENGINES if is_engine_enabled(e["code"])]:
        name = next((e["name"] for e in ENGINES if e["code"] == code), code)
        try:
            eng = get_engine(code)
            available = await eng.health()
        except Exception:
            available = False
        result.append({"code": code, "name": name, "available": available})
    return result


def _build_user_jobs_payload(db: Session, user_id: int) -> list:
    """Текущий статус активных задач пользователя (для SSE)."""
    active = (
        db.query(Job)
        .filter(Job.user_id == user_id, Job.status.in_(["queued", "processing"]))
        .order_by(Job.created_at.asc())
        .all()
    )
    payload = []
    for j in active:
        payload.append({
            "id": j.id,
            "filename": f"{len(j.files)} файл(ов)" if len(j.files) != 1 else j.files[0].original_filename,
            "engine": j.engine_display,
            "status": j.status,
            "queue_position": get_queue_position(j.id) if j.status == "queued" else None,
        })
    return payload


# ====================================================================
#  Аутентификация — пользователь
# ====================================================================

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, error: str = None):
    # В открытом режиме форма входа не нужна — сразу на главную.
    if not is_auth_required():
        return RedirectResponse(url="/", status_code=303)
    if get_current_user_from_request(request):
        return RedirectResponse(url="/", status_code=303)
    return render(request, "login.html", error=error)


@app.post("/login")
async def login_submit(request: Request, email: str = Form(...), password: str = Form(...)):
    db = SessionLocal()
    try:
        user = authenticate_user(email, password, db)
        if not user:
            return render(request, "login.html", error="Неверный email или пароль")
        token = create_access_token(data={"sub": str(user.id), "type": "user"})
        response = RedirectResponse(url="/", status_code=303)
        response.set_cookie(
            "access_token", token, httponly=True, max_age=60 * 60 * 24 * 7, samesite="lax"
        )
        return response
    finally:
        db.close()


@app.get("/logout")
async def logout():
    # В открытом режиме выхода нет — просто возвращаем на главную.
    if not is_auth_required():
        return RedirectResponse(url="/", status_code=303)
    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie("access_token")
    return response


@app.get("/forgot-password", response_class=HTMLResponse)
async def forgot_password_page(request: Request, error: str = None, success: str = None):
    if not is_auth_required():
        return RedirectResponse(url="/", status_code=303)
    return render(request, "forgot_password.html", error=error, success=success)


@app.post("/forgot-password")
async def forgot_password_submit(request: Request, email: str = Form(...)):
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.email == email).first()
        if user:
            from auth import generate_random_password, get_password_hash
            new_pwd = generate_random_password()
            user.password_hash = get_password_hash(new_pwd)
            db.commit()
            try:
                from email_utils import send_password_reset_email
                send_password_reset_email(user, new_pwd)
            except Exception as e:
                logger.error(f"Не удалось отправить письмо: {e}")
        return render(
            request, "forgot_password.html",
            success="Если аккаунт с таким email существует, новый пароль отправлен на почту.",
        )
    finally:
        db.close()


# ====================================================================
#  Пользовательские страницы
# ====================================================================

@app.get("/", response_class=HTMLResponse)
async def user_dashboard(request: Request):
    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    db = SessionLocal()
    try:
        jobs = (
            db.query(Job)
            .filter(Job.user_id == user.id)
            .order_by(desc(Job.created_at))
            .limit(10)
            .all()
        )
        jobs_with_expiry = [
            {"job": j, "expiry": get_expiration_info(j)} for j in jobs
        ]
        engines = await _engines_with_availability()
        return render(
            request, "user/dashboard.html",
            user=user, jobs=jobs_with_expiry, engines=engines,
            output_modes=OUTPUT_MODES,
            open_mode=not is_auth_required(),
        )
    finally:
        db.close()


@app.get("/history", response_class=HTMLResponse)
async def user_history(
    request: Request,
    q: str = Query(default=""),
    page: int = Query(default=1, ge=1),
):
    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    db = SessionLocal()
    try:
        per_page = 20
        query = db.query(Job).filter(Job.user_id == user.id)
        if q.strip():
            like = f"%{q.strip()}%"
            query = query.join(JobFile, JobFile.job_id == Job.id).filter(
                (JobFile.original_filename.ilike(like)) | (JobFile.text_content.ilike(like))
            ).distinct()
        total = query.count()
        jobs = (
            query.order_by(desc(Job.created_at))
            .offset((page - 1) * per_page)
            .limit(per_page)
            .all()
        )
        pages_count = max(1, (total + per_page - 1) // per_page)
        jobs_with_expiry = [{"job": j, "expiry": get_expiration_info(j)} for j in jobs]
        return render(
            request, "user/history.html",
            user=user, jobs=jobs_with_expiry, q=q,
            page=page, pages_count=pages_count, total=total,
            open_mode=not is_auth_required(),
        )
    finally:
        db.close()


@app.get("/profile", response_class=HTMLResponse)
async def user_profile(request: Request, error: str = None, success: str = None):
    # В открытом режиме профиля/смены пароля нет — гость не управляет аккаунтом.
    if not is_auth_required():
        return RedirectResponse(url="/", status_code=303)
    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    db = SessionLocal()
    try:
        total_jobs = db.query(Job).filter(Job.user_id == user.id).count()
        completed = db.query(Job).filter(
            Job.user_id == user.id, Job.status == "completed"
        ).count()
        return render(
            request, "user/profile.html",
            user=user, total_jobs=total_jobs, completed=completed,
            error=error, success=success,
        )
    finally:
        db.close()


@app.post("/profile")
async def user_profile_save(
    request: Request,
    current_password: str = Form(default=""),
    new_password: str = Form(default=""),
    notifications: str = Form(default="off"),
    sound: str = Form(default="off"),
):
    if not is_auth_required():
        return RedirectResponse(url="/", status_code=303)
    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    db = SessionLocal()
    try:
        u = db.query(User).filter(User.id == user.id).first()
        u.notifications_enabled = notifications == "on"
        u.sound_enabled = sound == "on"

        if new_password.strip():
            from auth import verify_password
            if not current_password or not verify_password(current_password, u.password_hash):
                return render(
                    request, "user/profile.html", user=u,
                    total_jobs=0, completed=0,
                    error="Текущий пароль введён неверно.",
                )
            if len(new_password) < 6:
                return render(
                    request, "user/profile.html", user=u,
                    total_jobs=0, completed=0,
                    error="Новый пароль слишком короткий (минимум 6 символов).",
                )
            u.password_hash = get_password_hash(new_password)
            db.commit()
            return render(
                request, "user/profile.html", user=u,
                total_jobs=0, completed=0, success="Пароль изменён.",
            )
        db.commit()
        return RedirectResponse(url="/profile", status_code=303)
    finally:
        db.close()


# ====================================================================
#  API: загрузка, прогресс, скачивание
# ====================================================================

@app.post("/api/upload")
async def api_upload(
    request: Request,
    files: list[UploadFile] = File(...),
    engine: str = Form(...),
    output_mode: str = Form("combined"),
):
    user = get_session_user(request)
    if not user:
        return JSONResponse({"detail": "Не авторизован"}, status_code=401)

    if not files:
        return JSONResponse({"detail": "Нет файлов"}, status_code=400)

    if not is_engine_enabled(engine):
        return JSONResponse({"detail": f"Движок {engine} отключён"}, status_code=400)
    if output_mode not in ("combined", "per_file"):
        output_mode = "combined"

    db = SessionLocal()
    try:
        job = Job(
            user_id=user.id,
            engine=engine,
            output_mode=output_mode,
            status="queued",
        )
        db.add(job)
        db.flush()  # получаем job.id

        saved = []
        skipped = []
        for idx, up in enumerate(files):
            ext = Path(up.filename or "").suffix.lower()
            if ext not in SUPPORTED_EXTENSIONS:
                skipped.append({"name": up.filename, "reason": f"Неподдерживаемый формат: {ext}"})
                continue
            # Читаем содержимое (есть лимит).
            data = await up.read()
            if len(data) > MAX_FILE_SIZE_BYTES:
                skipped.append({"name": up.filename, "reason": "Превышен лимит размера файла"})
                continue
            stored = f"{uuid.uuid4().hex}{ext}"
            (UPLOADS_DIR / stored).write_bytes(data)
            jf = JobFile(
                job_id=job.id,
                order_index=idx,
                original_filename=up.filename or stored,
                stored_filename=stored,
                file_size=len(data),
                is_pdf=(ext in SUPPORTED_PDF_EXTENSIONS),
                status="queued",
            )
            db.add(jf)
            saved.append(up.filename or stored)

        if not saved:
            db.rollback()
            return JSONResponse({"detail": "Нет валидных файлов для обработки"}, status_code=400)

        db.commit()
        await enqueue_job(job.id)
        return JSONResponse({
            "job_id": job.id,
            "job_ids": [job.id],
            "saved": saved,
            "skipped": skipped,
        })
    finally:
        db.close()


@app.get("/api/events")
async def api_events(request: Request):
    user = get_session_user(request)
    if not user:
        return JSONResponse({"detail": "Не авторизован"}, status_code=401)

    async def stream():
        last_payload = None
        idle_ticks = 0
        while True:
            if await request.is_disconnected():
                break
            status = get_current_status()
            db = SessionLocal()
            try:
                user_jobs = _build_user_jobs_payload(db, user.id)
            finally:
                db.close()

            # Глобальный статус (_current_status) — это ЧУЖАЯ задача, если её
            # job_id не принадлежит текущему пользователю. В этом случае не
            # раскрываем детали (current_file/progress/message), показываем
            # только «в очереди», чтобы пользователь не видел чужую обработку.
            global_job_id = status.get("job_id")
            global_belongs_to_user = False
            if global_job_id:
                db_check = SessionLocal()
                try:
                    global_belongs_to_user = bool(
                        db_check.query(Job).filter(
                            Job.id == global_job_id, Job.user_id == user.id
                        ).first()
                    )
                finally:
                    db_check.close()

            if global_belongs_to_user:
                payload = {**status, "user_jobs": user_jobs}
            else:
                # Чужая задача обрабатывается — отдаём только свои активные задачи,
                # без глобальных деталей. Прогресс не показываем (null), статус «в очереди».
                payload = {
                    "job_id": None,
                    "status": "queued" if user_jobs else "idle",
                    "progress": None,
                    "message": ("В очереди (занято другой задачей)"
                                if user_jobs else "Ожидание задач..."),
                    "current_file": None,
                    "current_page": None,
                    "total_pages": None,
                    "user_jobs": user_jobs,
                }
            # Шлём апдейт при изменении или каждые ~5 сек (keepalive).
            if payload != last_payload or idle_ticks >= 5:
                yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
                last_payload = payload
                idle_ticks = 0
            else:
                idle_ticks += 1
            await asyncio.sleep(1)

    return StreamingResponse(stream(), media_type="text/event-stream")


@app.post("/api/jobs/{job_id}/cancel")
async def api_cancel_job(job_id: int, request: Request):
    user = get_session_user(request)
    if not user:
        return JSONResponse({"detail": "Не авторизован"}, status_code=401)
    db = SessionLocal()
    try:
        job = db.query(Job).filter(Job.id == job_id, Job.user_id == user.id).first()
        if not job:
            return JSONResponse({"detail": "Задача не найдена"}, status_code=404)
    finally:
        db.close()
    ok = cancel_job(job_id)
    if not ok:
        return JSONResponse({"detail": "Задачу нельзя отменить"}, status_code=400)
    return JSONResponse({"ok": True})


@app.post("/api/jobs/{job_id}/delete")
async def api_delete_job(job_id: int, request: Request):
    user = get_session_user(request)
    if not user:
        return JSONResponse({"detail": "Не авторизован"}, status_code=401)
    db = SessionLocal()
    try:
        job = db.query(Job).filter(Job.id == job_id, Job.user_id == user.id).first()
        if not job:
            return JSONResponse({"detail": "Задача не найдена"}, status_code=404)
        # Удаляем файлы с диска.
        from worker import _cleanup_job_files
        _cleanup_job_files(job)
        db.delete(job)
        db.commit()
        return JSONResponse({"ok": True})
    finally:
        db.close()


@app.get("/download/{job_id}/{kind}")
async def download_result(job_id: int, kind: str, request: Request):
    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    db = SessionLocal()
    try:
        job = db.query(Job).filter(Job.id == job_id, Job.user_id == user.id).first()
        if not job or not job.result_dir:
            raise HTTPException(status_code=404, detail="Результат недоступен")
        rdir = Path(job.result_dir)
        if not rdir.exists():
            raise HTTPException(status_code=404, detail="Файлы удалены")

        if kind == "txt":
            target = _first_file(rdir, ".txt")
        elif kind == "docx":
            target = _first_file(rdir, ".docx")
        else:
            raise HTTPException(status_code=400, detail="Неизвестный формат")

        if not target:
            raise HTTPException(status_code=404, detail="Файл не найден")
        return FileResponse(str(target), filename=target.name)
    finally:
        db.close()


@app.get("/download-all/{job_id}")
async def download_all(job_id: int, request: Request):
    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    db = SessionLocal()
    try:
        job = db.query(Job).filter(Job.id == job_id, Job.user_id == user.id).first()
        if not job or not job.result_dir:
            raise HTTPException(status_code=404, detail="Результат недоступен")
        rdir = Path(job.result_dir)
        if not rdir.exists():
            raise HTTPException(status_code=404, detail="Файлы удалены")

        import doc_builder
        zip_bytes = doc_builder.build_zip(rdir)
        headers = {"Content-Disposition": f'attachment; filename="job_{job_id}.zip"'}
        return StreamingResponse(
            BytesIO(zip_bytes), media_type="application/zip", headers=headers
        )
    finally:
        db.close()


def _first_file(directory: Path, suffix: str) -> Path | None:
    files = sorted(directory.glob(f"*{suffix}"))
    return files[0] if files else None


@app.post("/api/user/settings")
async def api_user_settings(
    request: Request,
    notifications: str = Form("off"),
    sound: str = Form("off"),
):
    user = get_session_user(request)
    if not user:
        return JSONResponse({"detail": "Не авторизован"}, status_code=401)
    db = SessionLocal()
    try:
        u = db.query(User).filter(User.id == user.id).first()
        if u:
            u.notifications_enabled = notifications == "on"
            u.sound_enabled = sound == "on"
            db.commit()
        return JSONResponse({"ok": True})
    finally:
        db.close()


# ====================================================================
#  Аутентификация — администратор
# ====================================================================

@app.get("/admin/login", response_class=HTMLResponse)
async def admin_login_page(request: Request, error: str = None):
    if check_admin_token(request):
        return RedirectResponse(url="/admin", status_code=303)
    return render(request, "admin_login.html", error=error)


@app.post("/admin/login")
async def admin_login_submit(
    request: Request, username: str = Form(...), password: str = Form(...)
):
    if not authenticate_admin(username, password):
        return render(request, "admin_login.html", error="Неверный логин или пароль")
    token = create_access_token(data={"sub": username, "type": "admin"})
    response = RedirectResponse(url="/admin", status_code=303)
    response.set_cookie(
        "admin_token", token, httponly=True, max_age=60 * 60 * 24 * 7, samesite="lax"
    )
    return response


@app.get("/admin/logout")
async def admin_logout():
    response = RedirectResponse(url="/admin/login", status_code=303)
    response.delete_cookie("admin_token")
    return response


# ====================================================================
#  Админ-панель
# ====================================================================

@app.get("/admin", response_class=HTMLResponse)
async def admin_dashboard(request: Request):
    if not check_admin_token(request):
        return RedirectResponse(url="/admin/login", status_code=303)
    db = SessionLocal()
    try:
        total_users = db.query(User).count()
        total_jobs = db.query(Job).count()
        completed_jobs = db.query(Job).filter(Job.status == "completed").count()
        total_pages = db.query(func.coalesce(func.sum(ProcessingStat.pages), 0)).scalar() or 0
        recent_jobs = (
            db.query(Job).order_by(desc(Job.created_at)).limit(8).all()
        )
        recent_users = (
            db.query(User).order_by(desc(User.created_at)).limit(5).all()
        )
        return render(
            request, "admin/dashboard.html",
            total_users=total_users, total_jobs=total_jobs,
            completed_jobs=completed_jobs, total_pages=total_pages,
            recent_jobs=recent_jobs, recent_users=recent_users,
            storage_used_gb=get_total_storage_used() / (1024 ** 3),
            storage_limit_gb=get_setting("storage_limit_gb", str(MAX_TOTAL_STORAGE_GB)),
        )
    finally:
        db.close()


@app.get("/admin/accounts", response_class=HTMLResponse)
async def admin_accounts(request: Request):
    if not check_admin_token(request):
        return RedirectResponse(url="/admin/login", status_code=303)
    db = SessionLocal()
    try:
        users = db.query(User).order_by(desc(User.created_at)).all()
        # Количество задач на пользователя.
        stats = {}
        for u in users:
            stats[u.id] = db.query(Job).filter(Job.user_id == u.id).count()
        return render(request, "admin/accounts.html", users=users, stats=stats)
    finally:
        db.close()


@app.get("/admin/accounts/create", response_class=HTMLResponse)
async def admin_account_create(request: Request, error: str = None):
    if not check_admin_token(request):
        return RedirectResponse(url="/admin/login", status_code=303)
    return render(request, "admin/account_form.html", mode="create", user=None, error=error)


@app.post("/admin/accounts/create")
async def admin_account_create_submit(
    request: Request,
    email: str = Form(...),
    first_name: str = Form(...),
    last_name: str = Form(...),
    password: str = Form(default=""),
):
    if not check_admin_token(request):
        return RedirectResponse(url="/admin/login", status_code=303)
    db = SessionLocal()
    try:
        existing = db.query(User).filter(User.email == email).first()
        if existing:
            return render(
                request, "admin/account_form.html",
                mode="create", user=None,
                error="Пользователь с таким email уже существует",
            )
        pwd = password.strip() or generate_random_password()
        generated = not bool(password.strip())
        u = User(
            email=email,
            first_name=first_name,
            last_name=last_name,
            password_hash=get_password_hash(pwd),
        )
        db.add(u)
        db.commit()
        try:
            from email_utils import send_new_account_email
            send_new_account_email(u, pwd)
        except Exception as e:
            logger.error(f"Не удалось отправить письмо: {e}")
        return render(
            request, "admin/account_created.html",
            user=u, password=pwd, generated=generated,
        )
    finally:
        db.close()


@app.get("/admin/accounts/{user_id}/edit", response_class=HTMLResponse)
async def admin_account_edit(request: Request, user_id: int, error: str = None):
    if not check_admin_token(request):
        return RedirectResponse(url="/admin/login", status_code=303)
    db = SessionLocal()
    try:
        u = db.query(User).filter(User.id == user_id).first()
        if not u:
            return RedirectResponse(url="/admin/accounts", status_code=303)
        return render(request, "admin/account_form.html", mode="edit", user=u, error=error)
    finally:
        db.close()


@app.post("/admin/accounts/{user_id}/edit")
async def admin_account_edit_submit(
    request: Request,
    user_id: int,
    email: str = Form(...),
    first_name: str = Form(...),
    last_name: str = Form(...),
    password: str = Form(default=""),
):
    if not check_admin_token(request):
        return RedirectResponse(url="/admin/login", status_code=303)
    db = SessionLocal()
    try:
        u = db.query(User).filter(User.id == user_id).first()
        if not u:
            return RedirectResponse(url="/admin/accounts", status_code=303)
        clash = db.query(User).filter(User.email == email, User.id != user_id).first()
        if clash:
            return render(
                request, "admin/account_form.html",
                mode="edit", user=u,
                error="Этот email уже занят другим пользователем",
            )
        u.email = email
        u.first_name = first_name
        u.last_name = last_name
        if password.strip():
            u.password_hash = get_password_hash(password.strip())
        db.commit()
        return RedirectResponse(url="/admin/accounts", status_code=303)
    finally:
        db.close()


@app.post("/admin/accounts/{user_id}/delete")
async def admin_account_delete(request: Request, user_id: int):
    if not check_admin_token(request):
        return RedirectResponse(url="/admin/login", status_code=303)
    db = SessionLocal()
    try:
        u = db.query(User).filter(User.id == user_id).first()
        if u:
            db.delete(u)
            db.commit()
        return RedirectResponse(url="/admin/accounts", status_code=303)
    finally:
        db.close()


@app.get("/admin/statistics", response_class=HTMLResponse)
async def admin_statistics(request: Request):
    if not check_admin_token(request):
        return RedirectResponse(url="/admin/login", status_code=303)
    db = SessionLocal()
    try:
        # По движкам.
        by_engine_rows = (
            db.query(ProcessingStat.engine, func.count("*"),
                     func.coalesce(func.sum(ProcessingStat.pages), 0),
                     func.coalesce(func.avg(ProcessingStat.processing_time), 0))
            .group_by(ProcessingStat.engine)
            .all()
        )
        by_engine = [
            {"engine": r[0], "jobs": r[1], "pages": int(r[2] or 0),
             "avg_time": round(float(r[3] or 0), 1)}
            for r in by_engine_rows
        ]
        # Задачи по статусам.
        by_status_rows = (
            db.query(Job.status, func.count("*"))
            .group_by(Job.status).all()
        )
        by_status = [{"status": r[0], "count": r[1]} for r in by_status_rows]
        # Задачи за последние 14 дней.
        cutoff = datetime.now(timezone.utc).replace(tzinfo=None).timestamp() - 14 * 86400
        from datetime import datetime as _dt
        cutoff_dt = _dt.fromtimestamp(cutoff)
        recent = (
            db.query(Job)
            .filter(Job.created_at >= cutoff_dt)
            .order_by(Job.created_at.asc())
            .all()
        )
        per_day: dict[str, int] = {}
        for j in recent:
            key = j.created_at.strftime("%Y-%m-%d")
            per_day[key] = per_day.get(key, 0) + 1

        return render(
            request, "admin/statistics.html",
            by_engine=by_engine, by_status=by_status, per_day=per_day,
            total_jobs=db.query(Job).count(),
            total_pages=db.query(func.coalesce(func.sum(ProcessingStat.pages), 0)).scalar() or 0,
        )
    finally:
        db.close()


@app.get("/admin/settings", response_class=HTMLResponse)
async def admin_settings(request: Request):
    if not check_admin_token(request):
        return RedirectResponse(url="/admin/login", status_code=303)
    success = None
    if request.query_params.get("saved") == "1":
        success = "Настройки сохранены."
    settings = {
        "engine_url_mineru": get_setting("engine_url_mineru"),
        "engine_url_unlimitedocr": get_setting("engine_url_unlimitedocr"),
        "engine_url_llm": get_setting("engine_url_llm"),
        "engine_llm_model": get_setting("engine_llm_model"),
        "engine_enabled_mineru": get_setting("engine_enabled_mineru", "true"),
        "engine_enabled_unlimitedocr": get_setting("engine_enabled_unlimitedocr", "true"),
        "engine_enabled_llm": get_setting("engine_enabled_llm", "true"),
        "engine_gpu_mineru": get_setting("engine_gpu_mineru", "auto"),
        "engine_gpu_unlimitedocr": get_setting("engine_gpu_unlimitedocr", "auto"),
        "retention_days": get_setting("retention_days", "60"),
        "storage_limit_gb": get_setting("storage_limit_gb", str(MAX_TOTAL_STORAGE_GB)),
        "auth_required": get_setting("auth_required", "true"),
    }
    return render(
        request, "admin/settings.html",
        settings=settings, success=success, engines=ENGINES,
        storage_used_gb=get_total_storage_used() / (1024 ** 3),
    )


@app.post("/admin/settings")
async def admin_settings_save(request: Request):
    if not check_admin_token(request):
        return RedirectResponse(url="/admin/login", status_code=303)
    form = await request.form()
    keys = [
        "engine_url_mineru", "engine_url_unlimitedocr", "engine_url_llm",
        "engine_llm_model",
        "engine_enabled_mineru", "engine_enabled_unlimitedocr",
        "engine_enabled_llm",
        "engine_gpu_mineru", "engine_gpu_unlimitedocr",
        "retention_days", "storage_limit_gb",
        "auth_required", "session_lifetime_hours",
    ]
    for k in keys:
        if k in form:
            set_setting(k, str(form[k]))
    return RedirectResponse(url="/admin/settings?saved=1", status_code=303)


@app.post("/admin/reset-stats")
async def admin_reset_stats(request: Request):
    if not check_admin_token(request):
        return RedirectResponse(url="/admin/login", status_code=303)
    db = SessionLocal()
    try:
        db.query(ProcessingStat).delete()
        db.commit()
        return RedirectResponse(url="/admin/statistics", status_code=303)
    finally:
        db.close()


# ====================================================================
#  Старт
# ====================================================================

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host=HOST, port=PORT, reload=False)
