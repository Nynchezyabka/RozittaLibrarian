"""
core/librarian_core.py — фасад, объединяющий archive/parser_db/librarian_db.

LibrarianCore держит в памяти открытые архивы и их базы:
    {
        archive_id: (Archive, ParserDB, LibrarianDB)
    }

Это позволяет UI/оркестратору не думать про lifecycle баз, а только звать
core.search(archive_id, query) и т.п.

Потокобезопасность: один connection на базу, check_same_thread=False.
Для стадии 1 (один пользователь, один браузер) этого достаточно. Если
когда-нибудь понадобится многопоток — каждый тред открывает свой connection
к тем же файлам.
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Optional

from .archive import Archive, ArchiveDiscovery
from .librarian_db import LibrarianDB
from .parser_db import ParserDB
from . import tools as T


class ArchiveNotOpenError(RuntimeError):
    pass


class LibrarianCore:
    """Фасад над всеми архивами в output/."""

    def __init__(self, output_root: Path | str):
        self.output_root = Path(output_root)
        self.output_root.mkdir(parents=True, exist_ok=True)
        self._discovery = ArchiveDiscovery(self.output_root)
        self._lock = threading.RLock()
        # archive_id -> (Archive, ParserDB, LibrarianDB)
        self._open: dict[str, tuple[Archive, ParserDB, LibrarianDB]] = {}

    # ------------------------------------------------------------------
    # Discovery & lifecycle
    # ------------------------------------------------------------------

    def list_archives(self) -> list[Archive]:
        """Список архивов в output/. Не открывает базы."""
        return self._discovery.list_archives()

    def list_archives_as_dict(self) -> list[dict]:
        """Полные словари архивов (включая паспорт целиком)."""
        return [a.to_dict() for a in self.list_archives()]

    def list_archives_as_cards(self) -> list[dict]:
        """
        Карточки архивов для экрана 1 (UI-спецификация §2).
        Только поля, нужные для отрисовки карточки: emoji, title, username,
        тип, счётчики, период, чипы. Без путей и без внутренних id баз.
        """
        return [a.to_card_dict() for a in self.list_archives()]

    def open_archive(self, archive_id: str) -> Archive:
        """
        Открыть архив: открыть parser.db (ro) и librarian.db (rw),
        при необходимости построить FTS-индекс.
        Возвращает Archive (с обновлённым has_librarian_db).
        """
        with self._lock:
            if archive_id in self._open:
                return self._open[archive_id][0]
            archives = self.list_archives()
            archive = next((a for a in archives if a.id == archive_id), None)
            if archive is None:
                raise ArchiveNotOpenError(
                    f"Архив не найден: {archive_id}. "
                    "Возможно, папку переименовали — обновите список."
                )
            parser_db = ParserDB(archive.parser_db_path).open()
            parser_db.assert_supported()
            lib_db_path = archive.root / "librarian.db"
            lib_db = LibrarianDB(lib_db_path).open()
            if not lib_db.is_built():
                lib_db.build_index(parser_db)
            archive.has_librarian_db = True
            self._open[archive_id] = (archive, parser_db, lib_db)
            return archive

    def ensure_index(self, archive_id: str, force: bool = False) -> dict:
        """Перестроить FTS-индекс архива. Возвращает статистику."""
        with self._lock:
            archive = self.open_archive(archive_id)
            parser_db, lib_db = self._get_dbs(archive_id)
            if force:
                # Сбрасываем версию и зовём build_index
                with lib_db.cursor() as cur:
                    cur.execute("DELETE FROM meta WHERE key = 'index_version'")
            stats = lib_db.build_index(parser_db)
            return stats

    def close_archive(self, archive_id: str) -> None:
        with self._lock:
            entry = self._open.pop(archive_id, None)
            if entry:
                _, parser_db, lib_db = entry
                parser_db.close()
                lib_db.close()

    def close_all(self) -> None:
        with self._lock:
            for aid in list(self._open.keys()):
                self.close_archive(aid)

    def _get_dbs(self, archive_id: str) -> tuple[ParserDB, LibrarianDB]:
        entry = self._open.get(archive_id)
        if entry is None:
            raise ArchiveNotOpenError(
                f"Архив не открыт: {archive_id}. Сначала open_archive()."
            )
        return entry[1], entry[2]

    def _archive(self, archive_id: str) -> Archive:
        entry = self._open.get(archive_id)
        if entry is None:
            raise ArchiveNotOpenError(
                f"Архив не открыт: {archive_id}. Сначала open_archive()."
            )
        return entry[0]

    # ------------------------------------------------------------------
    # Tools — тонкие обёртки, проверяют что архив открыт
    # ------------------------------------------------------------------

    def search(self, archive_id: str, query: str, **kw) -> dict:
        archive = self.open_archive(archive_id)
        _, lib_db = self._get_dbs(archive_id)
        return T.search(archive, lib_db, query, **kw)

    def read_post(self, archive_id: str, *, chat_id: int, message_id: int, **kw) -> dict:
        archive = self.open_archive(archive_id)
        parser_db, _ = self._get_dbs(archive_id)
        _, lib_db = self._get_dbs(archive_id)
        return T.read_post(archive, parser_db, lib_db,
                           chat_id=chat_id, message_id=message_id, **kw)

    def stats(self, archive_id: str, **kw) -> dict:
        archive = self.open_archive(archive_id)
        parser_db, _ = self._get_dbs(archive_id)
        return T.stats(archive, parser_db, **kw)

    def whats_new(self, archive_id: str, **kw) -> dict:
        archive = self.open_archive(archive_id)
        parser_db, _ = self._get_dbs(archive_id)
        return T.whats_new(archive, parser_db, **kw)

    def list_shelves(self, archive_id: str) -> dict:
        archive = self.open_archive(archive_id)
        parser_db, _ = self._get_dbs(archive_id)
        return T.list_shelves(archive, parser_db)
