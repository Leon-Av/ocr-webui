# OCR-серверы движков

Каждый движок работает как **отдельный HTTP-сервис** в **своём виртуальном окружении**
со своими зависимостями. Это необходимо, потому что VLM-стеки
(`vllm`, `mineru-vl-utils`, `torch`) конфликтуют с прочими, если поставить их в один
Python. Веб-приложение остаётся лёгким и CPU-only и ходит к каждому серверу по
единому контракту `POST /ocr`.

## Единый HTTP-контракт

```
POST /ocr
  multipart/form-data:
    image  — байты изображения (PNG/JPEG)
    filename — (опц.) имя файла
→ 200 {"text": "распознанный plain text", "markdown": "опц. разметка"}

GET /health → 200 {"status":"ok"} | 503 {"status":"loading"}
```

Веб-приложение **само** растеризует PDF в картинки (PyMuPDF) и шлёт на движок
только изображения, поэтому от движка не требуется поддержка PDF.

## Серверы

| Файл                       | Движок              | Порт по умолч. | URL веб-приложения         |
|----------------------------|---------------------|----------------|----------------------------|
| `run_mineru_server.py`     | MinerU 2.5-Pro-1.2B | 8501           | `http://localhost:8501`    |

LLM-движок отдельного сервера не имеет — он ходит напрямую в **LM Studio**
(`http://localhost:1235/v1`), который пользователь запускает сам.

## Запуск каждого сервера

### 1. MinerU 2.5-Pro-1.2B (на GPU или CPU)

```bash
# В корне проекта:
python -m venv venv_mineru
venv_mineru\Scripts\activate          # Windows
# source venv_mineru/bin/activate     # Linux/macOS

pip install -r ocr_servers/requirements_mineru.txt
# Для GPU — установите torch под вашу CUDA: https://pytorch.org/get-started/locally/

python ocr_servers/run_mineru_server.py --port 8501
```

Модель скачается с HuggingFace при первом запуске
(`opendatalab/MinerU2.5-2509-1.2B`, ~2.5 ГБ).

## Проверка работоспособности

```bash
# Здоровье:
curl http://localhost:8501/health     # -> {"status":"ok"} (после загрузки модели)

# Распознать картинку:
curl -F "image=@test.png" http://localhost:8501/ocr
# -> {"text":"...","markdown":null}
```

Веб-приложение само проверяет `/health` каждого движка при открытии страницы
обработки и помечает недоступные бейджем «оффлайн».

## Замечания по Windows

- Для MinerU на Windows при проблемах с CUDA проще всего использовать WSL2.
- Пути с дисками (`D:/...`) и пробелами берите в кавычки.
- Если GPU не определяется, проверьте `nvidia-smi` и совпадение версии CUDA в
  индексе установки torch.
