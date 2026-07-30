"""
Регрессионные тесты для core/citations.py (Г6).

Покрывают:
  - parse_citation: все 7 kinds + варианты (с/без №, с/без @)
  - format_citation: каноническая форма
  - to_url: hash-роутер
  - find_citations: поиск в тексте
  - validate_citation: проверка существования
  - citation_for_group: интеграция с Г1+Г2
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.citations import (
    Citation, parse_citation, format_citation, to_url, find_citations,
    validate_citation, citation_for_group,
    CITATION_KIND_POST, CITATION_KIND_COMMENT,
    CITATION_KIND_PARTICIPANT_THREAD, CITATION_KIND_TRANSCRIPT,
    CITATION_KIND_BOOK_SECTION, CITATION_KIND_SHELF, CITATION_KIND_MONTH,
)
from core.groups import (
    Group, GroupsBuilder,
    GROUP_TYPE_POST_THREAD, GROUP_TYPE_PARTICIPANT_THREAD,
    GROUP_TYPE_TRANSCRIPT, GROUP_TYPE_CYCLE, GROUP_TYPE_MONTH,
)
from core import LibrarianCore


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def core():
    registry = ROOT / "config" / "registry.toml"
    output = ROOT / "output"
    c = LibrarianCore.from_registry(registry, output_root=output)
    yield c
    c.close_all()


@pytest.fixture(scope="module")
def opened_philosophy(core):
    archive = core.open_archive("demo_philosophy_channel")
    parser_db, _ = core._get_dbs("demo_philosophy_channel")
    return archive, parser_db


# ---------------------------------------------------------------------------
# parse_citation — post
# ---------------------------------------------------------------------------

def test_parse_post_basic():
    c = parse_citation("[пост 92]")
    assert c is not None
    assert c.kind == CITATION_KIND_POST
    assert c.post_id == 92
    assert c.comment_id is None
    assert c.username is None


def test_parse_post_with_spaces():
    c = parse_citation("  [пост 92]  ")
    assert c is not None
    assert c.kind == CITATION_KIND_POST
    assert c.post_id == 92


def test_parse_post_rejects_garbage():
    assert parse_citation("[пост abc]") is None
    assert parse_citation("пост 92") is None  # без скобок
    assert parse_citation("[пост 92") is None  # без закрывающей
    assert parse_citation("") is None
    assert parse_citation("[какая-то ремарка]") is None


# ---------------------------------------------------------------------------
# parse_citation — comment
# ---------------------------------------------------------------------------

def test_parse_comment_with_number_sign():
    c = parse_citation("[пост 92, комментарий №14457]")
    assert c is not None
    assert c.kind == CITATION_KIND_COMMENT
    assert c.post_id == 92
    assert c.comment_id == 14457


def test_parse_comment_without_number_sign():
    c = parse_citation("[пост 92, комментарий 14457]")
    assert c is not None
    assert c.kind == CITATION_KIND_COMMENT
    assert c.comment_id == 14457


def test_parse_comment_does_not_fall_back_to_post():
    """Если сматчился comment — это comment, не post."""
    c = parse_citation("[пост 92, комментарий №14457]")
    assert c.kind == CITATION_KIND_COMMENT
    assert c.kind != CITATION_KIND_POST


# ---------------------------------------------------------------------------
# parse_citation — participant_thread
# ---------------------------------------------------------------------------

def test_parse_participant_thread_with_at():
    c = parse_citation("[пост 149, ветка @tatisimonenko]")
    assert c is not None
    assert c.kind == CITATION_KIND_PARTICIPANT_THREAD
    assert c.post_id == 149
    assert c.username == "@tatisimonenko"


def test_parse_participant_thread_without_at():
    """Без @ парсер тоже принимает, но канон добавляет @."""
    c = parse_citation("[пост 149, ветка tatisimonenko]")
    assert c is not None
    assert c.kind == CITATION_KIND_PARTICIPANT_THREAD
    assert c.username == "@tatisimonenko"


# ---------------------------------------------------------------------------
# parse_citation — transcript
# ---------------------------------------------------------------------------

def test_parse_transcript_long_form():
    c = parse_citation("[пост 850, расшифровка]")
    assert c is not None
    assert c.kind == CITATION_KIND_TRANSCRIPT
    assert c.post_id == 850


def test_parse_transcript_short_form():
    c = parse_citation("[расшифровка 850]")
    assert c is not None
    assert c.kind == CITATION_KIND_TRANSCRIPT
    assert c.post_id == 850


# ---------------------------------------------------------------------------
# parse_citation — book_section
# ---------------------------------------------------------------------------

def test_parse_book_section_simple_chapter():
    c = parse_citation("[книга «Границы и ответственность», гл. 3]")
    assert c is not None
    assert c.kind == CITATION_KIND_BOOK_SECTION
    assert c.book_title == "Границы и ответственность"
    assert c.chapter_path == ["3"]


def test_parse_book_section_nested_chapter():
    c = parse_citation("[книга «Границы и ответственность», гл. 3 › Практика отказа]")
    assert c is not None
    assert c.kind == CITATION_KIND_BOOK_SECTION
    assert c.book_title == "Границы и ответственность"
    assert c.chapter_path == ["3", "Практика отказа"]


def test_parse_book_section_three_levels():
    c = parse_citation("[книга «Название», гл. Часть 1 › Глава 3 › Раздел]")
    assert c is not None
    assert len(c.chapter_path) == 3
    assert c.chapter_path[0] == "Часть 1"
    assert c.chapter_path[2] == "Раздел"


# ---------------------------------------------------------------------------
# parse_citation — shelf, month
# ---------------------------------------------------------------------------

def test_parse_shelf():
    c = parse_citation("[полка messages]")
    assert c is not None
    assert c.kind == CITATION_KIND_SHELF
    assert c.shelf_kind == "messages"


def test_parse_month():
    c = parse_citation("[месяц 2024-09]")
    assert c is not None
    assert c.kind == CITATION_KIND_MONTH
    assert c.month == "2024-09"


# ---------------------------------------------------------------------------
# format_citation — каноническая форма
# ---------------------------------------------------------------------------

def test_format_post():
    c = Citation(kind=CITATION_KIND_POST, post_id=92)
    assert format_citation(c) == "[пост 92]"


def test_format_comment_canonical_has_number_sign():
    """Каноническая форма всегда с №."""
    c = Citation(kind=CITATION_KIND_COMMENT, post_id=92, comment_id=14457)
    assert format_citation(c) == "[пост 92, комментарий №14457]"


def test_format_participant_thread_canonical_has_at():
    """Каноническая форма всегда с @."""
    c = Citation(
        kind=CITATION_KIND_PARTICIPANT_THREAD,
        post_id=149, username="@tatisimonenko",
    )
    assert format_citation(c) == "[пост 149, ветка @tatisimonenko]"


def test_format_transcript_canonical_short_form():
    """Каноническая форма — короткая [расшифровка N]."""
    c = Citation(kind=CITATION_KIND_TRANSCRIPT, post_id=850)
    assert format_citation(c) == "[расшифровка 850]"


def test_format_book_section():
    c = Citation(
        kind=CITATION_KIND_BOOK_SECTION,
        book_title="Название",
        chapter_path=["3", "Практика отказа"],
    )
    assert format_citation(c) == "[книга «Название», гл. 3 › Практика отказа]"


# ---------------------------------------------------------------------------
# Round-trip: parse → format → parse — стабильность канона
# ---------------------------------------------------------------------------

def test_roundtrip_post():
    s = "[пост 92]"
    assert format_citation(parse_citation(s)) == s


def test_roundtrip_comment():
    s = "[пост 92, комментарий №14457]"
    assert format_citation(parse_citation(s)) == s


def test_roundtrip_participant_thread():
    """Без @ в исходной строке — канон добавит @, и round-trip даст с @."""
    s_in = "[пост 149, ветка tatisimonenko]"
    c = parse_citation(s_in)
    s_canon = format_citation(c)
    assert s_canon == "[пост 149, ветка @tatisimonenko]"
    # Повторный парс канонической формы稳定.
    assert format_citation(parse_citation(s_canon)) == s_canon


def test_roundtrip_book_section():
    s = "[книга «Название», гл. 3 › Практика отказа]"
    c = parse_citation(s)
    assert c is not None
    assert format_citation(c) == s


# ---------------------------------------------------------------------------
# to_url
# ---------------------------------------------------------------------------

def test_url_post():
    c = Citation(kind=CITATION_KIND_POST, post_id=92)
    assert to_url(c, "demo_philosophy_channel") == "#/a/demo_philosophy_channel/m/92"


def test_url_comment():
    c = Citation(kind=CITATION_KIND_COMMENT, post_id=92, comment_id=14457)
    assert to_url(c, "demo_philosophy_channel") == "#/a/demo_philosophy_channel/m/92?c=14457"


def test_url_participant_thread():
    c = Citation(
        kind=CITATION_KIND_PARTICIPANT_THREAD,
        post_id=149, username="@tatisimonenko",
    )
    assert to_url(c, "demo") == "#/a/demo/m/149?thread=tatisimonenko"


def test_url_transcript():
    c = Citation(kind=CITATION_KIND_TRANSCRIPT, post_id=850)
    assert to_url(c, "demo") == "#/a/demo/m/850?transcript=1"


def test_url_month():
    c = Citation(kind=CITATION_KIND_MONTH, month="2024-09")
    assert to_url(c, "demo") == "#/a/demo/month/2024-09"


def test_url_shelf():
    c = Citation(kind=CITATION_KIND_SHELF, shelf_kind="messages")
    assert to_url(c, "demo") == "#/a/demo/shelf/messages"


# ---------------------------------------------------------------------------
# find_citations
# ---------------------------------------------------------------------------

def test_find_citations_finds_all_in_text():
    text = (
        "Как прорабатывается обесценивание? См. [пост 100] и "
        "[пост 400, комментарий №402]. Подробности — [расшифровка 850]."
    )
    found = find_citations(text)
    assert len(found) == 3
    kinds = [c.kind for c, _ in found]
    assert kinds == [
        CITATION_KIND_POST,
        CITATION_KIND_COMMENT,
        CITATION_KIND_TRANSCRIPT,
    ]
    # Позиции корректны.
    for c, (start, end) in found:
        assert text[start:end].startswith("[")
        assert text[start:end].endswith("]")


def test_find_citations_ignores_non_citation_brackets():
    text = "Это [ремарка автора], не ссылка. А [пост 92] — ссылка."
    found = find_citations(text)
    assert len(found) == 1
    assert found[0][0].kind == CITATION_KIND_POST
    assert found[0][0].post_id == 92


def test_find_citations_empty_text():
    assert find_citations("") == []
    assert find_citations("без ссылок") == []


# ---------------------------------------------------------------------------
# validate_citation
# ---------------------------------------------------------------------------

def test_validate_post_exists(opened_philosophy):
    archive, parser_db = opened_philosophy
    c = Citation(kind=CITATION_KIND_POST, post_id=400)
    result = validate_citation(c, archive, parser_db)
    assert result["ok"] is True


def test_validate_post_not_found(opened_philosophy):
    archive, parser_db = opened_philosophy
    c = Citation(kind=CITATION_KIND_POST, post_id=99999)
    result = validate_citation(c, archive, parser_db)
    assert result["ok"] is False
    assert "99999" in result["error"]


def test_validate_comment_correct_parent(opened_philosophy):
    archive, parser_db = opened_philosophy
    # Комментарий 401 принадлежит посту 400.
    c = Citation(kind=CITATION_KIND_COMMENT, post_id=400, comment_id=401)
    result = validate_citation(c, archive, parser_db)
    assert result["ok"] is True


def test_validate_comment_wrong_parent(opened_philosophy):
    archive, parser_db = opened_philosophy
    # Комментарий 401 принадлежит посту 400, не 100.
    c = Citation(kind=CITATION_KIND_COMMENT, post_id=100, comment_id=401)
    result = validate_citation(c, archive, parser_db)
    assert result["ok"] is False
    assert "hint" in result  # подсказка с правильным постом


def test_validate_participant_thread_exists(opened_philosophy):
    archive, parser_db = opened_philosophy
    # Под постом 900 есть ветка @marina_s.
    c = Citation(
        kind=CITATION_KIND_PARTICIPANT_THREAD,
        post_id=900, username="@marina_s",
    )
    result = validate_citation(c, archive, parser_db)
    assert result["ok"] is True


def test_validate_participant_thread_wrong_user(opened_philosophy):
    archive, parser_db = opened_philosophy
    c = Citation(
        kind=CITATION_KIND_PARTICIPANT_THREAD,
        post_id=900, username="@nonexistent_user",
    )
    result = validate_citation(c, archive, parser_db)
    assert result["ok"] is False


def test_validate_transcript_from_db(opened_philosophy):
    archive, parser_db = opened_philosophy
    # Пост 300 — голосовое, расшифровка в parser.db.
    c = Citation(kind=CITATION_KIND_TRANSCRIPT, post_id=300)
    result = validate_citation(c, archive, parser_db)
    assert result["ok"] is True


def test_validate_transcript_from_file(opened_philosophy):
    archive, parser_db = opened_philosophy
    # Пост 850 — голосовое, расшифровка в файле (00_Индекс.md).
    c = Citation(kind=CITATION_KIND_TRANSCRIPT, post_id=850)
    result = validate_citation(c, archive, parser_db)
    assert result["ok"] is True


def test_validate_transcript_missing(opened_philosophy):
    archive, parser_db = opened_philosophy
    # Пост 400 — обычный, без расшифровки.
    c = Citation(kind=CITATION_KIND_TRANSCRIPT, post_id=400)
    result = validate_citation(c, archive, parser_db)
    assert result["ok"] is False


def test_validate_shelf_exists(opened_philosophy):
    archive, parser_db = opened_philosophy
    c = Citation(kind=CITATION_KIND_SHELF, shelf_kind="messages")
    result = validate_citation(c, archive, parser_db)
    assert result["ok"] is True


def test_validate_shelf_missing(opened_philosophy):
    archive, parser_db = opened_philosophy
    c = Citation(kind=CITATION_KIND_SHELF, shelf_kind="nonexistent")
    result = validate_citation(c, archive, parser_db)
    assert result["ok"] is False
    assert "hint" in result  # подсказка с доступными полками


def test_validate_month_exists(opened_philosophy):
    archive, parser_db = opened_philosophy
    c = Citation(kind=CITATION_KIND_MONTH, month="2024-09")
    result = validate_citation(c, archive, parser_db)
    assert result["ok"] is True


def test_validate_month_missing(opened_philosophy):
    archive, parser_db = opened_philosophy
    c = Citation(kind=CITATION_KIND_MONTH, month="1990-01")
    result = validate_citation(c, archive, parser_db)
    assert result["ok"] is False


# ---------------------------------------------------------------------------
# citation_for_group — интеграция с группами
# ---------------------------------------------------------------------------

def test_citation_for_post_thread_group(opened_philosophy):
    archive, parser_db = opened_philosophy
    builder = GroupsBuilder(archive, parser_db)
    groups = builder.post_threads()
    g400 = next(g for g in groups if g.keys["post_message_id"] == 400)
    cit = citation_for_group(g400)
    assert cit is not None
    assert cit.kind == CITATION_KIND_POST
    assert cit.post_id == 400
    assert format_citation(cit) == "[пост 400]"


def test_citation_for_participant_thread_group(opened_philosophy):
    archive, parser_db = opened_philosophy
    builder = GroupsBuilder(archive, parser_db)
    groups = builder.participant_threads()
    marina = next(
        g for g in groups
        if g.keys.get("post_message_id") == 900
        and g.keys.get("username") == "@marina_s"
    )
    cit = citation_for_group(marina)
    assert cit is not None
    assert cit.kind == CITATION_KIND_PARTICIPANT_THREAD
    assert cit.post_id == 900
    assert cit.username == "@marina_s"
    assert format_citation(cit) == "[пост 900, ветка @marina_s]"


def test_citation_for_transcript_group(opened_philosophy):
    archive, parser_db = opened_philosophy
    builder = GroupsBuilder(archive, parser_db)
    groups = builder.transcripts()
    g850 = next(g for g in groups if g.keys["post_message_id"] == 850)
    cit = citation_for_group(g850)
    assert cit is not None
    assert cit.kind == CITATION_KIND_TRANSCRIPT
    assert cit.post_id == 850
    assert format_citation(cit) == "[расшифровка 850]"


def test_citation_for_cycle_group_is_none(opened_philosophy):
    archive, parser_db = opened_philosophy
    builder = GroupsBuilder(archive, parser_db)
    groups = builder.cycles()
    assert len(groups) > 0
    # Для cycle citation не определён — мета-группа.
    for g in groups:
        assert citation_for_group(g) is None


def test_citation_for_month_group_is_none(opened_philosophy):
    archive, parser_db = opened_philosophy
    builder = GroupsBuilder(archive, parser_db)
    groups = builder.months()
    assert len(groups) > 0
    for g in groups:
        assert citation_for_group(g) is None


# ---------------------------------------------------------------------------
# Group.to_dict() / GroupHit.to_dict() — поле citation
# ---------------------------------------------------------------------------

def test_group_to_dict_has_citation_field(opened_philosophy):
    archive, parser_db = opened_philosophy
    builder = GroupsBuilder(archive, parser_db)
    g400 = next(
        g for g in builder.post_threads()
        if g.keys["post_message_id"] == 400
    )
    d = g400.to_dict()
    assert "citation" in d
    assert d["citation"] == "[пост 400]"


def test_group_to_dict_citation_none_for_cycle(opened_philosophy):
    archive, parser_db = opened_philosophy
    builder = GroupsBuilder(archive, parser_db)
    g = builder.cycles()[0]
    d = g.to_dict()
    assert d["citation"] is None


def test_grouphit_to_dict_has_citation_field(opened_philosophy):
    """GroupHit.to_dict тоже включает citation — для топ-групп в поиске."""
    from core.groups import GroupHit
    gh = GroupHit(
        group_id="post_thread:400",
        type=GROUP_TYPE_POST_THREAD,
        label="Пост 400",
        keys={"post_message_id": 400},
        chat_id=1,
        extras={},
        matched_count=2,
        total_count=4,
        best_rank=0,
        best_message_id=400,
        matched_message_ids=[400, 401],
    )
    d = gh.to_dict()
    assert d["citation"] == "[пост 400]"


# ---------------------------------------------------------------------------
# End-to-end: текст ответа модели → список Citation'ов → валидация
# ---------------------------------------------------------------------------

def test_end_to_end_model_answer(opened_philosophy):
    """Эталонный сценарий этапа 3: модель пишет ответ с ссылками,
    Верификатор их находит и проверяет."""
    archive, parser_db = opened_philosophy
    # Примерный ответ модели.
    text = (
        "Обесценивание — защитный механизм [пост 100]. В обсуждении "
        "различают скромность и обесценивание [пост 100, комментарий №102]. "
        "Про труд — отдельно [пост 400]. Голосовой разбор проекции — "
        "[расшифровка 850]. Ветка @marina_s про границы — [пост 900, ветка @marina_s]. "
        "Несуществующее: [пост 99999] и [пост 100, комментарий №99999]."
    )
    found = find_citations(text)
    # 5 реальных ссылок + 2 несуществующие = 7 всего.
    # find_citations не проверяет существование — это работа validate_citation.
    assert len(found) == 7
    results = [validate_citation(c, archive, parser_db) for c, _ in found]
    ok_count = sum(1 for r in results if r["ok"])
    # 5 реальных + 2 несуществующие = 5 ok, 2 не ok.
    assert ok_count == 5
    bad = [r for r in results if not r["ok"]]
    assert len(bad) == 2
    results = [validate_citation(c, archive, parser_db) for c, _ in found]
    ok_count = sum(1 for r in results if r["ok"])
    # 4 реальные ссылки + 2 несуществующие = 4 ok, 2 не ok.
    assert ok_count == 4
    bad = [r for r in results if not r["ok"]]
    assert len(bad) == 2
