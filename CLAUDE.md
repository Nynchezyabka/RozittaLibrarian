# CLAUDE.md — Карта проекта Rozitta Librarian

> Карта для ассистента: что это за проект, как устроен, где что лежит,
> какие правила соблюдать при правках. Источник правды о **текущем**
> состоянии — `librarian_статус.md`; о порядке работ —
> `librarian_рабочий_план.md`.

## 📋 О проекте

**Название:** Rozitta Librarian
**Что это:** Веб-приложение для поиска и чтения архивов Telegram, скачанных
[Rozitta Parser](https://github.com/Nynchezyabka/RozittaLibrarian).
**Скелет:** FastAPI + одна HTML-страница + WebSocket, порт по умолчанию 8011
(перекрывается переменной окружения `LIBRARIAN_PORT`).
**Слой без LLM («Читальный зал») — цель этапа 1:** открыть программу →
выбрать архив → искать по FTS5 → читать посты и комментарии →
пользоваться без единой настройки модели.

---

## 🧱 Технологический стек

- **Python 3.10+**
- **FastAPI + Uvicorn** — HTTP + WebSocket на одном порту
- **SQLite3** — два подключения: `parser.db` (read-only) и собственный
  `librarian.db` (FTS5, токенизатор `unicode61`)
- **webbrowser + threading** — авто-открытие браузера после старта сервера
  (демон-поток, задержка 1.5 c, отключается флагом `--no-browser`)
- **Vanilla JS** в браузере — без сборщиков, без фреймворков; один
  `static/index.html`, генерируется из макета через `scripts/build_index_html.py`

---

## 📂 Структура проекта

```
rozitta_librarian/
├── main.py                       # FastAPI + WebSocket, HTTP API, точка входа
├── requirements.txt
├── core/
│   ├── archive.py                # ArchivePassport, discovery, card/to_dict
│   ├── parser_db.py              # read-only (mode=ro) к parser.db
│   ├── librarian_db.py           # своя librarian.db, FTS5, INDEX_VERSION=2
│   ├── tools.py                  # инструменты: search, read_post, get_message, stats, whats_new, list_shelves
│   └── librarian_core.py         # LibrarianCore — фасад, lifecycle архивов
├── static/
│   └── index.html                # продакшен-сборка (НЕ править вручную)
├── scripts/
│   ├── make_demo_archive.py      # два демо-архива (философский канал + тех-форум)
│   ├── build_index_html.py       # из макета → static/index.html (идемпотентный)
│   ├── test_ui_ws.py             # 11 WS smoke-тестов (UI-1..UI-4)
│   ├── ws_smoke_test.py          # старый smoke 6 операций
│   └── run_and_test.sh           # запуск сервера + smoke
├── tests/
│   └── test_tools.py             # 22 юнит-теста инструментов
├── output/
│   ├── demo_philosophy_channel/  # архив канала (parser.db + librarian.db + passport.json)
│   └── demo_tech_forum/          # архив форума
└── librarian_*.md                # документы (план, статус, архитектура)
```

---

## 🏛️ Архитектура в двух словах

```
пользователь ─→ браузер ─→ WebSocket /ws
                              │
                              ▼
                          main.py (FastAPI)
                              │
                              ▼
                       LibrarianCore (фасад)
                       ┌──────┴──────┐
                       ▼             ▼
                   ParserDB       LibrarianDB
                  (read-only)     (librarian.db + FTS5)
                       │             │
                       └──┬──────────┘
                          ▼
                      tools.py
            ┌────────┬────────┬────────┬────────┬────────┐
            ▼        ▼        ▼        ▼        ▼        ▼
        search() read_post() get_message() stats() whats_new() list_shelves()
```

- Браузер общается с сервером **только через WebSocket** на `/ws`.
  Сообщения вида `{"op": "...", "archive_id": "...", "args": {...}}`.
- HTTP-эндпоинты (`/api/archives`, `/api/archives/{id}/card` и т.д.) —
  для лёгкой интеграции и проверки без WS.
- Долгие SQL-операции уходят в threadpool через `asyncio.to_thread`,
  чтобы event loop мог параллельно слать `log`-сообщения в браузер.

---

## 🔌 WebSocket-операции (текущий список)

| `op`              | Назначение                                                       |
|-------------------|------------------------------------------------------------------|
| `list_archives`   | Карточки архивов из `output/`                                    |
| `scan_archives`   | Принудительно пересканировать `output/`                          |
| `open_archive`    | Карточка + полный паспорт; попутно строит FTS, если устарел      |
| `top_terms`       | Топ-термины архива (для чипов-примеров)                          |
| `search`          | FTS5-поиск с фильтрами (author, date_from/to, source/shelf)      |
| `get_message`     | UI-4 ридер: пост + транскрипция + комментарии + соседи + t.me    |
| `read_post`       | То же, но без соседей и t.me (оставлен для совместимости)        |
| `stats`           | `kind=overview` (счётчики, диапазон, топ-авторов) или `authors` |
| `whats_new`       | Лента новых сообщений с `since`                                  |
| `list_shelves`    | Полки из паспорта архива                                         |

Каждая операция возвращает `{"type": "result", "op": ..., "data": ...}`;
параллельно шлёт `{"type": "log", "level": "info|success|warning|error", "message": ...}`.

---

## 🧭 UX (4 экрана + dev-панель)

Hash-based routing, без сервер-сайд рендера:

| Хеш                          | Экран                              |
|------------------------------|------------------------------------|
| `#/`                         | 1. Главная: карточки архивов       |
| `#/a/{id}`                   | 2. Старт архива: TOC + summary + recent |
| `#/a/{id}/search?q=...`      | 3. Поиск: результаты FTS           |
| `#/a/{id}/m/{msg}`           | 4. Ридер: пост + комментарии       |
| `#/a/{id}/shelf/{shelf}`     | Вариант 2 для конкретной полки     |

- Палитра Rozitta: `#2B2B2B` фон, `#FF9500` акцент, `#FF6BC9` розовый, `#F0F0F0` текст.
- Аватар Rozitta в правом верхнем углу даёт контекстные подсказки по 7 правилам
  из спецификации (см. `librarian_ui_спецификация_этап1.md` §6).
- Dev-панель: открывается тройным кликом по логотипу, показывает live-лог WS
  и кнопки переиндексации.

---

## 🧊 Ключевые правила — НЕ нарушать

1. **База Parser — только чтение.** `parser.db` открывается через
   `file:...?mode=ro` + `PRAGMA query_only = ON`. Ни одной записи.
2. **Своя база `librarian.db`** лежит рядом с `parser.db`. FTS5
   (токенизатор `unicode61`) над `messages.text` + `transcriptions.text`.
   При изменении схемы поднимаем `INDEX_VERSION` в `librarian_db.py`.
3. **Чипы — динамические, в библиотекаре, не в парсере.**
   `chips` больше нет в `archive_passport.json`. Чипы вычисляются через
   `LibrarianDB.top_terms(limit=8, min_len=6)` из FTS5-словаря (`fts5vocab`)
   при открытии архива. Простые средние слова — этого достаточно,
   никакой интерактивности.
4. **Префиксные формы** для русской морфологии: `обесценива*` находит
   «обесценивание», «обесценивает», «обесценивающее». ~90% покрытия без
   лемматизатора.
5. **5 инструментов не знают про LLM** — тестируются без модели. Их же
   использует UI (этап 1) и (будущий) MCP-выход (этап 6).
6. **Ответы Librarian (этап 2+) никогда не попадают в поисковый индекс** —
   иначе он начнёт цитировать сам себя.
7. **`static/index.html` НЕ правится вручную.** Источник — UI-макет, сборка
   через `python3 scripts/build_index_html.py`. Правки JS — в макете или в
   шаблонах сборщика, потом пересобираем.
8. **Перед коммитом:** `pytest tests/ -v` (22 теста) и
   `python3 scripts/test_ui_ws.py` (11 smoke-тестов) должны быть зелёными.

---

## 🔄 Поток выполнения

```
main.py:main()
  ├── uvicorn.run(host, port)
  ├── запускает фоновый поток _open_browser_after_delay(url, 1.5)
  │   └── webbrowser.open(url)  если не передан --no-browser
  └── FastAPI обслуживает:
        GET  /                  → static/index.html
        GET  /api/archives      → JSON-карточки
        GET  /api/archives/{id} → JSON-паспорт
        GET  /api/archives/{id}/card
        POST /api/archives/{id}/reindex
        WS   /ws                → все операции из таблицы выше
```

---

## 📦 Запуск

```bash
cd rozitta_librarian
pip install -r requirements.txt

# 1. Демо-архивы (опционально, для первого знакомства)
python3 scripts/make_demo_archive.py

# 2. Сервер (откроет браузер сам)
python3 main.py
# откроется на http://localhost:8011

# 3. Тесты
pytest tests/ -v                 # 22 юнит-теста
python3 scripts/test_ui_ws.py    # 11 WS smoke-тестов (сервер должен быть запущен)

# 4. Пересобрать UI из макета (после правки JS/CSS)
python3 scripts/build_index_html.py
```

Если порт 8011 занят: `LIBRARIAN_PORT=8012 python3 main.py`.
Без авто-открытия браузера: `python3 main.py --no-browser`.

---

## 🧪 Тесты

| Файл                       | Что проверяет                                  | Кол-во |
|----------------------------|------------------------------------------------|--------|
| `tests/test_tools.py`      | Инструменты на демо-архиве (pytest)            | 22     |
| `scripts/test_ui_ws.py`    | WS-циклы UI-1..UI-4 (запускать при поднятом сервере) | 11 |
| `scripts/ws_smoke_test.py` | Базовый smoke 6 операций (legacy)              | 6      |

---

## 📚 Смежные документы

- `librarian_рабочий_план.md` — что делать дальше (редко меняется)
- `librarian_статус.md` — где остановились (после каждого захода)
- `librarian_ui_спецификация_этап1.md` (в `/upload`) — спецификация экранов
- `librarian_ui_макет.html` (в `/upload`) — UI-макет, источник `index.html`

---

**Последнее обновление:** 2026-07-29
**Версия документа:** 1.0 (Librarian; заменила ошибочно оставленную карту Parser)
**Этап:** 1, завершается — UI-1..UI-4 + динамические чипы готовы.
