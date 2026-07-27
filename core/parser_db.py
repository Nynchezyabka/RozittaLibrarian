"""
core/parser_db.py — read-only доступ к базе Parser.

Схема Parser (CLAUDE.md):
    messages: id, chat_id, message_id, topic_id, user_id, username, date,
              text, media_path, file_type, file_size, reply_to_msg_id,
              post_id, is_comment, from_linked_group, merge_group_id,
              merge_part_index, sender_type (schema v2)
    transcriptions: message_id, peer_id, text, model_type, created_at

PRAGMA user_version:
    v1 — без sender_type
    v2 — с sender_type TEXT DEFAULT 'user'

Строгое правило (librarian_рабочий_план.md §2.2): ни одной записи в базу
Parser. Открываем только read-only через file:...?mode=ro, чтобы даже баг
в коде не мог ничего испортить.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional


SCHEMA_V1 = 1
SCHEMA_V2 = 2
SUPPORTED_SCHEMAS = (SCHEMA_V1, SCHEMA_V2)


# ---------------------------------------------------------------------------
# DTO (простые плоские строки — для FTS и сниппетов)
# ---------------------------------------------------------------------------

@dataclass
class MessageRow:
    id: int                    # PK в таблице messages
    chat_id: int
    message_id: int
    topic_id: Optional[int]
    user_id: Optional[int]
    username: Optional[str]
    date: str
    text: Optional[str]
    media_path: Optional[str]
    file_type: Optional[str]
    file_size: Optional[int]
    reply_to_msg_id: Optional[int]
    post_id: Optional[int]
    is_comment: bool
    from_linked_group: bool
    sender_type: str           # "user" | "channel" | "deleted" (всегда есть, для v1 дефолт "user")

    @property
    def author(self) -> str:
        """Человекочитаемый автор: username, fallback на user_id."""
        if self.username:
            return self.username if self.username.startswith("@") else f"@{self.username}"
        if self.user_id:
            return f"user_{self.user_id}"
        return "anon"

    def text_or_empty(self) -> str:
        return (self.text or "").strip()


@dataclass
class TranscriptionRow:
    message_id: int
    peer_id: int
    text: str
    model_type: str
    created_at: str


# ---------------------------------------------------------------------------
# Reader
# ---------------------------------------------------------------------------

class ParserDB:
    """
    Read-only доступ к parser.db. Один экземпляр на архив.

    Использование:
        with ParserDB(path) as db:
            version = db.schema_version()
            for msg in db.iter_messages():
                ...
    """

    def __init__(self, path: Path | str):
        self.path = Path(path)
        if not self.path.exists():
            raise FileNotFoundError(f"Parser DB не найден: {self.path}")
        self._conn: Optional[sqlite3.Connection] = None

    # -- lifecycle ---------------------------------------------------------

    def open(self) -> "ParserDB":
        # mode=ro — физическая невозможность писать. URI обязателен.
        uri = f"file:{self.path.as_posix()}?mode=ro"
        self._conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        # Дополнительная защита: PRAGMA query_only. На mode=ro это избыточно,
        # но документирует намерение.
        self._conn.execute("PRAGMA query_only = ON")
        return self

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def __enter__(self) -> "ParserDB":
        return self.open()

    def __exit__(self, *exc) -> None:
        self.close()

    @contextmanager
    def cursor(self) -> Iterator[sqlite3.Cursor]:
        if self._conn is None:
            self.open()
        assert self._conn is not None
        cur = self._conn.cursor()
        try:
            yield cur
        finally:
            cur.close()

    # -- schema -----------------------------------------------------------

    def schema_version(self) -> int:
        """PRAGMA user_version: 0 если база без миграций, иначе v1/v2."""
        with self.cursor() as cur:
            cur.execute("PRAGMA user_version")
            return int(cur.fetchone()[0] or 0)

    def has_sender_type(self) -> bool:
        """True, если в messages есть колонка sender_type (schema v2)."""
        return self.schema_version() >= SCHEMA_V2

    def assert_supported(self) -> int:
        v = self.schema_version()
        if v not in SUPPORTED_SCHEMAS:
            # v0 трактуем как v1 (старые базы без миграций — допустимо).
            if v == 0:
                return SCHEMA_V1
            raise RuntimeError(
                f"Неподдерживаемая схема parser.db: user_version={v}. "
                f"Поддерживаются: {SUPPORTED_SCHEMAS}."
            )
        return v

    # -- queries ----------------------------------------------------------

    _COLS_V2 = (
        "id, chat_id, message_id, topic_id, user_id, username, date, text, "
        "media_path, file_type, file_size, reply_to_msg_id, post_id, "
        "is_comment, from_linked_group, sender_type"
    )
    _COLS_V1 = (
        "id, chat_id, message_id, topic_id, user_id, username, date, text, "
        "media_path, file_type, file_size, reply_to_msg_id, post_id, "
        "is_comment, from_linked_group"
    )

    def _message_cols(self) -> str:
        return self._COLS_V2 if self.has_sender_type() else self._COLS_V1

    def _row_to_message(self, r: sqlite3.Row) -> MessageRow:
        sender_type = r["sender_type"] if "sender_type" in r.keys() else "user"
        return MessageRow(
            id=int(r["id"]),
            chat_id=int(r["chat_id"]),
            message_id=int(r["message_id"]),
            topic_id=r["topic_id"],
            user_id=r["user_id"],
            username=r["username"],
            date=r["date"],
            text=r["text"],
            media_path=r["media_path"],
            file_type=r["file_type"],
            file_size=r["file_size"],
            reply_to_msg_id=r["reply_to_msg_id"],
            post_id=r["post_id"],
            is_comment=bool(r["is_comment"]),
            from_linked_group=bool(r["from_linked_group"]),
            sender_type=sender_type or "user",
        )

    def count_messages(self) -> int:
        with self.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM messages")
            return int(cur.fetchone()[0] or 0)

    def count_transcriptions(self) -> int:
        with self.cursor() as cur:
            cur.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='transcriptions'"
            )
            if cur.fetchone() is None:
                return 0
            cur.execute("SELECT COUNT(*) FROM transcriptions")
            return int(cur.fetchone()[0] or 0)

    def iter_messages(self, batch_size: int = 1000) -> Iterator[MessageRow]:
        """Ленивая выборка всех сообщений — для построения FTS-индекса."""
        cols = self._message_cols()
        with self.cursor() as cur:
            cur.execute(f"SELECT {cols} FROM messages ORDER BY id")
            while True:
                rows = cur.fetchmany(batch_size)
                if not rows:
                    return
                for r in rows:
                    yield self._row_to_message(r)

    def get_message_by_id(self, internal_id: int) -> Optional[MessageRow]:
        cols = self._message_cols()
        with self.cursor() as cur:
            cur.execute(f"SELECT {cols} FROM messages WHERE id = ?", (internal_id,))
            r = cur.fetchone()
            return self._row_to_message(r) if r else None

    def get_message_by_message_id(self, chat_id: int, message_id: int) -> Optional[MessageRow]:
        """Поиск по паре (chat_id, message_id) — это Telegram-координата поста."""
        cols = self._message_cols()
        with self.cursor() as cur:
            cur.execute(
                f"SELECT {cols} FROM messages WHERE chat_id = ? AND message_id = ? LIMIT 1",
                (chat_id, message_id),
            )
            r = cur.fetchone()
            return self._row_to_message(r) if r else None

    def get_messages_by_ids(self, internal_ids: list[int]) -> list[MessageRow]:
        if not internal_ids:
            return []
        cols = self._message_cols()
        placeholders = ",".join("?" * len(internal_ids))
        with self.cursor() as cur:
            cur.execute(
                f"SELECT {cols} FROM messages WHERE id IN ({placeholders})",
                internal_ids,
            )
            return [self._row_to_message(r) for r in cur.fetchall()]

    def get_comments_for_post(self, chat_id: int, post_message_id: int) -> list[MessageRow]:
        """Комментарии, привязанные к посту через post_id."""
        cols = self._message_cols()
        with self.cursor() as cur:
            cur.execute(
                f"SELECT {cols} FROM messages WHERE post_id = ? AND is_comment = 1 "
                f"ORDER BY date ASC",
                (post_message_id,),
            )
            return [self._row_to_message(r) for r in cur.fetchall()]

    def get_replies_to(self, chat_id: int, message_id: int) -> list[MessageRow]:
        """Прямые ответы на сообщение (через reply_to_msg_id)."""
        cols = self._message_cols()
        with self.cursor() as cur:
            cur.execute(
                f"SELECT {cols} FROM messages WHERE chat_id = ? AND reply_to_msg_id = ? "
                f"ORDER BY date ASC",
                (chat_id, message_id),
            )
            return [self._row_to_message(r) for r in cur.fetchall()]

    def get_transcription(self, message_id: int, peer_id: int) -> Optional[TranscriptionRow]:
        with self.cursor() as cur:
            cur.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='transcriptions'"
            )
            if cur.fetchone() is None:
                return None
            cur.execute(
                "SELECT message_id, peer_id, text, model_type, created_at "
                "FROM transcriptions WHERE message_id = ? AND peer_id = ?",
                (message_id, peer_id),
            )
            r = cur.fetchone()
            if not r:
                return None
            return TranscriptionRow(
                message_id=int(r["message_id"]),
                peer_id=int(r["peer_id"]),
                text=r["text"],
                model_type=r["model_type"],
                created_at=r["created_at"],
            )

    def date_range(self) -> tuple[Optional[str], Optional[str]]:
        with self.cursor() as cur:
            cur.execute("SELECT MIN(date), MAX(date) FROM messages")
            r = cur.fetchone()
            return (r[0], r[1]) if r else (None, None)

    def top_authors(self, limit: int = 20) -> list[tuple[str, int]]:
        """Топ авторов по числу сообщений. Только арифметика."""
        with self.cursor() as cur:
            cur.execute(
                "SELECT COALESCE(username, 'user_' || COALESCE(user_id, 'anon')) AS author, "
                "       COUNT(*) AS cnt "
                "FROM messages "
                "WHERE text IS NOT NULL AND TRIM(text) <> '' "
                "GROUP BY author "
                "ORDER BY cnt DESC "
                "LIMIT ?",
                (limit,),
            )
            return [(row[0], int(row[1])) for row in cur.fetchall()]

    def messages_after(self, since_iso: str, limit: int = 50) -> list[MessageRow]:
        cols = self._message_cols()
        with self.cursor() as cur:
            cur.execute(
                f"SELECT {cols} FROM messages WHERE date > ? "
                f"ORDER BY date ASC LIMIT ?",
                (since_iso, limit),
            )
            return [self._row_to_message(r) for r in cur.fetchall()]
