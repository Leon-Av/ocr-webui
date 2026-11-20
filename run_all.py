#!/usr/bin/env python3
"""
Запуск всех сервисов OCR-сервиса одним скриптом.

Каждый OCR-движок живёт в своём виртуальном окружении (см. ocr_servers/README.md),
потому что их зависимости конфликтуют между собой. Этот скрипт:

  • поднимает сервисы, чьи venv существуют (web всегда; mineru —
    после запуска install_mineru.bat);
  • пишет stdout/stderr каждого в logs/<name>.log;
  • сохраняет PID в logs/<name>.pid;
  • по Ctrl+C корректно останавливает всё;
  • поддерживает `--stop` (остановить всё) и `--status` (кто жив).

LLM (LM Studio) этим скриптом НЕ запускается — он уже должен работать
локально на http://localhost:1235/v1 (его поднимают в самом LM Studio).

Использование:
    python run_all.py                # запустить: web + готовые OCR-серверы
    python run_all.py --all          # все поддерживаемые текущей ОС
    python run_all.py --only web,mineru   # запустить только указанные
    python run_all.py --stop         # остановить всё (по PID-файлам)
    python run_all.py --status       # показать, что запущено
"""

import argparse
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LOGS_DIR = ROOT / "logs"
LOGS_DIR.mkdir(exist_ok=True)

IS_WIN = os.name == "nt"

# Принудительный UTF-8 для собственного вывода run_all, чтобы русские символы
# не падали с UnicodeEncodeError на системах с локалью cp1251/cp1252.
for _stream in (sys.stdout, sys.stderr):
    reconfigure = getattr(_stream, "reconfigure", None)
    if reconfigure:
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (TypeError, ValueError):
            pass

# ============================================================
#  КОНФИГУРАЦИЯ: какие сервисы поднимать.
#
#  Поля:
#   name      — короткое имя (для логов, PID-файлов, --only).
#   venv      — имя папки виртуального окружения в корне проекта.
#               None = использовать тот же python, которым запущен run_all.
#   module    — запускаемый .py (от корня проекта).
#   args      — аргументы командной строки.
#   url       — человекочитаемый адрес (для печати).
#   auto      — True: поднимать автоматически, ЕСЛИ venv существует.
#               False: поднимать только через --all или --only.
#   linux_only — сервис работает только на Linux/WSL2 (vLLM).
#                На Windows будет пропущен с пояснением.
# ============================================================
SERVICES = [
    {
        "name": "web",                       # главное веб-приложение (порт 2002)
        "venv": "venv",                      # корневой venv (создаётся install_all.bat)
        "module": "main.py",
        "args": [],
        "url": "http://localhost:2002",
        "port": 2002,
        "auto": True,
        "linux_only": False,
        # Если корневого venv нет, запускаемся текущим python (системным или
        # тем, что активирован в run_all.bat). Для OCR-серверов этого флага НЕТ:
        # у них жёсткие требования к версии Python, без venv не запустятся.
        "fallback_system": True,
    },
    {
        "name": "mineru",                    # MinerU 2.5-Pro-1.2B (порт 8501)
        "venv": "venv_mineru",
        "module": "ocr_servers/run_mineru_server.py",
        "args": ["--host", "127.0.0.1", "--port", "8501"],
        "url": "http://localhost:8501",
        "port": 8501,
        "auto": True,                        # поднимется, если venv_mineru существует
        "linux_only": False,
    },
    {
        "name": "unlimitedocr",              # Unlimited-OCR Baidu (порт 8503)
        "venv": "venv_unlimitedocr",
        "module": "ocr_servers/run_unlimitedocr_server.py",
        "args": ["--host", "127.0.0.1", "--port", "8503"],
        "url": "http://localhost:8503",
        "port": 8503,
        "auto": True,                        # поднимется, если venv_unlimitedocr существует
        "linux_only": False,
    },
    # LLM (LM Studio) намеренно отсутствует — он запускается пользователем
    # в самом LM Studio и слушает http://localhost:1235/v1.
]


# ---------------------------------------------------------------------
#  Вспомогательные функции
# ---------------------------------------------------------------------

def venv_python(venv):
    """Вернуть путь к python нужного venv или None.

    Проверяет не только существование python.exe, но и его запуск: venv,
    перенесённый с другой машины, содержит битые абсолютные пути.
    """
    if venv is None:
        return sys.executable
    venv_path = ROOT / venv
    if IS_WIN:
        py = venv_path / "Scripts" / "python.exe"
    else:
        py = venv_path / "bin" / "python"
    if not py.exists():
        return None
    # Проверяем запускаемость: venv, перенесённый с другой машины, битый
    # (его python.exe ссылается на несуществующий базовый интерпретатор).
    # Кэшируем результат, чтобы не запускать проверку повторно.
    cache_key = f"_venv_ok:{venv}"
    if cache_key in _venv_cache:
        return str(py) if _venv_cache[cache_key] else None
    try:
        r = subprocess.run([str(py), "--version"], capture_output=True)
        ok = r.returncode == 0
    except OSError:
        ok = False
    _venv_cache[cache_key] = ok
    if not ok:
        print(f"  [ВНИМАНИЕ] venv '{venv}' существует, но его python не запускается.")
        print(f"             Вероятно, venv перенесён с другой машины. Пересоздайте его:")
        print(f"             install_all.bat  (или install_{venv.replace('venv_','')}.bat)")
    return str(py) if ok else None


# Кэш результатов проверки venv (venv_name -> bool).
_venv_cache: dict = {}


def pid_file(name) -> Path:
    return LOGS_DIR / f"{name}.pid"


def read_pid(name):
    f = pid_file(name)
    if not f.exists():
        return None
    try:
        return int(f.read_text().strip())
    except (ValueError, OSError):
        return None


def is_alive(pid):
    if not pid:
        return False
    try:
        if IS_WIN:
            # Вывод может быть в OEM-кодировке — берём байты и декодируем через errors.
            r = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"],
                capture_output=True,
            )
            out = r.stdout.decode("utf-8", errors="ignore")
            # Если процесс есть, в выводе появится строка, начинающаяся с кавычки
            # (имя, pid, ...). Если нет — "INFO: No tasks are running ..."
            return pid and ('"' in out) and ("No tasks" not in out)
        else:
            os.kill(pid, 0)
            return True
    except (OSError, subprocess.SubprocessError):
        return False


def stop_pid(pid):
    if not pid:
        return False
    try:
        if IS_WIN:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True,
            )
        else:
            # Сначала SIGTERM, потом SIGKILL.
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                return False
            time.sleep(1)
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        return True
    except Exception:
        return False


def free_port(port):
    """Принудительно освободить порт: убить процессы, которые его держат.

    Чинит «Errno 10048 / адрес уже используется», когда прошлый запуск
    закрыли крестиком (процесс остался висеть и держит порт). После убийства
    ждём до 5 секунд, пока ОС реально освободит сокет (TIME_WAIT).
    """
    if not port:
        return []
    killed = []
    if IS_WIN:
        # Ищем PID процесса, который держит порт в ЛЮБОМ состоянии
        # (LISTENING/ESTABLISHED/TIME_WAIT), через netstat.
        r = subprocess.run(
            ["netstat", "-ano", "-p", "TCP"], capture_output=True,
        )
        out = r.stdout.decode("utf-8", errors="ignore")
        seen_pids = set()
        for line in out.splitlines():
            parts = line.split()
            # Формат: Proto LocalAddress ForeignAddress State PID
            # Для ESTABLISHED State есть; для LISTENING тоже.
            if len(parts) >= 4:
                local = parts[1]
                if local.endswith(f":{port}"):
                    pid = parts[-1]
                    if pid.isdigit() and pid != "0" and pid not in seen_pids:
                        seen_pids.add(pid)
                        # /T — убить и дерево потомков (worker'ы uvicorn).
                        subprocess.run(
                            ["taskkill", "/F", "/T", "/PID", pid],
                            capture_output=True,
                        )
                        killed.append(pid)
    else:
        # Linux/macOS: fuser.
        r = subprocess.run(["fuser", "-k", f"{port}/tcp"], capture_output=True)

    if killed:
        # Подождать, пока ОС реально освободит сокет (TIME_WAIT/ CLOSE_WAIT).
        for _ in range(10):
            time.sleep(0.5)
            if not _port_in_use(port):
                break
    return killed


def _port_in_use(port) -> bool:
    """Проверить, занят ли порт TCP (в любом состоянии)."""
    try:
        if IS_WIN:
            r = subprocess.run(
                ["netstat", "-ano", "-p", "TCP"], capture_output=True,
            )
            out = r.stdout.decode("utf-8", errors="ignore")
            for line in out.splitlines():
                parts = line.split()
                if len(parts) >= 4 and parts[1].endswith(f":{port}"):
                    return True
            return False
        else:
            import socket
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(0.3)
            try:
                s.bind(("127.0.0.1", port))
                s.close()
                return False
            except OSError:
                return True
    except Exception:
        return False


# ---------------------------------------------------------------------
#  Команды
# ---------------------------------------------------------------------

def engine_gpu_from_db(name: str) -> str:
    """Прочитать настройку GPU движка из БД (таблица settings).

    Возвращает 'auto' (не закреплять) или номер CUDA-устройства ('0','1',...).
    Читаем напрямую через sqlite3 (без sqlalchemy): run_all выполняется
    системным python, где зависимостей веб-приложения может не быть.
    Значение редактируется в админке (Настройки → OCR-движки → GPU).
    """
    db_path = ROOT / "ocr_service.db"
    if not db_path.exists():
        return "auto"
    try:
        import sqlite3
        con = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True, timeout=2)
        try:
            row = con.execute(
                "SELECT value FROM settings WHERE key = ?", (f"engine_gpu_{name}",)
            ).fetchone()
        finally:
            con.close()
        val = str(row[0]).strip() if row else ""
        return val if val else "auto"
    except Exception:
        return "auto"


def _supports(svc):
    """Работает ли сервис в текущей ОС (linux_only отсекается на Windows)."""
    if svc.get("linux_only") and IS_WIN:
        return False
    return True


def select_services(args):
    """Отфильтровать SERVICES согласно аргументам CLI."""
    if args.only:
        wanted = {s.strip() for s in args.only.split(",")}
        return [s for s in SERVICES if s["name"] in wanted]
    if args.all:
        # --all: всё, что поддерживается текущей ОС.
        return [s for s in SERVICES if _supports(s)]
    # По умолчанию: auto-сервисы, поддерживаемые текущей ОС.
    return [s for s in SERVICES if s.get("auto") and _supports(s)]


def cmd_start(args):
    services = select_services(args)
    print("Запуск сервисов...\n")
    procs = {}
    for svc in services:
        py = venv_python(svc["venv"])
        if py is None:
            # Для веб-приложения — фолбэк на текущий python (например, системный
            # или активированный через run_all.bat), чтобы сервис всегда поднимался.
            if svc.get("fallback_system"):
                py = sys.executable
                print(f"  [ВНИМАНИЕ] {svc['name']}: venv '{svc['venv']}' не найден — "
                      f"используем {py}. Для чистой установки запустите install_all.bat.")
            else:
                installer = {
                    "mineru": "install_mineru.bat",
                    "unlimitedocr": "install_unlimitedocr.bat",
                    "web": "install_all.bat",
                }.get(svc["name"], "install_all.bat")
                venv_hint = f" (или создайте venv '{svc['venv']}' сами)"
                print(f"  [ПРОПУСК] {svc['name']}: venv '{svc['venv']}' не найден. "
                      f"Установите: {installer}{venv_hint if svc['venv'] else ''}")
                continue
        cmd = [py, str(ROOT / svc["module"])] + svc["args"]
        logf = open(LOGS_DIR / f"{svc['name']}.log", "a", encoding="utf-8")
        logf.write(f"\n===== запуск {time.strftime('%Y-%m-%d %H:%M:%S')} =====\n")
        logf.flush()
        # Освобождаем порт: убиваем висящие процессы с прошлого запуска,
        # закрытого крестиком (чинит "Errno 10048 / адрес уже используется").
        port = svc.get("port")
        if port:
            stuck = free_port(port)
            if stuck:
                logf.write(f"Освобождён порт {port}: убиты PID {', '.join(stuck)}\n")
                logf.flush()
        # Новая группа процессов — чтобы Ctrl+C в родителе не убил детей напрямую,
        # мы гасим их сами по PID.
        kwargs = {}
        if IS_WIN:
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        # Окружение для дочернего процесса: принудительный UTF-8.
        # Это чинит UnicodeEncodeError ('charmap' cp1252/cp1251) при печати
        # кириллицы/китайских символов в run_*_server.py на системах с
        # не-UTF локалью (Windows-1252 и т.п.).
        child_env = dict(os.environ)
        child_env["PYTHONUTF8"] = "1"
        child_env["PYTHONIOENCODING"] = "utf-8"
        # GPU-пиннинг движка: настройка из админки (Настройки → OCR-движки →
        # GPU). CUDA-устройства перенумеровываются в PCI-порядок, чтобы
        # номера GPU совпадали с порядком nvidia-smi.
        gpu_note = ""
        if svc["name"] in ("mineru", "unlimitedocr"):
            gpu = engine_gpu_from_db(svc["name"])
            if gpu not in ("auto", "none", "None"):
                child_env["CUDA_VISIBLE_DEVICES"] = gpu
                child_env.setdefault("CUDA_DEVICE_ORDER", "PCI_BUS_ID")
                gpu_note = f"  [GPU {gpu}]"
        try:
            proc = subprocess.Popen(
                cmd, stdout=logf, stderr=subprocess.STDOUT,
                cwd=str(ROOT), env=child_env, **kwargs,
            )
        except OSError as e:
            print(f"  [ОШИБКА] {svc['name']}: не удалось запустить ({e})")
            continue
        pid_file(svc["name"]).write_text(str(proc.pid))
        procs[svc["name"]] = proc
        print(f"  [OK]   {svc['name']:10} PID {proc.pid:<6} → {svc['url']}  "
              f"(лог: logs/{svc['name']}.log){gpu_note}")

    if not procs:
        print("\nНечего запускать.")
        return

    print("\nВсе сервисы подняты. Откройте http://localhost:2002 в браузере.")
    print("LM Studio (LLM-движок) нужно запустить отдельно в приложении LM Studio")
    print("(порт 1234/1235).")
    print("Ctrl+C — остановить всё.\n")

    try:
        while True:
            time.sleep(2)
            for name, p in list(procs.items()):
                rc = p.poll()
                if rc is not None:
                    print(f"  [!] {name} завершился (код {rc}). "
                          f"Смотрите logs/{name}.log")
                    procs.pop(name, None)
                    pid_file(name).unlink(missing_ok=True)
            if not procs:
                print("\nВсе сервисы остановились.")
                break
    except KeyboardInterrupt:
        print("\nОстанавливаю все сервисы...")
        for name, p in procs.items():
            if stop_pid(p.pid):
                print(f"  [стоп] {name}: PID {p.pid}")
            pid_file(name).unlink(missing_ok=True)
        print("Готово.")


def cmd_stop(args):
    print("Остановка сервисов по PID-файлам...")
    found = False
    for svc in SERVICES:
        pid = read_pid(svc["name"])
        if pid:
            found = True
            alive = is_alive(pid)
            if alive:
                stop_pid(pid)
                print(f"  [стоп] {svc['name']}: PID {pid} — остановлен")
            else:
                print(f"  [--]   {svc['name']}: PID {pid} — уже не работает")
            pid_file(svc["name"]).unlink(missing_ok=True)
    if not found:
        print("  Нет PID-файлов — останавливать нечего.")


def cmd_status(args):
    print("Статус сервисов:\n")
    any_alive = False
    for svc in SERVICES:
        name = svc["name"]
        pid = read_pid(name)
        venv_present = venv_python(svc.get("venv")) is not None
        note = ""
        if svc.get("linux_only") and IS_WIN:
            note = "  [только Linux/WSL2]"
        elif not venv_present and svc.get("venv"):
            note = f"  [venv '{svc['venv']}' не установлен]"
        if pid and is_alive(pid):
            any_alive = True
            print(f"  [РАБОТАЕТ]   {name:10} PID {pid:<6} → {svc['url']}{note}")
        elif pid:
            print(f"  [УПАЛ]       {name:10} PID {pid} (процесс не найден){note}")
        else:
            print(f"  [ОСТАНОВЛЕН] {name:10}{note}")
    if not any_alive:
        print("\n(ничего не запущено)")
    # LLM (LM Studio) не управляется этим скриптом — подскажем отдельно.
    print("\n  LLM (LM Studio) запускается отдельно в приложении LM Studio "
          "(http://localhost:1235/v1).")


def main():
    parser = argparse.ArgumentParser(
        description="Запуск всех сервисов OCR-сервиса разом.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--all", action="store_true",
                       help="запустить ВСЕ сервисы, поддерживаемые текущей ОС")
    group.add_argument("--only", metavar="LIST",
                       help="запустить только указанные (через запятую, напр. web,mineru)")
    group.add_argument("--stop", action="store_true", help="остановить все сервисы")
    group.add_argument("--status", action="store_true", help="показать статус сервисов")
    args = parser.parse_args()

    if args.stop:
        cmd_stop(args)
    elif args.status:
        cmd_status(args)
    else:
        cmd_start(args)


if __name__ == "__main__":
    main()
