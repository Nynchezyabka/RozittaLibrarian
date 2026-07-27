"""
core/archive.py — открытие архивов Rozitta Parser.

Архивом считается папка в output/, в которой:
  - есть archive_passport.json (метаданные, что внутри, периоды, чаты),
  - есть parser.db (SQLite с таблицами messages/transcriptions, см. CLAUDE.md).

Этот модуль ничего не пишет — только чтение. Поиск — рекурсивный, чтобы
найти архивы, лежащие на один уровень глубже (output/ChannelName/parser.db).

Паспорт — JSON-файл с минимально ожидаемой схемой:
    {
        "title":           "Название канала",
        "chat_id":         -1001234567890,
        "chat_type":       "channel" | "group" | "forum" | "private",
        "username":        "@channel_name" | null,
        "date_from":       "2024-01-15T10:00:00",
        "date_to":         "2026-07-20T18:30:00",
        "messages_count":  1234,
        "transcriptions_count": 12,
        "shelves": [
            {"kind": "messages",       "label": "Сообщения", "count": 1234},
            {"kind": "transcriptions", "label": "Транскрипции", "count": 12},
            ...
        ],
        "parser_version":  "1.7.3",
        "exported_at":     "2026-07-21T12:00:00"
    }

Если каких-то полей нет —填补ываем дефолтами и помечаем паспорт как partial.
Это безопасно: Librarian должен работать даже с архивами ранних версий Parser.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional


PASSPORT_FILENAME = "archive_passport.json"
PARSER_DB_FILENAME = "parser.db"


# ---------------------------------------------------------------------------
# DTO
# ---------------------------------------------------------------------------

@dataclass
class Shelf:
    """Полка архива — тип контента в паспорте."""
    kind: str            # "messages" | "transcriptions" | "media" | ...
    label: str           # человеко-читаемое название
    count: int = 0       # сколько записей на полке


@dataclass
class ArchivePassport:
    """Метаданные одного архива. Может быть partial."""
    archive_id: str              # имя папки архива (стабильный идентификатор)
    title: str
    chat_id: Optional[int] = None
    chat_type: Optional[str] = None
    username: Optional[str] = None
    date_from: Optional[str] = None
    date_to: Optional[str] = None
    messages_count: int = 0
    transcriptions_count: int = 0
    shelves: list[Shelf] = field(default_factory=list)
    parser_version: Optional[str] = None
    exported_at: Optional[str] = None
    partial: bool = False        # True, если паспорт собрали из дефолтов

    def to_dict(self) -> dict:
        d = asdict(self)
        return d


@dataclass
class Archive:
    """Один найденный архив."""
    id: str                 # archive_id (= имя папки)
    root: Path              # абсолютный путь к папке архива
    passport: ArchivePassport
    parser_db_path: Path    # путь к parser.db
    has_librarian_db: bool = False  # обновляется после индексации

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "root": str(self.root),
            "passport": self.passport.to_dict(),
            "parser_db_path": str(self.parser_db_path),
            "has_librarian_db": self.has_librarian_db,
        }


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

class ArchiveDiscovery:
    """Поиск архивов в корневой папке (обычно output/)."""

    def __init__(self, root: Path | str):
        self.root = Path(root)

    def list_archives(self) -> list[Archive]:
        """
        Рекурсивно ищет пары (archive_passport.json, parser.db).
        Возвращает список без дублей (если паспорт найден на двух уровнях —
        приоритет у более глубокой папки, где лежит parser.db).
        """
        if not self.root.exists():
            return []

        found: dict[str, Archive] = {}

        # Сначала ищем все parser.db — это маркер архива.
        for db_path in self.root.rglob(PARSER_DB_FILENAME):
            archive_root = db_path.parent
            archive_id = archive_root.name
            if archive_id in found:
                continue
            passport = self._read_passport(archive_root, archive_id)
            found[archive_id] = Archive(
                id=archive_id,
                root=archive_root,
                passport=passport,
                parser_db_path=db_path,
                has_librarian_db=(archive_root / "librarian.db").exists(),
            )

        # Если parser.db нет, но паспорт есть — показываем как «битый» архив,
        # чтобы пользователь видел его в UI и понимал, что Parser не закончил.
        for pass_path in self.root.rglob(PASSPORT_FILENAME):
            archive_root = pass_path.parent
            archive_id = archive_root.name
            if archive_id in found:
                continue
            passport = self._read_passport(archive_root, archive_id)
            # Parser.db отсутствует — нет смысла показывать.
            # Решение: пропускаем, чтобы не плодить пустые карточки.
            # (Если когда-нибудь захотим показать — раскомментировать.)

        return sorted(found.values(), key=lambda a: a.passport.title.lower() or a.id)

    # -----------------------------------------------------------------------

    def _read_passport(self, archive_root: Path, archive_id: str) -> ArchivePassport:
        pass_path = archive_root / PASSPORT_FILENAME
        if not pass_path.exists():
            return ArchivePassport(
                archive_id=archive_id,
                title=archive_id,
                partial=True,
            )
        try:
            raw = json.loads(pass_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            # Битый паспорт — не падаем, показываем как partial.
            return ArchivePassport(
                archive_id=archive_id,
                title=archive_id,
                partial=True,
            )

        shelves_raw = raw.get("shelves") or []
        shelves = [
            Shelf(
                kind=s.get("kind", "unknown"),
                label=s.get("label", s.get("kind", "unknown")),
                count=int(s.get("count", 0) or 0),
            )
            for s in shelves_raw if isinstance(s, dict)
        ]

        return ArchivePassport(
            archive_id=archive_id,
            title=raw.get("title") or archive_id,
            chat_id=raw.get("chat_id"),
            chat_type=raw.get("chat_type"),
            username=raw.get("username"),
            date_from=raw.get("date_from"),
            date_to=raw.get("date_to"),
            messages_count=int(raw.get("messages_count", 0) or 0),
            transcriptions_count=int(raw.get("transcriptions_count", 0) or 0),
            shelves=shelves,
            parser_version=raw.get("parser_version"),
            exported_at=raw.get("exported_at"),
            partial=False,
        )
