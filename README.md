# JobRadar v0.1 — технический прототип

Первый этап новой реализации: модульная структура, конфигурация, промпты и SQLite.
Рабочая ветка — `jobradar-v0.1`, основа — `jobradar-v2.2`.
Полноценные диалог, поиск и UI v0.1 ещё не реализованы.

## Установка и проверка на Windows (CMD)

Требуется Python 3.11 или новее с поддержкой SQLite JSON-функций.
Из корня репозитория:

```bat
py -m venv .venv
.venv\Scripts\activate
python -m pip install -e .
copy .env.example .env
python -m jobradar init-db --config config/v0.1.json
python -m unittest discover -s tests/v01 -v
```

На Linux: `python3 -m venv .venv`, `source .venv/bin/activate`; остальные Python-команды те же.
На первом этапе ключ DeepSeek и установка браузера Playwright не нужны.
Когда будет подключён адаптер HH.ru: `python -m playwright install chromium`.

Новая база — `data/jobradar-v0.1.sqlite3`. Она не использует старую `jobradar.db`.
Повторная инициализация сохраняет данные v0.1. Если указана старая/неизвестная
схема, команда завершается ошибкой без её миграции или перезаписи.
Пути БД и промптов разрешаются относительно файла конфигурации, а не рабочего каталога.
При запуске вне корня передайте абсолютный путь к конфигурации.

## Структура

- `src/jobradar/storage/` — схема SQLite, соединения и транзакции.
- `src/jobradar/config.py`, `config/v0.1.json` — загрузка публичных настроек.
- `src/jobradar/{orchestration,dialogue,llm,tools,profiles,search,sources,analysis,web}/`
  — границы будущих компонентов; пока без реализации.
- `prompts/` — утверждённые тексты v1.0 из Confluence.
- `tests/v01/` — автономные тесты первого этапа.
- `docs/reference/` — снимки прочитанной документации с версиями источников.
- [Результаты и границы первого этапа](docs/stage-1.md).

Секреты помещаются только в локальный `.env`, исключённый из Git.
`.env.example` не содержит ключей. Команда init-db не читает секреты и не вызывает сеть.
Функции проверки владельца, предметной валидации JSON и операций профилей
будут добавлены в следующих этапах; наличие таблиц не означает готовность этих сервисов.

## Сохранённый прототип v2.2

Корневые `app.py`, `storage.py`, `orchestrator.py`, `deepseek.py`,
`collector.py`, `toolspec.py`, `templates/`, `config.json` и `requirements.txt`
относятся к v2.2. Новая реализация их не импортирует. Не запускайте `py app.py`
для проверки v0.1: это старое приложение с прежней БД.

[Инструкция старого прототипа](docs/README-v2.2.md).
Его отдельные регрессионные тесты: `python -m unittest discover -s tests -v`.
