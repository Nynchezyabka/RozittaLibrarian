# Rozitta Librarian — Этап 1 «Читальный зал»

Поиск и чтение архивов Telegram, скачанных [Rozitta Parser](https://github.com/Nynchezyabka/RozittaLibrarian).
Скелет как у RozittaTranscriber: **FastAPI + одна HTML-страница + WebSocket, порт 8011**.

## Что внутри

| Компонент | Файл | Назначение |
|---|---|---|
| Точка входа | `main.py` | FastAPI + WebSocket :8011, HTTP API |
| Discovery | `core/archive.py` | Поиск архивов в `output/`, чтение `archive_passport.json` |
| Parser DB | `core/parser_db.py` | Read-only (`mode=ro`) доступ к `parser.db`, проверка `PRAGMA user_version` |
| Librarian DB | `core/librarian_db.py` | Своя `librarian.db`, FTS5-индекс над `messages.text` + `transcriptions.text` |
| Инструменты | `core/tools.py` | 5 инструментов: `search`, `read_post`, `stats`, `whats_new`, `list_shelves` |
| Фасад | `core/librarian_core.py` | `LibrarianCore` — открытые архивы, lifecycle, делегирование инструментам |
| UI | `static/index.html` | Тёмная тема Rozitta, 3-колоночный layout, live-лог WS |
| Тесты | `tests/test_tools.py` | 22 юнит-теста на демо-архиве |
| Демо | `scripts/make_demo_archive.py` | Создаёт два тестовых архива в `output/` |

## Запуск

```bash
cd rozitta_librarian
pip install -r requirements.txt

# 1. Создать демо-архивы (опционально, для первого знакомства)
python3 scripts/make_demo_archive.py

# 2. Запустить сервер
python3 main.py
# откроется на http://localhost:8011

# 3. Тесты
pytest tests/ -v

# 4. Сквозной smoke-тест через WebSocket
bash scripts/run_and_test.sh
```

Если порт 8011 занят — задайте `LIBRARIAN_PORT=8012 python3 main.py`.

## Архитектура

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
                  ┌────────┴─────────┐
                  ▼                  ▼
        search() read_post()  stats() whats_new() list_shelves()
```

## Принципы (из `librarian_рабочий_план.md`)

- **База Parser — только чтение.** `parser.db` открывается через `file:...?mode=ro` + `PRAGMA query_only = ON`. Никаких записей.
- **Своя база `librarian.db`** лежит рядом с `parser.db`. FTS5 (токенизатор `unicode61`) над `messages.text` + `transcriptions.text`.
- **Префиксные формы** для русской морфологии: `обесценива*` находит «обесценивание», «обесценивает», «обесценивающее». ~90% покрытия без лемматизатора.
- **5 инструментов** не знают про LLM — тестируются без модели. Их же использует UI и (на этапе 6) MCP-выход.
- **Каждое попадание поиска — кликабельная ссылка** на конкретный пост (`#post/{chat_id}/{message_id}`).

## Что уже работает (этап 1)

✅ Скан `output/` на наличие архивов (`parser.db` + `archive_passport.json`)
✅ Открытие архива: read-only parser.db + создание/чтение librarian.db
✅ Построение FTS5-индекса при первом открытии
✅ `search()` с фильтрами (author, date_from/to, source), ≤ 20 хитов, сниппеты ≤ 300 символов
✅ `read_post()` с пагинацией комментариев, транскрипцией, метаданными
✅ `stats()` — overview (счётчики, диапазон дат, топ авторов) и authors
✅ `whats_new()` — что нового после отметки
✅ `list_shelves()` — полки архива из паспорта
✅ UI: карточки архивов, поисковая форма, рендер результатов, просмотр поста, live-лог
✅ 22 юнит-теста — все проходят
✅ WS smoke-тест — полный цикл поиск→чтение

## Дальнейшие этапы (по плану)

- **Этап 2** — LLM-оркестратор (Ollama + OpenAI-совместимый API, цикл ≤ 12 шагов)
- **Этап 3** — Верификатор (детерминированная проверка ссылок без LLM)
- **Этап 4** — Режимы (Архивариус / Консультант / Свободный) + память
- **Этап 5** — Стенд качества (10–15 эталонных вопросов)
- **Этап 6** — MCP-выход (те же 5 инструментов наружу для Claude Desktop)
