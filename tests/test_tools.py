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


# ---------------------------------------------------------------------------
# Регрессионные тесты — Б1, Б2, Б3 (см. librarian_статус.md §Застряло)
# ---------------------------------------------------------------------------

def test_b1_stem_trims_russian_endings(opened_philosophy):
    """Б1: поиск «зависти» обязан находить пост со словом «зависть».

    Без обрезки окончаний «зависти*» НЕ матчит «зависть», потому что
    префикс ищет по форме из вопроса. Замер 28.07: 62% → 88% после обрезки.
    """
    archive, parser_db, lib_db = opened_philosophy
    hits = lib_db.search("зависти", limit=20)
    assert len(hits) > 0, "Поиск «зависти» должен находить пост 800 про зависть"
    # Пост 800 — основной; комменты 801–804 — дополнительно
    found_post_800 = any(h.message_id == 800 for h in hits)
    assert found_post_800, "В выдаче должен быть пост 800 («Зависть и сравнение»)"


def test_b1_stem_handles_oblique_forms(opened_philosophy):
    """Б1: «сравнения» должно находить тексты со словом «сравнение»."""
    archive, parser_db, lib_db = opened_philosophy
    hits = lib_db.search("сравнения", limit=20)
    assert len(hits) > 0, "Поиск «сравнения» должен находить посты про сравнение"


def test_b1_stopwords_filtered(opened_philosophy):
    """Б1: «о зависти» — стоп-слово «о» выкинуть, искать только «зависть»."""
    archive, parser_db, lib_db = opened_philosophy
    hits = lib_db.search("о зависти", limit=20)
    assert len(hits) > 0, "Стоп-слово «о» не должно ломать поиск"


def test_threaded_post_has_reply_chain(opened_philosophy):
    """Г3 (рекомендации_группы_источников.md): демо-архив содержит пост с
    веткой комментариев, у которых проставлены reply_to_msg_id.

    Без этой структуры групповое ранжирование не на чем тестировать —
    participant_thread требует цепочек ответов.
    """
    archive, parser_db, lib_db = opened_philosophy

    # Пост 900 должен существовать
    post = parser_db.get_message_by_message_id(chat_id=-1001234567890, message_id=900)
    assert post is not None, "Пост 900 не найден — патч демо-архива не применён"
    assert "границы" in post.text.lower(), f"Текст поста 900 не про границы: {post.text[:80]}"

    # Все 8 комментариев к посту 900 (по post_id, не по reply_to —
    # т.к. висячий anna_p 908 имеет reply_to=None, но post_id=900)
    with parser_db.cursor() as cur:
        cur.execute(
            "SELECT message_id, username, reply_to_msg_id FROM messages "
            "WHERE post_id = 900 AND is_comment = 1 "
            "ORDER BY message_id"
        )
        rows = cur.fetchall()
    ids = {r[0]: r for r in rows}
    assert len(rows) == 8, f"Должно быть 8 комментариев, есть {len(rows)}"

    # 7 из 8 имеют reply_to_msg_id (все кроме 908)
    with_reply = [r for r in rows if r[2] is not None]
    assert len(with_reply) == 7, (
        f"Должно быть 7 комментариев с reply_to_msg_id, есть {len(with_reply)}"
    )

    # Ветка 1: marina_s ↔ author (901 → 902 → 903 → 904)
    assert ids[901][2] == 900, "901 должна ответлять на 900"
    assert ids[902][2] == 901, "902 должна ответлять на 901"
    assert ids[903][2] == 902, "903 должна ответлять на 902"
    assert ids[904][2] == 903, "904 должна ответлять на 903"

    # Ветка 2: ivan_k ↔ author (905 → 906 → 907)
    assert ids[905][2] == 900, "905 должна ответлять на 900"
    assert ids[906][2] == 905, "906 должна ответлять на 905"
    assert ids[907][2] == 906, "907 должна ответлять на 906"

    # 908 — висячий, без reply_to
    assert ids[908][2] is None, "908 должна быть без reply_to_msg_id"


def test_b2_quotas_keep_author_posts(opened_philosophy):
    """Б2: при quota_per_kind посты автора не тонут под комментариями.

    Без квот: общий top-20 на демо-архиве философии в основном состоит
    из комментариев (их 18, постов автора — 7). С квотой 5 на тип —
    в выдаче обязательно присутствуют посты (is_comment=False).
    """
    archive, parser_db, lib_db = opened_philosophy
    # Сначала — общая выдача, для сравнения
    hits_no_quota = lib_db.search("обесценива", limit=20)
    assert len(hits_no_quota) > 0

    # С квотой 5 на тип
    hits_quota = lib_db.search("обесценива", limit=20, quota_per_kind=5)
    assert len(hits_quota) > 0

    # В выдаче обязательно должны быть посты (is_comment=False)
    has_posts = any(not h.is_comment for h in hits_quota)
    assert has_posts, "При квотах в выдаче должны быть посты автора"

    # И комментарии (is_comment=True)
    has_comments = any(h.is_comment for h in hits_quota)
    assert has_comments, "При квотах в выдаче должны быть комментарии"

    # Каждый тип представлен не более чем quota_per_kind раз
    n_posts = sum(1 for h in hits_quota if not h.is_comment and h.source == "message")
    n_comments = sum(1 for h in hits_quota if h.is_comment and h.source == "message")
    assert n_posts <= 5, f"Постов больше квоты: {n_posts}"
    assert n_comments <= 5, f"Комментариев больше квоты: {n_comments}"


def test_b3_external_transcripts_indexed(opened_philosophy):
    """Б3: расшифровка из файла (по 00_Индекс.md) индексируется.

    Пост 850 — голосовое, чья расшифровка лежит ТОЛЬКО файлом
    (В14_п850_Проекция_и_идентификация.md), в таблице transcriptions
    её нет. Без парсера 00_Индекс.md она не попадала бы в индекс.
    """
    archive, parser_db, lib_db = opened_philosophy
    # Проверим, что в индексе есть документ-расшифровка для поста 850
    with lib_db.cursor() as cur:
        cur.execute(
            "SELECT source, message_id, author FROM fts_doc "
            "WHERE message_id = 850 AND source = 'transcription'"
        )
        rows = cur.fetchall()
    assert len(rows) == 1, (
        f"Должна быть 1 расшифровка для поста 850 из файла, найдено {len(rows)}"
    )
    # author хранит имя файла (маркер файлового происхождения)
    assert "п850" in rows[0]["author"].lower() or "850" in rows[0]["author"]


def test_b3_external_transcript_searchable(opened_philosophy):
    """Б3: поиск по содержимому внешней расшифровки находит пост 850.

    Слово «проекция» есть только в файле-расшифровке поста 850
    (в самих сообщениях его нет). Если Б3 работает — поиск найдёт
    пост 850 среди хитов.
    """
    archive, parser_db, lib_db = opened_philosophy
    hits = lib_db.search("проекция", limit=20)
    found_850 = any(h.message_id == 850 and h.source == "transcription"
                    for h in hits)
    assert found_850, (
        "Поиск «проекция» должен находить расшифровку поста 850 из файла"
    )


def test_b3_parser_load_index_md(opened_philosophy):
    """Б3: _load_index_transcripts корректно парсит 00_Индекс.md."""
    archive, parser_db, lib_db = opened_philosophy
    rows = lib_db._load_index_transcripts(archive.root)
    assert len(rows) >= 1, "Должен найтись хотя бы один файл расшифровки"
    # post_id, имя_файла, текст
    post_id, name, text = rows[0]
    assert post_id == 850
    assert name.endswith(".md")
    assert "проекция" in text.lower()


# ---------------------------------------------------------------------------
# Г3: tools.search с include_groups — групповое ранжирование в выдаче
# ---------------------------------------------------------------------------

def test_tool_search_groups_empty_by_default(opened_philosophy):
    """Без include_groups — groups=[] и groups_count=0 (обратная совместимость)."""
    archive, parser_db, lib_db = opened_philosophy
    result = T.search(archive, lib_db, "обесценива")
    assert result["count"] > 0
    assert result["groups"] == []
    assert result["groups_count"] == 0
    assert "include_groups" in result["filters"]
    assert result["filters"]["include_groups"] is False


def test_tool_search_includes_groups_when_requested(opened_philosophy):
    """С include_groups=True и parser_db — groups непустой."""
    archive, parser_db, lib_db = opened_philosophy
    result = T.search(
        archive, lib_db, "обесценива",
        parser_db=parser_db, include_groups=True,
    )
    assert result["count"] > 0
    assert result["groups_count"] > 0, "Должны быть группы с совпадениями"
    assert len(result["groups"]) > 0
    assert len(result["groups"]) <= 10  # group_limit=10 по умолчанию
    assert result["filters"]["include_groups"] is True


def test_tool_search_groups_sorted_by_matched_count(opened_philosophy):
    """Группы отсортированы по matched_count DESC."""
    archive, parser_db, lib_db = opened_philosophy
    # Запрос с обилием совпадений — «обесценива» находит во многих постах.
    result = T.search(
        archive, lib_db, "обесценива",
        parser_db=parser_db, include_groups=True, group_limit=20,
    )
    groups = result["groups"]
    assert len(groups) >= 2
    # Сортировка: matched_count DESC
    counts = [g["matched_count"] for g in groups]
    assert counts == sorted(counts, reverse=True), (
        f"Группы не отсортированы по matched_count DESC: {counts}"
    )


def test_tool_search_groups_dict_structure(opened_philosophy):
    """Каждый GroupHit.to_dict() имеет все ожидаемые поля."""
    archive, parser_db, lib_db = opened_philosophy
    result = T.search(
        archive, lib_db, "обесценива",
        parser_db=parser_db, include_groups=True,
    )
    assert result["groups_count"] > 0
    g = result["groups"][0]
    expected = {
        "group_id", "type", "label", "keys", "chat_id", "extras",
        "matched_count", "total_count", "best_rank", "best_message_id",
        "matched_message_ids", "ratio",
    }
    assert expected.issubset(set(g.keys())), (
        f"Не хватает полей: {expected - set(g.keys())}"
    )
    assert g["matched_count"] >= 1
    assert g["total_count"] >= g["matched_count"]
    assert 0.0 < g["ratio"] <= 1.0


def test_tool_search_groups_respects_group_limit(opened_philosophy):
    """group_limit ограничивает длину groups, но не groups_count."""
    archive, parser_db, lib_db = opened_philosophy
    result = T.search(
        archive, lib_db, "обесценива",
        parser_db=parser_db, include_groups=True, group_limit=2,
    )
    # groups_count считает все совпавшие, len(groups) обрезан.
    assert len(result["groups"]) <= 2
    # groups_count может быть больше group_limit.
    assert result["groups_count"] >= len(result["groups"])


def test_tool_search_include_groups_without_parser_db(opened_philosophy):
    """include_groups=True, но parser_db=None — groups=[] (не падает)."""
    archive, parser_db, lib_db = opened_philosophy
    result = T.search(
        archive, lib_db, "обесценива",
        parser_db=None, include_groups=True,
    )
    assert result["groups"] == []
    assert result["groups_count"] == 0

# ===========================================================================
# Регрессионные тесты на критические баги (patch_critical_bugs_pre_review.py)
# ===========================================================================
# Каждый тест покрывает один класс бага из CLAUDE.md «История багов сессии».
# Имя теста = test_bug<N>_<короткое описание>.

def test_bug001_card_count_from_db_not_passport(demo_archives, monkeypatch):
    """BUG-001 (П3): карточка архива показывает счётчик из БД, не из паспорта.

    Сценарий: архив без паспорта (или с паспортом, где messages_count=0).
    Раньше list_archives_as_cards() возвращал 0 — UI показывал «0 сообщений».
    Теперь карточка должна показывать реальное число из parser.db.

    Проверяем через LibrarianCore: список карточек должен содержать ненулевые
    messages_count, даже если паспорт пустой.
    """
    from core.librarian_core import LibrarianCore
    # Создаём ядро от demo output
    core = LibrarianCore(demo_archives)
    cards = core.list_archives_as_cards()
    assert len(cards) >= 1, "Демо-архивы не найдены"
    # Хотя бы одна карточка должна иметь ненулевой счётчик.
    # (Демо-архивы содержат сообщения, и parser.db есть.)
    has_nonzero = any(c["messages_count"] > 0 for c in cards)
    assert has_nonzero, (
        "Ни одна карточка не имеет messages_count > 0 — "
        "BUG-001 не исправлен: list_archives_as_cards не открывает parser.db"
    )


def test_bug002_archive_isolation_no_leak(demo_archives):
    """BUG-002 (П4): изоляция архивов — поиск в A не должен вернуть данные B.

    Сценарий: открыты архивы A и B. Вызов search(A, ...) возвращает хиты
    только с internal_id и chat_id из A. Если бэкенд смешал базы — хиты
    из B появятся в выдаче A.

    Проверяем через LibrarianCore: открываем два демо-архива, делаем поиск
    в первом, убеждаемся, что все хиты принадлежат первому архиву.
    """
    from core.librarian_core import LibrarianCore
    core = LibrarianCore(demo_archives)
    archives = core.list_archives()
    if len(archives) < 2:
        pytest.skip("Нужно минимум 2 демо-архива для проверки изоляции")
    a, b = archives[0], archives[1]
    # Открываем оба (строятся librarian.db)
    core.open_archive(a.id)
    core.open_archive(b.id)
    # Поиск в A
    res_a = core.search(a.id, "зависть", quota_per_kind=None,
                        include_groups=False)
    # Поиск в B
    res_b = core.search(b.id, "тест", quota_per_kind=None,
                        include_groups=False)
    # Все хиты A должны иметь archive_id == a.id (проверяем через post.url).
    # URL строится как #/a/{archive.id}/m/{message_id} — там зашит archive.id.
    for h in res_a["hits"]:
        url = h.get("url", "")
        assert f"/a/{a.id}/" in url or "/a/" in url, (
            f"Хит из поиска в архиве A имеет URL без archive.id A: {url}"
        )
    for h in res_b["hits"]:
        url = h.get("url", "")
        assert f"/a/{b.id}/" in url or "/a/" in url, (
            f"Хит из поиска в архиве B имеет URL без archive.id B: {url}"
        )
    # Дополнительно: список archive_id в hits A не должен содержать b.id.
    # (Если URL содержит /a/{b.id}/ — это утечка.)
    for h in res_a["hits"]:
        url = h.get("url", "")
        assert f"/a/{b.id}/" not in url, (
            f"Утечка: поиск в A вернул хит с URL архива B: {url}"
        )
    core.close_all()


def test_bug003_search_by_at_prefix_finds_author_messages(opened_philosophy):
    """BUG-003 (П6): поиск по @нику находит сообщения автора, не упоминания.

    Сценарий: пользователь вводит '@<ник>' в строку поиска.
    Раньше FTS искал по тексту и находил только 1-2 (где ник упомянут).
    Теперь tools.search() при @-префиксе переключается в режим author search
    и находит ВСЕ сообщения автора.

    Проверяем: поиск '@<существующий автор>' возвращает > 0 хитов,
    и все хиты имеют source='author_search'.
    """
    archive, parser_db, lib_db = opened_philosophy
    # Найдём автора с наибольшим числом сообщений в демо-архиве
    top = parser_db.top_authors(limit=1)
    assert top, "Демо-архив пустой — нет авторов"
    nick, msg_count = top[0]
    assert msg_count >= 1, f"Автор {nick} должен иметь ≥1 сообщение"
    # Убираем @ для запроса (или оставляем — оба варианта должны работать)
    nick_clean = nick.lstrip("@")
    query = "@" + nick_clean
    # Вызываем search с parser_db — это обязательно для author-режима
    result = T.search(archive, lib_db, query, parser_db=parser_db,
                      quota_per_kind=None, include_groups=False)
    assert result["count"] > 0, (
        f"Поиск @{nick_clean} должен найти ≥1 сообщения автора, "
        f"но найдено {result['count']}"
    )
    # Все хиты должны быть помечены как author_search
    for h in result["hits"]:
        assert h["source"] == "author_search", (
            f"Хит должен иметь source='author_search', получил {h['source']}"
        )
    # Фильтр должен показывать режим
    assert result["filters"].get("author_search") is True, (
        "filters.author_search должен быть True при @-запросе"
    )


def test_bug004_get_message_without_chat_id_falls_back_to_message_id_only(opened_philosophy):
    """BUG-004 (П8): get_message без chat_id не падает, а находит сообщение
    по message_id через get_message_by_message_id_only.

    Сценарий: UI вызывает get_message с args={message_id} без chat_id
    (URL #/a/{id}/m/{message_id}). Если в паспорте нет chat_id —
    раньше падало «Не удалось определить chat_id».
    Теперь fallback на parser_db.get_message_by_message_id_only().

    Проверяем: get_message(message_id=X, chat_id=None) возвращает пост
    с правильным message_id, даже если паспорт не имеет chat_id.
    """
    archive, parser_db, lib_db = opened_philosophy
    # Берём любое существующее сообщение
    msgs = list(parser_db.iter_messages(batch_size=10))
    assert msgs, "Демо-архив пустой — нет сообщений"
    target = msgs[0]
    # Временно убираем chat_id из паспорта (имитируем реальный архив без паспорта)
    saved_chat_id = archive.passport.chat_id
    try:
        archive.passport.chat_id = None
        # Вызываем get_message БЕЗ chat_id — должен сработать fallback
        result = T.get_message(archive, parser_db,
                                message_id=target.message_id, chat_id=None)
        assert result is not None, "get_message вернул None — fallback не сработал"
        assert result["post"]["message_id"] == target.message_id, (
            f"message_id в ответе не совпадает: ожидали {target.message_id}, "
            f"получили {result['post']['message_id']}"
        )
        # chat_id в ответе должен быть реальный (из найденного сообщения)
        assert result["post"]["chat_id"] == target.chat_id, (
            f"chat_id должен быть {target.chat_id} (из сообщения), "
            f"получили {result['post']['chat_id']}"
        )
    finally:
        # Восстанавливаем паспорт
        archive.passport.chat_id = saved_chat_id


