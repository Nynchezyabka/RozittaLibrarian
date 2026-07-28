"""
scripts/make_demo_archive.py — создаёт тестовый архив в output/.

Этот скрипт строит минимальный parser.db + archive_passport.json,
похожий на то, что реально генерирует Rozitta Parser. Используется:
- для юнит-тестов (tests/test_tools.py);
- для ручной проверки UI без наличия реального архива.

ВАЖНО: в паспорте больше нет поля `chips` — чипы вычисляются библиотекарем
через FTS5 top_terms на лету. Парсер не должен знать про «важные слова».

Запуск: python scripts/make_demo_archive.py
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


DEMO_ARCHIVES = [
    {
        "id": "demo_philosophy_channel",
        "title": "Философия буднего дня",
        "chat_id": -1001234567890,
        "chat_type": "channel",
        "username": "@philosophy_daily",
        "parser_version": "1.7.3",
        "schema_version": 2,
        "posts": [
            {
                "message_id": 100,
                "date": "2024-09-15T10:30:00",
                "username": "@philosophy_daily",
                "sender_type": "channel",
                "text": "Сегодня поговорим про обесценивание. Это защитный механизм психики, при котором человек принижает значимость событий, достижений или чувств — своих или чужих. Часто встречается в форме «да это ерунда, кому это важно».",
                "comments": [
                    {"message_id": 101, "date": "2024-09-15T10:35:00", "username": "@anna_p", "text": "А как отличить обесценивание от здоровой скромности?"},
                    {"message_id": 102, "date": "2024-09-15T10:38:00", "username": "@ivan_k", "text": "Скромность — про форму подачи, обесценивание — про содержание. Если вы реально считаете достижение пустяком — это одно. Если обесцениваете, чтобы не чувствовать уязвимость — другое."},
                    {"message_id": 103, "date": "2024-09-15T10:42:00", "username": "@anna_p", "text": "Понятно, спасибо. То есть обесценивающее отношение всегда про защиту?"},
                ],
            },
            {
                "message_id": 200,
                "date": "2024-09-22T14:00:00",
                "username": "@philosophy_daily",
                "sender_type": "channel",
                "text": "Проективная идентификация — самый хитрый механизм. Человек приписывает другому свои чувства, а потом реагирует на них. Классика: «ты на меня злишься!» — при этом злится сам говорящий.",
                "comments": [
                    {"message_id": 201, "date": "2024-09-22T14:15:00", "username": "@marina_s", "text": "У меня так было с мамой. Я злилась, что она не поддерживает, а потом поняла — это я сама себя не поддерживала."},
                    {"message_id": 202, "date": "2024-09-22T14:30:00", "username": "@ivan_k", "text": "Главное — заметить этот механизм. Самое сложное — поймать момент, когда приписываешь чужое чувство."},
                ],
            },
            {
                "message_id": 300,
                "date": "2024-09-25T18:55:00",
                "username": "@philosophy_daily",
                "sender_type": "channel",
                "text": "Аудио-эфир «Ценности и цена». Слушайте в записи — или читайте расшифровку ниже.",
                "voice": {
                    "transcription": "Сегодня услышала, как мама обесценивает мой новый проект. Сказала «ну, посмотрим, надолго ли тебя хватит». Стало обидно, но я поняла, что это её страх за меня, а не реальная оценка. Ценности и цена — разные вещи. Цена — то, что платим. Ценности — то, что получаем. Обесценивание начинается там, где мы перестаём различать одно и другое. Сорок две минуты поговорили про то, как удерживать свои ценности, когда окружение просит их «обесценить» ради удобства.",
                    "model_type": "base",
                },
                "comments": [
                    {"message_id": 301, "date": "2024-09-25T19:10:00", "username": "@marina_s", "text": "Спасибо за расшифровку! Слушать не могу сейчас, почитать — самое то."},
                    {"message_id": 302, "date": "2024-09-25T19:25:00", "username": "@anna_p", "text": "Про различение ценности и цены — очень в точку. Я долгое время путала."},
                ],
            },
            {
                "message_id": 400,
                "date": "2024-09-28T14:22:00",
                "username": "@philosophy_daily",
                "sender_type": "channel",
                "text": "Проблема обесценивания труда в современных условиях не в том, что платят мало. Она в том, что результат труда перестал быть видимым: закрытая задача исчезает в трекере, а сложенная стена стоит десятилетиями. Невидимое легко обесценить.",
                "comments": [
                    {"message_id": 401, "date": "2024-09-28T15:10:00", "username": "@vassa_k", "text": "Очень точно про невидимость результата. У нас помогло еженедельное демо — труд снова стал видимым."},
                    {"message_id": 402, "date": "2024-09-28T16:02:00", "username": "@dim5x", "text": "А что делать с трудом, который невидим по своей природе — поддержка, инфраструктура?"},
                    {"message_id": 403, "date": "2024-09-28T17:00:00", "username": "@philosophy_daily", "text": "Хороший вопрос. Невидимый труд — отдельная категория, его надо делать видимым через мета-описание: что держится благодаря ему."},
                ],
            },
            {
                "message_id": 500,
                "date": "2024-09-29T10:05:00",
                "username": "@philosophy_daily",
                "sender_type": "channel",
                "text": "Обесценивание чужого опыта начинается со слов «да это же просто». Просто — для того, кто уже прошёл путь. Уважение к сложности чужого пути — минимальная форма честности.",
                "comments": [],
            },
            {
                "message_id": 600,
                "date": "2024-09-30T16:30:00",
                "username": "@philosophy_daily",
                "sender_type": "channel",
                "text": "Привычка — это решение, принятое один раз и исполняемое бесплатно. Сила воли дорога, привычки дёшевы. Хочешь изменить поведение — найди, какое решение вы приняли однажды и теперь исполняете бесплатно.",
                "comments": [
                    {"message_id": 601, "date": "2024-09-30T17:10:00", "username": "@marina_s", "text": "«Исполняемое бесплатно» — точно. Привычка не требует силы воли, в этом её сила и опасность."},
                ],
            },
            {
                "message_id": 700,
                "date": "2024-10-01T12:00:00",
                "username": "@philosophy_daily",
                "sender_type": "channel",
                "text": "Итоги сентябрьского цикла: говорили про ценности, интерфейсы восприятия, обесценивание и привычки. Общая нить — внимание. Куда уходит внимание — туда уходит и жизнь.",
                "comments": [],
            },
            {
                # Б1+Б2 regression: пост про зависть и сравнение.
                # Без обрезки окончаний поиск «зависти» / «сравнения» молча
                # вернёт 0 — это и есть эталонный пример из eval_questions.yaml.
                # Без квот по типу источникa: 4 комментария (801–804) топят
                # сам пост 800 в выдаче — это пример Б2.
                "message_id": 800,
                "date": "2024-10-05T11:00:00",
                "username": "@philosophy_daily",
                "sender_type": "channel",
                "text": "Зависть и сравнение. Зависть — это всегда про сравнение себя с другим, причём невыгодное сравнение: берём чужой фасад и подставляем к нему свою изнанку. Зависть начинается там, где сравнение перестаёт быть инструментом и становится привычкой.",
                "comments": [
                    {"message_id": 801, "date": "2024-10-05T11:20:00", "username": "@marina_s", "text": "У меня зависть всегда вспыхивает к тем, у кого «всё легко». Думаю, привычка сравнивать себя идёт из детства."},
                    {"message_id": 802, "date": "2024-10-05T11:35:00", "username": "@ivan_k", "text": "Сравнение как привычка — точно. Я ловлю себя на этом по 20 раз за день, и почти всегда невыгода придуманная."},
                    {"message_id": 803, "date": "2024-10-05T12:00:00", "username": "@anna_p", "text": "А как отличить здоровое сравнение от зависти? Сравнение ради ориентира — одно, ради самоутверждения — другое."},
                    {"message_id": 804, "date": "2024-10-05T12:15:00", "username": "@philosophy_daily", "text": "Зависть всегда имеет привкус несправедливости. Сравнение ради ориентира такого привкуса не несёт — оно техническое."},
                ],
            },
            {
                # Б3 regression: голосовое, чья расшифровка лежит ТОЛЬКО файлом
                # в 00_Индекс.md — в таблице transcriptions её нет.
                # Поле voice с пометкой "external_file" указывает демо-генератору
                # положить файл-расшифровку отдельно и добавить запись в индекс.
                "message_id": 850,
                "date": "2024-10-06T19:00:00",
                "username": "@philosophy_daily",
                "sender_type": "channel",
                "text": "Аудио-эфир «Проекция и идентификация». Слушайте запись или читайте расшифровку по ссылке в индексе.",
                "voice_external": {
                    "filename": "В14_п850_Проекция_и_идентификация.md",
                    "transcription": (
                        "Сегодня разбирали проекцию и идентификацию. Проекция — это когда "
                        "человек приписывает другому свои собственные чувства или мысли. "
                        "Идентификация — обратный ход: человек принимает чувства другого "
                        "как свои. Зависть часто работает через проекцию: мы приписываем "
                        "другому «лёгкость», а потом завидуем этой выдуманной лёгкости. "
                        "Сравнение в этом случае превращается в comparing-mirror: мы "
                        "сравниваем себя не с реальным человеком, а с его проекцией в "
                        "нашей голове. Сорок минут поговорили про то, как замечать этот "
                        "механизм и возвращать себе своё."
                    ),
                },
                "comments": [
                    {"message_id": 851, "date": "2024-10-06T19:30:00", "username": "@anna_p",
                     "text": "Спасибо за расшифровку. Слушать не могу сейчас, почитать — самое то."},
                ],
            },
        ],
    },
    {
        "id": "demo_tech_forum",
        "title": "Tech Forum — Python",
        "chat_id": -1009876543210,
        "chat_type": "group",
        "username": "@pyforum",
        "parser_version": "1.7.3",
        "schema_version": 2,
        "posts": [
            {
                "message_id": 1000,
                "date": "2025-02-10T11:00:00",
                "username": "@dev_anna",
                "sender_type": "user",
                "text": "Коллеги, кто уже переводил большой проект на строгую типизацию? Интересует, насколько mypy в strict-режиме ловит реальные баги.",
                "comments": [
                    {"message_id": 1001, "date": "2025-02-10T11:05:00", "username": "@dev_boris", "text": "SQLAlchemy 2.0 async — топ. Миграции Alembic работают, типы приятные. mypy ловит около 30% багов, остальное — ревью."},
                    {"message_id": 1002, "date": "2025-02-10T11:08:00", "username": "@dev_carol", "text": "asyncpg быстрее в 2-3 раза на сырых запросах. Но если кодогенерации много — SQLAlchemy удобнее."},
                ],
            },
            {
                "message_id": 2000,
                "date": "2025-02-14T18:40:00",
                "username": "@dev_boris",
                "sender_type": "user",
                "text": "Переводили проект на строгую типизацию. Типизация окупилась при первом же рефакторинге: mypy подсветил все места, где поменялась сигнатура.",
                "comments": [
                    {"message_id": 2001, "date": "2025-02-14T19:00:00", "username": "@dev_anna", "text": "А сколько времени занял перевод? У нас кодовая база — 80 тысяч строк."},
                    {"message_id": 2002, "date": "2025-02-14T19:15:00", "username": "@dev_boris", "text": "Два человека, три недели. Без mypy --strict. Со strict — ещё месяц."},
                ],
            },
            {
                "message_id": 3000,
                "date": "2025-02-20T09:02:00",
                "username": "@dev_anna",
                "sender_type": "user",
                "text": "Вопрос про асинхронность: FastAPI + SQLite. Есть ли смысл в async-драйвере, если база локальная?",
                "comments": [
                    {"message_id": 3001, "date": "2025-02-20T09:30:00", "username": "@dev_carol", "text": "Для локальной однопользовательской базы асинхронность драйвера почти ничего не даёт — узкое место не там. aiosqlite не быстрее sqlite3."},
                    {"message_id": 3002, "date": "2025-02-20T10:00:00", "username": "@dev_boris", "text": "Согласен. Для FastAPI + SQLite проще sync + run_in_threadpool. Асинхронность пригодится, если база сетевая."},
                ],
            },
            {
                "message_id": 4000,
                "date": "2025-03-05T16:30:00",
                "username": "@dev_boris",
                "sender_type": "user",
                "text": "FTS5 в SQLite — недооценённая штука. Для полнотекстового поиска по архиву до 10М строк работает отлично, без всяких Elasticsearch. Юникод-токенизатор unicode61 + remove_diacritics 2 закрывает русскую морфологию префиксными формами.",
                "comments": [
                    {"message_id": 4001, "date": "2025-03-05T17:00:00", "username": "@dev_anna", "text": "Подтверждаю. Использовала FTS5 для архива Telegram-канала — летает. Главное — не забыть про fts5vocab для аналитики."},
                ],
            },
        ],
    },
]


def build_db(db_path: Path, archive: dict) -> None:
    """Создать parser.db со схемой v2 и заполнить сообщениями."""
    if db_path.exists():
        db_path.unlink()
    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()

    # Создаём таблицы
    cur.execute("""
        CREATE TABLE messages (
            id                 INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id            INTEGER NOT NULL,
            message_id         INTEGER NOT NULL,
            topic_id           INTEGER,
            user_id            INTEGER,
            username           TEXT,
            date               TEXT    NOT NULL,
            text               TEXT,
            media_path         TEXT,
            file_type          TEXT,
            file_size          INTEGER,
            reply_to_msg_id    INTEGER,
            post_id            INTEGER,
            is_comment         INTEGER DEFAULT 0,
            from_linked_group  INTEGER DEFAULT 0,
            merge_group_id     INTEGER,
            merge_part_index   INTEGER,
            sender_type        TEXT    DEFAULT 'user'
        )
    """)
    cur.execute("""
        CREATE TABLE transcriptions (
            message_id  INTEGER NOT NULL,
            peer_id     INTEGER NOT NULL,
            text        TEXT    NOT NULL,
            model_type  TEXT    NOT NULL DEFAULT 'base',
            created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (message_id, peer_id)
        )
    """)
    cur.execute(f"PRAGMA user_version = 2")

    chat_id = archive["chat_id"]

    for post in archive["posts"]:
        # Сам пост
        cur.execute(
            "INSERT INTO messages (chat_id, message_id, date, username, text, "
            "post_id, is_comment, sender_type, user_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (chat_id, post["message_id"], post["date"], post["username"],
             post["text"], None, 0, post["sender_type"], 100500),
        )
        # Если есть транскрипция
        if "voice" in post:
            cur.execute(
                "INSERT INTO transcriptions (message_id, peer_id, text, model_type, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (post["message_id"], chat_id,
                 post["voice"]["transcription"],
                 post["voice"]["model_type"],
                 post["date"]),
            )
        # Комментарии
        for c in post.get("comments", []):
            cur.execute(
                "INSERT INTO messages (chat_id, message_id, date, username, text, "
                "post_id, is_comment, from_linked_group, sender_type, reply_to_msg_id, user_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (chat_id, c["message_id"], c["date"], c["username"], c["text"],
                 post["message_id"], 1, 1, "user", None, 200600),
            )

    conn.commit()
    conn.close()


def write_passport(archive_root: Path, archive: dict) -> None:
    """Сгенерировать archive_passport.json на основе данных архива.

    ВАЖНО: поле `chips` НЕ записываем. Чипы вычисляются библиотекарем
    через FTS5 top_terms (см. LibrarianDB.top_terms).
    """
    total_messages = sum(1 + len(p.get("comments", [])) for p in archive["posts"])
    total_transcriptions = sum(1 for p in archive["posts"] if "voice" in p)
    dates = []
    for p in archive["posts"]:
        dates.append(p["date"])
        for c in p.get("comments", []):
            dates.append(c["date"])
    dates.sort()

    passport = {
        "title": archive["title"],
        "chat_id": archive["chat_id"],
        "chat_type": archive["chat_type"],
        "username": archive["username"],
        "date_from": dates[0] if dates else None,
        "date_to": dates[-1] if dates else None,
        "messages_count": total_messages,
        "transcriptions_count": total_transcriptions,
        "shelves": [
            {"kind": "messages", "label": "Сообщения", "count": total_messages},
            {"kind": "transcriptions", "label": "Транскрипции", "count": total_transcriptions},
        ],
        "parser_version": archive["parser_version"],
        "exported_at": datetime.now(timezone.utc).isoformat(),
    }
    (archive_root / "archive_passport.json").write_text(
        json.dumps(passport, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def write_external_transcripts(archive_root: Path, archive: dict) -> list[tuple[int, str]]:
    """Положить на диск расшифровки, которых НЕТ в parser.db.

    Используется для теста Б3 (см. librarian_статус.md): когда Transcriber
    пишет файлы .md, а не строки в `transcriptions`. Связь «пост → файл»
    держит 00_Индекс.md в формате markdown-таблицы.

    Возвращает [(post_id, имя_файла), ...] — для построения индекса.
    """
    written: list[tuple[int, str]] = []
    for post in archive["posts"]:
        ext = post.get("voice_external")
        if not ext:
            continue
        # Лёгкая маркдаун-обёртка, как у Transcriber
        md_body = (
            f"# Расшифровка поста {post['message_id']}\n\n"
            f"_{post['date']}_\n\n"
            f"---\n\n"
            f"{ext['transcription']}\n"
        )
        (archive_root / ext["filename"]).write_text(md_body, encoding="utf-8")
        written.append((post["message_id"], ext["filename"]))
    return written


def write_transcript_index(archive_root: Path, archive: dict,
                           externals: list[tuple[int, str]]) -> None:
    """Сгенерировать 00_Индекс.md — таблица «пост → файл расшифровки».

    Формат намеренно простой: pipe-таблица с колонками
    «Пост | Дата | Файл расшифровки». Парсер в librarian_db.py понимает
    такой формат (см. load_index_transcripts).
    """
    if not externals:
        return
    lines = [
        "# Индекс расшифровок",
        "",
        "| Пост | Дата | Файл расшифровки |",
        "|---|---|---|",
    ]
    # Возьмём дату из самого поста
    posts_by_id = {p["message_id"]: p for p in archive["posts"]}
    for post_id, filename in externals:
        date = posts_by_id.get(post_id, {}).get("date", "")
        lines.append(f"| {post_id} | {date} | [{filename}]({filename}) |")
    (archive_root / "00_Индекс.md").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )


def main():
    output_root = Path(__file__).resolve().parent.parent / "output"
    output_root.mkdir(parents=True, exist_ok=True)

    for archive in DEMO_ARCHIVES:
        archive_root = output_root / archive["id"]
        archive_root.mkdir(parents=True, exist_ok=True)
        db_path = archive_root / "parser.db"
        build_db(db_path, archive)
        write_passport(archive_root, archive)
        # Б3: внешние расшифровки + 00_Индекс.md
        externals = write_external_transcripts(archive_root, archive)
        write_transcript_index(archive_root, archive, externals)
        # Удаляем старый librarian.db, чтобы при следующем открытии
        # индекс пересобрался с новой схемой.
        old_lib = archive_root / "librarian.db"
        if old_lib.exists():
            old_lib.unlink()
            print(f"    (удалил старый librarian.db)")
        # WAL/SHM тоже удаляем на всякий случай
        for suffix in ("-wal", "-shm"):
            f = archive_root / f"librarian.db{suffix}"
            if f.exists():
                f.unlink()
        n_ext = len(externals)
        print(f"  ✓ {archive['id']}: {archive_root}"
              + (f" (+{n_ext} внешних расшифровок)" if n_ext else ""))

    print(f"\nГотово. Демо-архивы созданы в: {output_root}")


if __name__ == "__main__":
    main()
