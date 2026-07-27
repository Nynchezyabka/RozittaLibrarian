"""
scripts/make_demo_archive.py — создаёт тестовый архив в output/.

Этот скрипт строит минимальный parser.db + archive_passport.json,
похожий на то, что реально генерирует Rozitta Parser. Используется:
- для юнит-тестов (tests/test_tools.py);
- для ручной проверки UI без наличия реального архива.

Запуск: python scripts/make_demo_archive.py
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
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
                "comments": [],
            },
            {
                "message_id": 300,
                "date": "2024-10-01T09:15:00",
                "username": "@philosophy_daily",
                "sender_type": "channel",
                "text": "Голосовое сообщение от участника с разобранной транскрипцией.",
                "voice": {
                    "transcription": "Сегодня услышала, как мама обесценивает мой новый проект. Сказала «ну, посмотрим, надолго ли тебя хватит». Стало обидно, но я поняла, что это её страх за меня, а не реальная оценка.",
                    "model_type": "base",
                },
                "comments": [],
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
                "text": "Кто использовал asyncpg vs SQLAlchemy async? Что выбираете для новых проектов?",
                "comments": [
                    {"message_id": 1001, "date": "2025-02-10T11:05:00", "username": "@dev_boris", "text": "SQLAlchemy 2.0 async — топ. Миграции Alembic работают, типы приятные."},
                    {"message_id": 1002, "date": "2025-02-10T11:08:00", "username": "@dev_carol", "text": "asyncpg быстрее в 2-3 раза на сырых запросах. Но если кодогенерации много — SQLAlchemy удобнее."},
                ],
            },
            {
                "message_id": 2000,
                "date": "2025-03-05T16:30:00",
                "username": "@dev_boris",
                "sender_type": "user",
                "text": "FTS5 в SQLite — недооценённая штука. Для полнотекстового поиска по архиву до 10М строк работает отлично, без всяких Elasticsearch.",
                "comments": [],
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
    """Сгенерировать archive_passport.json на основе данных архива."""
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


def main():
    output_root = Path(__file__).resolve().parent.parent / "output"
    output_root.mkdir(parents=True, exist_ok=True)

    for archive in DEMO_ARCHIVES:
        archive_root = output_root / archive["id"]
        archive_root.mkdir(parents=True, exist_ok=True)
        db_path = archive_root / "parser.db"
        build_db(db_path, archive)
        write_passport(archive_root, archive)
        print(f"  ✓ {archive['id']}: {archive_root}")

    print(f"\nГотово. Демо-архивы созданы в: {output_root}")


if __name__ == "__main__":
    main()
