"""
core/citations.py — единый формат ссылок на источники (Г6).

Группа (Г1+Г2) — это представление, сообщение — единица поиска. Ссылка
(Г6) — это способ указать на конкретное место в архиве: пост целиком,
конкретный комментарий, ветку участника, главу книги. Формат вводится
СЕЙЧАС, до этапа 3 (Верификатор), чтобы потом менять только регулярку
Верификатора и подсветку в UI — формат будет уже зафиксирован.

Поддерживаемые виды ссылок (Г6):

    [пост 92]                          — пост целиком
    [пост 92, комментарий №14457]      — конкретное сообщение
    [пост 92, комментарий 14457]       — то же, без №
    [пост 149, ветка tatisimonenko]    — участник-тред
    [пост 149, ветка @tatisimonenko]   — то же, с @
    [пост 850, расшифровка]            — расшифровка голосового поста
    [расшифровка 850]                  — короткая форма (без «пост»)
    [книга «Название», гл. 3 › Практика отказа]  — глава книги (Г7)
    [полка messages]                   — полка (cycle)
    [месяц 2024-09]                    — календарная группа

Каноническая форма (format_citation) — единственная: для модели и UI
все ссылки нормализуются. Парсер (parse_citation) принимает варианты
(с/без №, с/без @), форматтер выдаёт канон.

Архитектурные принципы:
  - Парсер СТРОГИЙ: если строка не похожа на ссылку — None. Никаких
    эвристик «может, имелось в виду…». Верификатор (этап 3) будет
    полагаться на это.
  - Форматтер ДЕТЕРМИНИРОВАННЫЙ: один Citation → одна строка. Это
    позволяет сравнивать ссылки на равенство.
  - to_url — расширение текущего hash-роутера (#/a/{id}/m/{msg}).
    Существующие URL-ы не меняются.
  - validate_citation — проверяет существование источника в архиве.
    Используется Верификатором (этап 3) и UI-подсветкой (зелёная /
    жёлтая / красная).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional


# ---------------------------------------------------------------------------
# DTO
# ---------------------------------------------------------------------------

# Виды ссылок. Соответствуют типам групп (Г2) + comment (для конкретного
# сообщения внутри поста) + book_section (Г7, когда появится).
CITATION_KIND_POST = "post"                    # [пост 92]
CITATION_KIND_COMMENT = "comment"              # [пост 92, комментарий №14457]
CITATION_KIND_PARTICIPANT_THREAD = "participant_thread"  # [пост 149, ветка tatisimonenko]
CITATION_KIND_TRANSCRIPT = "transcript"        # [пост 850, расшифровка] или [расшифровка 850]
CITATION_KIND_BOOK_SECTION = "book_section"    # [книга «Название», гл. 3 › Практика отказа]
CITATION_KIND_SHELF = "shelf"                  # [полка messages]
CITATION_KIND_MONTH = "month"                  # [месяц 2024-09]

ALL_CITATION_KINDS = (
    CITATION_KIND_POST,
    CITATION_KIND_COMMENT,
    CITATION_KIND_PARTICIPANT_THREAD,
    CITATION_KIND_TRANSCRIPT,
    CITATION_KIND_BOOK_SECTION,
    CITATION_KIND_SHELF,
    CITATION_KIND_MONTH,
)


@dataclass
class Citation:
    """Типизированное представление ссылки на источник (Г6).

    Поля зависят от kind:
      post                  : post_id
      comment               : post_id, comment_id
      participant_thread    : post_id, username  (с @, каноническая форма)
      transcript            : post_id
      book_section          : book_title, chapter_path (список сегментов)
      shelf                 : shelf_kind
      month                 : month (YYYY-MM)
    """

    kind: str
    # Для post / comment / participant_thread / transcript — message_id поста.
    post_id: Optional[int] = None
    # Для comment — message_id конкретного комментария.
    comment_id: Optional[int] = None
    # Для participant_thread — username участницы (канонически с @).
    username: Optional[str] = None
    # Для book_section — название книги (без кавычек-ёлочек).
    book_title: Optional[str] = None
    # Для book_section — путь главы, список сегментов («Гл. 3», «Практика отказа»).
    chapter_path: list = field(default_factory=list)
    # Для shelf — kind из паспорта (messages, transcriptions, …).
    shelf_kind: Optional[str] = None
    # Для month — YYYY-MM.
    month: Optional[str] = None

    def to_dict(self) -> dict:
        """Сериализация для API/UI."""
        return {
            "kind": self.kind,
            "post_id": self.post_id,
            "comment_id": self.comment_id,
            "username": self.username,
            "book_title": self.book_title,
            "chapter_path": list(self.chapter_path) if self.chapter_path else [],
            "shelf_kind": self.shelf_kind,
            "month": self.month,
            "text": format_citation(self),
            "url": None,  # URL зависит от archive_id — заполняется вызывающим.
        }


# ---------------------------------------------------------------------------
# Регулярка — для Верификатора (этап 3) и find_citations
# ---------------------------------------------------------------------------

# Целое число (без знака).
_RE_INT = r"\d+"

# Имя пользователя: буквы/цифры/подчёркивания, БЕЗ @ (парсер принимает с @
# и без, форматтер всегда с @).
_RE_USERNAME = r"[A-Za-z0-9_]+"

# Название книги: всё до закрывающей «» (без самих кавычек). Не жадное.
_RE_BOOK_TITLE = r"[^»]+"

# Сегмент пути главы: всё, кроме "›" и "]". Не жадное.
_RE_CHAPTER_SEGMENT = r"[^›\]]+?"

# YYYY-MM
_RE_MONTH = r"\d{4}-\d{2}"

# kind полки — буквы/цифры/подчёркивания.
_RE_SHELF_KIND = r"[A-Za-z0-9_]+"

# Полный паттерн одной цитаты. Именованные группы — чтобы parse_citation
# мог вытащить поля без отдельной регулярки на каждый kind.
#
# Внимание: PATTERNS — список, упорядоченный от самого специфичного к
# наименее специфичному. Это важно: [пост 92, комментарий №14457] должен
# парситься как comment, а не как post (с обрезанной второй частью).
CITATION_PATTERNS = [
    # [пост 92, комментарий №14457]  или  [пост 92, комментарий 14457]
    re.compile(
        r"\[пост\s+(?P<post_id>\d+)\s*,\s*комментарий\s*(?:№\s*)?(?P<comment_id>\d+)\s*\]",
        re.UNICODE,
    ),
    # [пост 149, ветка tatisimonenko]  или  [пост 149, ветка @tatisimonenko]
    re.compile(
        r"\[пост\s+(?P<post_id>\d+)\s*,\s*ветка\s*@?(?P<username>"
        + _RE_USERNAME + r")\s*\]",
        re.UNICODE,
    ),
    # [пост 850, расшифровка]
    re.compile(
        r"\[пост\s+(?P<post_id>\d+)\s*,\s*расшифровка\s*\]",
        re.UNICODE,
    ),
    # [расшифровка 850]  — короткая форма
    re.compile(
        r"\[расшифровка\s+(?P<post_id>\d+)\s*\]",
        re.UNICODE,
    ),
    # [пост 92]  — базовый пост (после всех «пост + часть» вариантов)
    re.compile(
        r"\[пост\s+(?P<post_id>\d+)\s*\]",
        re.UNICODE,
    ),
    # [книга «Название», гл. 3 › Практика отказа]
    # Глава — путь из сегментов, разделённых " › " (с пробелами).
    re.compile(
        r"\[книга\s+«(?P<book_title>" + _RE_BOOK_TITLE + r")»\s*,\s*"
        r"гл\.\s+(?P<chapter_path>" + _RE_CHAPTER_SEGMENT + r"(?:\s*›\s*"
        + _RE_CHAPTER_SEGMENT + r")*)\s*\]",
        re.UNICODE,
    ),
    # [полка messages]
    re.compile(
        r"\[полка\s+(?P<shelf_kind>" + _RE_SHELF_KIND + r")\s*\]",
        re.UNICODE,
    ),
    # [месяц 2024-09]
    re.compile(
        r"\[месяц\s+(?P<month>" + _RE_MONTH + r")\s*\]",
        re.UNICODE,
    ),
]

# Общий паттерн «любая цитата» — для find_citations. Грубее: ищет все [...],
# потом каждый пропускает через parse_citation.
_RE_ANY_BRACKET = re.compile(r"\[[^\]\n]{1,300}\]")


# ---------------------------------------------------------------------------
# Парсер
# ---------------------------------------------------------------------------

def parse_citation(text: str) -> Optional[Citation]:
    """Распарсить строку в Citation. Если не похоже — None.

    Строка должна быть РОВНО ссылкой, без окружающего текста. Пробелы по
    краям допустимы (strip). Для поиска ссылок в тексте используйте
    find_citations.

    Принимает варианты:
      - «№» перед номером комментария — опционально
      - «@» перед username — опционально
      - «расшифровка» как часть поста или как самостоятельная ссылка

    Возвращает None для:
      - пустой строки
      - строки без скобок
      - строки с мусором вокруг скобок
      - неизвестного вида ссылки
    """
    if not text:
        return None
    s = text.strip()
    if not s.startswith("[") or not s.endswith("]"):
        return None
    for pat in CITATION_PATTERNS:
        m = pat.match(s)
        if not m:
            continue
        gd = m.groupdict()
        if "comment_id" in gd and gd.get("comment_id"):
            return Citation(
                kind=CITATION_KIND_COMMENT,
                post_id=int(gd["post_id"]),
                comment_id=int(gd["comment_id"]),
            )
        if "username" in gd and gd.get("username"):
            uname = gd["username"]
            if not uname.startswith("@"):
                uname = "@" + uname
            return Citation(
                kind=CITATION_KIND_PARTICIPANT_THREAD,
                post_id=int(gd["post_id"]),
                username=uname,
            )
        if "book_title" in gd and gd.get("book_title") is not None:
            # chapter_path — строка вида "Гл. 3 › Практика отказа",
            # разбиваем по " › " (с пробелами).
            raw_path = gd.get("chapter_path") or ""
            segments = [seg.strip() for seg in raw_path.split("›") if seg.strip()]
            return Citation(
                kind=CITATION_KIND_BOOK_SECTION,
                book_title=gd["book_title"].strip(),
                chapter_path=segments,
            )
        if "shelf_kind" in gd and gd.get("shelf_kind"):
            return Citation(
                kind=CITATION_KIND_SHELF,
                shelf_kind=gd["shelf_kind"],
            )
        if "month" in gd and gd.get("month"):
            return Citation(
                kind=CITATION_KIND_MONTH,
                month=gd["month"],
            )
        # Расшифровка (длинная или короткая форма) — post_id есть, но
        # ни comment_id, ни username. Различаем по тому, какой паттерн
        # сматчился: если в паттерне есть слово "расшифровка" — это
        # transcript, иначе post.
        if "post_id" in gd and gd.get("post_id"):
            # Отличить post от transcript: посмотрим на исходную строку.
            if "расшифровка" in s.lower():
                return Citation(
                    kind=CITATION_KIND_TRANSCRIPT,
                    post_id=int(gd["post_id"]),
                )
            return Citation(
                kind=CITATION_KIND_POST,
                post_id=int(gd["post_id"]),
            )
    return None


# ---------------------------------------------------------------------------
# Форматтер
# ---------------------------------------------------------------------------

def format_citation(c: Citation) -> str:
    """Каноническая строка ссылки.

    Детерминированная: один Citation → одна строка. Используется и для
    рендера в UI, и для сравнения на равенство.
    """
    if c.kind == CITATION_KIND_POST:
        if c.post_id is None:
            raise ValueError("post citation требует post_id")
        return f"[пост {c.post_id}]"
    if c.kind == CITATION_KIND_COMMENT:
        if c.post_id is None or c.comment_id is None:
            raise ValueError("comment citation требует post_id и comment_id")
        return f"[пост {c.post_id}, комментарий №{c.comment_id}]"
    if c.kind == CITATION_KIND_PARTICIPANT_THREAD:
        if c.post_id is None or not c.username:
            raise ValueError("participant_thread citation требует post_id и username")
        # username хранится с @ — оставляем как есть.
        return f"[пост {c.post_id}, ветка {c.username}]"
    if c.kind == CITATION_KIND_TRANSCRIPT:
        if c.post_id is None:
            raise ValueError("transcript citation требует post_id")
        # Каноническая форма — короткая: [расшифровка 850]. Это позволяет
        # отличить transcript-ссылку от post-ссылки с одного взгляда.
        return f"[расшифровка {c.post_id}]"
    if c.kind == CITATION_KIND_BOOK_SECTION:
        if not c.book_title or not c.chapter_path:
            raise ValueError("book_section citation требует book_title и chapter_path")
        path = " › ".join(c.chapter_path)
        return f"[книга «{c.book_title}», гл. {path}]"
    if c.kind == CITATION_KIND_SHELF:
        if not c.shelf_kind:
            raise ValueError("shelf citation требует shelf_kind")
        return f"[полка {c.shelf_kind}]"
    if c.kind == CITATION_KIND_MONTH:
        if not c.month:
            raise ValueError("month citation требует month")
        return f"[месяц {c.month}]"
    raise ValueError(f"Неизвестный kind цитаты: {c.kind!r}")


# ---------------------------------------------------------------------------
# URL — для UI-роутера
# ---------------------------------------------------------------------------

def to_url(c: Citation, archive_id: str) -> str:
    """URL для UI-роутера (расширение текущего hash-роутера).

    Существующие URL-ы НЕ меняются:
      пост                  → #/a/{archive_id}/m/{post_id}
      пост + комментарий    → #/a/{archive_id}/m/{post_id}?c={comment_id}

    Новые:
      пост + ветка          → #/a/{archive_id}/m/{post_id}?thread={username}
      расшифровка поста     → #/a/{archive_id}/m/{post_id}?transcript=1
      книга + глава         → #/a/{archive_id}/book/{slug}?path={chapter}
      полка                 → #/a/{archive_id}/shelf/{kind}
      месяц                 → #/a/{archive_id}/month/{YYYY-MM}

    NB: slug для книги и path для главы — пока черновики (книг нет до Г7).
    Когда появятся — функция будет доработана.
    """
    from urllib.parse import quote
    aid = quote(archive_id, safe="")
    if c.kind == CITATION_KIND_POST:
        return f"#/a/{aid}/m/{c.post_id}"
    if c.kind == CITATION_KIND_COMMENT:
        return f"#/a/{aid}/m/{c.post_id}?c={c.comment_id}"
    if c.kind == CITATION_KIND_PARTICIPANT_THREAD:
        # username хранится с @, в URL кодируем без @.
        uname = (c.username or "").lstrip("@")
        return f"#/a/{aid}/m/{c.post_id}?thread={quote(uname, safe='')}"
    if c.kind == CITATION_KIND_TRANSCRIPT:
        return f"#/a/{aid}/m/{c.post_id}?transcript=1"
    if c.kind == CITATION_KIND_BOOK_SECTION:
        # slug для книги — пока имя файла без расширения, когда книги появятся.
        # chapter_path кодируем через " › " → "%20%E2%80%BA%20".
        slug = quote(c.book_title or "", safe="")
        path_str = " › ".join(c.chapter_path)
        return f"#/a/{aid}/book/{slug}?path={quote(path_str, safe='')}"
    if c.kind == CITATION_KIND_SHELF:
        return f"#/a/{aid}/shelf/{quote(c.shelf_kind or '', safe='')}"
    if c.kind == CITATION_KIND_MONTH:
        return f"#/a/{aid}/month/{c.month}"
    raise ValueError(f"Неизвестный kind цитаты: {c.kind!r}")


# ---------------------------------------------------------------------------
# find_citations — найти все ссылки в тексте
# ---------------------------------------------------------------------------

def find_citations(text: str) -> list:
    """Найти все [..]-ссылки в тексте.

    Возвращает список (Citation, (start, end)) — цитату и её позицию в
    строке. Непохожие на цитату скобки (например, «[ремарка автора]»)
    пропускаются: parse_citation вернёт для них None.

    Это безопасно: Верификатор (этап 3) будет звать find_citations →
    parse_citation → validate_citation, и неподтверждённые ссылки
    помечаются жёлтым, а не красным.
    """
    if not text:
        return []
    result = []
    for m in _RE_ANY_BRACKET.finditer(text):
        snippet = m.group(0)
        c = parse_citation(snippet)
        if c is not None:
            result.append((c, (m.start(), m.end())))
    return result


# ---------------------------------------------------------------------------
# validate_citation — проверка существования источника
# ---------------------------------------------------------------------------

def validate_citation(c: Citation, archive, parser_db) -> dict:
    """Проверить, существует ли указанный источник в архиве.

    Возвращает dict:
      {ok: True}                          — источник существует
      {ok: False, error: str}             — источник не найден
      {ok: False, error: str, hint: str}  — не найден + подсказка

    Используется Верификатором (этап 3) и UI-подсветкой. Не используется
    в текущем поиске/ридере — это слой проверки НАД инструментами.

    Для book_section — всегда ok=True (книг ещё нет, проверять нечего;
    когда Г7 появится, добавим реальную проверку по text_folder).
    """
    if c.kind == CITATION_KIND_POST:
        msg = parser_db.get_message_by_message_id_only(c.post_id)
        if msg is None:
            return {"ok": False, "error": f"Пост {c.post_id} не найден"}
        if msg.is_comment:
            return {"ok": False, "error": f"Сообщение {c.post_id} — комментарий, не пост"}
        return {"ok": True}

    if c.kind == CITATION_KIND_COMMENT:
        # Проверяем, что comment_id существует и что post_id — его родитель.
        comment = parser_db.get_message_by_message_id_only(c.comment_id)
        if comment is None:
            return {"ok": False, "error": f"Комментарий {c.comment_id} не найден"}
        if not comment.is_comment:
            return {
                "ok": False,
                "error": f"Сообщение {c.comment_id} — не комментарий",
                "hint": f"Может быть, вы имели в виду [пост {c.comment_id}]?",
            }
        if comment.post_id != c.post_id:
            return {
                "ok": False,
                "error": (
                    f"Комментарий {c.comment_id} принадлежит посту "
                    f"{comment.post_id}, а не {c.post_id}"
                ),
                "hint": f"Может быть, [пост {comment.post_id}, комментарий №{c.comment_id}]?",
            }
        return {"ok": True}

    if c.kind == CITATION_KIND_PARTICIPANT_THREAD:
        # Проверяем, что пост существует и что под ним есть ветка этого username.
        post = parser_db.get_message_by_message_id_only(c.post_id)
        if post is None:
            return {"ok": False, "error": f"Пост {c.post_id} не найден"}
        if post.is_comment:
            return {"ok": False, "error": f"Сообщение {c.post_id} — комментарий, не пост"}
        # username в БД хранится с @ (как в @marina_s). Наш Citation тоже
        # хранит с @. Проверяем оба варианта — с @ и без — на случай, если
        # какой-то архив хранит без @.
        uname_with_at = c.username or ""
        uname_without_at = uname_with_at.lstrip("@")
        with parser_db.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM messages "
                "WHERE post_id = ? AND is_comment = 1 "
                "AND (username = ? OR username = ?)",
                (c.post_id, uname_with_at, uname_without_at),
            )
            n = int(cur.fetchone()[0] or 0)
        if n == 0:
            return {
                "ok": False,
                "error": (
                    f"Под постом {c.post_id} нет комментариев от "
                    f"{uname_with_at}"
                ),
            }
        return {"ok": True}

    if c.kind == CITATION_KIND_TRANSCRIPT:
        # Проверяем, что у поста есть расшифровка — в parser.db ИЛИ в 00_Индекс.md.
        msg = parser_db.get_message_by_message_id_only(c.post_id)
        if msg is None:
            return {"ok": False, "error": f"Пост {c.post_id} не найден"}
        if msg.is_comment:
            return {"ok": False, "error": f"Сообщение {c.post_id} — комментарий, не пост"}
        # Есть ли расшифровка в parser.db?
        with parser_db.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM transcriptions WHERE message_id = ?",
                (c.post_id,),
            )
            in_db = int(cur.fetchone()[0] or 0) > 0
        if in_db:
            return {"ok": True}
        # Есть ли в 00_Индекс.md?
        try:
            from .librarian_db import LibrarianDB
            file_rows = LibrarianDB._load_index_transcripts(parser_db.path.parent)
            for post_id, _name, _text in file_rows:
                if int(post_id) == c.post_id:
                    return {"ok": True}
        except Exception:
            pass
        return {
            "ok": False,
            "error": f"У поста {c.post_id} нет расшифровки",
        }

    if c.kind == CITATION_KIND_BOOK_SECTION:
        # Книг пока нет (Г7). Когда появятся — добавим проверку по
        # text_folder, что файл главы существует.
        return {"ok": True}

    if c.kind == CITATION_KIND_SHELF:
        shelves = archive.passport.shelves or []
        if not any(s.kind == c.shelf_kind for s in shelves):
            kinds = [s.kind for s in shelves] or ["(полок нет)"]
            return {
                "ok": False,
                "error": f"Полка «{c.shelf_kind}» не найдена",
                "hint": f"Доступные полки: {', '.join(kinds)}",
            }
        return {"ok": True}

    if c.kind == CITATION_KIND_MONTH:
        # Проверяем, что в архиве есть хотя бы один пост этого месяца.
        with parser_db.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM messages "
                "WHERE is_comment = 0 AND substr(date, 1, 7) = ?",
                (c.month,),
            )
            n = int(cur.fetchone()[0] or 0)
        if n == 0:
            return {"ok": False, "error": f"В архиве нет постов за {c.month}"}
        return {"ok": True}

    return {"ok": False, "error": f"Неизвестный kind цитаты: {c.kind!r}"}


# ---------------------------------------------------------------------------
# Хелпер для групп — построить citation по группе
# ---------------------------------------------------------------------------

def citation_for_group(group) -> Optional[Citation]:
    """Построить Citation для группы, если это уместно.

    Не у всех групп есть citation:
      post_thread          → [пост N]
      participant_thread   → [пост N, ветка @username]
      transcript           → [расшифровка N]
      cycle                → None  (мета-группа, на неё нельзя сослаться)
      month                → None  (мета-группа)
      book_section         → [книга «Title», гл. path]  (когда Г7 появится)

    Возвращает None, если для группы нет осмысленного citation.
    """
    # Локальный импорт, чтобы избежать цикла.
    from .groups import (
        GROUP_TYPE_POST_THREAD,
        GROUP_TYPE_PARTICIPANT_THREAD,
        GROUP_TYPE_TRANSCRIPT,
    )
    if group.type == GROUP_TYPE_POST_THREAD:
        post_id = group.keys.get("post_message_id")
        if post_id is None:
            return None
        return Citation(kind=CITATION_KIND_POST, post_id=int(post_id))
    if group.type == GROUP_TYPE_PARTICIPANT_THREAD:
        post_id = group.keys.get("post_message_id")
        username = group.keys.get("username")
        if post_id is None or not username:
            return None
        return Citation(
            kind=CITATION_KIND_PARTICIPANT_THREAD,
            post_id=int(post_id),
            username=username,
        )
    if group.type == GROUP_TYPE_TRANSCRIPT:
        post_id = group.keys.get("post_message_id")
        if post_id is None:
            return None
        return Citation(kind=CITATION_KIND_TRANSCRIPT, post_id=int(post_id))
    return None
