"""
Сервер OCR-движка MinerU 2.5-Pro-1.2B (OpenDataLab).

АРХИТЕКТУРА (важно для производительности):
  При старте мы ОДИН РАЗ запускаем `mineru-api` (FastAPI-сервер из пакета mineru)
  с флагом --enable-vlm-preload. VLM-модель загружается в GPU один раз и держится
  в памяти всё время жизни процесса. Каждый запрос /ocr проксируется в синхронный
  POST /file_parse этого сервера. Так 10 страниц PDF обрабатываются за ~30-60 сек
  вместо 20 минут (когда модель грузилась заново на каждую страницу).

Контракт к веб-приложению: POST /ocr (multipart image) -> {"text", "markdown"}.
"""

import argparse
import logging
import os
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from server_common import build_app, apply_gpu_pin  # noqa: E402

logger = logging.getLogger("mineru_server")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

# Backend: vlm-engine (GPU + VLM, лучший разбор формул) | hybrid-engine | pipeline.
BACKEND = os.environ.get("MINERU_BACKEND", "vlm-engine")
# Устройство: cuda на GPU, cpu — медленно.
DEVICE = os.environ.get("MINERU_DEVICE", "cuda")
# Внутренний порт mineru-api (локальный, не виден снаружи).
MINERU_API_PORT = int(os.environ.get("MINERU_API_PORT", "8501"))

# Состояние: PID запущенного mineru-api.
_state: dict = {"api_proc": None}


def _find_mineru_api_entry():
    """Найти способ запустить mineru-api. Перебираем варианты.

    mineru-api — это сервер из mineru/cli/fast_api.py. На Windows pip иногда не
    создаёт Scripts/mineru-api.exe, поэтому проверяем и подмодуль, и python -c.
    """
    venv_bin = Path(sys.executable).parent
    candidates = [
        [str(venv_bin / ("mineru-api.exe" if os.name == "nt" else "mineru-api"))],
        [sys.executable, "-m", "mineru.cli.fast_api"],
        [sys.executable, "-c", "from mineru.cli.fast_api import main; main()"],
        [sys.executable, "-c", "from mineru.cli.fast_api import app; "
                              "import uvicorn; uvicorn.run(app, host='0.0.0.0', port=%d)" % MINERU_API_PORT],
    ]
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"

    for cmd in candidates:
        exe = cmd[0]
        if exe != sys.executable and not Path(exe).exists():
            continue
        try:
            r = subprocess.run(
                cmd + ["--help"], capture_output=True, text=True,
                env=env, encoding="utf-8", timeout=30,
            )
        except (subprocess.SubprocessError, FileNotFoundError, OSError):
            continue
        out = (r.stdout or "") + (r.stderr or "")
        # Признаки серверного CLI: ключи --host/--port или упоминание uvicorn.
        if ("--host" in out or "--port" in out or "uvicorn" in out.lower()
                or "enable-vlm-preload" in out):
            logger.info("mineru-api entry: %s", " ".join(cmd[:3]))
            return cmd
        # Для python -c варианта: модуль импортируется — значит сервер есть.
        if "-c" in cmd and ("argparse" in out or "TypeError" in out
                            or "uvicorn" in out.lower()):
            logger.info("mineru-api entry (python -c): %s", " ".join(cmd[:3]))
            return cmd
    return None


def _start_mineru_api():
    """Запустить mineru-api как дочерний процесс (один раз при старте)."""
    entry = _find_mineru_api_entry()
    if not entry:
        raise RuntimeError(
            "Не удалось найти mineru-api. Проверьте установку mineru[pipeline] "
            "в venv_mineru: venv_mineru\\Scripts\\python -m pip show mineru"
        )

    # ВАЖНО: mineru-api принимает ТОЛЬКО эти флаги: --host, --port, --reload,
    # --allow-public-http-client, --enable-vlm-preload. Другие (--backend,
    # --device) — НЕ существуют и ломают запуск (процесс мгновенно выходит с
    # кодом 0). Backend/device/preload передаём через переменные окружения,
    # которые mineru-api читает при старте.
    cmd = entry + [
        "--host", "127.0.0.1",
        "--port", str(MINERU_API_PORT),
        "--enable-vlm-preload", "true",   # критично: модель грузится 1 раз
    ]
    logger.info("Запускаем mineru-api: %s", " ".join(cmd))

    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    # backend и device — через env vars (mineru читает их при инициализации).
    env["MINERU_DEVICE_MODE"] = DEVICE
    env["MINERU_API_BACKEND"] = BACKEND          # какой backend поднимать
    env["MINERU_API_DEVICE"] = DEVICE             # cuda/cpu
    env["MINERU_API_ENABLE_VLM_PRELOAD"] = "true" # предзагрузка VLM (дубль с CLI)
    # Для vlm-engine формулы/таблицы включены по умолчанию.

    # Лог mineru-api — отдельный файл, чтобы не смешивать.
    project_root = Path(__file__).resolve().parent.parent
    logs_dir = project_root / "logs"
    logs_dir.mkdir(exist_ok=True)
    api_log = open(logs_dir / "mineru_api.log", "a", encoding="utf-8")
    api_log.write(f"\n===== запуск mineru-api {time.strftime('%Y-%m-%d %H:%M:%S')} =====\n")
    gpu_env = env.get("CUDA_VISIBLE_DEVICES")
    api_log.write(f"env: MINERU_API_BACKEND={BACKEND} MINERU_API_DEVICE={DEVICE} "
                  f"MINERU_API_ENABLE_VLM_PRELOAD=true"
                  + (f" CUDA_VISIBLE_DEVICES={gpu_env}" if gpu_env else "") + "\n")
    api_log.flush()

    kwargs = {}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    proc = subprocess.Popen(
        cmd, stdout=api_log, stderr=subprocess.STDOUT,
        cwd=str(Path(__file__).resolve().parent.parent),
        env=env, **kwargs,
    )
    _state["api_proc"] = proc
    return proc


def _wait_for_api(timeout=180):
    """Дождаться, пока mineru-api начнёт отвечать (модель грузится ~1-2 мин)."""
    import httpx
    deadline = time.time() + timeout
    last_err = None
    while time.time() < deadline:
        # Проверяем, жив ли процесс.
        if _state["api_proc"] and _state["api_proc"].poll() is not None:
            # Процесс мгновенно вышел — покажем последние строки его лога,
            # чтобы пользователь сразу видел причину (не найдя файла лога).
            try:
                log_path = Path(__file__).resolve().parent.parent / "logs" / "mineru_api.log"
                tail = log_path.read_text(encoding="utf-8", errors="replace")[-1500:]
            except Exception:
                tail = "(лог недоступен)"
            raise RuntimeError(
                f"mineru-api упал при старте (код {_state['api_proc'].returncode}).\n"
                f"--- последние строки logs/mineru_api.log ---\n{tail}"
            )
        try:
            r = httpx.get(f"http://127.0.0.1:{MINERU_API_PORT}/health", timeout=3)
            if r.status_code == 200:
                logger.info("mineru-api готов (модель загружена).")
                return True
            # 503 — ещё грузится.
        except Exception as e:
            last_err = e
        time.sleep(3)
    raise RuntimeError(
        f"mineru-api не ответил за {timeout}с. Последняя ошибка: {last_err}. "
        f"Смотрите logs/mineru_api.log"
    )


def _stop_mineru_api():
    proc = _state.get("api_proc")
    if proc and proc.poll() is None:
        try:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        except Exception:
            pass
    _state["api_proc"] = None


def _normalize_markdown_to_text(md: str) -> str:
    """Превратить Markdown (с LaTeX/HTML) в читаемый plain text для .txt-вывода."""
    if not md:
        return ""
    text = md
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"\$\$+\s*", " ", text)
    text = re.sub(r"(?<!\$)\$(?!\$)", " ", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"^[#>\-\*\+]+\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"^\s*\|?[\s:|-]+\|?\s*$", "", text, flags=re.MULTILINE)
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _parse_via_api(image_bytes: bytes, filename: str):
    """Отправить изображение в POST /file_parse mineru-api, вернуть markdown.

    Используем СИНХРОННЫЙ /file_parse (а не асинхронный /tasks) — проще и надёжнее.
    mineru-api сам обрабатывает файл и возвращает результат.
    """
    import httpx
    import io
    import zipfile

    url = f"http://127.0.0.1:{MINERU_API_PORT}/file_parse"
    # ВАЖНО: поле формы "files" (множественное число) — FastAPI-эндпоинт
    # /file_parse принимает List[UploadFile] под именем files. Единственное
    # "file" даёт 422 "Field required".
    files = {"files": (filename, image_bytes, "image/png")}
    data = {
        "backend": BACKEND,
        "parse_method": "auto",
        # НЕ передаём formula_enable/table_enable — для vlm-engine они включены
        # по умолчанию, а явная передача вызывала конфликт "multiple values".
        "return_md": "true",
        "response_format_zip": "true",
    }
    # OCR одной страницы может занять 1-3 мин на сложных формулах.
    with httpx.Client(timeout=httpx.Timeout(600.0, connect=30.0)) as client:
        r = client.post(url, files=files, data=data)
    if r.status_code != 200:
        raise RuntimeError(
            f"mineru-api /file_parse вернул HTTP {r.status_code}: {r.text[:500]}"
        )

    # Ответ — ZIP с .md внутри (response_format_zip=true).
    markdown = ""
    try:
        zf = zipfile.ZipFile(io.BytesIO(r.content))
    except zipfile.BadZipFile:
        # Если zip не получился — возможно, вернулся JSON. Попробуем извлечь md.
        try:
            j = r.json()
            markdown = j.get("md_content") or j.get("markdown") or ""
        except Exception:
            markdown = r.text
    else:
        for name in zf.namelist():
            if name.endswith(".md"):
                markdown = zf.read(name).decode("utf-8", errors="replace")
                break
    return markdown


def run_ocr(img):
    """img — PIL.Image. Возвращает (text, markdown)."""
    import io
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    markdown = _parse_via_api(buf.getvalue(), "input.png")
    return _normalize_markdown_to_text(markdown), (markdown or None)


def _check_gpu():
    """Предупредить, если GPU недоступен; показать видимые GPU (проверка пиннинга)."""
    try:
        r = subprocess.run(
            [sys.executable, "-c",
             "import torch\n"
             "print('cuda' if torch.cuda.is_available() else 'cpu')\n"
             "if torch.cuda.is_available():\n"
             "    print('gpu_count:', torch.cuda.device_count())\n"
             "    for i in range(torch.cuda.device_count()):\n"
             "        print(f'  cuda:{i} =', torch.cuda.get_device_name(i))\n"],
            capture_output=True, text=True,
        )
        out = (r.stdout or "").strip()
        logger.info("Проверка GPU:\n%s", out)
        if out.startswith("cpu") and DEVICE == "cuda":
            logger.warning(
                "ВНИМАНИЕ: torch.cuda.is_available()=False, но MINERU_DEVICE=cuda. "
                "MinerU будет падать. Установите torch с CUDA: "
                "https://pytorch.org/get-started/locally/"
            )
    except Exception as e:
        logger.warning("Не удалось проверить GPU: %s", e)


def main():
    parser = argparse.ArgumentParser(description="MinerU 2.5-Pro-1.2B сервер")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8501)
    parser.add_argument("--gpu", default=None,
                        help="закрепить GPU по номеру CUDA (0, 1, ...); "
                             "по умолчанию не менять (все видимые GPU)")
    args = parser.parse_args()

    # Пиннинг GPU (при запуске вручную): выставить до старта mineru-api,
    # env наследуется дочерним процессом. При запуске через run_all env
    # уже выставлен им самим (настройка GPU из админки).
    apply_gpu_pin(args.gpu)

    # Если внешний порт совпадает с внутренним — конфликт. Сдвигаем внутренний.
    global MINERU_API_PORT
    if args.port == MINERU_API_PORT:
        MINERU_API_PORT = args.port + 100  # напр. 8601
        logger.info("Внутренний порт mineru-api сдвинут на %d", MINERU_API_PORT)

    _check_gpu()

    # Запускаем mineru-api один раз и ждём загрузки модели.
    _start_mineru_api()
    try:
        _wait_for_api()
    except Exception:
        _stop_mineru_api()
        raise

    import uvicorn
    app = build_app("MinerU 2.5-Pro-1.2B", run_ocr, on_startup=None)
    print(f"MinerU server (proxy): backend={BACKEND}, device={DEVICE}, "
          f"mineru-api=http://127.0.0.1:{MINERU_API_PORT}, "
          f"listen={args.host}:{args.port}")
    try:
        uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    finally:
        _stop_mineru_api()


if __name__ == "__main__":
    main()
