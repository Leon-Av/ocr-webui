#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Единый установщик OCR-сервиса.

Вся логика и русские подсказки — здесь (Python 3 читает .py как UTF-8).
.bat-обёртки вызывают этот скрипт и намеренно не содержат кириллицы.

Использование:
    python install.py            # интерактивное меню
    python install.py web        # только веб-приложение
    python install.py mineru     # только MinerU
        python install.py all        # веб + MinerU + Unlimited-OCR
"""

import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
IS_WIN = os.name == "nt"

# Принудительный UTF-8 для собственного вывода (см. проблемы с cp1251/cp1252).
for _s in (sys.stdout, sys.stderr):
    _reconfigure = getattr(_s, "reconfigure", None)
    if _reconfigure:
        try:
            _reconfigure(encoding="utf-8", errors="replace")
        except (TypeError, ValueError):
            pass


# ---------------------------------------------------------------------
#  Поиск Python нужной версии (через py launcher).
# ---------------------------------------------------------------------

def find_python(min_version=None, max_version=None):
    """Найти python через `py -X.Y`, попадающий в диапазон версий.

    min_version / max_version — кортежи (major, minor), например (3, 10).
    Возвращает строку с командой ("py -3.12" / "python") или None.
    """
    candidates = []
    if min_version or max_version:
        # Перебираем возможные минор-версии 3.8..3.13.
        for minor in range(13, 7, -1):
            v = (3, minor)
            if min_version and v < min_version:
                continue
            if max_version and v > max_version:
                continue
            candidates.append((3, minor))
    else:
        candidates = [(3, minor) for minor in range(13, 7, -1)]

    for major, minor in candidates:
        cmd = ["py", f"-{major}.{minor}"]
        try:
            r = subprocess.run(cmd + ["--version"], capture_output=True)
            if r.returncode == 0:
                return cmd
        except FileNotFoundError:
            break  # py launcher не установлен — переходим к системному python.

    # Фолбэк: системный python.
    try:
        r = subprocess.run(["python", "--version"], capture_output=True)
        if r.returncode == 0:
            return ["python"]
    except FileNotFoundError:
        pass
    return None


def python_version_ok(py_cmd):
    """Проверить, что выбранный python попадает в 3.8-3.13. Возвращает (ok, version_str)."""
    try:
        r = subprocess.run(py_cmd + ["--version"], capture_output=True, text=True)
        out = (r.stdout or r.stderr).strip()
        return (r.returncode == 0, out)
    except Exception:
        return (False, "?")


def detect_cuda():
    """Определить версию CUDA драйвера через nvidia-smi.

    Возвращает мажорную версию CUDA (напр. 12, 11) или None, если GPU нет.
    """
    try:
        r = subprocess.run(
            ["nvidia-smi", "--query-gpu=driver_version", "--format=csv,noheader"],
            capture_output=True, text=True,
        )
        if r.returncode != 0 or not r.stdout.strip():
            return None
        # Полная версия CUDA доступна в выводе nvidia-smi (без --query).
        r2 = subprocess.run(["nvidia-smi"], capture_output=True, text=True)
        m = re.search(r"CUDA Version:\s*(\d+)", r2.stdout)
        if m:
            return int(m.group(1))
        # Фолбэк: некоторые новые драйверы (ветка 6xx) не показывают
        # "CUDA Version" в шапке nvidia-smi. GPU точно есть (первый запрос
        # вернул версию драйвера) — оцениваем CUDA по мажору драйвера.
        dm = re.match(r"(\d+)\.", r.stdout.strip())
        if dm:
            drv = int(dm.group(1))
            if drv >= 580:
                return 13
            if drv >= 520:
                return 12
            return 11
    except FileNotFoundError:
        return None
    except Exception:
        return None
    return None


def pick_cuda_wheel_index():
    """Выбрать индекс pip для torch CUDA под текущий драйвер.

    Возвращает torch_index_url или cpu_url.

    ВАЖНО: torch 2.6.0 публикует только: cu118, cu124, cu126
      (cu120/cu121/cu128 НЕ существует — было причиной падения установки).
    CUDA-рантайм обратно-совместим: колесо cu124/cu118 работает на любом
    драйвере >= 12.x (включая 12.0 и 13.0).
    """
    cuda = detect_cuda()
    if not cuda:
        print("    [!] GPU не найден (nvidia-smi недоступен).")
        print("        torch будет установлен в CPU-режиме (OCR будет МЕДЛЕННЫМ).")
        return "https://download.pytorch.org/whl/cpu"

    print(f"    Обнаружена CUDA драйвера: {cuda}.x")

    # Доступные теги (только РЕАЛЬНО существующие колёса для torch 2.6.0).
    torch_tags = [118, 124, 126]

    # CUDA-рантайм ОБРАТНО-совместим: cu124/cu126 колесо работает на любом
    # драйвере CUDA >= 12.0 (включая 12.0, 13.0). Поэтому для драйвера 12+ и выше
    # смело берём самый старший доступный тег (более свежий рантайм = лучше).
    # Только для старых драйверов (CUDA < 12) ограничиваемся cu118.

    def _pick(tags, driver_major):
        if driver_major >= 12:
            # Любой тег cu118..cu128 работает. Берём самый старший доступный.
            return max(tags)
        # Драйвер CUDA 11.x — только cu118 (более новые рантаймы требуют >= 12).
        return min(tags)

    torch_tag = _pick(torch_tags, cuda)

    torch_index = f"https://download.pytorch.org/whl/cu{torch_tag}"
    print(f"    Выбрано колесо: torch cu{torch_tag}")
    return torch_index


# ---------------------------------------------------------------------
#  Установка одного компонента.
# ---------------------------------------------------------------------

def _pip_install(py_cmd, *args, env=None):
    """Запустить pip install в переданном окружении."""
    full_env = dict(os.environ)
    if env:
        full_env.update(env)
    cmd = py_cmd + ["-m", "pip", "install", *args]
    print("    $", " ".join(cmd))
    r = subprocess.run(cmd, env=full_env)
    return r.returncode == 0


def make_venv(py_cmd, venv_name):
    """Создать venv, если его нет ИЛИ если он сломан (перенос с другой машины).

    venv содержит абсолютные пути к Python-интерпретатору, на котором создан.
    При копировании проекта на другой компьютер/другому пользователю старый
    venv становится нерабочим (указывает на несуществующий python.exe).
    Поэтому проверяем не только существование python.exe, но и его запуск.
    Возвращает путь к python внутри venv как список [путь].
    """
    import shutil
    venv_path = ROOT / venv_name
    python_in_venv = (
        venv_path / "Scripts" / "python.exe" if IS_WIN
        else venv_path / "bin" / "python"
    )

    need_create = True
    if python_in_venv.exists():
        # Проверяем, что python в venv реально запускается.
        r = subprocess.run(
            [str(python_in_venv), "--version"],
            capture_output=True,
        )
        if r.returncode == 0:
            print(f"    venv '{venv_name}' существует и работоспособен.")
            need_create = False
        else:
            print(f"    venv '{venv_name}' сломан (вероятно, перенесён с другой машины).")
            print(f"    Удаляем и пересоздаём...")
            shutil.rmtree(venv_path, ignore_errors=True)

    if need_create:
        print(f"    Создаём venv '{venv_name}' ...")
        r = subprocess.run(py_cmd + ["-m", "venv", str(venv_path)])
        if r.returncode != 0:
            print(f"    [ОШИБКА] не удалось создать venv '{venv_name}'.")
            return None
    return [str(python_in_venv)]


def install_web():
    """Установить веб-приложение в корневой venv."""
    print("\n=== Веб-приложение (основной venv) ===")
    py_cmd = find_python(min_version=(3, 10))
    if not py_cmd:
        print("[ОШИБКА] Python 3.10+ не найден. Установите с https://www.python.org/downloads/")
        return False
    ok, ver = python_version_ok(py_cmd)
    print(f"    Интерпретатор: {' '.join(py_cmd)} ({ver})")
    venv_py = make_venv(py_cmd, "venv")
    if not venv_py:
        return False
    if not _pip_install(venv_py, "--upgrade", "pip"):
        print("[ОШИБКА] не удалось обновить pip.")
        return False
    print("    Устанавливаем зависимости веб-приложения (requirements.txt) ...")
    if not _pip_install(venv_py, "-r", "requirements.txt"):
        print("[ОШИБКА] установка зависимостей веб-приложения не удалась.")
        return False
    print("    [OK] Веб-приложение готово.")
    return True


def install_mineru():
    """Установить MinerU 2.5-Pro-1.2B (полный pipeline, GPU).

    Порядок важен:
      1. torch С CUDA первым (иначе pip вытянет CPU-only torch).
      2. mineru[pipeline] — полный pipeline, даёт качественные формулы на GPU.
    """
    print("\n=== MinerU 2.5-Pro-1.2B ===")
    # 3.13 не поддерживается mineru → ограничиваем 3.10-3.12.
    py_cmd = find_python(min_version=(3, 10), max_version=(3, 12))
    if not py_cmd:
        print("[ОШИБКА] Нужен Python 3.10, 3.11 или 3.12 (на 3.13 mineru не работает).")
        print("         Установите с https://www.python.org/downloads/")
        return False
    ok, ver = python_version_ok(py_cmd)
    print(f"    Интерпретатор: {' '.join(py_cmd)} ({ver})")
    venv_py = make_venv(py_cmd, "venv_mineru")
    if not venv_py:
        return False
    if not _pip_install(venv_py, "--upgrade", "pip"):
        return False

    # 1. torch С CUDA (первым, чтобы pip не подменял на CPU-версию).
    print("    Выбираем версию CUDA ...")
    torch_index = pick_cuda_wheel_index()
    print(f"    Устанавливаем torch==2.6.0 + torchvision==0.21.0 ({torch_index}) ...")
    torch_ok = _pip_install(
        venv_py,
        "torch==2.6.0", "torchvision==0.21.0",
        "--index-url", torch_index,
    )
    if not torch_ok:
        # Фолбэк: позволить pip подобрать совместимую версию torch под этот тег.
        print("    [!] torch==2.6.0 не получился. Пробуем авто-версию torch ...")
        torch_ok = _pip_install(
            venv_py, "torch", "torchvision", "--index-url", torch_index,
        )
    if not torch_ok:
        # Последний фолбэк: cu118 — самый широко совместимый тег.
        print("    [!] Пробуем torch cu118 (универсальный) ...")
        torch_ok = _pip_install(
            venv_py, "torch==2.6.0", "torchvision==0.21.0",
            "--index-url", "https://download.pytorch.org/whl/cu118",
        )
    if not torch_ok:
        print("[ОШИБКА] установка torch не удалась ни одним способом.")
        print("         См. https://pytorch.org/get-started/locally/ и подберите команду вручную.")
        return False

    # 2. Полный pipeline mineru.
    print("    Устанавливаем mineru[pipeline] (минут 10-20, ~2-3 ГБ пакетов) ...")
    if not _pip_install(venv_py, "-r", "ocr_servers/requirements_mineru.txt"):
        print("[ОШИБКА] установка MinerU не удалась. См. вывод выше.")
        return False

    # Проверка: torch видит CUDA + mineru CLI доступен + accelerate на месте.
    print("    Проверяем torch.cuda + accelerate + mineru CLI ...")
    r = subprocess.run(venv_py + ["-c",
        "import torch; print('cuda' if torch.cuda.is_available() else 'CPU ONLY')"],
        capture_output=True, text=True)
    torch_state = r.stdout.strip() or r.stderr.strip()[:200]
    print(f"    torch.cuda.is_available() -> {torch_state}")
    r = subprocess.run(venv_py + ["-c",
        "import accelerate; print('accelerate', accelerate.__version__)"],
        capture_output=True, text=True)
    if r.returncode != 0:
        print("    [ВНИМАНИЕ] accelerate не установлен! MinerU не сможет грузить VLM на GPU.")
        print("              Установите: venv_mineru\\Scripts\\pip install accelerate")
    else:
        print(f"    {r.stdout.strip()}")
    r = subprocess.run(venv_py + ["-m", "mineru.cli", "--help"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        print(f"    [ВНИМАНИЕ] mineru CLI недоступен: {r.stderr.strip()[:200]}")
    else:
        print("    mineru CLI: OK")
    if "CPU" in torch_state:
        print("    [ВНИМАНИЕ] GPU не активирован — OCR будет медленным.")
        print("              Проверьте, что torch поставлен с правильной CUDA.")
    print("    [OK] MinerU установлен.")
    return True


def install_unlimitedocr():
    """Установить Unlimited-OCR (Baidu) — VLM ~3.3B, PyTorch+Transformers.

    Модель baidu/Unlimited-OCR-2 (~6.7 ГБ BF16) скачивается с HuggingFace при
    первом запуске сервера. Требования из README: Python 3.10, torch>=2.13,
    transformers==4.52.1. GPU: ~10+ ГБ VRAM (на 12 ГБ 4070 помещается).
    """
    print("\n=== Unlimited-OCR (Baidu) ===")
    py_cmd = find_python(min_version=(3, 10), max_version=(3, 12))
    if not py_cmd:
        print("[ОШИБКА] Нужен Python 3.10, 3.11 или 3.12.")
        print("         Установите с https://www.python.org/downloads/")
        return False
    ok, ver = python_version_ok(py_cmd)
    print(f"    Интерпретатор: {' '.join(py_cmd)} ({ver})")
    venv_py = make_venv(py_cmd, "venv_unlimitedocr")
    if not venv_py:
        return False
    if not _pip_install(venv_py, "--upgrade", "pip"):
        return False

    # 1. torch С CUDA (первым, чтобы pip не подменял на CPU-версию).
    # README Unlimited-OCR: torch==2.13.0; если колеса нет — фолбэки.
    print("    Выбираем версию CUDA ...")
    torch_index = pick_cuda_wheel_index()
    torch_ok = _pip_install(
        venv_py, "torch==2.13.0", "torchvision",
        "--index-url", torch_index,
    )
    if not torch_ok:
        print("    [!] torch==2.13.0 не получился. Пробуем авто-версию torch ...")
        torch_ok = _pip_install(venv_py, "torch", "torchvision",
                                "--index-url", torch_index)
    if not torch_ok:
        print("    [!] Пробуем torch cu118 (универсальный) ...")
        torch_ok = _pip_install(venv_py, "torch", "torchvision",
                                "--index-url", "https://download.pytorch.org/whl/cu118")
    if not torch_ok:
        print("[ОШИБКА] установка torch не удалась ни одним способом.")
        return False

    # 2. Transformers-стек Unlimited-OCR.
    print("    Устанавливаем transformers-стек (requirements_unlimitedocr.txt) ...")
    if not _pip_install(venv_py, "-r", "ocr_servers/requirements_unlimitedocr.txt"):
        print("[ОШИБКА] установка Unlimited-OCR не удалась. См. вывод выше.")
        return False

    # Проверка: torch видит CUDA + модель импортируется (transformers).
    print("    Проверяем torch.cuda + transformers ...")
    r = subprocess.run(venv_py + ["-c",
        "import torch, transformers; "
        "print('cuda' if torch.cuda.is_available() else 'CPU ONLY', "
        "'transformers', transformers.__version__)"],
        capture_output=True, text=True)
    state = (r.stdout or "").strip() or (r.stderr or "").strip()[:200]
    print(f"    -> {state}")
    if "CPU ONLY" in state:
        print("    [ВНИМАНИЕ] GPU не активирован — движок не запустится (VLM, CPU не поддерживается).")
        return False
    print("    [OK] Unlimited-OCR установлен. Первый запуск сервера скачает модель (~6.7 ГБ).")
    return True


# ---------------------------------------------------------------------
#  Меню.
# ---------------------------------------------------------------------

def show_menu():
    print()
    print("=" * 56)
    print("  Установка OCR-сервиса")
    print("=" * 56)
    print("  [1] Веб-приложение (обязательно)")
    print("  [2] MinerU 2.5-Pro-1.2B          (Python 3.10-3.12)")
    print("  [3] Unlimited-OCR (Baidu)        (Python 3.10-3.12, GPU 10+ ГБ)")
    print("  [4] Всё под Windows: веб + MinerU + Unlimited-OCR")
    print("  [0] Выход")
    print()


def main():
    # Аргумент из командной строки → пропуск меню.
    if len(sys.argv) > 1:
        arg = sys.argv[1].lower()
        targets = {
            "web": [install_web],
            "mineru": [install_mineru],
            "unlimited": [install_unlimitedocr],
            "all": [install_web, install_mineru, install_unlimitedocr],
        }
        if arg in targets:
            ok_all = True
            for fn in targets[arg]:
                ok_all = fn() and ok_all
            _final_message(ok_all)
            return 0
        print(f"Неизвестный аргумент: {arg}")
        print("Допустимо: web, mineru, unlimited, all")
        return 1

    # Интерактивное меню.
    while True:
        show_menu()
        choice = input("Ваш выбор [0-4]: ").strip()
        if choice == "1":
            install_web()
            _pause()
        elif choice == "2":
            install_mineru()
            _pause()
        elif choice == "3":
            install_unlimitedocr()
            _pause()
        elif choice == "4":
            ok_all = install_web()
            ok_all = install_mineru() and ok_all
            ok_all = install_unlimitedocr() and ok_all
            _final_message(ok_all)
            _pause()
            break
        elif choice == "0":
            break
        else:
            print("Некорректный выбор.")
    return 0


def _pause():
    try:
        input("\nНажмите Enter для продолжения...")
    except EOFError:
        pass


def _final_message(ok):
    print()
    print("=" * 56)
    print("  Установка завершена." if ok else "  Установка завершена с ошибками.")
    print("=" * 56)
    print("Запуск всех сервисов:   run_all.bat")
    print("Статус:                 run_all.bat --status")
    print()
    print("LM Studio (LLM-движок) запускается отдельно в приложении LM Studio.")
    print()


if __name__ == "__main__":
    sys.exit(main())
