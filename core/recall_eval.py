"""
core/recall_eval.py — оценка recall@k поиска Librarian по эталонному набору
вопросов.

Это адаптация scripts/eval_fts.py под живой LibrarianDB:
- eval_fts.py строит свою отдельную базу и идёт через sqlite3 напрямую,
  чтобы замерить потолок «голого FTS5 без агента»;
- этот модуль ходит через LibrarianDB.search() — то есть через ту же
  функцию, которой пользуется сервер и UI. Любой регресс в _prepare_query,
  квотах, парсере 00_Индекс.md здесь виден сразу.

Использование из теста или CLI:

    from core.recall_eval import evaluate_recall, load_questions
    from core.librarian_db import LibrarianDB
    from core.parser_db import ParserDB

    lib = LibrarianDB(path).open()
    if not lib.is_built():
        lib.build_index(ParserDB(parser_db_path).open())

    qs = load_questions("scripts/eval_questions.yaml")
    report = evaluate_recall(lib, qs, top_k=10)
    print(report.summary())

Критерий зачёта вопроса (мягкий):
    хотя бы min_hits результатов из expected_posts в выдаче top_k.
Критерий (строгий):
    то же + хотя бы один НЕ-комментарий в попаданиях
    (is_comment=False). Проверяет, что выдача не утонула в комментариях.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

try:
    import yaml  # type: ignore
except ImportError as e:
    raise ImportError(
        "recall_eval требует pyyaml: pip install pyyaml"
    ) from e

from .librarian_db import LibrarianDB


# ---------------------------------------------------------------------------
# Вопросы
# ---------------------------------------------------------------------------

@dataclass
class EvalQuestion:
    """Один вопрос из YAML-набора.

    См. scripts/eval_questions.yaml — структура перенесена 1:1.
    Поля, которых нет в YAML, заполняются значениями по умолчанию.
    """
    id: str
    question: str
    type: str = "unknown"                  # terminological / thematic / personal / ...
    measures: str = "retrieval"            # retrieval / semantics / tools / honesty
    expected_posts: list[int] = field(default_factory=list)
    expected_issues: list[str] = field(default_factory=list)
    expected_files: list[str] = field(default_factory=list)
    expected_terms: list[str] = field(default_factory=list)
    min_hits: int = 1
    top_k: int = 10
    is_trap: bool = False
    note: str = ""


def load_questions(yaml_path: str | Path) -> list[EvalQuestion]:
    """Прочитать YAML с вопросами.

    Поддерживает формат scripts/eval_questions.yaml: верхний уровень
    `meta:` + `questions:`. Каждый вопрос — dict с полями id, question, …
    """
    p = Path(yaml_path)
    if not p.exists():
        raise FileNotFoundError(f"Файл вопросов не найден: {p}")
    data = yaml.safe_load(p.read_text(encoding="utf-8"))
    raw_questions = data.get("questions") or []
    out: list[EvalQuestion] = []
    for rq in raw_questions:
        out.append(EvalQuestion(
            id=rq["id"],
            question=rq["question"],
            type=rq.get("type", "unknown"),
            measures=rq.get("measures", "retrieval"),
            expected_posts=list(rq.get("expected_posts") or []),
            expected_issues=list(rq.get("expected_issues") or []),
            expected_files=list(rq.get("expected_files") or []),
            expected_terms=list(rq.get("expected_terms") or []),
            min_hits=int(rq.get("min_hits") or 1),
            top_k=int(rq.get("top_k") or 10),
            is_trap=bool(rq.get("is_trap")),
            note=rq.get("note") or rq.get("expected_note") or "",
        ))
    return out


# ---------------------------------------------------------------------------
# Оценка
# ---------------------------------------------------------------------------

@dataclass
class QuestionResult:
    """Результат одного вопроса."""
    qid: str
    question: str
    passed: bool                       # мягкий зачёт: ≥ min_hits попаданий
    passed_strict: bool                # строгий: + хотя бы один не-комментарий
    hits_count: int                    # сколько релевантных в выдаче
    hits_needed: int                   # min_hits из YAML
    hits_post_ids: list[int]           # какие из expected_posts найдены
    top_k: int
    fts_query: str                     # подготовленный FTS-запрос
    first_hit_kind: str                # 'post' | 'comment' | 'transcript' | 'none'
    error: Optional[str] = None


@dataclass
class EvalReport:
    """Сводный отчёт по набору вопросов."""
    total: int
    passed_soft: int
    passed_strict: int
    by_measure: dict[str, dict[str, int]] = field(default_factory=dict)
    details: list[QuestionResult] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)  # id вопросов, которые пропущены
    # baseline рассчитывается отдельно, если нужен — см. eval_fts.py
    baseline_soft: Optional[float] = None
    baseline_strict: Optional[float] = None

    def summary(self) -> str:
        """Краткий текстовый отчёт в одну строку."""
        s = (f"recall: soft {self.passed_soft}/{self.total}"
             f" | strict {self.passed_strict}/{self.total}")
        if self.baseline_soft is not None:
            s += f" | baseline soft={self.baseline_soft:.1f}"
        if self.baseline_strict is not None:
            s += f" | baseline strict={self.baseline_strict:.1f}"
        if self.skipped:
            s += f" | skipped: {','.join(self.skipped)}"
        return s

    def detailed(self) -> str:
        """Многострочный отчёт с каждым вопросом."""
        lines = [
            "=" * 70,
            f"RECALL REPORT  —  {self.passed_soft}/{self.total} soft"
            f"  |  {self.passed_strict}/{self.total} strict",
            "=" * 70,
        ]
        for r in self.details:
            mark = "OK  " if r.passed else "MISS"
            kind = r.first_hit_kind or "—"
            lines.append(
                f"  [{mark}] {r.qid}  {r.hits_count}/{r.hits_needed} "
                f"in top-{r.top_k}  first={kind}"
                f"  — {r.question[:60]}"
            )
            if not r.passed and r.hits_post_ids:
                lines.append(f"           found: {r.hits_post_ids}")
            elif not r.passed:
                lines.append(f"           no expected_posts in hits; query={r.fts_query[:80]}")
            if r.error:
                lines.append(f"           ERROR: {r.error}")
        if self.skipped:
            lines.append("")
            lines.append(f"  Skipped: {', '.join(self.skipped)}")
        lines.append("=" * 70)
        return "\n".join(lines)


def _is_hit(hit, q: EvalQuestion) -> bool:
    """Попадание — message_id или post_message_id в expected_posts."""
    if not q.expected_posts:
        return False
    return (hit.message_id in q.expected_posts
            or (hit.post_message_id is not None
                and hit.post_message_id in q.expected_posts))


def evaluate_recall(
    lib_db: LibrarianDB,
    questions: list[EvalQuestion],
    *,
    top_k_override: Optional[int] = None,
    quota_per_kind: Optional[int] = None,
    include_measures: Optional[tuple[str, ...]] = ("retrieval", "semantics"),
    skip_tools: bool = True,
    skip_traps: bool = True,
) -> EvalReport:
    """Прогнать вопросы через LibrarianDB.search() и посчитать recall.

    Параметры:
        top_k_override — если задать, перебивает top_k из YAML (как в eval_fts).
        quota_per_kind — передаётся в search(). По умолчанию None = общая
            выдача. Для сравнения с замером 28.07 (62%→88%) нужно
            quota_per_kind=5 или около того.
        include_measures — какие группы вопросов оценивать.
            По умолчанию retrieval + semantics (как в eval_fts.evaluate).
        skip_tools — пропустить questions с measures='tools'
            (они требуют stats/read_post, не FTS).
        skip_traps — пропустить questions с is_trap=True
            (требуют модельного суждения, не FTS).

    Возвращает EvalReport. Пропущенные вопросы идут в .skipped.
    """
    report = EvalReport(total=0, passed_soft=0, passed_strict=0)
    include_measures = set(include_measures or ())

    for q in questions:
        if skip_tools and q.measures == "tools":
            report.skipped.append(q.id)
            continue
        if skip_traps and q.is_trap:
            report.skipped.append(q.id)
            continue
        if q.measures not in include_measures:
            report.skipped.append(q.id)
            continue

        report.total += 1
        k = top_k_override or q.top_k
        fts_query = lib_db._prepare_query(q.question)

        try:
            hits = lib_db.search(
                q.question,
                limit=k,
                quota_per_kind=quota_per_kind,
            )
        except Exception as e:
            report.details.append(QuestionResult(
                qid=q.id, question=q.question,
                passed=False, passed_strict=False,
                hits_count=0, hits_needed=q.min_hits,
                hits_post_ids=[], top_k=k, fts_query=fts_query,
                first_hit_kind="none", error=str(e),
            ))
            report._bump_measure(q.measures, soft=False, strict=False)
            continue

        hit_posts = []
        has_non_comment = False
        for h in hits:
            if _is_hit(h, q):
                hit_posts.append(h.message_id)
            if not h.is_comment:
                has_non_comment = True

        nhit = len(set(hit_posts))
        passed = nhit >= q.min_hits
        passed_strict = passed and has_non_comment

        # Тип первого результата — для отладки «утонуло в комментариях»
        if hits:
            h0 = hits[0]
            if h0.source == "transcription":
                first_kind = "transcript"
            elif h0.is_comment:
                first_kind = "comment"
            else:
                first_kind = "post"
        else:
            first_kind = "none"

        report.details.append(QuestionResult(
            qid=q.id, question=q.question,
            passed=passed, passed_strict=passed_strict,
            hits_count=nhit, hits_needed=q.min_hits,
            hits_post_ids=sorted(set(hit_posts)),
            top_k=k, fts_query=fts_query,
            first_hit_kind=first_kind,
        ))
        if passed:
            report.passed_soft += 1
        if passed_strict:
            report.passed_strict += 1
        report._bump_measure(q.measures, soft=passed, strict=passed_strict)

    return report


def _bump_measure(self: EvalReport, measure: str, *, soft: bool, strict: bool) -> None:
    """Внутренний: обновить счётчики по группе measures."""
    m = self.by_measure.setdefault(measure, {"total": 0, "soft": 0, "strict": 0})
    m["total"] += 1
    if soft:
        m["soft"] += 1
    if strict:
        m["strict"] += 1


# Привязка метода к dataclass — не самый красивый приём, но позволяет
# держать _bump_measure рядом с классом, не делая его публичным.
EvalReport._bump_measure = _bump_measure  # type: ignore[attr-defined]
