"""
tests/test_tools.py — юнит-тесты для пяти инструментов Librarian.

Запуск: pytest tests/test_tools.py -v

Тесты создают временный демо-архив (если его нет) и проверяют:
- search() — FTS5-поиск, префиксные формы, фильтры
- read_post() — полный пост + комментарии + транскрипция, пагинация
- stats() — счётчики, диапазон дат, топ авторов
- whats_new() — что нового
- list_shelves() — полки из паспорта
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

# Импортируем make_demo_archive как модуль (он лежит в scripts/, не в пакете)
SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

# Добавим корень проекта в sys.path, чтобы импорт core/ работал
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from core.archive import ArchiveDiscovery, Archive
from core.librarian_db import LibrarianDB
from core.parser_db import ParserDB
from core import tools as T


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def demo_archives():
    """Создаёт демо-архивы один раз на модуль, возвращает Path output/."""
    import make_demo_archive
    output_root = PROJECT_ROOT / "output"
    make_demo_archive.DEMO_ARCHIVES = make_demo_archive.DEMO_ARCHIVES  # использует дефолт
    make_demo_archive.main()
    return output_root


@pytest.fixture
def first_archive(demo_archives) -> Archive:
    """Открывает первый демо-архив, возвращает Archive."""
    archives = ArchiveDiscovery(demo_archives).list_archives()
    assert len(archives) >= 1, "Демо-архивы не найдены"
    return archives[0]


@pytest.fixture
def philosophy_archive(demo_archives) -> Archive:
    """Открывает демо-архив «Философия буднего дня» — там есть и «обесценивание», и транскрипция."""
    archives = ArchiveDiscovery(demo_archives).list_archives()
    a = next((a for a in archives if "demo_philosophy" in a.id), None)
    assert a is not None, "Демо-архив «Философия» не найден"
    return a


@pytest.fixture
def opened(first_archive):
    """Открывает parser.db + librarian.db, строит индекс, отдаёт кортеж."""
    parser_db = ParserDB(first_archive.parser_db_path).open()
    lib_db_path = first_archive.root / "librarian.db"
    if lib_db_path.exists():
        lib_db_path.unlink()
    lib_db = LibrarianDB(lib_db_path).open()
    if not lib_db.is_built():
        lib_db.build_index(parser_db)
    try:
        yield (first_archive, parser_db, lib_db)
    finally:
        parser_db.close()
        lib_db.close()


@pytest.fixture
def opened_philosophy(philosophy_archive):
    """Открывает philosophy-архив: parser.db + librarian.db с построенным индексом."""
    parser_db = ParserDB(philosophy_archive.parser_db_path).open()
    lib_db_path = philosophy_archive.root / "librarian.db"
    if lib_db_path.exists():
        lib_db_path.unlink()
    lib_db = LibrarianDB(lib_db_path).open()
    if not lib_db.is_built():
        lib_db.build_index(parser_db)
    try:
        yield (philosophy_archive, parser_db, lib_db)
    finally:
        parser_db.close()
        lib_db.close()


# ---------------------------------------------------------------------------
# Archive discovery
# ---------------------------------------------------------------------------

def test_discovery_finds_archives(demo_archives):
    archives = ArchiveDiscovery(demo_archives).list_archives()
    assert len(archives) >= 2
    titles = [a.passport.title for a in archives]
    assert any("Философия" in t for t in titles)
    assert any("Tech" in t for t in titles)


def test_passport_has_expected_fields(first_archive):
    p = first_archive.passport
    assert p.title
    assert p.chat_type in ("channel", "group", "forum", "private")
    assert p.messages_count > 0
    assert len(p.shelves) >= 1
    assert not p.partial, "Паспорт должен быть полный, не partial"


# ---------------------------------------------------------------------------
# Parser DB
# ---------------------------------------------------------------------------

def test_parser_db_schema_version(opened):
    archive, parser_db, _ = opened
    v = parser_db.schema_version()
    assert v in (1, 2), f"Schema version должна быть 1 или 2, получили {v}"


def test_parser_db_read_only(opened):
    """Проверяем, что запись в parser.db действительно невозможна."""
    archive, parser_db, _ = opened
    with pytest.raises(Exception):
        # mode=ro — должна вылететь ошибка на любой INSERT
        parser_db._conn.execute("INSERT INTO messages (chat_id, message_id, date) VALUES (1, 1, '2024-01-01')")


def test_parser_db_iter_messages(opened):
    archive, parser_db, _ = opened
    messages = list(parser_db.iter_messages())
    assert len(messages) > 0
    for m in messages:
        assert m.chat_id is not None
        assert m.message_id is not None
        assert m.date


def test_parser_db_has_transcriptions(opened_philosophy):
    archive, parser_db, _ = opened_philosophy
    # Демо-архив «Философия буднего дня» содержит 1 транскрипцию
    assert parser_db.count_transcriptions() >= 1


# ---------------------------------------------------------------------------
# Librarian DB / FTS5
# ---------------------------------------------------------------------------

def test_fts_index_built(opened):
    archive, parser_db, lib_db = opened
    assert lib_db.is_built()
    assert lib_db.doc_count() > 0


def test_fts_search_basic(opened_philosophy):
    archive, parser_db, lib_db = opened_philosophy
    hits = lib_db.search("обесценива")
    assert len(hits) > 0, "Должно найти по префиксу «обесценива»"
    # Все сниппеты должны содержать корень слова
    found = any("обесценив" in h.snippet.lower() for h in hits)
    assert found, "Хотя бы один сниппет должен содержать корень"


def test_fts_search_prefix_form(opened_philosophy):
    """Префиксная форма «обесценива*» должна находить все словоформы."""
    archive, parser_db, lib_db = opened_philosophy
    hits = lib_db.search("обесценива*")
    assert len(hits) > 0


def test_fts_search_empty_query(opened):
    archive, parser_db, lib_db = opened
    assert lib_db.search("") == []


# ---------------------------------------------------------------------------
# Tools — search
# ---------------------------------------------------------------------------

def test_tool_search(opened_philosophy):
    archive, parser_db, lib_db = opened_philosophy
    result = T.search(archive, lib_db, "обесценива")
    assert result["count"] > 0
    assert len(result["hits"]) <= 20
    assert all("snippet" in h for h in result["hits"])
    assert all("url" in h for h in result["hits"])


def test_tool_search_empty(opened):
    archive, parser_db, lib_db = opened
    result = T.search(archive, lib_db, "")
    assert result["count"] == 0
    assert result["hits"] == []


def test_tool_search_no_results(opened):
    archive, parser_db, lib_db = opened
    result = T.search(archive, lib_db, "zzzznonexistentword")
    assert result["count"] == 0


# ---------------------------------------------------------------------------
# Tools — read_post
# ---------------------------------------------------------------------------

def test_tool_read_post(opened_philosophy):
    archive, parser_db, lib_db = opened_philosophy
    # Берём первый пост канала
    result = T.read_post(archive, parser_db, lib_db,
                         chat_id=archive.passport.chat_id,
                         message_id=100)
    assert result["post"]["message_id"] == 100
    assert "обесценива" in result["post"]["text"].lower()
    assert result["comments"]["total"] >= 3
    assert len(result["comments"]["items"]) >= 3


def test_tool_read_post_pagination(opened_philosophy):
    archive, parser_db, lib_db = opened_philosophy
    # Запрашиваем страницу размером 2
    result = T.read_post(archive, parser_db, lib_db,
                         chat_id=archive.passport.chat_id,
                         message_id=100,
                         comment_limit=2, comment_offset=0)
    assert result["comments"]["limit"] == 2
    assert len(result["comments"]["items"]) <= 2
    # Вторая страница
    result2 = T.read_post(archive, parser_db, lib_db,
                          chat_id=archive.passport.chat_id,
                          message_id=100,
                          comment_limit=2, comment_offset=2)
    # Идентификаторы не должны пересекаться
    ids1 = {c["message_id"] for c in result["comments"]["items"]}
    ids2 = {c["message_id"] for c in result2["comments"]["items"]}
    assert not (ids1 & ids2), "Пагинация сломана — комментарии пересекаются"


def test_tool_read_post_with_transcription(opened_philosophy):
    archive, parser_db, lib_db = opened_philosophy
    # Пост 300 содержит транскрипцию
    result = T.read_post(archive, parser_db, lib_db,
                         chat_id=archive.passport.chat_id,
                         message_id=300)
    assert result["transcription"] is not None
    assert "обесценива" in result["transcription"]["text"].lower()


def test_tool_read_post_not_found(opened):
    archive, parser_db, lib_db = opened
    with pytest.raises(T.ToolError):
        T.read_post(archive, parser_db, lib_db,
                    chat_id=archive.passport.chat_id,
                    message_id=999999)


# ---------------------------------------------------------------------------
# Tools — stats
# ---------------------------------------------------------------------------

def test_tool_stats_overview(opened):
    archive, parser_db, lib_db = opened
    result = T.stats(archive, parser_db, kind="overview")
    assert result["kind"] == "overview"
    assert result["messages_count"] > 0
    assert result["date_from"]
    assert result["date_to"]
    assert len(result["top_authors"]) > 0


def test_tool_stats_authors(opened):
    archive, parser_db, lib_db = opened
    result = T.stats(archive, parser_db, kind="authors", top_authors=5)
    assert result["kind"] == "authors"
    assert len(result["top_authors"]) <= 5
    # Топ-1 должен иметь больше сообщений, чем топ-2
    if len(result["top_authors"]) >= 2:
        assert result["top_authors"][0]["count"] >= result["top_authors"][1]["count"]


# ---------------------------------------------------------------------------
# Tools — whats_new
# ---------------------------------------------------------------------------

def test_tool_whats_new_all(opened):
    archive, parser_db, lib_db = opened
    result = T.whats_new(archive, parser_db, limit=10)
    assert result["count"] > 0
    assert len(result["items"]) <= 10


def test_tool_whats_new_since(opened):
    archive, parser_db, lib_db = opened
    # Запросим всё после 2024-09-16 — должно найти посты 200 и 300
    result = T.whats_new(archive, parser_db, since="2024-09-16T00:00:00", limit=50)
    assert result["count"] > 0
    # Никаких сообщений из первого поста (15 сентября) быть не должно
    for item in result["items"]:
        assert item["date"] > "2024-09-16T00:00:00"


# ---------------------------------------------------------------------------
# Tools — list_shelves
# ---------------------------------------------------------------------------

def test_tool_list_shelves(opened):
    archive, parser_db, lib_db = opened
    result = T.list_shelves(archive, parser_db)
    assert result["archive_id"] == archive.id
    assert result["archive_title"] == archive.passport.title
    assert len(result["shelves"]) >= 1
    # Хотя бы одна полка должна иметь count > 0
    assert any(s["count"] > 0 for s in result["shelves"])
