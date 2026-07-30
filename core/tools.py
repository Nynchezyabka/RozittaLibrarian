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

def _search_by_author(
    archive: Archive,
    parser_db: "ParserDB",
    nick: str,
    *,
    limit: int = 20,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
) -> dict:
    """
    Поиск по автору — отдельный режим, не FTS.

    BUG-003 (П6): пользователь вводит '@ник' в строку поиска.
    FTS ищет по тексту и находит только 2 сообщения (где ник упомянут).
    Этот режим делает SELECT из messages WHERE author LIKE '%ник%' и
    возвращает последние сообщения автора (по дате DESC).

    Возвращает ту же структуру, что и search(), но:
      - hits[].source = 'author_search' (UI может пометить «по автору»)
      - filters.author_search = True (для отладки)
      - count может быть > 20 (но в hits только limit)
    """
    from .parser_db import MessageRow  # only for typing
    # Нормализуем ник для LIKE: убираем @, ищем без учёта регистра.
    # Поддерживаем оба варианта хранения: с @ и без.
    nick_clean = nick.lstrip("@").strip()
    if not nick_clean:
        return {"query": "@" + nick, "filters": {"author_search": True},
                "count": 0, "hits": [], "groups": [], "groups_count": 0}

    # SQL: в parser.db НЕТ колонки `author` — она вычисляется в MessageRow.author
    # из `username` (с @) или из `user_id` (как user_<id>). Для поиска по нику
    # используем `username`: он хранится как 'nickname' (без @), MessageRow
    # добавляет @ на лету. Сравнение case-insensitive, проверяем оба варианта.
    # Лимит — limit + запас на случай, если нужны и посты, и комментарии
    # (пока отдаём просто последние по дате).
    cols = parser_db._message_cols()
    where = [
        "(username = ? COLLATE NOCASE OR username = ? COLLATE NOCASE "
        "  OR username LIKE ? COLLATE NOCASE OR username LIKE ? COLLATE NOCASE)",
    ]
    args: list = [
        nick_clean, "@" + nick_clean,
        "%" + nick_clean + "%", "%" + "@" + nick_clean + "%",
    ]
    if date_from is not None:
        where.append("date >= ?")
        args.append(date_from)
    if date_to is not None:
        where.append("date <= ?")
        args.append(date_to)

    sql = (
        f"SELECT {cols} FROM messages "
        f"WHERE " + " AND ".join(where) + " "
        f"ORDER BY date DESC LIMIT ?"
    )
    args.append(limit)

    with parser_db.cursor() as cur:
        cur.execute(sql, args)
        rows = cur.fetchall()

    hits: list[dict] = []
    for r in rows:
        msg = parser_db._row_to_message(r)
        text = (msg.text or "").strip()
        snippet = text[:300] + ("…" if len(text) > 300 else "")
        is_comment = bool(msg.is_comment)
        hits.append({
            "internal_id": msg.id,
            "chat_id": msg.chat_id,
            "message_id": msg.message_id,
            "source": "author_search",
            "author": msg.author,
            "date": msg.date,
            "snippet": snippet,
            "is_comment": is_comment,
            "post_message_id": msg.post_id,
            "url": (
                f"#/a/{archive.id}/m/{msg.post_id}?c={msg.message_id}"
                if is_comment and msg.post_id
                else f"#/a/{archive.id}/m/{msg.message_id}"
            ),
        })

    return {
        "query": "@" + nick,
        "filters": {
            "author_search": True,
            "nick": nick_clean,
            "date_from": date_from,
            "date_to": date_to,
        },
        "count": len(hits),
        "hits": hits,
        "groups": [],
        "groups_count": 0,
    }


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
    quota_per_kind: Optional[int] = None,
    # Г3: групповое ранжирование
    parser_db: Optional[ParserDB] = None,
    include_groups: bool = False,
    group_limit: int = 10,
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

    Б2 (librarian_статус.md): если `quota_per_kind` задан — поиск идёт
    двумя запросами (посты + комментарии) и сливается round-robin.
    На реальном архиве это критично: иначе материал автора тонет под
    комментариями. По умолчанию для UI — 5 на тип (4б: узкая выдача
    лучше широкой).

    Г3 (рекомендации_группы_источников.md): если `include_groups=True` и
    `parser_db` задан — в результат добавляются два поля:
      - `groups`: список GroupHit.to_dict(), отсортированных по числу
        совпавших сообщений (matched_count DESC, best_rank ASC,
        total_count DESC). Не более `group_limit` (по умолчанию 10).
      - `groups_count`: общее число групп с хотя бы одним совпадением
        (до обрезки group_limit).
    Если `include_groups=False` или `parser_db=None` — `groups=[]`,
    `groups_count=0`. Квоты (Б2) остаются; групповое ранжирование
    даёт порядок, а не заменяет наличие.
    """
    query = (query or "").strip()
    if not query:
        return {"query": "", "filters": {}, "count": 0, "hits": []}

    # Нормализация автора: пользователь вводит "MariaMariaButterfly",
    # а в БД author хранится как "@MariaMariaButterfly". Добавляем @,
    # если его нет и автор не содержит пробелов (значит это ник, а не имя).
    if author:
        a = author.strip()
        if a and not a.startswith("@") and " " not in a:
            author = "@" + a
        else:
            author = a or None

    limit = max(1, min(int(limit or SEARCH_MAX_RESULTS), SEARCH_MAX_RESULTS))

    # BUG-003 (П6): если запрос начинается с '@' — режим поиска по автору.
    # FTS ищет по тексту и не находит сообщения автора (его ник в тексте
    # его сообщений не упоминается). Для режима «по автору» делаем прямой
    # SELECT из parser.db: WHERE author LIKE '%ник%' LIMIT N.
    # Каждый результат помечается source='author_search', чтобы UI отличал.
    if query.startswith("@") and parser_db is not None:
        nick = query[1:].strip().lstrip("@").strip()
        if nick:
            return _search_by_author(
                archive, parser_db, nick,
                limit=limit, date_from=date_from, date_to=date_to,
            )

    hits = lib_db.search(
        query,
        limit=limit,
        author=author,
        date_from=date_from,
        date_to=date_to,
        source=source,
        snippet_size=SEARCH_SNIPPET_MAX,
        quota_per_kind=quota_per_kind,
    )

    # Г3: групповое ранжирование. Обратно-совместимо: если include_groups
    # выключен или parser_db не задан — отдаём пустые поля.
    groups_payload: list[dict] = []
    groups_count = 0
    if include_groups and parser_db is not None:
        try:
            from .groups import GroupsBuilder, rank_groups
            builder = GroupsBuilder(archive, parser_db)
            all_groups = builder.build_all()
            group_hits = rank_groups(hits, all_groups)
            groups_count = len(group_hits)
            groups_payload = [gh.to_dict() for gh in group_hits[:group_limit]]
        except Exception:
            # Группы — дополнение, не критика. Если упало — отдаём пустые,
            # основной поиск не страдает.
            groups_payload = []
            groups_count = 0

    return {
        "query": query,
        "filters": {
            "author": author,
            "date_from": date_from,
            "date_to": date_to,
            "source": source,
            "quota_per_kind": quota_per_kind,
            "include_groups": include_groups,
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
                "is_comment": h.is_comment,
                # post_message_id — для комментариев: message_id родительского поста.
                # UI использует его для навигации: клик по комментарию открывает
                # родительский пост с прокруткой к этому комментарию.
                "post_message_id": h.post_message_id,
                # URL-фрагмент для роутера. Если пост — /m/{message_id};
                # если комментарий — /m/{post_message_id}?c={message_id}.
                "url": (
                    f"#/a/{archive.id}/m/{h.post_message_id}?c={h.message_id}"
                    if h.is_comment and h.post_message_id
                    else f"#/a/{archive.id}/m/{h.message_id}"
                ),
            }
            for h in hits
        ],
        "groups": groups_payload,
        "groups_count": groups_count,
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
# Tool: get_message (UI-4 — ридер)
# ---------------------------------------------------------------------------

def get_message(
    archive: Archive,
    parser_db: ParserDB,
    *,
    message_id: int,
    chat_id: Optional[int] = None,
    comment_limit: int = READ_POST_COMMENT_LIMIT,
    comment_offset: int = 0,
) -> dict:
    """
    Полное сообщение для ридера одним запросом (UI-спец. §5, §9).

    Возвращает:
        post          — текст, автор, дата, медиа-инфо, флаг is_comment
        transcription — если есть голосовое сообщение с расшифровкой
        comments      — комментарии (для постов канала) или ответы (для обычных)
        neighbors     — prev/next по хронологии того же чата (только посты, не комментарии)
        telegram_link — t.me/{username}/{message_id}, если username есть в паспорте
        is_voice      — True, если у сообщения есть транскрипция (т.е. это была голосовая)

    chat_id: если None — берётся из паспорта архива (паспорт хранит один chat_id).
    Это упрощает URL ридера: #/a/{id}/m/{message_id} (без chat_id в пути).
    """
    # Если chat_id не передан — пробуем из паспорта
    if chat_id is None:
        chat_id = archive.passport.chat_id

    # BUG-004 (П8): если в паспорте chat_id отсутствует (частый случай для
    # реальных архивов МГ) — ищем сообщение только по message_id.
    # parser_db.get_message_by_message_id_only() уже существует для этого.
    # Если найдено — берём его реальный chat_id и продолжаем как обычно.
    # Это чинит «не удалось определить chat_id» при клике на результат поиска
    # на реальном архиве, когда URL ридера = #/a/{id}/m/{message_id} (без chat_id).
    if chat_id is None:
        fallback_msg = parser_db.get_message_by_message_id_only(int(message_id))
        if fallback_msg is None:
            raise ToolError(
                f"Сообщение не найдено по message_id={message_id}. "
                "chat_id в паспорте отсутствует, поиск по message_id во всех чатах "
                "ничего не дал. Возможно, архив построен некорректно."
            )
        chat_id = fallback_msg.chat_id
        msg = fallback_msg
    else:
        msg = parser_db.get_message_by_message_id(int(chat_id), int(message_id))
        if msg is None:
            raise ToolError(
                f"Сообщение не найдено: chat_id={chat_id}, message_id={message_id}."
            )

    # Транскрипция (если голосовое)
    transcription = None
    tr = parser_db.get_transcription(msg.message_id, msg.chat_id)
    if tr is not None:
        transcription = {
            "text": tr.text,
            "model_type": tr.model_type,
            "created_at": tr.created_at,
        }
    is_voice = transcription is not None

    # Комментарии или ответы
    comments_raw: list = []
    is_channel_post = msg.post_id is None and not msg.is_comment
    if is_channel_post:
        comments_raw = parser_db.get_comments_for_post(int(chat_id), msg.message_id)
    else:
        comments_raw = parser_db.get_replies_to(int(chat_id), msg.message_id)

    comment_limit = max(1, min(int(comment_limit or READ_POST_COMMENT_LIMIT), READ_POST_COMMENT_LIMIT))
    comment_offset = max(0, int(comment_offset or 0))
    total_comments = len(comments_raw)
    page = comments_raw[comment_offset:comment_offset + comment_limit]

    # Соседи по дате — только для навигации между постами (не комментариями)
    prev_msg, next_msg = parser_db.get_neighbors_by_date(
        int(chat_id), msg.message_id, msg.date
    )

    # Telegram-ссылка: t.me/{username}/{message_id}
    # username в паспорте хранится с '@' — убираем для URL.
    telegram_link = None
    uname = archive.passport.username
    if uname:
        clean = uname.lstrip("@")
        if clean:
            telegram_link = f"https://t.me/{clean}/{msg.message_id}"

    return {
        "archive_id": archive.id,
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
        "is_voice": is_voice,
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
                    "username": c.username,
                    "date": c.date,
                    "text": c.text_or_empty(),
                    "sender_type": c.sender_type,
                    "reply_to_msg_id": c.reply_to_msg_id,
                }
                for c in page
            ],
        },
        "neighbors": {
            "prev": {
                "message_id": prev_msg.message_id,
                "date": prev_msg.date,
            } if prev_msg else None,
            "next": {
                "message_id": next_msg.message_id,
                "date": next_msg.date,
            } if next_msg else None,
        },
        "telegram_link": telegram_link,
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
    shelf: Optional[str] = None,
) -> dict:
    """
    Сообщения, появившиеся после отметки since (ISO-строка).
    Если since=None — последние limit сообщений (по дате, от новых к старым).

    shelf (опционально): 'messages' (по умолчанию) или 'records'.
      - 'messages' — все последние сообщения.
      - 'records' — только голосовые сообщения с транскрипцией
        (использует latest_transcriptions; если транскрипций нет — пусто).

    ФИКС: раньше при since=None вызывался messages_after("0000-00-00T00:00:00")
    с ORDER BY date ASC — это давало limit САМЫХ СТАРЫХ, потом reversed() —
    старые в обратном порядке. Сейчас используется latest_messages() с
    ORDER BY date DESC — действительно последние.
    """
    limit = max(1, min(int(limit or WHATS_NEW_DEFAULT_LIMIT), WHATS_NEW_MAX_LIMIT))

    if since:
        msgs = parser_db.messages_after(since, limit=limit)
    elif shelf == "records":
        # Полка «Записи» — только сообщения с транскрипцией.
        # Если в БД нет transcriptions — вернётся пустой список (UI покажет
        # «На этой полке ничего нет»).
        msgs = parser_db.latest_transcriptions(limit=limit)
    else:
        # Полка «Сообщения» (или без указания полки) — последние сообщения.
        msgs = parser_db.latest_messages(limit=limit)

    return {
        "since": since,
        "shelf": shelf,
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
