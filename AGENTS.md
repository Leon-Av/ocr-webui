# AGENTS.md — OCR Web-сервис

Документ для AI-агентов (и разработчиков): архитектура, ключевые файлы,
контракты, частые задачи.

## Назначение

Веб-сервис для OCR изображений и PDF с несколькими движками (MinerU 2.5-Pro-1.2B,
Unlimited-OCR Baidu, локальная LLM в LM Studio). Результат — .docx/.txt/ZIP.
Многопользовательский: авторизация пользователей + админ-панель. Визуальный стиль
унаследован из `example/` (WhisperX-сервис): Tailwind-CDN, индиговая primary-палитра,
slate-нейтральные, Heroicons-SVG, русский UI.

## Архитектура движков

**Ключевое решение:** веб-приложение CPU-only, не тянет ни vllm,
ни torch. Каждый OCR-движок — отдельный HTTP-сервис (свой venv, свои deps) с
единым контрактом `POST /ocr`. Это позволяет:

- избежать конфликта тяжёлых зависимостей в одном окружении;
- независимо масштабировать/перезапускать движки;
- совпадает с уже работающим LLM в LM Studio.

### Единый контракт движка

```
POST /ocr   multipart: image=<байты PNG/JPEG>, filename=<имя>
            → 200 {"text": str, "markdown": str|None}
            → 400 некорректное изображение
            → 500 ошибка модели
            → 503 модель ещё грузится
GET /health → 200 {"status":"ok"} | 503 {"status":"loading"}
```

Веб-приложение **всегда** само растеризует PDF → PNG (PyMuPDF, 200 dpi) и шлёт
движку только изображения. Поэтому от движка не требуется поддержка PDF.

### URL по умолчанию

| Движок | URL |
|---|---|
| MinerU 2.5-Pro-1.2B | `http://localhost:8501` |
| Unlimited-OCR (Baidu) | `http://localhost:8503` |
| LLM (LM Studio) | `http://localhost:1235/v1` (OpenAI-совместимый) |

URL'ы и флаги включения хранятся в таблице `settings` и редактируются в админке
(Настройки). `ocr/__init__.py:get_engine(code)` собирает адаптер с актуальным URL.

## Модели данных (`models.py`)

- **User** — как в example (email, hash, имя, is_admin, статистика входов).
- **Job** — одна загрузка пользователя (1..N файлов): engine, output_mode,
  status, total_pages, processing_time, result_dir.
- **JobFile** — один загруженный файл: original/stored filename, is_pdf,
  page_count, status, text_content, markdown_content.
- **ProcessingStat** — статистика по задаче (для графиков админки).
- **Setting** — key/value (URL движков, retention, лимиты).

## Поток обработки (`worker.py:process_job`)

1. Job → status=processing.
2. Для каждого JobFile: если PDF → `rasterize_pdf` → список PNG; иначе сам файл.
3. Каждое изображение → `engine.ocr(bytes)` через httpx (с ретраями/таймаутом).
4. Текст/Markdown копится в JobFile.text_content / markdown_content.
5. Сборка результатов (`doc_builder.py`):
   - `combined` → один `result.docx` + `result.txt`;
   - `per_file` → отдельные `<filename>.docx/.txt` на каждый файл.
6. Файлы пишутся в `results/<job_id>/`; ZIP собирается in-memory на лету.
7. Записывается ProcessingStat; status=completed; SSE обновляется.

## SSE-прогресс

`GET /api/events` (EventSource) шлёт JSON с:
`{job_id, status, progress (0–100), message, current_file, current_page,
total_pages, user_jobs: [активные задачи юзера с queue_position]}`.

Статус хранится в `_current_status` (`worker.py`), обновляется на каждом шаге.
Фронтенд (`user/dashboard.html`) слушает и обновляет прогресс-бар + список задач.

## Маршруты (`main.py`)

**Пользователь:** `/login`, `/logout`, `/` (обработка), `/history`, `/profile`,
`/forgot-password`.

**API:** `POST /api/upload`, `GET /api/events`, `POST /api/jobs/{id}/cancel`,
`POST /api/jobs/{id}/delete`, `GET /download/{id}/{kind}` (docx|txt),
`GET /download-all/{id}` (ZIP), `POST /api/user/settings`.

**Админка:** `/admin/login`, `/admin/logout`, `/admin`, `/admin/accounts`,
`/admin/accounts/create`, `/admin/accounts/{id}/edit`,
`/admin/accounts/{id}/delete`, `/admin/statistics`, `/admin/settings`,
`/admin/reset-stats`.

Авторизация: JWT в httpOnly cookie (`access_token` для юзера, `admin_token`
для админа). Каждый маршрут-гард при отсутствии токена редиректит на логин (303).
Админские креды — из `config.py` (`ADMIN_USERNAME`/`ADMIN_PASSWORD`).

## Частые задачи

### Добавить новый движок

1. `ocr/servers/`: новый `run_<name>_server.py`, реализующий функцию `run_ocr(img)`
   и вызывающий `build_app(...)` из `server_common.py`. Завести
   `requirements_<name>.txt`.
2. `config.py`: добавить запись в `ENGINES`, `DEFAULT_*_URL`, флаг в `DEFAULT_ENGINE_ENABLED`.
3. `ocr/__init__.py`: добавить класс-наследник `HTTPEngine` и ветку в `get_engine`.
4. `database.py:_seed_settings`: добавить новые дефолтные settings-ключи.
5. `templates/admin/settings.html`: добавить блок настроек (URL + чекбокс).
6. `templates/user/dashboard.html`: краткое описание в тексте карточки движка.

### Сменить порт сервиса

`config.py:PORT` + перезапуск. Админ-URL'ы движков — в админке (Настройки).

### Выбрать GPU для OCR-движка

Админка → Настройки → OCR-движки → GPU (ключи в `settings`:
`engine_gpu_mineru`; значения `auto` или номер CUDA).
`run_all.py` при запуске движка читает настройку прямо из SQLite (без deps)
и выставляет `CUDA_VISIBLE_DEVICES` + `CUDA_DEVICE_ORDER=PCI_BUS_ID` — номера
GPU совпадают с порядком nvidia-smi. Применяется при следующем запуске
сервисов (run_all.bat). Вручную: `run_*_server.py --gpu 1`. GPU для LLM
настраивается в самом LM Studio (движок — внешний сервис).

### Изменить обработку результатов

Сборка — в `doc_builder.py`. Режимы `combined` / `per_file` — поле `Job.output_mode`,
передаётся с формы (`POST /api/upload`). Добавить новый режим: `OUTPUT_MODES`
в `config.py`, радиокарточка в дашборде, ветка в `process_job`.

### Отладка

- Логи: `logging.basicConfig` в `main.py` (INFO). Запросы к движкам логируются.
- Проверить движок: `curl http://localhost:8501/health`, `curl -F "image=@t.png" http://localhost:8501/ocr`.
- БД: `ocr_service.db` в корне проекта (SQLite WAL). Схема — в `models.py`.

## Известные ограничения / TODO

- Очередь однозадачная (`asyncio.Queue`, одна задача за раз). Для параллелизма —
  запуск нескольких worker_loop'ов или внешняя очередь (Celery/RQ).
- Растеризация PDF всегда в PNG (200 dpi). Можно добавить детект текстовых PDF
  и пропускать OCR — сейчас все PDF считаются сканами (как и просил заказчик).
- Email-уведомления выключены по умолчанию (`EMAIL_ENABLED=false`).
- LM Studio: имя модели берётся из settings (`engine_llm_model`), по умолчанию
  `local-llm` — поменяйте на реальное имя модели в админке.
