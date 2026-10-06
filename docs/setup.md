# Проверка установки и сдачи

В текущем окружении проверены команды из README. Следующие процедуры предназначены для независимого окружения с доступом к PyPI/Docker; в этой сессии они **не выполнялись**. Не объявлять setup завершённым до их проверки.

## Чистая установка

Из копии исходников без `.venv` и artifacts, Python 3.11+ (проверенная версия — 3.13.5):

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pip install -e . --no-deps
.venv/bin/python -m pytest
.venv/bin/python -m ruff check .
.venv/bin/python -m ruff format --check .
.venv/bin/python -m mypy src
.venv/bin/python -m fintech_copilot demo --company all --provider mock --seed 42
```

Build backend зафиксирован в pyproject. Дополнительно проверить wheel (`pip wheel . --no-deps`), его установку в другое окружение и CLI вне каталога репозитория: scenarios/tools/prompt должны читаться из package resources.

## Docker

```bash
docker build -t fintech-copilot .
docker run --rm fintech-copilot
docker run --rm -v "$PWD/artifacts/docker:/app/artifacts" fintech-copilot
```

Для bind mount заранее создать каталог и дать UID 10001 право записи. Без mount артефакты находятся внутри контейнера. Проверить непривилегированного пользователя, три complete отчёта, отсутствие runtime network/API dependence и secrets в build context.

## Live, отдельный платный режим

Установить optional dependency `pip install '.[live]'`. Экспортировать `LLM_MODEL` с префиксом `openai/`, `anthropic/` или `gemini/` и ключ соответствующего провайдера. `.env` автоматически не загружается. Ключи не помещать в команды сдачи, Git или trace.

```bash
python -m fintech_copilot demo --company cloudscale-ai --provider live --seed 42 --output artifacts/live
python -m pytest -m live
```

Эти команды обращаются к API и могут расходовать бюджет. Default pytest исключает marker live. Перед заявлением поддержки модели подтвердить настоящие tool calls и grounding, сохранить mode/model/trace. Offline transport fixture не заменяет live smoke.

## GitHub delivery checklist

- [x] Domain → Tools → Orchestration и воспроизводимое offline CLI.
- [x] Независимые golden ожидания, три когорты и негативные grounding tests.
- [x] Короткий README: sequence, actual mock trace, ограничения и Kafka/EDA.
- [x] `.gitignore`/`.dockerignore`: `.env`, `.venv`, artifacts, caches, PDF и `docs/source/` исключены.
- [ ] Чистая установка/wheel и Docker smoke; обновить implementation-status.
- [ ] Выбрать и при необходимости проверить live-модель отдельно.
- [ ] Сохранить локальный коммит (в текущем окружении `.git` доступен только для чтения).
- [ ] Опубликовать код и проверить URL репозитория.
