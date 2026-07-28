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
# v3: тот же формат, но _prepare_query теперь обрезает русские окончания
#     (стабильная эвристика из eval_fts.py: 62% → 88% recall на эталонном
#     наборе). Старые librarian.db при открытии автоматически перестраиваются.
INDEX_VERSION = 3


# ---------------------------------------------------------------------------
# Подготовка поискового запроса — порт из scripts/eval_fts.py
# (см. librarian_статус.md §Б1). Без этой обрезки FTS5 молча возвращает 0
# на запросе «зависти», потому что "зависти*" не матчит «зависть».
# ---------------------------------------------------------------------------

# Короткие служебные слова — отбрасываем до стемминга. Список намеренно
# не агрессивный: он убирает только явный шум, остальное прореживают
# требования MIN_TOKEN_LEN и токенизатор FTS5.
_STOPWORDS = frozenset("""
а без более больше будет будто бы был была были было быть в вам вас вдруг ведь
весь во вот впрочем все всего всех вы где да даже два для до другой его ее ей
если есть еще ещё же за здесь и из или им иногда их к как какая какой когда
конечно кто куда ли лучше между меня мне много может мои мой мы на над надо
наконец нас не него нее ней нельзя нет ни нибудь никогда ним них ничего но ну о
об один он она они опять от очень перед по под после потом потому почти при про
раз разве с сам свое своей свой себе себя сегодня сейчас со совсем так такой там
тебя тем теперь то тогда того тоже той только том тот ту тут ты у уж уже хорошо
хоть чего человек чем через что чтоб чтобы чуть эти этого этой этом этот эту я
мной мною нам ними тобой чём этим этих какие каким который которая которые
""".split())

MIN_TOKEN_LEN = 3

# Обрезка окончаний. Без неё префиксный поиск проваливается на падежах:
# вопрос «о зависти» -> "зависти*" НЕ находит слово «зависть» в тексте.
# Порядок важен: длинные окончания идут раньше коротких, чтобы снять
# «ость» вместо «и», а «ился» вместо «а». Сортировка по убыванию длины.
ENDINGS = sorted([
    "ами", "ями", "ого", "его", "ому", "ему", "ыми", "ими", "ует", "уют",
    "ает", "ают", "яет", "яют", "ить", "ать", "ять", "еть", "ыть", "ился",
    "илась", "ется", "ются", "ость", "ений", "ения", "аний", "ания",
    "ая", "яя", "ое", "ее", "ые", "ие", "ой", "ей", "ый", "ий", "ом", "ем",
    "ах", "ях", "ам", "ям", "ов", "ев", "ью", "ия", "ии", "ла", "ло", "ли",
    "а", "я", "о", "е", "ы", "и", "у", "ю", "ь", "й",
], key=len, reverse=True)

STEM_MIN = 4      # короче этого не режем — иначе «дом» → «до»
STEM_CAP = 6      # и в любом случае не длиннее — «зависти» → «завист»


def _stem(token: str, cap: int = STEM_CAP) -> str:
    """Грубая обрезка до основы. Не лингвистика, а рабочая эвристика."""
    if cap <= 0 or token.startswith("#"):
        return token
    t = token
    for e in ENDINGS:
        if t.endswith(e) and len(t) - len(e) >= STEM_MIN:
            t = t[: len(t) - len(e)]
            break
    return t[:cap] if len(t) > cap else t


def _tokenize_question(question: str) -> list[str]:
    """Разбить вопрос пользователя на поисковые токены.

    Шаги:
    - lowercase + ё→е (совпадает с тем, что делает FTS5 unicode61
      с remove_diacritics 2);
    - выкинуть пунктуацию;
    - оставить только токены длиннее MIN_TOKEN_LEN, не стоп-слова и
      не чистые числа.
    """
    import re
    q = (question or "").lower().replace("ё", "е")
    q = re.sub(r"[«»\"'(),.!?:;—–\-]", " ", q)
    raw = re.findall(r"[#\w]+", q, flags=re.UNICODE)
    out = []
    for t in raw:
        if len(t) < MIN_TOKEN_LEN or t in _STOPWORDS or t.isdigit():
            continue
        out.append(t)
    return out


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
        total_transcriptions_from_db = 0
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
                    total_transcriptions_from_db += 1
                cur.execute("COMMIT")
            except sqlite3.OperationalError as e:
                # transcriptions может не быть — это нормально
                if "no such table" in str(e).lower():
                    cur.execute("ROLLBACK")
                else:
                    cur.execute("ROLLBACK")
                    raise

        # Б3: транскрипции из файлов — второй источник индекса.
        # Transcriber пишет расшифровки как .md-файлы, связь «пост → файл»
        # держит 00_Индекс.md (см. librarian_статус.md §Б3, CLAUDE.md п. 2а).
        # Берём все файловые расшифровки, которых ЕЩЁ НЕТ в индексе (по message_id).
        # Это покрывает: полностью пустую таблицу (как в архиве МГ), частично
        # заполненную (часть в базе, часть файлами) и обычный случай (всё в базе).
        total_transcriptions_from_files = 0
        if parser_db.path.parent.exists():
            file_rows = self._load_index_transcripts(parser_db.path.parent)
            if file_rows:
                # Какие message_id уже в индексе как transcription?
                with self.cursor() as cur:
                    cur.execute(
                        "SELECT message_id, chat_id FROM fts_doc "
                        "WHERE source = 'message' LIMIT 1"
                    )
                    r = cur.fetchone()
                    fallback_chat_id = int(r["chat_id"]) if r and r["chat_id"] is not None else 0
                    cur.execute(
                        "SELECT message_id FROM fts_doc WHERE source = 'transcription'"
                    )
                    seen_msg_ids = {int(r["message_id"]) for r in cur.fetchall()}
                with self.cursor() as cur:
                    cur.execute("BEGIN")
                    try:
                        for post_id, name, text in file_rows:
                            if post_id in seen_msg_ids:
                                continue
                            seen_msg_ids.add(post_id)
                            cur.execute(
                                "INSERT INTO fts_doc "
                                "(source, internal_id, chat_id, message_id, author, date, snippet) "
                                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                                (
                                    "transcription",
                                    # синтетический id, чтобы не сталкиваться с messages
                                    (int(post_id) << 32) | 0xDEAD,
                                    fallback_chat_id,
                                    int(post_id),
                                    name,  # имя файла вместо автора — для отладки
                                    None,
                                    text[:300],
                                ),
                            )
                            doc_rowid = cur.lastrowid
                            cur.execute(
                                "INSERT INTO fts_text(rowid, text) VALUES (?, ?)",
                                (doc_rowid, text),
                            )
                            total_transcriptions_from_files += 1
                            done += 1
                        cur.execute("COMMIT")
                    except Exception:
                        cur.execute("ROLLBACK")
                        raise

        self._set_meta("index_version", str(INDEX_VERSION))
        self._set_meta("built_at", str(int(time.time())))

        seconds = round(time.monotonic() - start, 2)
        return {
            "messages": total_messages,
            "transcriptions": total_transcriptions_from_db + total_transcriptions_from_files,
            "transcriptions_from_db": total_transcriptions_from_db,
            "transcriptions_from_files": total_transcriptions_from_files,
            "seconds": seconds,
        }

    # ------------------------------------------------------------------
    # Б3: парсер 00_Индекс.md — вынесен в метод, чтобы переиспользовать
    # при переиндексации и в юнит-тестах. Логика портирована из
    # scripts/eval_fts.py:load_index_transcripts.
    # ------------------------------------------------------------------

    # Регэксп для markdown-ссылок [текст](цель)
    import re as _re_module  # локальный импорт, чтобы не тащить наверх
    _RE_MD_LINK = _re_module.compile(r"\[([^\]]*)\]\(([^)]+)\)")

    @classmethod
    def _load_index_transcripts(cls, folder: Path,
                                index_name: str = "00_Индекс.md"
                                ) -> list[tuple[int, str, str]]:
        """Прочитать расшифровки с диска по 00_Индекс.md.

        Возвращает [(post_id, имя_файла, полный_текст), ...].

        Формат 00_Индекс.md (см. make_demo_archive.write_transcript_index):
        pipe-таблица с колонками; первая ячейка-цифра — это post_id,
        где-то в строке должна быть md-ссылка [..](файл.md).
        Если 00_Индекс.md нет — пробуем *Индекс*.md (как в eval_fts).
        """
        folder = Path(folder)
        idx = folder / index_name
        if not idx.exists():
            cands = sorted(folder.glob("*Индекс*.md"))
            if not cands:
                return []
            idx = cands[0]

        rows: list[tuple[int, str, str]] = []
        for line in idx.read_text(encoding="utf-8", errors="replace").splitlines():
            if not line.startswith("|"):
                continue
            cells = [c.strip() for c in line.strip("|").split("|")]
            # Первое число в строке — это post_id
            post: Optional[int] = None
            for c in cells:
                if c.isdigit():
                    post = int(c)
                    break
            if post is None:
                continue
            # Ищем markdown-ссылку на .md-файл
            link: Optional[str] = None
            for c in cells:
                for m in cls._RE_MD_LINK.findall(c):
                    target = m[1]
                    if target.lower().endswith(".md") or "transcript" in target.lower():
                        link = target
                        break
                if link:
                    break
            if not link:
                continue
            # unquote для кириллицы и пробелов в имени файла
            from urllib.parse import unquote
            p = folder / unquote(link)
            if not p.exists():
                continue
            text = p.read_text(encoding="utf-8", errors="replace")
            rows.append((post, p.name, text))
        return rows

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
        quota_per_kind: Optional[int] = None,
    ) -> list[SearchHit]:
        """FTS5-поиск с квотами по типу источника.

        Б1 (librarian_статус.md): запрос проходит через _prepare_query,
        которая обрезает русские окончания (`зависти` → `"завист"*`).

        Б2: quota_per_kind — если задан, поиск идёт двумя запросами:
        top-N среди `is_comment=0` (посты/сообщения автора) ПЛЮС top-N среди
        `is_comment=1` (комментарии), потом слияние. Это критично на реальном
        архиве: при 16 177 комментариях против 102 постов автора материал
        автора не попадает в топ-10 ни разу при общей выдаче. Замер 28.07:
        0/7 при общей выдаче, 7/7 при квотах.

        Если `quota_per_kind` не задан — выдача общая (как раньше), для
        обратной совместимости.

        Фильтры:
        - author: точное совпадение по `author`
        - date_from/date_to: строковое сравнение ISO-дат
        - source: "message" | "transcription" — оставить только один тип
        """
        fts_query = self._prepare_query(query)
        if not fts_query:
            return []

        # Б2: если задана квота — два узких запроса и слияние.
        if quota_per_kind is not None and source is None:
            posts_hits = self._search_one(
                fts_query,
                limit=quota_per_kind,
                author=author, date_from=date_from, date_to=date_to,
                source="message", is_comment=False,
                snippet_size=snippet_size,
            )
            comments_hits = self._search_one(
                fts_query,
                limit=quota_per_kind,
                author=author, date_from=date_from, date_to=date_to,
                source="message", is_comment=True,
                snippet_size=snippet_size,
            )
            # Если есть транскрипции — отдельная квота, обычно меньше
            transcripts_hits = self._search_one(
                fts_query,
                limit=max(2, quota_per_kind // 2),
                author=author, date_from=date_from, date_to=date_to,
                source="transcription", is_comment=None,
                snippet_size=snippet_size,
            )
            # Слияние round-robin: пост, коммент, пост, коммент…
            # Учитываем общий `limit`.
            merged: list[SearchHit] = []
            seen_ids: set[int] = set()
            for rank in range(max(len(posts_hits), len(comments_hits))):
                if rank < len(posts_hits) and posts_hits[rank].rowid not in seen_ids:
                    merged.append(posts_hits[rank])
                    seen_ids.add(posts_hits[rank].rowid)
                if rank < len(comments_hits) and comments_hits[rank].rowid not in seen_ids:
                    merged.append(comments_hits[rank])
                    seen_ids.add(comments_hits[rank].rowid)
                if len(merged) >= limit:
                    break
            # Транскрипции добавляем в конец — у них отдельная метка
            for h in transcripts_hits:
                if h.rowid not in seen_ids and len(merged) < limit:
                    merged.append(h)
                    seen_ids.add(h.rowid)
            return merged[:limit]

        # Общая выдача — старый путь (когда квота не задана или указан source).
        return self._search_one(
            fts_query,
            limit=limit,
            author=author, date_from=date_from, date_to=date_to,
            source=source, is_comment=None,
            snippet_size=snippet_size,
        )

    def _search_one(
        self,
        fts_query: str,
        *,
        limit: int,
        author: Optional[str],
        date_from: Optional[str],
        date_to: Optional[str],
        source: Optional[str],
        is_comment: Optional[bool],
        snippet_size: int,
    ) -> list[SearchHit]:
        """Один FTS5-запрос с опциональным фильтром по типу источника."""
        # Внимание на порядок ? в SQL: snippet_size (в SELECT) идёт ПЕРЕД
        # fts_query (в WHERE). Поэтому args собираем в порядке появления ?.
        where = ["fts_text MATCH ?"]
        args: list = [snippet_size // 6, fts_query]

        if source is not None:
            where.append("d.source = ?")
            args.append(source)
        if is_comment is not None:
            where.append("d.is_comment = ?")
            args.append(1 if is_comment else 0)

        sql = (
            "SELECT d.rowid, d.source, d.internal_id, d.chat_id, "
            "       d.message_id, d.author, d.date, "
            "       d.is_comment, d.post_message_id, "
            "       snippet(fts_text, 0, '<<H>>', '<</H>>', '…', ?) AS snip "
            "FROM fts_text JOIN fts_doc d ON d.rowid = fts_text.rowid "
            "WHERE " + " AND ".join(where) + " "
            "ORDER BY rank "
            "LIMIT ?"
        )
        # *3 — запас на пост-фильтрацию по author/date
        args.append(limit * 3)

        with self.cursor() as cur:
            cur.execute(sql, args)
            rows = cur.fetchall()

        hits: list[SearchHit] = []
        for r in rows:
            if author and r["author"] != author:
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
        """Подготовка FTS5-запроса: вопрос пользователя → OR из префиксных термов.

        Эвристика: токенизировать (lowercase + ё→е + выкинуть стоп-слова),
        обрезать русские окончания (`stem()`), каждый терм обернуть в
        `"стем"*`. Без обрезки «зависти» не находит «зависть» — проверено
        замером 28.07: 62% → 88% recall.

        Если пользователь уже передал FTS-синтаксис (кавычки, AND/OR/NEAR) —
        не вмешиваемся: это расширенный режим для разработчика.
        """
        raw = (raw or "").strip()
        if not raw:
            return ""
        # Расширенный режим: пользователь сам знает, что делает.
        if '"' in raw or " NEAR " in raw or " AND " in raw or " OR " in raw:
            return raw
        tokens = _tokenize_question(raw)
        if not tokens:
            return ""
        parts, seen = [], set()
        for t in tokens:
            safe = _stem(t).replace('"', "")
            if safe and safe not in seen:
                seen.add(safe)
                parts.append(f'"{safe}"*')
        return " OR ".join(parts)

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
