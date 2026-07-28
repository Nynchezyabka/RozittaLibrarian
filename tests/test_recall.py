"""
tests/test_recall.py — сквозной тест recall@k для LibrarianDB.search().

Две части:

1. test_recall_on_demo_archive
   Гоняет синтетический мини-набор вопросов (вшит в тест) через демо-архив
   «Философия буднего дня». Проверяет, что механика оценки работает
   и что фиксы Б1/Б2/Б3 дают ненулевой recall на этом архиве.
   Этот тест всегда проходит — если он падает, регресс в коде поиска.

2. test_recall_on_real_archive (skip by default)
   Если рядом лежит scripts/eval_questions.yaml и указан путь к реальному
   архиву МГ через переменную окружения LIBRARIAN_REAL_ARCHIVE, прогоняет
   полный замер из librarian_статус.md. Без переменной skip-ается.

Запуск:
    pytest tests/test_recall.py -v

Полный замер на реальном архиве МГ:
    LIBRARIAN_REAL_ARCHIVE=/path/to/мг_архив pytest tests/test_recall.py::test_recall_on_real_archive -v -s
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from core.archive import ArchiveDiscovery, Archive
from core.librarian_db import LibrarianDB
from core.parser_db import ParserDB
from core.recall_eval import (
    EvalQuestion, EvalReport, evaluate_recall, load_questions,
)


# ---------------------------------------------------------------------------
# Фикстуры
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def demo_archives():
    import make_demo_archive
    out = PROJECT_ROOT / "output"
    make_demo_archive.main()
    return out


@pytest.fixture
def philosophy_archive(demo_archives) -> Archive:
    archives = ArchiveDiscovery(demo_archives).list_archives()
    a = next((a for a in archives if "demo_philosophy" in a.id), None)
    assert a is not None
    return a


@pytest.fixture
def opened_philosophy(philosophy_archive):
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
# 1. Синтетический мини-замер на демо-архиве
# ---------------------------------------------------------------------------

# Демо-архив «Философия буднего дня» (см. scripts/make_demo_archive.py)
# содержит 25 сообщений:
#   - пост 100: «Сегодня поговорим про обесценивание...»
#   - пост 200: «Привычка — это автоматический паттерн...»
#   - пост 300: «Идентификация: почему мы перенимаем чужие состояния...»
#   - пост 400: «Инфраструктура внимания...»
#   - пост 500: «Ценности — это не абстракция...»
#   - пост 700: «Скромность — про форму подачи...»
#   - пост 800: «Зависть и сравнение...»
#   - пост 850: голосовое «Проекция и идентификация» (транскрипт файлом)
#   + комментарии 101, 201, 301, 401, 501, 701-704, 801-804, 851-853
#
# Синтетические вопросы подобраны так, чтобы ответ в индексе ЕСТЬ
# и фиксы Б1/Б2/Б3 были необходимы для нахождения.

DEMO_QUESTIONS = [
    EvalQuestion(
        id="demo_q1",
        type="terminological",
        measures="retrieval",
        question="Что такое обесценивание и почему оно возникает?",
        expected_posts=[100],         # пост 100 — основной
        min_hits=1,
        top_k=10,
    ),
    EvalQuestion(
        id="demo_q2",
        type="terminological",
        measures="retrieval",
        # Б1 в действии: «о зависти» должно находить пост 800 со словом «зависть».
        # Без обрезки: "зависти*" НЕ матчит «зависть» → miss.
        # Со стеммингом: "завист"* матчит «зависть» → hit.
        question="Что в материалах говорится о зависти?",
        expected_posts=[800],
        min_hits=1,
        top_k=10,
    ),
    EvalQuestion(
        id="demo_q3",
        type="terminological",
        measures="retrieval",
        # Б1 в действии: «сравнения» → стемминг до «сравн» → матчит «сравнение» в посте 800.
        question="Как работает сравнение в психологическом контексте?",
        expected_posts=[800],
        min_hits=1,
        top_k=10,
    ),
    EvalQuestion(
        id="demo_q4",
        type="terminological",
        measures="retrieval",
        # Б3 в действии: «проекция и идентификация» — слова есть ТОЛЬКО в
        # файле-расшифровке поста 850, в самой messages-таблице их нет.
        # Без парсера 00_Индекс.md — miss.
        question="Что такое проекция и идентификация в разборе автора?",
        expected_posts=[850],
        min_hits=1,
        top_k=10,
    ),
    EvalQuestion(
        id="demo_q5",
        type="thematic",
        measures="retrieval",
        # Широкий запрос: «обесценивание» встречается и в посте 100,
        # и в нескольких комментариях. min_hits=2 — должно пройти.
        question="Где у автора обсуждается обесценивание как защитный механизм?",
        expected_posts=[100, 101],  # пост + комментарий
        min_hits=2,
        top_k=10,
    ),
]


def test_recall_on_demo_archive(opened_philosophy):
    """Сквозной recall-замер на демо-архиве.

    Гоняет 5 синтетических вопросов через LibrarianDB.search() с
    quota_per_kind=5 (как в замере 28.07 из librarian_статус.md).
    Ожидаем: ≥4/5 soft, ≥3/5 strict.

    Падение ниже — регресс в Б1/Б2/Б3:
      - demo_q2 miss → сломалась Б1 (stem)
      - demo_q3 miss → сломалась Б1 (stem)
      - demo_q4 miss → сломалась Б3 (00_Индекс.md парсер)
      - demo_q1 miss → вообще FTS не работает
      - strict <3 → сломалась Б2 (квоты, всё ушло в комментарии)
    """
    archive, parser_db, lib_db = opened_philosophy

    report = evaluate_recall(
        lib_db,
        DEMO_QUESTIONS,
        top_k_override=None,
        quota_per_kind=5,
        include_measures=("retrieval",),
    )

    print()
    print(report.detailed())

    assert report.total == 5, f"Должно быть 5 вопросов, получили {report.total}"
    assert report.skipped == [], f"Никто не должен был пропуститься: {report.skipped}"

    # Мягкий зачёт: минимум 4/5
    assert report.passed_soft >= 4, (
        f"Soft recall слишком низкий: {report.passed_soft}/5. "
        f"Детали: {[r.qid for r in report.details if not r.passed]}"
    )

    # Строгий: минимум 3/5 (один miss в строгом допустим — например,
    # если в выдаче нет не-комментария из-за quirks демо-данных)
    assert report.passed_strict >= 3, (
        f"Strict recall слишком низкий: {report.passed_strict}/5. "
        f"Возможно, Б2 сломана — все хиты комментарии, без постов автора."
    )


# ---------------------------------------------------------------------------
# 2. Skip-тест для реального архива МГ
# ---------------------------------------------------------------------------

REAL_ARCHIVE_ENV = "LIBRARIAN_REAL_ARCHIVE"


@pytest.mark.skipif(
    not os.environ.get(REAL_ARCHIVE_ENV),
    reason=f"Чтобы запустить, укажи путь к архиву МГ через ${REAL_ARCHIVE_ENV}",
)
def test_recall_on_real_archive():
    """Полный замер recall на реальном архиве Мастер-группы.

    Требует:
      - LIBRARIAN_REAL_ARCHIVE=/path/to/мг_архив (папка с parser.db
        и 00_Индекс.md)
      - scripts/eval_questions.yaml (эталонный набор, уже в репо)

    Ожидаемые числа (librarian_статус.md §Застряло, замер 28.07):
      До фиксов Б1/Б2/Б3:  ~62% strict recall (retrieval+semantics)
      После фиксов:        ~88% strict recall

    Этот тест фиксирует регрессию: если после рефакторинга search()
    упадёт обратно к 62% — тест упадёт.
    """
    archive_path = Path(os.environ[REAL_ARCHIVE_ENV])
    assert archive_path.exists(), f"Путь не существует: {archive_path}"

    # Найти parser.db в указанной папке
    parser_db_file = archive_path / "parser.db"
    if not parser_db_file.exists():
        # maybe the path is the db file itself
        if archive_path.is_file() and archive_path.suffix == ".db":
            parser_db_file = archive_path
            archive_path = archive_path.parent
        else:
            pytest.fail(f"parser.db не найден в {archive_path}")

    # Найти eval_questions.yaml — сначала рядом с тестом, потом в scripts/
    questions_yaml = PROJECT_ROOT / "scripts" / "eval_questions.yaml"
    assert questions_yaml.exists(), f"Файл вопросов не найден: {questions_yaml}"

    # Открыть и построить индекс
    parser_db = ParserDB(parser_db_file).open()
    lib_db_path = archive_path / "librarian.db"
    lib_db = LibrarianDB(lib_db_path).open()
    # Принудительная пересборка — на случай, если код Б1/Б2/Б3 изменился
    # после последнего открытия.
    lib_db.build_index(parser_db)
    try:
        questions = load_questions(questions_yaml)
        # top_k=10, quota_per_kind=5 — конфигурация замера 28.07
        report = evaluate_recall(
            lib_db,
            questions,
            top_k_override=10,
            quota_per_kind=5,
            include_measures=("retrieval", "semantics"),
        )
        print()
        print(report.detailed())

        # Проверка регресса: после фиксов Б1/Б2/Б3 strict должен быть ≥80%.
        # Если упало ниже — что-то откатилось.
        strict_pct = report.passed_strict / report.total if report.total else 0
        assert strict_pct >= 0.80, (
            f"Strict recall {strict_pct:.0%} ниже ожидаемого 80%. "
            f"Возможный регресс Б1/Б2/Б3. "
            f"Детали в выводе выше."
        )
    finally:
        parser_db.close()
        lib_db.close()


# ---------------------------------------------------------------------------
# Юнит-тесты для самой recall_eval-логики (без демо-архива)
# ---------------------------------------------------------------------------

class TestRecallEvalLogic:
    """Проверка вспомогательной логики — без открытия БД."""

    def test_load_questions_parses_yaml(self, tmp_path):
        """load_questions читает минимальный YAML."""
        yml = tmp_path / "q.yaml"
        yml.write_text(
            "meta:\n  total: 2\n"
            "questions:\n"
            "  - id: q1\n    question: Что такое зависть?\n"
            "    type: terminological\n    measures: retrieval\n"
            "    expected_posts: [85]\n    min_hits: 1\n    top_k: 5\n"
            "  - id: t1\n    question: Ловушка\n"
            "    is_trap: true\n    measures: honesty\n",
            encoding="utf-8",
        )
        qs = load_questions(yml)
        assert len(qs) == 2
        assert qs[0].id == "q1"
        assert qs[0].expected_posts == [85]
        assert qs[0].top_k == 5
        assert qs[1].is_trap is True

    def test_report_summary_format(self):
        r = EvalReport(total=10, passed_soft=8, passed_strict=7)
        s = r.summary()
        assert "8/10" in s
        assert "7/10" in s

    def test_report_detailed_shows_miss(self):
        from core.recall_eval import QuestionResult
        r = EvalReport(total=1, passed_soft=0, passed_strict=0)
        r.details.append(QuestionResult(
            qid="qX", question="Что такое X?",
            passed=False, passed_strict=False,
            hits_count=0, hits_needed=1,
            hits_post_ids=[], top_k=10,
            fts_query='"X"*', first_hit_kind="none",
        ))
        out = r.detailed()
        assert "MISS" in out
        assert "qX" in out
