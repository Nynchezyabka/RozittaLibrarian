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
# Каноническое имя БД парсера.
PARSER_DB_FILENAME = "parser.db"
# Альтернативные имена, которые тоже признаются как БД парсера.
# Реальные архивы от Rozitta Parser создают telegram_archive.db, демо-архивы
# в этом проекте создают parser.db. При поиске и детекте оба имени валидны.
PARSER_DB_ALIASES = ("parser.db", "telegram_archive.db")


def _find_parser_db(folder: Path) -> Path | None:
    """Вернуть путь к БД парсера в folder, если он есть (любое из алиасов).
    Возвращает первый найденный. Если нет ни одного — None."""
    for name in PARSER_DB_ALIASES:
        p = folder / name
        if p.exists():
            return p
    return None


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
    # ВАЖНО: поле `chips` УБРАНО из паспорта. Чипы — это данные библиотекаря,
    # а не парсера: парсер не должен знать, какие слова «важные». Чипы
    # вычисляются на лету из FTS5-словаря (см. LibrarianDB.top_terms) и
    # подставляются в карточку при открытии архива.
    # Поле `chips` в JSON-паспорте (если есть от старых версий) — игнорируется.

    def to_dict(self) -> dict:
        d = asdict(self)
        return d


# ---------------------------------------------------------------------------
# UI-вспомогательные функции (спецификация §2-3)
# ---------------------------------------------------------------------------

# Иконка по типу источника (спецификация §2): канал 📚 / группа 💬 / личный чат 👤
_EMOJI_BY_TYPE = {
    "channel": "📚",
    "group":   "💬",
    "forum":   "💬",
    "private": "👤",
}


def emoji_for_type(chat_type: Optional[str]) -> str:
    """Эмодзи для карточки архива по типу источника."""
    if not chat_type:
        return "📦"
    return _EMOJI_BY_TYPE.get(chat_type, "📦")


def type_label(chat_type: Optional[str]) -> str:
    """Человекочитаемый тип источника для карточки/сводки."""
    return {
        "channel": "канал",
        "group":   "группа",
        "forum":   "форум",
        "private": "личный чат",
    }.get(chat_type or "", "архив")


def format_date_period(date_from: Optional[str], date_to: Optional[str]) -> str:
    """
    Превратить ISO-даты паспорта в человекочитаемый период:
        '15 сен 2024 — 1 окт 2024'
    Если даты нет — пустая строка. Если только одна — без тире.
    """
    if not date_from and not date_to:
        return ""
    parts = []
    if date_from:
        parts.append(_humanize_date(date_from))
    parts.append("—")
    if date_to:
        parts.append(_humanize_date(date_to))
    if not date_from:
        # только date_to
        return parts[1] + " " + parts[2]
    if not date_to:
        return parts[0] + " —"
    return " ".join(parts)


_RU_MONTHS_SHORT = [
    "янв", "фев", "мар", "апр", "мая", "июн",
    "июл", "авг", "сен", "окт", "ноя", "дек",
]


def _humanize_date(iso: str) -> str:
    """
    '2024-09-15T10:30:00' → '15 сен 2024'
    Любой сбой → вернуть исходную строку обрезанной.
    """
    try:
        # Берём только дату, игнорируем время.
        date_part = iso.split("T")[0]
        y, m, d = date_part.split("-")
        m_idx = int(m) - 1
        if m_idx < 0 or m_idx > 11:
            return date_part
        return f"{int(d)} {_RU_MONTHS_SHORT[m_idx]} {y}"
    except Exception:
        return (iso or "")[:10] or "—"


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

    def to_card_dict(self, chips: Optional[list[str]] = None) -> dict:
        """
        Карточка архива для экрана 1 (спецификация §2):
        только те поля, что нужны UI для отрисовки карточки.

        Полный паспорт остаётся доступен через to_dict()['passport'] —
        UI может вытаскивать оттуда chat_id и т.п. для будущих цитат-ссылок.

        Параметр `chips` — список примеров поисковых слов под строкой
        поиска. Если None — чипы не подставлены (карточка с экрана 1, где
        строки поиска ещё нет). Если список — подставляется как есть.
        Источник чипов — LibrarianDB.top_terms(), вычисляются на лету при
        открытии архива; парсер их не хранит.
        """
        p = self.passport
        return {
            "id":                 self.id,
            "emoji":              emoji_for_type(p.chat_type),
            "title":              p.title or self.id,
            "username":           p.username,
            "chat_type":          p.chat_type,
            "type_label":         type_label(p.chat_type),
            "messages_count":     p.messages_count,
            "transcriptions_count": p.transcriptions_count,
            "date_from":          p.date_from,
            "date_to":            p.date_to,
            "date_period":        format_date_period(p.date_from, p.date_to),
            "chips":              list(chips) if chips is not None else [],
            "has_librarian_db":   self.has_librarian_db,
            "partial":            p.partial,
        }


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

class ArchiveDiscovery:
    """
    Поиск архивов по списку папок.

    Два режима:
    1) ArchiveDiscovery(root) — старый API, для тестов: одна корневая папка,
       рекурсивный поиск parser.db внутри (как раньше).
    2) ArchiveDiscovery.from_paths([path1, path2, ...]) — новый API: каждая
       папка-аргумент сама является архивом (содержит parser.db + паспорт).
       Это режим для реестра — никаких rglob, пользователь явно указал
       каждую папку.

    Недоступные папки (нет диска, нет прав) пропускаются молча.
    """

    def __init__(self, root: Path | str):
        self.root = Path(root)
        self._paths: list[Path] | None = None  # None = legacy-режим
        # _entries: список (id, path) — режим реестра с явными id.
        # Если задан — используется вместо _paths. Это чинит несоответствие
        # id между реестром (slug) и discovery (p.name): discovery берёт id
        # из entry, а не из имени папки.
        self._entries: list[tuple[str, Path]] | None = None

    @classmethod
    def from_paths(cls, paths: list[Path | str]) -> "ArchiveDiscovery":
        """Создать discovery по списку путей к архивам (режим реестра).

        Deprecated: prefer from_entries() — он берёт id из реестра,
        а не из имени папки, что чинит несоответствие id для архивов
        с кириллическими именами. from_paths оставлен для совместимости
        со старыми тестами.
        """
        d = cls.__new__(cls)
        d.root = Path("/")  # не используется, но поле нужно
        d._paths = [Path(p) for p in paths]
        d._entries = None
        return d

    @classmethod
    def from_entries(cls, entries: list) -> "ArchiveDiscovery":
        """
        Создать discovery по списку RegistryEntry (или любых объектов
        с полями .id и .path).

        В отличие от from_paths, этот метод сохраняет id из реестра —
        это гарантирует, что Archive.id совпадает с RegistryEntry.id,
        и registry.remove(archive_id) сработает.

        :param entries: list[RegistryEntry] (или list[dict] с keys id/path)
        """
        d = cls.__new__(cls)
        d.root = Path("/")
        d._paths = None
        d._entries = []
        for e in entries:
            # Поддерживаем и RegistryEntry (dataclass), и dict
            if hasattr(e, "id") and hasattr(e, "path"):
                eid, epath = str(e.id), Path(e.path)
            elif isinstance(e, dict):
                eid, epath = str(e["id"]), Path(e["path"])
            else:
                continue
            d._entries.append((eid, epath))
        return d

    def list_archives(self) -> list[Archive]:
        """
        Возвращает список архивов.

        Legacy-режим (root задан): рекурсивный rglob по parser.db.
        Реестр-режим (_entries задан): открываем каждую папку с явным id.
        Реестр-режим (_paths задан, deprecated): каждая папка открывается напрямую,
            id = имя папки (НЕ совпадает с реестром для кириллических имён).
        """
        if self._entries is not None:
            return self._list_from_entries()
        if self._paths is not None:
            return self._list_from_paths()
        return self._list_from_root()

    # ------------------------------------------------------------------
    # Legacy: одна корневая папка (тесты)
    # ------------------------------------------------------------------

    def _list_from_root(self) -> list[Archive]:
        if not self.root.exists():
            return []

        found: dict[str, Archive] = {}

        # Сначала ищем все БД парсера — это маркер архива. Ищем по всем
        # алиасам (parser.db, telegram_archive.db), чтобы поддержать и демо-
        # архивы, и реальные архивы от Rozitta Parser.
        for db_filename in PARSER_DB_ALIASES:
            for db_path in self.root.rglob(db_filename):
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

    # ------------------------------------------------------------------
    # Новый режим: список конкретных путей (реестр)
    # ------------------------------------------------------------------

    def _list_from_paths(self) -> list[Archive]:
        found: dict[str, Archive] = {}
        for p in self._paths or []:
            try:
                if not p.exists() or not p.is_dir():
                    continue
                db_path = _find_parser_db(p)
                if db_path is None:
                    continue
                archive_id = p.name
                # Если два разных пути дают одинаковый id (имя папки) —
                # добавляем суффикс _2, _3, ...
                base_id = archive_id
                n = 2
                while archive_id in found:
                    archive_id = f"{base_id}_{n}"
                    n += 1
                passport = self._read_passport(p, archive_id)
                found[archive_id] = Archive(
                    id=archive_id,
                    root=p,
                    passport=passport,
                    parser_db_path=db_path,
                    has_librarian_db=(p / "librarian.db").exists(),
                )
            except (OSError, PermissionError):
                # Недоступная папка (нет диска, нет прав) — пропускаем.
                continue
        return sorted(found.values(), key=lambda a: a.passport.title.lower() or a.id)

    # -----------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Режим реестра: список (id, path) — id из реестра, не из имени папки.
    # Это чинит несоответствие id для архивов с кириллическими именами:
    # _slugify("Мастер Группа") = "Мастер Группа" (после фикса registry.py),
    # а не "archive". Discovery вернёт Archive с тем же id, что в реестре.
    # ------------------------------------------------------------------

    def _list_from_entries(self) -> list[Archive]:
        found: dict[str, Archive] = {}
        for entry_id, p in self._entries or []:
            try:
                if not p.exists() or not p.is_dir():
                    continue
                db_path = _find_parser_db(p)
                if db_path is None:
                    continue
                archive_id = entry_id
                # Если два разных пути дают одинаковый id (маловероятно,
                # но теоретически возможно из старого реестра) — суффикс.
                base_id = archive_id
                n = 2
                while archive_id in found:
                    archive_id = f"{base_id}_{n}"
                    n += 1
                passport = self._read_passport(p, archive_id)
                found[archive_id] = Archive(
                    id=archive_id,
                    root=p,
                    passport=passport,
                    parser_db_path=db_path,
                    has_librarian_db=(p / "librarian.db").exists(),
                )
            except (OSError, PermissionError):
                continue
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

        # Поле `chips` в паспорте (если осталось от старых версий) — игнорируем.
        # Чипы теперь вычисляются на стороне библиотекаря через FTS5 top_terms.

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
