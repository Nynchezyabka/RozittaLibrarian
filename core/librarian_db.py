"""
core/librarian_db.py — собственная база Librarian и FTS5-индекс.

Правила (librarian_рабочий_план.md §2):
- Своя база `librarian.db` лежит рядом с parser.db в папке архива.
- FTS5 строится поверх messages.text + transcriptions.text.
- Токенизатор unicode61 (русская морфология — префиксными формами, см. §7).
- Ответы Librarian никогда не попадают в индекс (§2.5) — здесь это
  обеспечивается архитектурно: таблица fts_doc содержит только источники
  типа "message" и "transcription".

Схема librarian.db:
    fts_doc (
        rowid,
        source      TEXT,    -- "message" | "transcription"
        internal_id INTEGER, -- PK в messages.id (для message) или
                             -- конкатенация (message_id, peer_id) для transcription
        chat_id     INTEGER,
        message_id  INTEGER, -- Telegram-координата (для ссылки)
        author      TEXT,
        date        TEXT,
        snippet     TEXT     -- текст, который индексируем (для реконструкции
                             -- сниппета без повторного открытия parser.db)
    )
    fts_doc USING fts5 (
        text,
        content='',          -- external-content-table НЕ используем: parser.db
                             -- read-only и не наш, проще держать копию
        tokenize='unicode61 remove_diacritics 2'
    )

    meta (key TEXT PRIMARY KEY, value TEXT)  -- версия индекса, дата сборки, и т.д.
"""
from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional

from .parser_db import ParserDB, MessageRow


LIBRARIAN_DB_FILENAME = "librarian.db"
# v1: исходная схема (source, internal_id, chat_id, message_id, author, date, snippet)
# v2: добавлены is_comment, post_message_id — для метки «↳ в комментарии» в поиске
#     и навигации из поиска-по-комментарию в ридер родительского поста.
INDEX_VERSION = 2


# ---------------------------------------------------------------------------
# DTO
# ---------------------------------------------------------------------------

@dataclass
class FTSDoc:
    """Один документ в FTS-индексе (message или transcription)."""
    rowid: int
    source: str           # "message" | "transcription"
    internal_id: int
    chat_id: int
    message_id: int
    author: str
    date: str
    snippet: str          # сохранённый короткий текст (для быстрого показа)
    is_comment: bool = False        # v2: сообщение является комментарием к посту
    post_message_id: Optional[int] = None  # v2: message_id родительского поста (для комментариев)


@dataclass
class SearchHit:
    """Результат search() — одно попадание."""
    rowid: int
    source: str
    internal_id: int
    chat_id: int
    message_id: int
    author: str
    date: str
    snippet: str          # подсвеченный сниппет из FTS5 (≤ 300 символов)
    is_comment: bool = False
    post_message_id: Optional[int] = None  # для комментариев — message_id родительского поста


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------

class LibrarianDB:
    """
    Своя база Librarian. Один экземпляр на архив.
    Создаёт librarian.db рядом с parser.db, строит FTS5-индекс.
    """

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self._conn: Optional[sqlite3.Connection] = None

    # -- lifecycle --------------------------------------------------------

    def open(self) -> "LibrarianDB":
        # Своя база — rw. check_same_thread=False: вебсокеты гоняют запросы
        # из разных корутин одного потока, но мы и так сериализуем через
        # единый connection (sqlite3.Connection сам потокобезопасен в одном
        # потоке; для разных потоков нужен свой connection — пока не требуется).
        self._conn = sqlite3.connect(
            str(self.path),
            check_same_thread=False,
            isolation_level=None,  # autocommit — индексация в одну транзакцию
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.execute("PRAGMA synchronous = NORMAL")
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._ensure_schema()
        return self

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def __enter__(self) -> "LibrarianDB":
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

    def _ensure_schema(self) -> None:
        with self.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS meta (
                    key   TEXT PRIMARY KEY,
                    value TEXT
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS fts_doc (
                    rowid        INTEGER PRIMARY KEY AUTOINCREMENT,
                    source       TEXT NOT NULL,
                    internal_id  INTEGER NOT NULL,
                    chat_id      INTEGER,
                    message_id   INTEGER,
                    author       TEXT,
                    date         TEXT,
                    snippet      TEXT,
                    UNIQUE(source, internal_id)
                )
                """
            )
            # v2: добавляем колонки, если их нет (для баз, созданных в v1).
            # ALTER TABLE ADD COLUMN не имеет IF NOT EXISTS в SQLite — через try/except.
            for col, decl in [
                ("is_comment",       "INTEGER NOT NULL DEFAULT 0"),
                ("post_message_id",  "INTEGER"),
            ]:
                try:
                    cur.execute(f"ALTER TABLE fts_doc ADD COLUMN {col} {decl}")
                except sqlite3.OperationalError:
                    pass  # колонка уже есть
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_fts_doc_internal
                ON fts_doc(source, internal_id)
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_fts_doc_message_coord
                ON fts_doc(chat_id, message_id)
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_fts_doc_post
                ON fts_doc(post_message_id) WHERE post_message_id IS NOT NULL
                """
            )

            # FTS5 — отдельная виртуальная таблица, связанная по rowid.
            # Без content='' — FTS5 сам хранит текст (standalone), и snippet()
            # может его вернуть. С content='' таблица становится contentless,
            # и snippet() всегда пустой.
            try:
                cur.execute(
                    """
                    CREATE VIRTUAL TABLE IF NOT EXISTS fts_text
                    USING fts5(
                        text,
                        tokenize='unicode61 remove_diacritics 2'
                    )
                    """
                )
            except sqlite3.OperationalError as e:
                if "fts5" in str(e).lower():
                    raise RuntimeError(
                        "FTS5 недоступен в этой сборке SQLite. "
                        "Переустановите Python с поддержкой FTS5."
                    ) from e
                raise

    # -- index management -------------------------------------------------

    def _get_meta(self, key: str) -> Optional[str]:
        with self.cursor() as cur:
            cur.execute("SELECT value FROM meta WHERE key = ?", (key,))
            r = cur.fetchone()
            return r[0] if r else None

    def _set_meta(self, key: str, value: str) -> None:
        with self.cursor() as cur:
            cur.execute(
                "INSERT INTO meta(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    def index_version(self) -> int:
        v = self._get_meta("index_version")
        return int(v) if v else 0

    def is_built(self) -> bool:
        return self.index_version() == INDEX_VERSION and self.doc_count() > 0

    def doc_count(self) -> int:
        with self.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM fts_doc")
            return int(cur.fetchone()[0] or 0)

    # -- build ------------------------------------------------------------

    def build_index(self, parser_db: ParserDB, progress_cb=None) -> dict:
        """
        Полная перестройка индекса из parser.db.
        Возвращает статистику: {messages, transcriptions, seconds}.
        progress_cb(done, total) — необязательный колбэк для UI/логов.
        """
        start = time.monotonic()
        # Полный сброс — гарантия консистентности.
        with self.cursor() as cur:
            cur.execute("DELETE FROM fts_doc")
            cur.execute("DELETE FROM fts_text")

        total_messages = parser_db.count_messages()
        total_transcriptions = parser_db.count_transcriptions()
        total = total_messages + total_transcriptions
        done = 0

        # Сообщения
        with self.cursor() as cur:
            cur.execute("BEGIN")
            try:
                for msg in parser_db.iter_messages():
                    text = msg.text_or_empty()
                    if not text:
                        done += 1
                        continue
                    # v2: для комментариев post_id указывает на родительский пост.
                    # У постов (is_comment=0) post_id обычно NULL.
                    is_comment = 1 if msg.is_comment else 0
                    post_mid = msg.post_id if msg.is_comment else None
                    cur.execute(
                        "INSERT INTO fts_doc "
                        "(source, internal_id, chat_id, message_id, author, date, snippet, "
                        " is_comment, post_message_id) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            "message",
                            msg.id,
                            msg.chat_id,
                            msg.message_id,
                            msg.author,
                            msg.date,
                            text[:300],
                            is_comment,
                            post_mid,
                        ),
                    )
                    doc_rowid = cur.lastrowid
                    cur.execute("INSERT INTO fts_text(rowid, text) VALUES (?, ?)",
                                (doc_rowid, text))
                    done += 1
                    if progress_cb and done % 500 == 0:
                        progress_cb(done, total)
                cur.execute("COMMIT")
            except Exception:
                cur.execute("ROLLBACK")
                raise

        # Транскрипции (если таблица есть)
        with self.cursor() as cur:
            cur.execute("BEGIN")
            try:
                cur.execute(
                    "SELECT message_id, peer_id, text, model_type, created_at "
                    "FROM transcriptions"
                )
                for r in cur.fetchall():
                    text = (r["text"] or "").strip()
                    if not text:
                        done += 1
                        continue
                    # internal_id — синтетический: хэш от (message_id, peer_id)
                    synth_id = (int(r["message_id"]) << 32) | (int(r["peer_id"]) & 0xFFFFFFFF)
                    cur.execute(
                        "INSERT INTO fts_doc "
                        "(source, internal_id, chat_id, message_id, author, date, snippet) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (
                            "transcription",
                            synth_id,
                            int(r["peer_id"]),
                            int(r["message_id"]),
                            "[transcription]",
                            r["created_at"],
                            text[:300],
                        ),
                    )
                    doc_rowid = cur.lastrowid
                    cur.execute("INSERT INTO fts_text(rowid, text) VALUES (?, ?)",
                                (doc_rowid, text))
                    done += 1
                cur.execute("COMMIT")
            except sqlite3.OperationalError as e:
                # transcriptions может не быть — это нормально
                if "no such table" in str(e).lower():
                    cur.execute("ROLLBACK")
                else:
                    cur.execute("ROLLBACK")
                    raise

        self._set_meta("index_version", str(INDEX_VERSION))
        self._set_meta("built_at", str(int(time.time())))

        seconds = round(time.monotonic() - start, 2)
        return {
            "messages": total_messages,
            "transcriptions": total_transcriptions,
            "seconds": seconds,
        }

    # -- queries ----------------------------------------------------------

    def search(
        self,
        query: str,
        *,
        limit: int = 20,
        author: Optional[str] = None,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
        source: Optional[str] = None,        # "message" | "transcription"
        snippet_size: int = 300,
    ) -> list[SearchHit]:
        """
        FTS5-поиск. query проходит через _prepare_query — превращает
        «обесценива» → «обесценива*» для префиксных форм.
        """
        fts_query = self._prepare_query(query)
        if not fts_query:
            return []

        # Базовый FTS5-запрос с подсветкой и лимитом сниппета.
        with self.cursor() as cur:
            cur.execute(
                "SELECT d.rowid, d.source, d.internal_id, d.chat_id, "
                "       d.message_id, d.author, d.date, "
                "       d.is_comment, d.post_message_id, "
                "       snippet(fts_text, 0, '<<H>>', '<</H>>', '…', ?) AS snip "
                "FROM fts_text JOIN fts_doc d ON d.rowid = fts_text.rowid "
                "WHERE fts_text MATCH ? "
                "ORDER BY rank "
                "LIMIT ?",
                (snippet_size // 6, fts_query, limit * 3),  # *3 для постфильтров
            )
            rows = cur.fetchall()

        hits: list[SearchHit] = []
        for r in rows:
            if author and r["author"] != author:
                continue
            if source and r["source"] != source:
                continue
            if date_from and r["date"] and r["date"] < date_from:
                continue
            if date_to and r["date"] and r["date"] > date_to:
                continue
            snip = (r["snip"] or "").strip()
            if len(snip) > snippet_size:
                snip = snip[:snippet_size].rstrip() + "…"
            hits.append(SearchHit(
                rowid=int(r["rowid"]),
                source=r["source"],
                internal_id=int(r["internal_id"]),
                chat_id=int(r["chat_id"]) if r["chat_id"] is not None else 0,
                message_id=int(r["message_id"]) if r["message_id"] is not None else 0,
                author=r["author"] or "",
                date=r["date"] or "",
                snippet=snip,
                is_comment=bool(r["is_comment"]),
                post_message_id=int(r["post_message_id"]) if r["post_message_id"] is not None else None,
            ))
            if len(hits) >= limit:
                break
        return hits

    @staticmethod
    def _prepare_query(raw: str) -> str:
        """
        Подготовка FTS5-запроса:
        - разбить на токены по пробелам;
        - каждый токен превратить в префиксный (token*);
        - не пытаемся парсить операторы AND/OR/NEAR — пользовательские
          запросы обычно простые.

        Это покрывает ~90% русской морфологии (§7 риска), без лемматизатора.
        """
        raw = (raw or "").strip()
        if not raw:
            return ""
        # Если пользователь уже использует синтаксис FTS ("" или *), не вмешиваемся.
        if '"' in raw or " NEAR " in raw or " AND " in raw or " OR " in raw:
            return raw
        tokens = []
        for tok in raw.split():
            tok = tok.strip('"*')
            if not tok:
                continue
            # Простой префиксный токен. Кавычки не нужны — токен без пробелов
            # и так воспринимается как одно слово.
            tokens.append(f"{tok}*")
        return " ".join(tokens)

    def get_doc_by_rowid(self, rowid: int) -> Optional[FTSDoc]:
        with self.cursor() as cur:
            cur.execute(
                "SELECT rowid, source, internal_id, chat_id, message_id, "
                "       author, date, snippet, is_comment, post_message_id "
                "FROM fts_doc WHERE rowid = ?",
                (rowid,),
            )
            r = cur.fetchone()
            if not r:
                return None
            return FTSDoc(
                rowid=int(r["rowid"]),
                source=r["source"],
                internal_id=int(r["internal_id"]),
                chat_id=int(r["chat_id"]) if r["chat_id"] is not None else 0,
                message_id=int(r["message_id"]) if r["message_id"] is not None else 0,
                author=r["author"] or "",
                date=r["date"] or "",
                snippet=r["snippet"] or "",
                is_comment=bool(r["is_comment"]),
                post_message_id=int(r["post_message_id"]) if r["post_message_id"] is not None else None,
            )

    def get_doc_by_message_coord(
        self, chat_id: int, message_id: int
    ) -> Optional[FTSDoc]:
        """Поиск документа по Telegram-координате (для read_post)."""
        with self.cursor() as cur:
            cur.execute(
                "SELECT rowid, source, internal_id, chat_id, message_id, "
                "       author, date, snippet, is_comment, post_message_id "
                "FROM fts_doc "
                "WHERE chat_id = ? AND message_id = ? LIMIT 1",
                (chat_id, message_id),
            )
            r = cur.fetchone()
            if not r:
                return None
            return FTSDoc(
                rowid=int(r["rowid"]),
                source=r["source"],
                internal_id=int(r["internal_id"]),
                chat_id=int(r["chat_id"]) if r["chat_id"] is not None else 0,
                message_id=int(r["message_id"]) if r["message_id"] is not None else 0,
                author=r["author"] or "",
                date=r["date"] or "",
                snippet=r["snippet"] or "",
                is_comment=bool(r["is_comment"]),
                post_message_id=int(r["post_message_id"]) if r["post_message_id"] is not None else None,
            )

    # ------------------------------------------------------------------
    # v2: top_terms — топ-термины из FTS5-словаря для чипов-примеров
    # ------------------------------------------------------------------

    # Кэш стоп-слов: союзами, предлогами, местоимениями и т.п. — отбрасываем.
    # Список намеренно короткий: задача — убрать только явный мусор, остальное
    # отфильтруется требованиями min_len и алфавитностью.
    _STOPWORDS_RU = frozenset({
        # местоимения
        "который","которая","которое","которые","этот","этого","эта","это","эти",
        "тот","та","то","те","такой","такая","такое","такие","мой","моя","моё",
        "мои","твой","твоя","твоё","твои","свой","своя","своё","свои","наш","наш",
        "ваш","их","его","её","их","себя","себе","собой","меня","тебя","его","её",
        # глаголы-связки и вспомогательные
        "быть","был","была","было","были","есть","будет","будут","мочь","мог",
        "могла","могло","могли","может","могут","должен","должна","должно",
        # предлоги и союзы (unicode61 не отделяет их как отдельную категорию,
        # но они часто оказываются в топе частот)
        "что","чтобы","как","так","но","или","ибо","ли","же","бы","тоже","также",
        "только","ещё","уже","когда","где","куда","откуда","почему","зачем",
        "если","чтобы","хотя","потому","поэтому","при","про","через","между",
        "перед","без","для","над","под","из-за","из-под","от","до","об","при",
        # наречия времени/места
        "там","тут","здесь","тогда","сейчас","потом","сначала","опять","всегда",
        "никогда","часто","иногда","обычно",
        # частицы
        "бы","ли","же","ведь","вот","это","эти","то","не","ни","нет","да",
        # прочие частые
        "один","два","три","раз","время","дело","человек","люди","всё","ничего",
    })

    def top_terms(self, limit: int = 8, min_len: int = 6) -> list[str]:
        """
        Топ-N частотных терминов архива — для чипов-примеров под строкой
        поиска (UI-спец. §3). Использует fts5vocab в режиме 'row' —
        документная частота (в скольких документах термин встречается).

        Фильтры:
        - длина >= min_len (по умолчанию 6 — отсекает «и», «но», «как», «или»);
        - только буквы (без цифр и пунктуации);
        - не входит в стоп-слова.

        Сортировка композитная: сначала по частоте (DESC), при равенстве —
        по длине (DESC, длинные слова информативнее), затем по алфавиту.
        На маленьких архивах, где у всех слов doc_freq=1, это даёт длинные
        осмысленные термины вместо коротких стоп-слов.

        Возвращает список терминов (строк), без частот.
        """
        limit = max(1, min(int(limit or 8), 50))
        with self.cursor() as cur:
            # Создаём vocab-таблицу, если её нет. 'row' = (term, doc, col).
            cur.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS fts_vocab "
                "USING fts5vocab(fts_text, 'row')"
            )
            # Группируем по term — COUNT(*) даёт документную частоту.
            # Фильтр по длине и алфавиту — прямо в SQL, остальное (стоп-слова)
            # проверяем в Python (список короткий).
            cur.execute(
                "SELECT term, COUNT(*) AS doc_freq "
                "FROM fts_vocab "
                "WHERE length(term) >= ? "
                "GROUP BY term "
                "ORDER BY doc_freq DESC, length(term) DESC, term ASC "
                "LIMIT ?",
                (min_len, limit * 10),  # ×10 — запас для стоп-слов
            )
            rows = cur.fetchall()

        result: list[str] = []
        for r in rows:
            term = r["term"] or ""
            # Только буквы (Unicode). Цифры/пунктуация/смесь — отбрасываем.
            if not term.isalpha():
                continue
            if term.lower() in self._STOPWORDS_RU:
                continue
            result.append(term)
            if len(result) >= limit:
                break
        return result
