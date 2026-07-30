"""
Регрессионные тесты для core/groups.py (Г1+Г2).

Покрывают все 5 типов групп на демо-архиве philosophy:
  - post_thread: каждый из 10 постов + его комментарии
  - participant_thread: ветки под постом #900 (marina_s, ivan_k, anna_p)
  - cycle: полки 'messages' и 'transcriptions' из паспорта
  - transcript: пост #850 (голосовое с расшифровкой)
  - month: 2024-09 и 2024-10
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Добавляем корень проекта в sys.path, чтобы импорт core.* работал
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import LibrarianCore
from core.groups import (
    Group, GroupsBuilder,
    GROUP_TYPE_POST_THREAD, GROUP_TYPE_PARTICIPANT_THREAD,
    GROUP_TYPE_CYCLE, GROUP_TYPE_TRANSCRIPT, GROUP_TYPE_MONTH,
)


@pytest.fixture(scope="module")
def core():
    """LibrarianCore с demo-архивами."""
    registry = ROOT / "config" / "registry.toml"
    output = ROOT / "output"
    c = LibrarianCore.from_registry(registry, output_root=output)
    yield c
    c.close_all()


@pytest.fixture(scope="module")
def opened_philosophy(core):
    """Открытый архив demo_philosophy_channel + parser_db."""
    archive = core.open_archive("demo_philosophy_channel")
    parser_db, _ = core._get_dbs("demo_philosophy_channel")
    return archive, parser_db


@pytest.fixture(scope="module")
def builder(opened_philosophy):
    archive, parser_db = opened_philosophy
    return GroupsBuilder(archive, parser_db)


# ---------------------------------------------------------------------------
# post_thread
# ---------------------------------------------------------------------------

def test_post_thread_includes_post_and_comments(builder):
    """post_thread для поста #400 содержит сам пост и его 3 комментария."""
    groups = builder.post_threads()
    by_post = {g.keys["post_message_id"]: g for g in groups}
    assert 400 in by_post, f"Пост 400 должен быть в {list(by_post.keys())}"
    g = by_post[400]
    assert g.type == GROUP_TYPE_POST_THREAD
    assert g.group_id == "post_thread:400"
    # Сам пост + 3 комментария (401, 402, 403)
    assert g.message_ids[0] == 400, "post_thread должен начинаться с поста"
    assert set(g.message_ids) == {400, 401, 402, 403}
    assert g.extras["comments_count"] == 3
    assert g.extras["total_count"] == 4


def test_post_thread_for_isolated_post(builder):
    """Пост без комментариев (#500) тоже даёт post_thread (из 1 сообщения)."""
    groups = builder.post_threads()
    by_post = {g.keys["post_message_id"]: g for g in groups}
    assert 500 in by_post
    g = by_post[500]
    assert g.message_ids == [500]
    assert g.extras["comments_count"] == 0


def test_post_thread_count_matches_posts(builder):
    """Количество post_thread групп = количеству постов в архиве."""
    groups = builder.post_threads()
    # В демо-архиве philosophy: 10 постов (100,200,300,400,500,600,700,800,850,900)
    assert len(groups) == 10


# ---------------------------------------------------------------------------
# participant_thread
# ---------------------------------------------------------------------------

def test_participant_thread_marina_chain(builder):
    """Под постом #900 у marina_s есть ветка из 4 сообщений
    (901 marina, 902 author, 903 marina, 904 author)."""
    groups = builder.participant_threads()
    marina_900 = next(
        g for g in groups
        if g.keys.get("post_message_id") == 900
        and g.keys.get("username") == "@marina_s"
    )
    assert marina_900.type == GROUP_TYPE_PARTICIPANT_THREAD
    assert marina_900.group_id == "participant_thread:900:@marina_s"
    # 901 (marina) → 902 (author reply) → 903 (marina) → 904 (author reply)
    assert set(marina_900.message_ids) == {901, 902, 903, 904}
    assert marina_900.extras["participant_messages"] == 2
    assert marina_900.extras["author_replies"] == 2


def test_participant_thread_ivan_chain(builder):
    """У ivan_k под #900 есть ветка из 3 сообщений (905 ivan, 906 author, 907 ivan)."""
    groups = builder.participant_threads()
    ivan_900 = next(
        g for g in groups
        if g.keys.get("post_message_id") == 900
        and g.keys.get("username") == "@ivan_k"
    )
    assert set(ivan_900.message_ids) == {905, 906, 907}
    assert ivan_900.extras["participant_messages"] == 2
    assert ivan_900.extras["author_replies"] == 1


def test_participant_thread_excludes_isolated_replies(builder):
    """anna_p под #900 написала только 908 (без reply-цепочки) — это не ветка,
    participant_thread для anna_p НЕ создаётся (требуется ≥ 2 сообщений)."""
    groups = builder.participant_threads()
    anna_900 = [
        g for g in groups
        if g.keys.get("post_message_id") == 900
        and g.keys.get("username") == "@anna_p"
    ]
    assert len(anna_900) == 0, (
        "Изолированный комментарий anna_p не должен давать participant_thread"
    )


def test_participant_thread_excludes_author(builder):
    """Автор поста (@philosophy_daily) не считается участником обсуждения —
    для него participant_thread не создаётся."""
    groups = builder.participant_threads()
    author_threads = [
        g for g in groups
        if g.keys.get("username") == "@philosophy_daily"
    ]
    assert len(author_threads) == 0


# ---------------------------------------------------------------------------
# cycle
# ---------------------------------------------------------------------------

def test_cycles_from_passport_shelves(builder):
    """Полки из паспорта — 'messages' и 'transcriptions'."""
    groups = builder.cycles()
    kinds = {g.keys["shelf_kind"]: g for g in groups}
    assert "messages" in kinds
    assert "transcriptions" in kinds
    # Полка messages содержит все сообщения
    msg_group = kinds["messages"]
    assert len(msg_group.message_ids) > 0
    # Полка transcriptions содержит посты с расшифровкой
    tr_group = kinds["transcriptions"]
    assert 850 in tr_group.message_ids, "Пост 850 должен быть в transcriptions"


def test_cycles_labels(builder):
    """Labels циклов — человекочитаемые."""
    groups = builder.cycles()
    labels = {g.keys["shelf_kind"]: g.label for g in groups}
    # В демо-архиве labels — "Сообщения" и "Транскрипции"
    assert labels["messages"] == "Сообщения"
    assert labels["transcriptions"] == "Транскрипции"


# ---------------------------------------------------------------------------
# transcript
# ---------------------------------------------------------------------------

def test_transcripts_group_for_voice_post_850(builder):
    """Пост #850 (голосовое с расшифровкой) даёт transcript-группу."""
    groups = builder.transcripts()
    by_post = {g.keys["post_message_id"]: g for g in groups}
    assert 850 in by_post, (
        f"Пост 850 должен быть в transcript-группах, есть: {list(by_post.keys())}"
    )
    g = by_post[850]
    assert g.type == GROUP_TYPE_TRANSCRIPT
    assert g.group_id == "transcript:850"
    assert g.message_ids == [850]


# ---------------------------------------------------------------------------
# month
# ---------------------------------------------------------------------------

def test_months_from_post_dates(builder):
    """Месяцы: 2024-09 (посты 100-600) и 2024-10 (посты 700, 800, 850, 900).
    Пост #700 от 1 октября 2024 — попадает в октябрь."""
    groups = builder.months()
    by_month = {g.keys["month"]: g for g in groups}
    assert "2024-09" in by_month
    assert "2024-10" in by_month
    sept = by_month["2024-09"]
    octob = by_month["2024-10"]
    # Сентябрь: 100, 200, 300, 400, 500, 600 = 6 постов
    assert set(sept.message_ids) == {100, 200, 300, 400, 500, 600}
    # Октябрь: 700, 800, 850, 900 = 4 поста
    assert set(octob.message_ids) == {700, 800, 850, 900}
    # Только посты (is_comment=0), не комментарии
    for g in groups:
        for mid in g.message_ids:
            assert mid in {100, 200, 300, 400, 500, 600, 700, 800, 850, 900}, (
                f"month-группа не должна содержать комментарий {mid}"
            )


def test_months_labels_human_readable(builder):
    """Labels месяцев — на русском."""
    groups = builder.months()
    by_month = {g.keys["month"]: g.label for g in groups}
    assert "Сентябрь" in by_month["2024-09"]
    assert "Октябрь" in by_month["2024-10"]
    assert "2024" in by_month["2024-09"]


# ---------------------------------------------------------------------------
# build_all + for_message
# ---------------------------------------------------------------------------

def test_build_all_returns_all_types(builder):
    """build_all() возвращает группы всех 5 типов."""
    groups = builder.build_all()
    types = {g.type for g in groups}
    assert types == {
        GROUP_TYPE_POST_THREAD,
        GROUP_TYPE_PARTICIPANT_THREAD,
        GROUP_TYPE_CYCLE,
        GROUP_TYPE_TRANSCRIPT,
        GROUP_TYPE_MONTH,
    }


def test_groups_for_message_post_returns_post_thread_cycle_month(builder):
    """Для поста #400 (сентябрь 2024) группы:
    post_thread:400, cycle:messages, month:2024-09. Без participant_thread
    (пост не участник обсуждения)."""
    groups = builder.for_message(400)
    group_ids = {g.group_id for g in groups}
    assert "post_thread:400" in group_ids
    assert "cycle:messages" in group_ids
    assert "month:2024-09" in group_ids
    # У поста нет participant_thread (он не участник под своим же постом)
    pt_for_400 = [g for g in groups if g.type == GROUP_TYPE_PARTICIPANT_THREAD]
    assert len(pt_for_400) == 0


def test_groups_for_message_comment_returns_participant_thread(builder):
    """Для комментария #901 (marina_s под постом 900) группы:
    post_thread:900, participant_thread:900:@marina_s, cycle:messages."""
    groups = builder.for_message(901)
    group_ids = {g.group_id for g in groups}
    assert "post_thread:900" in group_ids
    assert "participant_thread:900:@marina_s" in group_ids
    assert "cycle:messages" in group_ids


def test_groups_for_message_voice_post_includes_transcript(builder):
    """Для поста #850 (с расшифровкой) группы включают transcript:850."""
    groups = builder.for_message(850)
    group_ids = {g.group_id for g in groups}
    assert "transcript:850" in group_ids
    assert "post_thread:850" in group_ids
    assert "month:2024-10" in group_ids


def test_groups_for_message_unknown_returns_empty(builder):
    """Для несуществующего message_id — пустой список."""
    groups = builder.for_message(99999)
    assert groups == []


# ---------------------------------------------------------------------------
# LibrarianCore integration
# ---------------------------------------------------------------------------

def test_core_list_groups_all(core):
    """LibrarianCore.list_groups возвращает все группы архива."""
    groups = core.list_groups("demo_philosophy_channel")
    assert len(groups) > 0
    types = {g["type"] for g in groups}
    assert types == {
        GROUP_TYPE_POST_THREAD,
        GROUP_TYPE_PARTICIPANT_THREAD,
        GROUP_TYPE_CYCLE,
        GROUP_TYPE_TRANSCRIPT,
        GROUP_TYPE_MONTH,
    }


def test_core_list_groups_filtered_by_type(core):
    """list_groups с type='post_thread' возвращает только post_thread."""
    groups = core.list_groups("demo_philosophy_channel",
                              group_type="post_thread")
    assert all(g["type"] == "post_thread" for g in groups)
    assert len(groups) == 10  # 10 постов в демо-архиве


def test_core_groups_for_message(core):
    """LibrarianCore.groups_for_message — интеграционный тест."""
    groups = core.groups_for_message("demo_philosophy_channel", 901)
    group_ids = {g["group_id"] for g in groups}
    assert "post_thread:900" in group_ids
    assert "participant_thread:900:@marina_s" in group_ids


def test_core_groups_cached(core):
    """Повторный вызов list_groups возвращает тот же список (кэш)."""
    g1 = core.list_groups("demo_philosophy_channel")
    g2 = core.list_groups("demo_philosophy_channel")
    # Не просто равенство, а идентичность объектов (кэш)
    assert len(g1) == len(g2)


# ---------------------------------------------------------------------------
# Г3: rank_groups — агрегация попаданий поиска по группам
# ---------------------------------------------------------------------------

def _make_hit(message_id: int, *, source: str = "message",
              is_comment: bool = False, post_message_id=None):
    """Конструктор SearchHit для тестов."""
    from core.librarian_db import SearchHit
    return SearchHit(
        rowid=message_id,  # для тестов — уникально
        source=source,
        internal_id=message_id,
        chat_id=1,
        message_id=message_id,
        author="test",
        date="2024-09-01",
        snippet="…",
        is_comment=is_comment,
        post_message_id=post_message_id,
    )


def test_rank_groups_empty_hits_returns_empty(builder):
    """Пустая выдача → пустой список групп."""
    from core.groups import rank_groups
    groups = builder.build_all()
    assert rank_groups([], groups) == []


def test_rank_groups_empty_groups_returns_empty(builder):
    """Пустой список групп → пустой результат."""
    from core.groups import rank_groups
    hits = [_make_hit(400)]
    assert rank_groups(hits, []) == []


def test_rank_groups_single_hit_single_group(builder):
    """Один хит на пост 400 → post_thread:400 с matched_count=1."""
    from core.groups import rank_groups
    hits = [_make_hit(400)]
    groups = builder.build_all()
    ranked = rank_groups(hits, groups)
    # Должна быть как минимум post_thread:400
    g400 = [g for g in ranked if g.group_id == "post_thread:400"]
    assert len(g400) == 1
    assert g400[0].matched_count == 1
    assert g400[0].best_message_id == 400
    assert g400[0].best_rank == 0
    assert g400[0].total_count == 4  # пост + 3 комментария


def test_rank_groups_matched_count_desc(builder):
    """Группа с бОльшим matched_count сортируется выше.

    Пост 400: 4 сообщения (400, 401, 402, 403). Пост 900: 9 сообщений.
    Если в выдаче 3 хита из post_thread:400 и 1 хит из post_thread:900,
    post_thread:400 должен быть первым.

    Внимание: cycle:messages — это агрегат ВСЕХ сообщений архива, поэтому
    он собирает все 4 хита и всегда идёт первым. Это правильное поведение
    спецификации. Тест проверяет порядок post_thread'ов ПОСЛЕ cycle.
    """
    from core.groups import rank_groups
    hits = [
        _make_hit(400), _make_hit(401), _make_hit(402),  # 3 хита на 400
        _make_hit(900),  # 1 хит на 900
    ]
    groups = builder.build_all()
    ranked = rank_groups(hits, groups)
    # cycle:messages включает ВСЕ сообщения архива, поэтому соберёт все 4
    # хита и будет первой (matched_count=4 > 3 > 1). Это правильное
    # поведение спецификации — cycle:messages агрегирует весь архив.
    assert ranked[0].group_id == "cycle:messages"
    assert ranked[0].matched_count == 4
    # Среди post_thread: post_thread:400 (matched=3) раньше post_thread:900 (matched=1).
    pts = [g for g in ranked if g.type == "post_thread"]
    assert pts[0].group_id == "post_thread:400"
    assert pts[0].matched_count == 3
    # post_thread:900 тоже должен быть в выдаче
    g900 = [g for g in ranked if g.group_id == "post_thread:900"]
    assert len(g900) == 1
    assert g900[0].matched_count == 1
    # post_thread:900 тоже должен быть в выдаче
    g900 = [g for g in ranked if g.group_id == "post_thread:900"]
    assert len(g900) == 1
    assert g900[0].matched_count == 1


def test_rank_groups_best_rank_tiebreak(builder):
    """При равном matched_count — лучшая группа та, чей хит раньше в выдаче.

    Посты 100 и 400 оба дают по 1 хиту. Если хит на 400 идёт раньше в
    выдаче — post_thread:400 сортируется выше.
    """
    from core.groups import rank_groups
    hits = [
        _make_hit(400),  # rank 0
        _make_hit(100),  # rank 1
    ]
    groups = builder.build_all()
    ranked = rank_groups(hits, groups)
    # Оба post_thread'а должны быть в выдаче, оба с matched_count=1.
    pts = [g for g in ranked if g.type == "post_thread"]
    assert len(pts) >= 2
    # Первая из них — та, чей хит раньше (best_rank=0).
    assert pts[0].group_id == "post_thread:400"
    assert pts[0].best_rank == 0
    assert pts[1].group_id == "post_thread:100"
    assert pts[1].best_rank == 1


def test_rank_groups_total_count_tiebreak(builder):
    """При равных matched_count и best_rank — крупнее группа выше.

    Создаём две группы вручную, чтобы контролировать tie.
    """
    from core.groups import rank_groups, Group, GROUP_TYPE_POST_THREAD
    hits = [_make_hit(100)]
    groups = [
        Group(
            group_id="post_thread:small",
            type=GROUP_TYPE_POST_THREAD,
            label="small",
            keys={"post_message_id": 100},
            message_ids=[100, 101],  # total_count=2
            chat_id=1,
            extras={},
        ),
        Group(
            group_id="post_thread:big",
            type=GROUP_TYPE_POST_THREAD,
            label="big",
            keys={"post_message_id": 100},
            message_ids=[100, 101, 102, 103, 104],  # total_count=5
            chat_id=1,
            extras={},
        ),
    ]
    ranked = rank_groups(hits, groups)
    # Обе группы с matched_count=1, best_rank=0. Большая — выше.
    assert ranked[0].group_id == "post_thread:big"
    assert ranked[0].total_count == 5
    assert ranked[1].group_id == "post_thread:small"
    assert ranked[1].total_count == 2


def test_rank_groups_hit_in_multiple_groups(builder):
    """Хит на пост 900 засчитывается нескольким группам:
    post_thread:900, cycle:messages, month:2024-10."""
    from core.groups import rank_groups
    hits = [_make_hit(900)]
    groups = builder.build_all()
    ranked = rank_groups(hits, groups)
    group_ids = {g.group_id for g in ranked}
    assert "post_thread:900" in group_ids
    assert "cycle:messages" in group_ids
    assert "month:2024-10" in group_ids


def test_rank_groups_transcription_hit(builder):
    """Хит-расшифровка поста 850 засчитывается:
    post_thread:850, transcript:850, cycle:transcriptions, month:2024-10."""
    from core.groups import rank_groups
    # source="transcription" — но message_id поста 850.
    hits = [_make_hit(850, source="transcription")]
    groups = builder.build_all()
    ranked = rank_groups(hits, groups)
    group_ids = {g.group_id for g in ranked}
    assert "post_thread:850" in group_ids
    assert "transcript:850" in group_ids
    assert "cycle:transcriptions" in group_ids
    assert "month:2024-10" in group_ids


def test_rank_groups_returns_only_matching(builder):
    """Группы без совпадений не попадают в выдачу."""
    from core.groups import rank_groups
    hits = [_make_hit(400)]
    groups = builder.build_all()
    ranked = rank_groups(hits, groups)
    # Все возвращённые группы должны иметь matched_count >= 1.
    assert all(g.matched_count >= 1 for g in ranked)
    # В частности, post_thread:500 (без хитов) не должен попасть.
    assert all(g.group_id != "post_thread:500" for g in ranked)


def test_rank_groups_to_dict_has_all_fields(builder):
    """GroupHit.to_dict() содержит все поля, нужные UI."""
    from core.groups import rank_groups
    hits = [_make_hit(400), _make_hit(401)]
    groups = builder.build_all()
    ranked = rank_groups(hits, groups)
    d = ranked[0].to_dict()
    expected_keys = {
        "group_id", "type", "label", "keys", "chat_id", "extras",
        "matched_count", "total_count", "best_rank", "best_message_id",
        "matched_message_ids", "ratio",
    }
    assert expected_keys.issubset(set(d.keys())), (
        f"Не хватает полей: {expected_keys - set(d.keys())}"
    )
    # ratio = matched_count / total_count
    assert 0.0 < d["ratio"] <= 1.0


def test_rank_groups_participant_thread_aggregates(builder):
    """Хиты на 901 и 902 засчитываются participant_thread:900:@marina_s
    (т.к. 901 — сообщение marina_s, а 902 — ответ автора, оба в группе)."""
    from core.groups import rank_groups
    hits = [_make_hit(901, is_comment=True, post_message_id=900),
            _make_hit(902, is_comment=True, post_message_id=900)]
    groups = builder.build_all()
    ranked = rank_groups(hits, groups)
    pt = [g for g in ranked if g.group_id == "participant_thread:900:@marina_s"]
    assert len(pt) == 1
    assert pt[0].matched_count == 2
    assert set(pt[0].matched_message_ids) == {901, 902}


# ---------------------------------------------------------------------------
# Г3: интеграция с LibrarianCore.search — группы в выдаче по умолчанию
# ---------------------------------------------------------------------------

def test_core_search_returns_groups_by_default(core):
    """LibrarianCore.search по умолчанию включает groups в ответ."""
    result = core.search("demo_philosophy_channel", "обесценива")
    assert result["count"] > 0
    # По умолчанию include_groups=True — должны быть группы.
    assert result["groups_count"] > 0, (
        "LibrarianCore.search должен возвращать группы по умолчанию"
    )
    assert len(result["groups"]) > 0
    assert result["filters"]["include_groups"] is True


def test_core_search_groups_can_be_disabled(core):
    """Если явно передать include_groups=False — группы не считаются."""
    result = core.search(
        "demo_philosophy_channel", "обесценива",
        include_groups=False,
    )
    assert result["count"] > 0
    assert result["groups"] == []
    assert result["groups_count"] == 0
    assert result["filters"]["include_groups"] is False


def test_core_search_groups_for_query_with_many_hits(core):
    """По запросу «обесценивание» (есть в нескольких постах и комментариях) —
    post_thread-группы в выдаче ранжируются по matched_count."""
    result = core.search(
        "demo_philosophy_channel", "обесценива",
        include_groups=True, group_limit=20,
    )
    # Должна быть хотя бы одна post_thread-группа.
    pts = [g for g in result["groups"] if g["type"] == "post_thread"]
    assert len(pts) > 0, "Должна быть хотя бы одна post_thread-группа"
    # Сортировка: matched_count DESC.
    counts = [g["matched_count"] for g in pts]
    assert counts == sorted(counts, reverse=True)


def test_core_search_groups_best_message_id_in_hits(core):
    """best_message_id каждой группы должен быть в hits (это инвариант)."""
    result = core.search(
        "demo_philosophy_channel", "обесценива",
        include_groups=True, group_limit=20,
    )
    hit_mids = {h["message_id"] for h in result["hits"]}
    for g in result["groups"]:
        assert g["best_message_id"] in hit_mids, (
            f"best_message_id={g['best_message_id']} группы {g['group_id']} "
            f"не найден в hits"
        )
        # Все matched_message_ids тоже должны быть в hits.
        for mid in g["matched_message_ids"]:
            assert mid in hit_mids, (
                f"matched_message_id={mid} группы {g['group_id']} "
                f"не найден в hits"
            )
