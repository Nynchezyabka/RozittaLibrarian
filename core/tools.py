"""
core/tools.py — пять инструментов Librarian.

Инструменты — сердце программы (librarian_рабочий_план.md §4). Они:
- не знают про LLM,
- тестируются юнит-тестами без модели,
- их же использует строка поиска в UI и MCP-выход.

| Инструмент      | Что делает                              | Возвращает                          |
|-----------------|-----------------------------------------|-------------------------------------|
| search()        | FTS5-поиск, префиксные формы            | ≤ 20 сниппетов ≤ 300 симв.          |
| read_post()     | Полный пост + комментарии + транскрипция| текст со связями                    |
| stats()         | Готовые числа: счётчики, динамика, авторы| только арифметика                  |
| whats_new()     | Что появилось после отметки             | список нового                       |
| list_shelves()  | Полки архива из паспорта                | типы, описания, периоды             |

Возвращаемые значения — простые dict/list, готовые к JSON-сериализации.
"""
from __future__ import annotations

from dataclasses import asdict
from typing import Optional

from .archive import Archive
from .librarian_db import LibrarianDB
from .parser_db import ParserDB


# Бизнес-лимиты из спецификации (§4)
SEARCH_MAX_RESULTS = 20
SEARCH_SNIPPET_MAX = 300
READ_POST_COMMENT_LIMIT = 200
WHATS_NEW_DEFAULT_LIMIT = 50
WHATS_NEW_MAX_LIMIT = 500


class ToolError(Exception):
    """Ошибка инструмента — пользовательская (показывается в UI как есть)."""


# ---------------------------------------------------------------------------
# Tool: search
# ---------------------------------------------------------------------------

def search(
    archive: Archive,
    lib_db: LibrarianDB,
    query: str,
    *,
    author: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    source: Optional[str] = None,
    limit: int = SEARCH_MAX_RESULTS,
) -> dict:
    """
    FTS5-поиск по архиву.

    Возвращает:
        {
            "query":   исходная строка,
            "filters": применённые фильтры,
            "count":   сколько найдено,
            "hits":    [
                {
                    "internal_id", "chat_id", "message_id",
                    "source", "author", "date",
                    "snippet"     -- с подсветкой <<H>>..<<H>>,
                    "url"         -- кликабельная ссылка на пост в UI
                },
                ...
            ]
        }
    """
    query = (query or "").strip()
    if not query:
        return {"query": "", "filters": {}, "count": 0, "hits": []}

    limit = max(1, min(int(limit or SEARCH_MAX_RESULTS), SEARCH_MAX_RESULTS))
    hits = lib_db.search(
        query,
        limit=limit,
        author=author,
        date_from=date_from,
        date_to=date_to,
        source=source,
        snippet_size=SEARCH_SNIPPET_MAX,
    )

    return {
        "query": query,
        "filters": {
            "author": author,
            "date_from": date_from,
            "date_to": date_to,
            "source": source,
        },
        "count": len(hits),
        "hits": [
            {
                "internal_id": h.internal_id,
                "chat_id": h.chat_id,
                "message_id": h.message_id,
                "source": h.source,
                "author": h.author,
                "date": h.date,
                "snippet": h.snippet,
                "url": f"#post/{h.chat_id}/{h.message_id}",
            }
            for h in hits
        ],
    }


# ---------------------------------------------------------------------------
# Tool: read_post
# ---------------------------------------------------------------------------

def read_post(
    archive: Archive,
    parser_db: ParserDB,
    lib_db: LibrarianDB,
    *,
    chat_id: int,
    message_id: int,
    comment_limit: int = READ_POST_COMMENT_LIMIT,
    comment_offset: int = 0,
) -> dict:
    """
    Полный пост + комментарии + транскрипция, с пагинацией комментариев.

    Логика:
    1. Найти исходное сообщение по (chat_id, message_id).
    2. Если есть транскрипция — приложить текст.
    3. Если это пост канала — взять комментарии (post_id = message_id, is_comment=1).
    4. Если это комментарий — взять ответы (reply_to_msg_id = message_id).
    5. Пагинация комментариев limit/offset — для больших обсуждений.
    """
    msg = parser_db.get_message_by_message_id(chat_id, message_id)
    if msg is None:
        raise ToolError(
            f"Пост не найден: chat_id={chat_id}, message_id={message_id}. "
            "Возможно, архив построен с другим идентификатором чата."
        )

    # Транскрипция (если была голосовая/кружочек и STT отработал)
    transcription = None
    tr = parser_db.get_transcription(msg.message_id, msg.chat_id)
    if tr is not None:
        transcription = {
            "text": tr.text,
            "model_type": tr.model_type,
            "created_at": tr.created_at,
        }

    # Комментарии или ответы
    comments_raw: list = []
    is_channel_post = msg.post_id is None and not msg.is_comment
    if is_channel_post:
        # Пост канала → комментарии из linked-группы
        comments_raw = parser_db.get_comments_for_post(chat_id, msg.message_id)
    else:
        # Обычное сообщение → прямые ответы
        comments_raw = parser_db.get_replies_to(chat_id, msg.message_id)

    comment_limit = max(1, min(int(comment_limit or READ_POST_COMMENT_LIMIT), READ_POST_COMMENT_LIMIT))
    comment_offset = max(0, int(comment_offset or 0))
    total_comments = len(comments_raw)
    page = comments_raw[comment_offset:comment_offset + comment_limit]

    return {
        "post": {
            "internal_id": msg.id,
            "chat_id": msg.chat_id,
            "message_id": msg.message_id,
            "topic_id": msg.topic_id,
            "author": msg.author,
            "username": msg.username,
            "user_id": msg.user_id,
            "date": msg.date,
            "text": msg.text_or_empty(),
            "media_path": msg.media_path,
            "file_type": msg.file_type,
            "file_size": msg.file_size,
            "sender_type": msg.sender_type,
            "is_comment": msg.is_comment,
            "from_linked_group": msg.from_linked_group,
            "reply_to_msg_id": msg.reply_to_msg_id,
            "post_id": msg.post_id,
        },
        "transcription": transcription,
        "comments": {
            "total": total_comments,
            "limit": comment_limit,
            "offset": comment_offset,
            "items": [
                {
                    "internal_id": c.id,
                    "message_id": c.message_id,
                    "author": c.author,
                    "date": c.date,
                    "text": c.text_or_empty(),
                    "sender_type": c.sender_type,
                    "reply_to_msg_id": c.reply_to_msg_id,
                }
                for c in page
            ],
        },
    }


# ---------------------------------------------------------------------------
# Tool: stats
# ---------------------------------------------------------------------------

def stats(
    archive: Archive,
    parser_db: ParserDB,
    *,
    kind: str = "overview",
    top_authors: int = 20,
) -> dict:
    """
    Готовые числа. Никаких «смыслов» — только арифметика.

    kind:
        "overview" — общие счётчики + диапазон дат + топ авторов.
        "authors"  — только топ авторов (расширенный).
    """
    kind = (kind or "overview").lower()

    messages_count = parser_db.count_messages()
    transcriptions_count = parser_db.count_transcriptions()
    date_from, date_to = parser_db.date_range()

    if kind == "authors":
        top = parser_db.top_authors(limit=max(1, min(top_authors, 200)))
        return {
            "kind": "authors",
            "top_authors": [{"author": a, "count": c} for a, c in top],
        }

    # overview (default)
    top = parser_db.top_authors(limit=max(1, min(top_authors, 200)))
    return {
        "kind": "overview",
        "archive_id": archive.id,
        "archive_title": archive.passport.title,
        "schema_version": parser_db.schema_version(),
        "messages_count": messages_count,
        "transcriptions_count": transcriptions_count,
        "date_from": date_from,
        "date_to": date_to,
        "top_authors": [{"author": a, "count": c} for a, c in top],
    }


# ---------------------------------------------------------------------------
# Tool: whats_new
# ---------------------------------------------------------------------------

def whats_new(
    archive: Archive,
    parser_db: ParserDB,
    *,
    since: Optional[str] = None,
    limit: int = WHATS_NEW_DEFAULT_LIMIT,
) -> dict:
    """
    Сообщения, появившиеся после отметки since (ISO-строка).
    Если since=None — последние limit сообщений (по дате).
    """
    limit = max(1, min(int(limit or WHATS_NEW_DEFAULT_LIMIT), WHATS_NEW_MAX_LIMIT))

    if since:
        msgs = parser_db.messages_after(since, limit=limit)
    else:
        # Если since не задан — берём последние limit сообщений по дате.
        # Это не самый эффективный путь, но для архива разумного размера ок.
        # Используем тот же метод, но с минимальной датой.
        msgs = parser_db.messages_after("0000-00-00T00:00:00", limit=limit)
        msgs = list(reversed(msgs))  # от новых к старым

    return {
        "since": since,
        "count": len(msgs),
        "items": [
            {
                "internal_id": m.id,
                "chat_id": m.chat_id,
                "message_id": m.message_id,
                "author": m.author,
                "date": m.date,
                "text_preview": (m.text or "")[:200],
                "url": f"#post/{m.chat_id}/{m.message_id}",
            }
            for m in msgs
        ],
    }


# ---------------------------------------------------------------------------
# Tool: list_shelves
# ---------------------------------------------------------------------------

def list_shelves(archive: Archive, parser_db: ParserDB) -> dict:
    """
    Полки архива: типы контента, описания, периоды, количество.
    Источник — archive_passport.json; если полок там нет, выводим базовые
    на основе фактического содержимого parser.db.
    """
    passport_shelves = archive.passport.shelves
    date_from, date_to = parser_db.date_range()

    if passport_shelves:
        shelves = [
            {
                "kind": s.kind,
                "label": s.label,
                "count": s.count,
            }
            for s in passport_shelves
        ]
    else:
        # Фаб из фактических данных
        shelves = [
            {
                "kind": "messages",
                "label": "Сообщения",
                "count": parser_db.count_messages(),
            },
        ]
        tr_count = parser_db.count_transcriptions()
        if tr_count > 0:
            shelves.append({
                "kind": "transcriptions",
                "label": "Транскрипции",
                "count": tr_count,
            })

    return {
        "archive_id": archive.id,
        "archive_title": archive.passport.title,
        "chat_type": archive.passport.chat_type,
        "username": archive.passport.username,
        "date_from": date_from or archive.passport.date_from,
        "date_to": date_to or archive.passport.date_to,
        "shelves": shelves,
        "parser_version": archive.passport.parser_version,
        "exported_at": archive.passport.exported_at,
    }
