#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
eval_fts.py — замер качества полнотекстового поиска по эталонному набору.

Отвечает на вопрос: годится ли FTS5 как хребет для Rozitta Librarian,
и на каких типах вопросов он промахивается.

Это конфигурация A из плана тестирования: ОДИН префиксный FTS-запрос
по вопросу как есть, без модели и без переформулировок. Худший случай.
Всё, что окажется лучше этих чисел, — заслуга агентного цикла.

БАЗУ PARSER НЕ ТРОГАЕТ: открывает только на чтение, индекс строит
в отдельном файле, в конце сверяет размер и дату изменения.

Использование:
    python eval_fts.py --db путь/к/архиву              # можно папку
    python eval_fts.py --db ... --inspect               # показать схему
    python eval_fts.py --db ... --verbose               # разбор промахов
    python eval_fts.py --db ... --rebuild               # пересобрать индекс
    python eval_fts.py --db ... --stem-len 0            # без обрезки слов

Требуется: pip install pyyaml
"""

import argparse
import os
import random
import re
import sqlite3
import sys
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path
from urllib.parse import unquote

try:
    import yaml
except ImportError:
    sys.exit("Нужен pyyaml:  pip install pyyaml")


# ── Настройка схемы ───────────────────────────────────────────────
# Значения — реальная схема Rozitta Parser v2 (PRAGMA user_version = 2).
# Если имена не совпадут с базой, включится автоопределение по CANDIDATES.
# Посмотреть, что в базе на самом деле: запуск с --inspect.

SCHEMA = {
    "messages_table": "messages",
    "msg_id_col": "message_id",
    "text_col": "text",
    "post_col": "post_id",            # у комментария = id поста, у поста NULL
    "comment_flag_col": "is_comment",
    "author_col": "username",
    "date_col": "date",
    "transcriptions_table": "transcriptions",
    "transcript_text_col": "text",
    "transcript_link_col": "message_id",
}

CANDIDATES = {
    "messages_table": ["messages", "message", "telegram_messages", "posts"],
    "msg_id_col": ["message_id", "msg_id", "id", "tg_id"],
    "text_col": ["text", "message", "content", "body", "raw_text"],
    "post_col": ["post_id", "top_message_id", "thread_id", "reply_to_top_id",
                 "reply_to_msg_id", "reply_to", "grouped_id", "parent_id"],
    "comment_flag_col": ["is_comment", "comment", "from_linked_group"],
    "author_col": ["username", "user_name", "author", "sender"],
    "date_col": ["date", "created_at", "timestamp"],
    "transcriptions_table": ["transcriptions", "transcription", "transcripts"],
    "transcript_text_col": ["text", "transcript", "content", "body"],
    "transcript_link_col": ["message_id", "msg_id", "post_id", "source_id"],
}

STOPWORDS = set("""
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
ENDINGS = sorted([
    "ами", "ями", "ого", "его", "ому", "ему", "ыми", "ими", "ует", "уют",
    "ает", "ают", "яет", "яют", "ить", "ать", "ять", "еть", "ыть", "ился",
    "илась", "ется", "ются", "ость", "ений", "ения", "аний", "ания",
    "ая", "яя", "ое", "ее", "ые", "ие", "ой", "ей", "ый", "ий", "ом", "ем",
    "ах", "ях", "ам", "ям", "ов", "ев", "ью", "ия", "ии", "ла", "ло", "ли",
    "а", "я", "о", "е", "ы", "и", "у", "ю", "ь", "й",
], key=len, reverse=True)

STEM_MIN = 4      # короче этого не режем
STEM_CAP = 6      # и в любом случае не длиннее


def stem(token, cap=STEM_CAP):
    """Грубая обрезка до основы. Не лингвистика, а рабочая эвристика."""
    if cap <= 0 or token.startswith("#"):
        return token
    t = token
    for e in ENDINGS:
        if t.endswith(e) and len(t) - len(e) >= STEM_MIN:
            t = t[: len(t) - len(e)]
            break
    return t[:cap] if len(t) > cap else t


# ── Пути (Windows-совместимо) ─────────────────────────────────────

def to_uri(path, mode):
    """Путь -> SQLite URI. Windows-слеши, пробелы и кириллица кодируются
    сами. Подставлять сырой путь в f-строку нельзя: на Windows ломается."""
    return Path(path).resolve().as_uri() + f"?mode={mode}"


def resolve_db_path(given):
    """Можно указать папку архива — базу найдём внутри."""
    p = Path(given)
    if p.is_file():
        return p
    if p.is_dir():
        found = sorted(p.glob("*.db")) or sorted(p.glob("**/*.db"))
        if len(found) == 1:
            print(f"Указана папка, найдена база: {found[0].name}\n")
            return found[0]
        if not found:
            sys.exit(f"В папке {p} нет ни одного файла .db\n"
                     f"Укажи путь к самому файлу базы, а не к папке.")
        print("В папке несколько баз — укажи нужную явно:")
        for f in found:
            print(f'   --db "{f}"')
        sys.exit(1)
    sys.exit(f"Не найдено: {given}")


# ── Разведка схемы ────────────────────────────────────────────────

def list_tables(con):
    rows = con.execute(
        "SELECT name FROM src.sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    return [r[0] for r in rows]


def table_columns(con, table):
    return [r[1] for r in con.execute(f'PRAGMA src.table_info("{table}")')]


def pick(candidates, available, fallback_contains=None):
    low = {a.lower(): a for a in available}
    for c in candidates:
        if c.lower() in low:
            return low[c.lower()]
    if fallback_contains:
        for a in available:
            if fallback_contains in a.lower():
                return a
    return None


def detect_schema(con, verbose=True):
    s = dict(SCHEMA)
    tables = list_tables(con)

    if s["messages_table"] not in tables:
        s["messages_table"] = pick(CANDIDATES["messages_table"], tables,
                                   fallback_contains="mess")
    if not s["messages_table"]:
        print("НЕ НАЙДЕНА таблица сообщений. Таблицы в базе:")
        for t in tables:
            print("   ", t)
        sys.exit("Впиши имя вручную в SCHEMA['messages_table'] вверху скрипта.")

    cols = table_columns(con, s["messages_table"])
    for key in ("msg_id_col", "text_col", "post_col",
                "comment_flag_col", "author_col", "date_col"):
        if s.get(key) not in cols:
            s[key] = pick(CANDIDATES[key], cols)

    if not s["text_col"]:
        sys.exit(f"В таблице {s['messages_table']} не найдена колонка с "
                 f"текстом. Колонки: {cols}. Впиши в SCHEMA['text_col'].")

    if s["transcriptions_table"] not in tables:
        s["transcriptions_table"] = pick(CANDIDATES["transcriptions_table"],
                                         tables)
    if s["transcriptions_table"]:
        tcols = table_columns(con, s["transcriptions_table"])
        if s.get("transcript_text_col") not in tcols:
            s["transcript_text_col"] = pick(CANDIDATES["transcript_text_col"],
                                            tcols)
        if s.get("transcript_link_col") not in tcols:
            s["transcript_link_col"] = pick(CANDIDATES["transcript_link_col"],
                                            tcols)
        if not s["transcript_text_col"]:
            s["transcriptions_table"] = None

    if verbose:
        print("Схема определена:")
        for k, v in s.items():
            print(f"   {k:24} = {v}")
        print()
    return s


def inspect(con):
    print("PRAGMA user_version =",
          con.execute("PRAGMA src.user_version").fetchone()[0], "\n")
    for t in list_tables(con):
        n = con.execute(f'SELECT COUNT(*) FROM src."{t}"').fetchone()[0]
        print(f"── {t}  ({n} строк)")
        for c in con.execute(f'PRAGMA src.table_info("{t}")'):
            print(f"      {c[1]:24} {c[2]}")
        print()


# ── Транскрипты с диска ───────────────────────────────────────────
# Транскрипции создаёт отдельная программа (Transcriber), в базе Parser
# их нет. Единственная связь поста с файлом расшифровки — индекс
# 00_Индекс.md от make_index.py. Читаем его, если он лежит рядом с базой.

RE_MD_LINK = re.compile(r"\(([^)]+?\.md)\)")


def load_index_transcripts(folder, index_name="00_Индекс.md"):
    """-> ([(post_id, имя_файла, текст)], (имя_индекса, потеряно) | None)."""
    idx = Path(folder) / index_name
    if not idx.exists():
        cands = list(Path(folder).glob("*Индекс*.md"))
        if not cands:
            return [], None
        idx = cands[0]
    rows, missing = [], 0
    for line in idx.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        post = next((int(c) for c in cells if c.isdigit()), None)
        if post is None:
            continue
        link = next((m for c in cells for m in RE_MD_LINK.findall(c)
                     if "transcript" in m.lower()), None)
        if not link:
            continue
        p = Path(folder) / unquote(link)
        if not p.exists():
            missing += 1
            continue
        rows.append((post, p.name,
                     p.read_text(encoding="utf-8", errors="replace")))
    return rows, (idx.name, missing)


# ── Построение индекса ────────────────────────────────────────────

def build_index(con, s, rebuild=False, folder=None):
    """Собрать docs + FTS5 в НАШЕЙ базе. Базу Parser только читаем."""
    have = con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='docs'"
    ).fetchone()
    if have and not rebuild:
        n = con.execute("SELECT COUNT(*) FROM docs").fetchone()[0]
        print(f"Индекс уже собран: {n} документов "
              f"(--rebuild чтобы пересобрать)\n")
        return

    con.executescript("DROP TABLE IF EXISTS fts; DROP TABLE IF EXISTS docs;")
    con.execute("""
        CREATE TABLE docs (
            doc_id  INTEGER PRIMARY KEY,
            msg_id  INTEGER,
            post_id INTEGER,
            kind    TEXT,
            label   TEXT,
            text    TEXT
        )""")

    mt, mid, txt = s["messages_table"], s["msg_id_col"], s["text_col"]
    post, flag, auth = s["post_col"], s["comment_flag_col"], s["author_col"]

    # у комментария post_id = id поста; у самого поста post_id пуст,
    # и постом служит его собственный message_id
    post_expr = f'COALESCE(m."{post}", m."{mid}")' if post and mid else (
        f'm."{mid}"' if mid else "NULL")
    id_expr = f'm."{mid}"' if mid else "m.rowid"
    kind_expr = (f'CASE WHEN m."{flag}" = 1 THEN \'comment\' ELSE \'post\' END'
                 if flag else "'message'")
    label_expr = f'm."{auth}"' if auth else "NULL"

    con.execute(f"""
        INSERT INTO docs (msg_id, post_id, kind, label, text)
        SELECT {id_expr}, {post_expr}, {kind_expr}, {label_expr}, m."{txt}"
        FROM src."{mt}" m
        WHERE m."{txt}" IS NOT NULL AND LENGTH(TRIM(m."{txt}")) > 0
    """)
    n_msg = con.execute("SELECT COUNT(*) FROM docs").fetchone()[0]
    n_com = con.execute(
        "SELECT COUNT(*) FROM docs WHERE kind='comment'").fetchone()[0]

    # транскрипции из базы, если есть
    n_tr = 0
    if s["transcriptions_table"]:
        tt = s["transcriptions_table"]
        ttxt, tlink = s["transcript_text_col"], s["transcript_link_col"]
        link_expr = f't."{tlink}"' if tlink else "NULL"
        tcols = table_columns(con, tt)
        label_col = pick(["file", "filename", "path", "source_file", "title"],
                         tcols, fallback_contains="file")
        label_expr = f't."{label_col}"' if label_col else "NULL"
        try:
            con.execute(f"""
                INSERT INTO docs (msg_id, post_id, kind, label, text)
                SELECT {link_expr}, {link_expr}, 'transcript', {label_expr},
                       t."{ttxt}"
                FROM src."{tt}" t
                WHERE t."{ttxt}" IS NOT NULL AND LENGTH(TRIM(t."{ttxt}")) > 0
            """)
            n_tr = con.execute(
                "SELECT COUNT(*) FROM docs WHERE kind='transcript'"
            ).fetchone()[0]
        except sqlite3.Error as e:
            print(f"Транскрипции из базы пропущены: {e}")

    # транскрипций в базе нет -> берём файлами с диска по 00_Индекс.md
    n_file = 0
    if not n_tr and folder:
        rows, info = load_index_transcripts(folder)
        for post_id, name, text in rows:
            con.execute("INSERT INTO docs (msg_id, post_id, kind, label, text)"
                        " VALUES (?,?,'transcript',?,?)",
                        (post_id, post_id, name, text))
        n_file = len(rows)
        if info and n_file:
            print(f"Транскрипты подхвачены с диска по {info[0]}: "
                  f"{n_file} файлов"
                  + (f" (не найдено на диске: {info[1]})" if info[1] else ""))

    # unicode61 + tokenchars '#' — чтобы хештеги вроде #qrbase не разваливались
    con.execute("""
        CREATE VIRTUAL TABLE fts USING fts5(
            text,
            content='docs',
            content_rowid='doc_id',
            tokenize="unicode61 remove_diacritics 2 tokenchars '#'"
        )""")
    con.execute("INSERT INTO fts(fts) VALUES('rebuild')")
    con.commit()
    total_tr = n_tr + n_file
    print(f"Индекс собран: {n_msg} сообщений (из них комментариев: {n_com})"
          + (f", транскриптов: {total_tr}" if total_tr else ""))
    if not total_tr:
        print("   ВНИМАНИЕ: транскриптов нет ни в базе, ни на диске. Вопросы,")
        print("   где ответ лежит в видео, найтись не смогут.")
    print()


# ── Запрос ────────────────────────────────────────────────────────

def tokenize(question):
    q = question.lower().replace("ё", "е")
    q = re.sub(r"[«»\"'(),.!?:;—–\-]", " ", q)
    raw = re.findall(r"[#\w]+", q, flags=re.UNICODE)
    out = []
    for t in raw:
        if len(t) < MIN_TOKEN_LEN or t in STOPWORDS or t.isdigit():
            continue
        out.append(t)
    return out


def fts_query(tokens, cap=STEM_CAP):
    parts, seen = [], set()
    for t in tokens:
        safe = stem(t, cap).replace('"', "")
        if safe and safe not in seen:
            seen.add(safe)
            parts.append(f'"{safe}"*')
    return " OR ".join(parts)


def search(con, question, top_k, cap=STEM_CAP, kinds=None):
    tokens = tokenize(question)
    if not tokens:
        return [], ""
    q = fts_query(tokens, cap)
    kind_sql, kind_args = "", []
    if kinds:
        kind_sql = " AND d.kind IN (%s)" % ",".join("?" * len(kinds))
        kind_args = list(kinds)
    try:
        rows = con.execute(f"""
            SELECT d.doc_id, d.msg_id, d.post_id, d.kind, d.label,
                   substr(d.text, 1, 200)
            FROM fts JOIN docs d ON d.doc_id = fts.rowid
            WHERE fts MATCH ?{kind_sql}
            ORDER BY bm25(fts)
            LIMIT ?
        """, [q] + kind_args + [top_k]).fetchall()
    except sqlite3.OperationalError as e:
        print(f"   ! запрос не выполнился: {e}")
        return [], q
    return rows, q


def search_multi(con, terms, top_k, cap=STEM_CAP, kinds=None):
    """Отдельный поиск на КАЖДЫЙ термин, потом слияние по кругу.

    Так работает живой агент: несколько узких запросов подряд, а не один
    размазанный OR. Разница принципиальная — в общем OR-запросе лишние
    слова перетягивают ранжирование и топят нужные (замер 28.07: q15
    содержал верное «агрессия», но 8 посторонних слов увели выдачу
    в другой цикл).

    Слияние round-robin: берём первый результат каждого запроса, потом
    второй каждого и так далее. Ни один термин не забивает остальные.
    """
    per_query = []
    for t in terms:
        rows, _ = search(con, t, top_k, cap, kinds)
        per_query.append(rows)

    merged, seen = [], set()
    for rank in range(top_k):
        for rows in per_query:
            if rank < len(rows) and rows[rank][0] not in seen:
                seen.add(rows[rank][0])
                merged.append(rows[rank])
                if len(merged) >= top_k:
                    return merged, " | ".join(terms)
    return merged, " | ".join(terms)


# ── Оценка ────────────────────────────────────────────────────────

def norm(s):
    return unicodedata.normalize("NFKC", str(s or "")).lower().replace("ё", "е")


def is_hit(row, q):
    _, msg_id, post_id, kind, label, snippet = row

    posts = set(q.get("expected_posts") or [])
    if posts and (msg_id in posts or post_id in posts):
        return True

    hay = norm(label) + " " + norm(snippet)
    for term in (q.get("expected_terms") or []):
        if norm(term) in hay:
            return True
    for f in (q.get("expected_files") or []):
        stem_name = norm(f).rsplit(".", 1)[0]
        if stem_name and stem_name[:30] in hay:
            return True
    for iss in (q.get("expected_issues") or []):
        if re.search(rf"\b{re.escape(norm(iss))}\b", hay):
            return True
    return False


_POOL_CACHE = {}


def load_pool(con, kinds):
    """Все документы пула в память — чтобы контроль считался быстро."""
    key = tuple(kinds) if kinds else ("*",)
    if key in _POOL_CACHE:
        return _POOL_CACHE[key]
    base = ("SELECT doc_id, msg_id, post_id, kind, label, substr(text,1,200) "
            "FROM docs")
    if kinds:
        rows = con.execute(
            base + " WHERE kind IN (%s)" % ",".join("?" * len(kinds)),
            list(kinds)).fetchall()
    else:
        rows = con.execute(base).fetchall()
    _POOL_CACHE[key] = rows
    return rows


def baseline_dist(pool, qs, top_k_override=None, runs=1000, seed=42):
    """РАСПРЕДЕЛЕНИЕ случайного зачёта по `runs` независимым прогонам.

    Одна случайная выборка ничего не говорит: при 7 вопросах разброс
    в ±2 — обычное дело. Поэтому гоняем много раз и смотрим на среднее
    и на долю прогонов, где случайность дотянула до наблюдаемого.
    """
    rnd = random.Random(seed)
    n = len(pool)
    soft, strict = [], []
    for _ in range(runs):
        s = st = 0
        for q in qs:
            k = min(top_k_override or q.get("top_k", 10), n)
            need = q.get("min_hits", 1)
            hits = [r for r in rnd.sample(pool, k) if is_hit(r, q)]
            ok = len(hits) >= need
            s += ok
            st += ok and any(r[3] != "comment" for r in hits)
        soft.append(s)
        strict.append(st)
    return soft, strict


def p_value(dist, observed):
    """Доля случайных прогонов, где случайность дала НЕ ХУЖЕ наблюдаемого.
    Маленькое значение = наблюдаемый результат трудно объяснить удачей."""
    return sum(1 for d in dist if d >= observed) / max(len(dist), 1)


def mean(xs):
    return sum(xs) / len(xs) if xs else 0.0


def query_for(q, mode):
    """Текст, по которому ищем.

    question — вопрос как есть (худший случай, конфигурация A).
    naive    — поисковые слова из поля naive_terms. ВНИМАНИЕ: результат
               годится только если эти слова получены СЛЕПО — от модели,
               не видевшей корпус, индекс и правильные ответы
               (см. слепая_проверка.md). Написанные вручную задним
               числом дают завышенную оценку.
    oracle   — термины самого автора (ПОТОЛОК: так было бы при идеальном
               знании корпуса; для живой модели недостижимо).
    """
    if mode == "oracle" and q.get("author_terms"):
        return " ".join(q["author_terms"])
    if mode == "naive" and q.get("naive_terms"):
        return " ".join(q["naive_terms"])
    return q["question"]


def terms_for(q, mode):
    """Список отдельных терминов для режима — или None, если их нет."""
    if mode == "oracle":
        return q.get("author_terms")
    if mode == "naive":
        return q.get("naive_terms")
    return None


def evaluate(con, questions, top_k_override=None, verbose=False,
             cap=STEM_CAP, kinds=None, mode="question", runs=1000):
    groups = defaultdict(list)
    for q in questions:
        groups[q.get("measures", "?")].append(q)

    pool = load_pool(con, kinds)
    if not pool:
        sys.exit("В индексе нет документов выбранных типов — "
                 "проверь --kinds и наличие транскриптов.")
    POOL_STATE[0] = len(pool)

    results = {}
    for measure in ("retrieval", "semantics"):
        qs = groups.get(measure, [])
        if not qs:
            continue
        passed, passed_strict, details = 0, 0, []
        precs, rr = [], []
        for q in qs:
            k = top_k_override or q.get("top_k", 10)
            need = q.get("min_hits", 1)
            terms = terms_for(q, mode)
            if MULTI_STATE[0] and terms:
                rows, fq = search_multi(con, terms, k, cap, kinds)
            else:
                rows, fq = search(con, query_for(q, mode), k, cap, kinds)
            hits = [r for r in rows if is_hit(r, q)]
            ok = len(hits) >= need
            ok_strict = ok and any(r[3] != "comment" for r in hits)
            passed += ok
            passed_strict += ok_strict
            # точность@k: какая доля выдачи оказалась релевантной
            precs.append(len(hits) / k if k else 0.0)
            # MRR: насколько высоко стоит ПЕРВЫЙ релевантный результат
            pos = next((i + 1 for i, r in enumerate(rows) if is_hit(r, q)),
                       None)
            rr.append(1.0 / pos if pos else 0.0)
            details.append((q, ok, len(hits), need, len(rows), rows, fq,
                            hits, ok_strict))
        soft_dist, strict_dist = baseline_dist(pool, qs, top_k_override, runs)
        results[measure] = {
            "n": len(qs),
            "soft": passed,
            "strict": passed_strict,
            "details": details,
            "base_soft": mean(soft_dist),
            "base_strict": mean(strict_dist),
            "p_soft": p_value(soft_dist, passed),
            "p_strict": p_value(strict_dist, passed_strict),
            "prec": mean(precs),
            "mrr": mean(rr),
        }

    traps = []
    for q in groups.get("honesty", []):
        k = top_k_override or q.get("top_k", 10)
        rows, _ = search(con, q["question"], k, cap, kinds)
        distract = set(q.get("distractor_posts") or [])
        d_hits = sum(1 for r in rows if r[1] in distract or r[2] in distract)
        traps.append((q, len(rows), d_hits))

    tools = groups.get("tools", [])
    return results, traps, tools


# ── Отчёт ─────────────────────────────────────────────────────────

TITLES = {
    "retrieval": "ПОИСК       — находит ли FTS то, где слова совпадают",
    "semantics": "СЕМАНТИКА   — находит ли, когда общих слов НЕТ",
}

STEM_STATE = [STEM_CAP]
KINDS_STATE = [None]
ORACLE_STATE = [False]
MODE_STATE = ["question"]
MULTI_STATE = [False]
RUNS_STATE = [1000]

MODE_LABELS = {
    "question": "вопрос как есть (конфигурация A)",
    "naive": "переформулировка (конфигурация B) — ЧЬЯ ИМЕННО, см. YAML",
    "oracle": "термины автора (ПОТОЛОК, завышен)",
}
POOL_STATE = [0]


def report(results, traps, tools, verbose):
    print("=" * 70)
    print("КОНФИГУРАЦИЯ A/B: один FTS-запрос, без модели в цикле")
    print("режим запроса: " + MODE_LABELS.get(MODE_STATE[0], MODE_STATE[0]))
    print("склейка терминов: "
          + ("КАЖДЫЙ отдельным запросом, слияние по кругу"
             if MULTI_STATE[0] else "все в один OR-запрос"))
    print("обрезка слов до основы: "
          + ("выключена" if STEM_STATE[0] <= 0 else f"{STEM_STATE[0]} букв"))
    kk = KINDS_STATE[0]
    print("типы источников: " + (", ".join(kk) if kk else "все")
          + f"   (документов в пуле: {POOL_STATE[0]})")
    print(f"контроль: среднее по {RUNS_STATE[0]} случайным прогонам")
    print("=" * 70)

    for measure in ("retrieval", "semantics"):
        r = results.get(measure)
        if not r:
            continue
        n = r["n"]
        kinds = Counter(x[3] for d in r["details"] for x in d[7])
        print(f"\n{TITLES[measure]}")
        print(f"   мягкий зачёт:  {r['soft']}/{n}"
              f"   случайно в среднем {r['base_soft']:.1f}/{n}"
              f"   p = {r['p_soft']:.3f}")
        print(f"   СТРОГИЙ зачёт: {r['strict']}/{n}"
              f"   случайно в среднем {r['base_strict']:.1f}/{n}"
              f"   p = {r['p_strict']:.3f}")
        print(f"   точность@k: {r['prec']:.2f}   MRR: {r['mrr']:.2f}"
              f"   попадания: {dict(kinds) or '—'}")
        for q, ok, nhit, need, nres, rows, fq, _hh, ok_s in r["details"]:
            mark = "OK  " if ok else "МИМО"
            src = "автор в выдаче" if ok_s else "только пересказы"
            print(f"   [{mark}] {q['id']}  {nhit}/{need} попаданий "
                  f"из {nres}, {src} — {q['question'][:42]}...")
            if verbose and not ok:
                print(f"           запрос: {fq[:100]}")
                if q.get("author_terms"):
                    print("           термины автора: "
                          + ", ".join(q["author_terms"]))
                for x in rows[:3]:
                    print(f"           · пост {x[2]} [{x[3]}] "
                          f"{norm(x[5])[:60]}...")

    if traps:
        print("\nЛОВУШКИ     — полная проверка требует модели; здесь только шум")
        for q, nres, d_hits in traps:
            print(f"   {q['id']}: FTS вернул {nres} результатов"
                  + (f", из них {d_hits} — известные отвлекающие"
                     if d_hits else ""))

    if tools:
        print(f"\nИНСТРУМЕНТЫ — {len(tools)} вопросов пропущено: "
              f"{', '.join(q['id'] for q in tools)}")

    print("\n" + "─" * 70)
    r = results.get("semantics")
    if r:
        print(f"СЕМАНТИКА, строгий зачёт: {r['strict']}/{r['n']}"
              f"   случайно {r['base_strict']:.1f}/{r['n']}"
              f"   p = {r['p_strict']:.3f}")
        if r["p_strict"] <= 0.05:
            print("p ≤ 0.05: случайностью такой результат объясняется плохо.")
        else:
            print("p > 0.05: от случайности результат НЕ отличим. "
                  "Считать его успехом нельзя.")
        print("\nВНИМАНИЕ: n = %d. Это мало. Один вопрос туда-сюда двигает"
              % r["n"])
        print("результат на 14 процентных пунктов. p-value считается по")
        print("перестановкам и учитывает малую выборку, проценты — нет.")
    print("─" * 70)


def sweep(con, questions, cap, runs, top_ks=(3, 5, 10)):
    """Полная сетка конфигураций одной таблицей.

    Нужна против подгонки истории под результат: печатаются ВСЕ ячейки,
    а не та, которая понравилась. Смотреть на строгий зачёт и p.
    """
    print("=" * 78)
    print("СЕТКА КОНФИГУРАЦИЙ — семантическая группа, строгий зачёт")
    print(f"контроль: среднее по {runs} случайным прогонам, "
          f"p = доля прогонов, где случайность не хуже")
    print("=" * 78)
    print(f"{'пул':<10}{'запрос':<13}{'k':>3}  {'зачёт':>7}"
          f"{'случайно':>10}{'p':>8}{'точн@k':>9}{'MRR':>7}")
    print("-" * 78)
    for pool_name, kinds in (("автор", ["post", "transcript"]),
                             ("всё", None)):
        for mode, multi in (("question", False), ("naive", False),
                            ("naive+multi", True), ("oracle", False),
                            ("oracle+multi", True)):
            base_mode = mode.split("+")[0]
            for k in top_ks:
                MULTI_STATE[0] = multi
                res, _, _ = evaluate(con, questions, top_k_override=k,
                                     cap=cap, kinds=kinds, mode=base_mode,
                                     runs=runs)
                r = res.get("semantics")
                if not r:
                    continue
                print(f"{pool_name:<10}{mode:<13}{k:>3}  "
                      f"{r['strict']:>3}/{r['n']:<3}"
                      f"{r['base_strict']:>9.1f}{r['p_strict']:>8.3f}"
                      f"{r['prec']:>9.2f}{r['mrr']:>7.2f}")
        MULTI_STATE[0] = False
        print("-" * 78)
    print("question = вопрос как есть | naive = слова из naive_terms |")
    print("oracle = термины автора. ЧЬИ слова лежат в naive_terms и были ли")
    print("они получены вслепую — скрипт не знает, смотри шапку YAML.")
    print("multi = каждый термин отдельным запросом (как делает агент).")


def parse_kinds(raw):
    """'all' -> None (без фильтра); 'author' -> посты и транскрипты."""
    raw = (raw or "all").strip().lower()
    if raw in ("", "all", "все"):
        return None
    if raw in ("author", "автор"):
        return ["post", "transcript"]
    allowed = {"post", "transcript", "comment"}
    kinds = [k.strip() for k in raw.split(",") if k.strip()]
    bad = [k for k in kinds if k not in allowed]
    if bad:
        sys.exit(f"Неизвестный тип источника: {', '.join(bad)}. "
                 f"Допустимы: post, transcript, comment (или all / author)")
    return kinds


# ── Точка входа ───────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(
        description="Замер recall@k полнотекстового поиска по эталонному "
                    "набору")
    ap.add_argument("--db", required=True,
                    help="путь к базе Parser или к папке архива")
    ap.add_argument("--questions", default="eval_questions.yaml")
    ap.add_argument("--index", default="eval_index.db",
                    help="куда класть свой индекс")
    ap.add_argument("--top-k", type=int, default=None,
                    help="перебить top_k из файла")
    ap.add_argument("--inspect", action="store_true",
                    help="показать схему базы и выйти")
    ap.add_argument("--rebuild", action="store_true",
                    help="пересобрать индекс")
    ap.add_argument("--verbose", action="store_true",
                    help="показать промахи подробно")
    ap.add_argument("--query-mode", default="question",
                    choices=["question", "naive", "oracle"],
                    help="чем искать: question — вопрос как есть; "
                         "naive — переформулировка без знания корпуса "
                         "(реалистичная конфигурация B); oracle — термины "
                         "автора (ПОТОЛОК, завышен утечкой).")
    ap.add_argument("--oracle", action="store_true",
                    help="то же, что --query-mode oracle (старый синоним)")
    ap.add_argument("--multi-query", action="store_true",
                    help="искать КАЖДЫЙ термин отдельным запросом и сливать "
                         "выдачи по кругу — так делает живой агент. Без "
                         "этого все термины склеиваются в один OR-запрос, "
                         "и лишние слова топят нужные.")
    ap.add_argument("--sweep", action="store_true",
                    help="прогнать ВСЮ сетку конфигураций одной таблицей — "
                         "чтобы не выбирать удобную ячейку задним числом")
    ap.add_argument("--baseline-runs", type=int, default=1000,
                    help="сколько случайных прогонов для контроля "
                         "(по умолчанию 1000)")
    ap.add_argument("--kinds", default="all",
                    help="типы источников через запятую: post,transcript,"
                         "comment. Сокращение 'author' = post,transcript "
                         "(только материал автора). По умолчанию все.")
    ap.add_argument("--stem-len", type=int, default=STEM_CAP,
                    help="до скольких букв резать слово "
                         "(0 = не резать, для сравнения)")
    args = ap.parse_args()

    db_path = resolve_db_path(args.db)

    # снимок «до» — убедимся в конце, что базу Parser не тронули
    before = (db_path.stat().st_size, db_path.stat().st_mtime)

    con = sqlite3.connect(to_uri(args.index, "rwc"), uri=True)
    con.execute("ATTACH DATABASE ? AS src", (to_uri(db_path, "ro"),))

    try:
        if args.inspect:
            inspect(con)
            return

        s = detect_schema(con)
        build_index(con, s, rebuild=args.rebuild, folder=db_path.parent)

        if not os.path.exists(args.questions):
            sys.exit(f"Не найден файл вопросов: {args.questions}\n"
                     f"Положи eval_questions.yaml рядом со скриптом "
                     f"или укажи --questions путь/к/файлу.yaml")
        with open(args.questions, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        questions = data.get("questions", [])
        print(f"Вопросов в наборе: {len(questions)}\n")

        kinds = parse_kinds(args.kinds)
        mode = "oracle" if args.oracle else args.query_mode
        STEM_STATE[0] = args.stem_len
        KINDS_STATE[0] = kinds
        MODE_STATE[0] = mode
        MULTI_STATE[0] = args.multi_query
        RUNS_STATE[0] = args.baseline_runs

        if args.sweep:
            sweep(con, questions, args.stem_len, args.baseline_runs)
            return

        results, traps, tools = evaluate(
            con, questions, args.top_k, args.verbose, args.stem_len,
            kinds, mode, args.baseline_runs)
        report(results, traps, tools, args.verbose)
    finally:
        con.close()

    after = (db_path.stat().st_size, db_path.stat().st_mtime)
    print("\nБаза Parser не изменена ✓" if before == after
          else "\n!!! БАЗА PARSER ИЗМЕНИЛАСЬ — это ошибка, разберись "
               "до продолжения")


if __name__ == "__main__":
    main()
