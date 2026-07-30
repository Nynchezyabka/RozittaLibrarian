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
    """
    Фасад над всеми архивами.

    Два способа создать:
    1) LibrarianCore(output_root) — legacy, для тестов: одна папка, rglob.
    2) LibrarianCore.from_registry(config_path) — production: реестр из TOML,
       миграция output/ при первом запуске.

    В обоих случаях один и тот же публичный API: list_archives(), open_archive(),
    search(), read_post() и т.д.
    """

    def __init__(self, output_root: Path | str):
        self.output_root = Path(output_root)
        self.output_root.mkdir(parents=True, exist_ok=True)
        self._discovery = ArchiveDiscovery(self.output_root)
        self._registry: "ArchiveRegistry | None" = None  # legacy-режим
        self._lock = threading.RLock()
        # archive_id -> (Archive, ParserDB, LibrarianDB)
        self._open: dict[str, tuple[Archive, ParserDB, LibrarianDB]] = {}

    @classmethod
    def from_registry(cls, config_path: Path | str,
                      output_root: Path | str | None = None) -> "LibrarianCore":
        """
        Создать LibrarianCore из TOML-реестра.

        При первом запуске (реестр пуст + есть output_root с архивами) —
        миграция: архивы из output/ автоматически добавляются в реестр.
        Это сохраняет демо-архивы и старые пользовательские архивы доступными.

        :param config_path: путь к config/registry.toml
        :param output_root: папка для миграции (обычно BASE_DIR / "output").
                            None — миграция не выполняется.
        """
        from .registry import ArchiveRegistry
        registry = ArchiveRegistry(config_path)
        registry.load()
        if output_root is not None:
            registry.migrate_from_output_dir(output_root)
        # МИГРАЦИЯ: исправляем id='archive' / 'archive_N', оставшиеся от
        # старого _slugify (который превращал кириллицу в '_'). Переименовываем
        # в имя папки, если оно осмысленное и не конфликтует с другими id.
        cls._migrate_default_ids(registry)
        # Создаём экземпляр без вызова __init__ (через __new__), затем
        # инициализируем поля вручную — чтобы не создавать лишний discovery
        # по output_root.
        inst = cls.__new__(cls)
        inst.output_root = Path(output_root) if output_root else Path(".")
        # from_entries: discovery берёт id из реестра, а не из p.name —
        # это гарантирует, что Archive.id == RegistryEntry.id, и
        # registry.remove(archive_id) сработает.
        inst._discovery = ArchiveDiscovery.from_entries(registry.list())
        inst._registry = registry
        inst._lock = threading.RLock()
        inst._open = {}
        return inst

    @staticmethod
    def _migrate_default_ids(registry) -> None:
        """
        Переименовать id='archive', 'archive_2', ... в имя папки (если оно
        осмысленное и не конфликтует). Чинит уже добавленные архивы без
        ручного вмешательства пользователя.

        Условия переименования:
        - id совпадает с шаблоном 'archive' или 'archive_<N>'
        - path указывает на папку, имя которой ≠ 'archive' (т.е. имя папки
          осмысленное — кириллица, латиница со словами и т.п.)
        - новое id уникально в реестре (после удаления старой записи)
        - новое id проходит SLUG_RE (обновлённый, с поддержкой кириллицы)
        """
        import re as _re
        from .registry import SLUG_RE
        default_pat = _re.compile(r"^archive(?:_(\d+))?$")
        entries = registry.list()
        # Соберём занятые id (кроме тех, что будем переименовывать)
        used_ids = {e.id for e in entries if not default_pat.match(e.id)}
        for entry in list(entries):
            if not default_pat.match(entry.id):
                continue
            try:
                p = Path(entry.path)
            except Exception:
                continue
            folder_name = p.name
            if not folder_name or folder_name == "archive":
                continue
            # Проверяем, что новое id валидно и уникально
            new_id = folder_name.strip()
            if not SLUG_RE.match(new_id):
                continue
            if new_id in used_ids:
                # Конфликт — добавляем суффикс
                base = new_id
                n = 2
                while new_id in used_ids:
                    new_id = f"{base}_{n}"
                    n += 1
            # Переименование: удалить старую запись, добавить новую
            try:
                registry.remove(entry.id)
                registry.add(id=new_id, path=entry.path,
                             source_type=entry.source_type)
                used_ids.add(new_id)
            except Exception:
                # Не вышло — откатываем (хотя в теории запись уже удалена;
                # в крайнем случае пользователь добавит заново через UI).
                continue

    @property
    def registry(self) -> "ArchiveRegistry | None":
        """Реестр архивов (None в legacy-режиме, без реестра)."""
        return self._registry

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

        BUG-001 (П3): счётчики берём из parser.db, не из паспорта.
        У реальных архивов (telegram_archive.db) archive_passport.json часто
        отсутствует или содержит messages_count=0. Если рисовать паспортные
        нули — пользователь видит «0 сообщений» на главной, хотя /inspect
        показывает 16 495. Здесь открываем parser.db для каждого архива
        (временный read-only коннект), берём count_messages() и
        count_transcriptions(), подменяем в карточке. Если parser.db нет
        или не открывается — карточка остаётся с паспортом (fallback).
        """
        cards: list[dict] = []
        for a in self.list_archives():
            card = a.to_card_dict()
            try:
                from .parser_db import ParserDB as _ParserDB
                pdb = _ParserDB(a.parser_db_path).open()
                try:
                    real_msgs = pdb.count_messages()
                    real_transc = pdb.count_transcriptions()
                    if real_msgs > 0:
                        card["messages_count"] = real_msgs
                    if real_transc > 0:
                        card["transcriptions_count"] = real_transc
                finally:
                    pdb.close()
            except Exception:
                # Не смогли открыть parser.db — карточка остаётся как есть.
                # Это нормально для «битых» архивов: пользователь увидит
                # 0, но карточка будет показана.
                pass
            cards.append(card)
        return cards

    def refresh_discovery(self) -> None:
        """
        Пересоздать _discovery из текущего реестра.

        Вызывать после registry.add()/remove(), чтобы list_archives()
        увидел изменения. В legacy-режиме (без реестра) — no-op.
        """
        with self._lock:
            if self._registry is None:
                return
            from .archive import ArchiveDiscovery
            # from_entries: discovery использует id из реестра, не из p.name.
            self._discovery = ArchiveDiscovery.from_entries(self._registry.list())
            # Сбрасываем кэш открытых архивов, которые пропали из реестра.
            valid_ids = {e.id for e in self._registry.list()}
            self._open = {k: v for k, v in self._open.items()
                          if k in valid_ids}

    def detect_source_type(self, path: str) -> dict:
        """
        Автодетект типа источника по содержимому папки.

        Возвращает dict:
          {ok: True,  source_type: 'telegram_archive'|'text_folder', hint: str}
          {ok: False, error: str}

        Логика:
          - 'telegram_archive' если в папке есть parser.db (маркер архива Rozitta Parser)
          - 'text_folder' если в папке есть ≥1 .md/.txt файл на верхнем уровне
          - иначе — не похоже на архив
        """
        from .registry import _validate_path
        try:
            _validate_path(path)
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        p = Path(path).resolve()
        if not p.exists() or not p.is_dir():
            return {"ok": False, "error": f"Папка не существует: {path!r}"}
        # 1) Telegram-архив. Поддерживаем два имени БД:
        # parser.db (демо-архивы) и telegram_archive.db (реальные архивы
        # от Rozitta Parser).
        from .archive import PARSER_DB_ALIASES
        db_path = None
        for name in PARSER_DB_ALIASES:
            cand = p / name
            if cand.exists():
                db_path = cand
                break
        if db_path is not None:
            passport = p / "archive_passport.json"
            hint = f"Найден {db_path.name} — архив Rozitta Parser"
            if passport.exists():
                hint += " + archive_passport.json"
            return {"ok": True, "source_type": "telegram_archive", "hint": hint}
        # 2) Текстовая папка (книги, статьи, заметки)
        text_exts = {".md", ".txt", ".markdown"}
        try:
            text_count = sum(
                1 for child in p.iterdir()
                if child.is_file() and child.suffix.lower() in text_exts
            )
        except (OSError, PermissionError) as e:
            return {"ok": False, "error": f"Нет прав читать папку: {e}"}
        if text_count > 0:
            return {
                "ok": True,
                "source_type": "text_folder",
                "hint": f"Найдено текстовых файлов: {text_count}",
            }
        return {
            "ok": False,
            "error": (
                "Папка не похожа на архив: нет ни parser.db, "
                "ни telegram_archive.db, ни .md/.txt файлов"
            ),
        }

    def add_archive(self, path: str, archive_id: str | None = None) -> dict:
        """
        Добавить архив в реестр по пути.

        1. detect_source_type — если не вышло, ValueError.
        2. Сгенерить уникальный id (из имени папки или явно заданный).
        3. registry.add().
        4. refresh_discovery().

        Возвращает {archive_id, source_type, path, card}.
        """
        from .registry import _slugify, _validate_path
        if self._registry is None:
            raise RuntimeError("Реестр недоступен (legacy-режим). "
                               "Используйте LibrarianCore.from_registry().")
        # Валидация пути
        _validate_path(path)
        p = Path(path).resolve()
        if not p.exists() or not p.is_dir():
            raise ValueError(f"Папка не существует: {path!r}")
        # Детект
        detect = self.detect_source_type(str(p))
        if not detect["ok"]:
            raise ValueError(detect["error"])
        source_type = detect["source_type"]
        # ИДЕМПОТЕНТНОСТЬ ПО PATH: если архив с тем же (resolve) путём уже
        # в реестре — возвращаем существующую запись, не создаём _2, _3, ...
        # Фикс жалобы "добавились три раза": пользователь несколько раз
        # добавлял одну и ту же папку, и каждый раз получал новый id.
        resolved_path = str(Path(path).resolve())
        for entry in self._registry.list():
            try:
                if Path(entry.path).resolve() == Path(resolved_path):
                    # Уже в реестре — возвращаем существующий без шума.
                    try:
                        archives = self.list_archives()
                        a = next((x for x in archives if x.id == entry.id), None)
                        card = a.to_card_dict() if a else {
                            "id": entry.id,
                            "title": p.name,
                            "source_type": entry.source_type,
                            "path": entry.path,
                        }
                    except Exception:
                        card = {
                            "id": entry.id,
                            "title": p.name,
                            "source_type": entry.source_type,
                            "path": entry.path,
                        }
                    return {
                        "archive_id": entry.id,
                        "source_type": entry.source_type,
                        "path": entry.path,
                        "card": card,
                        "already_registered": True,
                    }
            except (OSError, ValueError):
                continue
        # ID
        if archive_id:
            archive_id = archive_id.strip()
            if not archive_id:
                archive_id = _slugify(p.name)
        else:
            archive_id = _slugify(p.name)
        # Уникальность id (если только что не вышли через already_registered)
        base = archive_id
        n = 2
        while self._registry.has(archive_id):
            archive_id = f"{base}_{n}"
            n += 1
        # Добавление
        entry = self._registry.add(
            id=archive_id, path=str(p), source_type=source_type,
        )
        # Обновить discovery, чтобы архив сразу появился в list_archives()
        self.refresh_discovery()
        # Карточка (если получится — на случай если папка не откроется,
        # вернём хотя бы минимальный словарь)
        try:
            archives = self.list_archives()
            archive = next((a for a in archives if a.id == archive_id), None)
            card = archive.to_card_dict() if archive else {
                "id": entry.id,
                "title": p.name,
                "source_type": source_type,
                "path": str(p),
            }
        except Exception:
            card = {
                "id": entry.id,
                "title": p.name,
                "source_type": source_type,
                "path": str(p),
            }
        return {
            "archive_id": entry.id,
            "source_type": source_type,
            "path": str(p),
            "card": card,
        }

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
            # Группы зависят от parser.db, но переиндексация часто идёт
            # рука об руку с обновлением parser.db — сбросим кэш на всякий.
            self._invalidate_groups_cache(archive_id)
            return stats

    def close_archive(self, archive_id: str) -> None:
        with self._lock:
            entry = self._open.pop(archive_id, None)
            if entry:
                _, parser_db, lib_db = entry
                parser_db.close()
                lib_db.close()
            self._invalidate_groups_cache(archive_id)

    def close_all(self) -> None:
        with self._lock:
            for aid in list(self._open.keys()):
                self.close_archive(aid)

    def remove_archive(self, archive_id: str) -> bool:
        """
        Удалить архив из реестра (только запись — файлы на диске остаются).

        Логика:
        1. close_archive() — закрыть БД, если они открыты.
        2. registry.remove() — убрать запись из TOML.
        3. refresh_discovery() — пересобрать discovery без этой папки.

        Возвращает True, если запись была удалена, False — если не найдена.
        Не выбрасывает исключения, если архив не в реестре (просто False).
        """
        if self._registry is None:
            raise RuntimeError(
                "Реестр недоступен (legacy-режим). "
                "Используйте LibrarianCore.from_registry()."
            )
        # 1) Закрыть БД, если они открыты (иначе Windows не даст трогать файлы,
        #    а на Unix просто освободим память).
        try:
            self.close_archive(archive_id)
        except Exception:
            pass
        # 2) Удалить из реестра
        removed = self._registry.remove(archive_id)
        # 3) Пересобрать discovery
        if removed:
            self.refresh_discovery()
        return removed

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
        """Поиск по архиву.

        Г3: по умолчанию включает групповое ранжирование — клиент получает
        `groups` и `groups_count` в ответе. Чтобы отключить (например, для
        отладки или экономии), передайте include_groups=False в kwargs.

        Б2: если quota_per_kind не передан — подставляем 5 (значение по
        умолчанию для UI, узкая выдача лучше широкой). Чтобы отключить
        квоты — передайте quota_per_kind=None явно. Квоты гарантируют
        присутствие постов автора в выдаче; групповое ранжирование даёт
        порядок (Г3).
        """
        archive = self.open_archive(archive_id)
        parser_db, lib_db = self._get_dbs(archive_id)
        kw.setdefault("parser_db", parser_db)
        kw.setdefault("include_groups", True)
        return T.search(archive, lib_db, query, **kw)

    def read_post(self, archive_id: str, *, chat_id: int, message_id: int, **kw) -> dict:
        archive = self.open_archive(archive_id)
        parser_db, _ = self._get_dbs(archive_id)
        _, lib_db = self._get_dbs(archive_id)
        return T.read_post(archive, parser_db, lib_db,
                           chat_id=chat_id, message_id=message_id, **kw)

    def get_message(self, archive_id: str, *, message_id: int,
                    chat_id: Optional[int] = None, **kw) -> dict:
        """UI-4 ридер: полное сообщение одним запросом."""
        archive = self.open_archive(archive_id)
        parser_db, _ = self._get_dbs(archive_id)
        return T.get_message(archive, parser_db,
                             message_id=message_id, chat_id=chat_id, **kw)

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

    # ------------------------------------------------------------------
    # Группы (Г1+Г2) — детерминированное вычисление из parser.db
    # ------------------------------------------------------------------

    def list_groups(self, archive_id: str,
                    group_type: Optional[str] = None) -> list[dict]:
        """
        Все группы архива, опционально одного типа.

        :param archive_id: id архива
        :param group_type: 'post_thread' | 'participant_thread' | 'cycle' |
                           'transcript' | 'month' | None (все)
        :return: список Group.to_dict()
        """
        groups = self._get_groups(archive_id)
        if group_type:
            groups = [g for g in groups if g.type == group_type]
        return [g.to_dict() for g in groups]

    def groups_for_message(self, archive_id: str,
                           message_id: int,
                           chat_id: Optional[int] = None) -> list[dict]:
        """Все группы, в которые входит данное сообщение."""
        from .groups import GroupsBuilder
        with self._lock:
            archive = self.open_archive(archive_id)
            parser_db, _ = self._get_dbs(archive_id)
            builder = GroupsBuilder(archive, parser_db)
            groups = builder.for_message(message_id, chat_id=chat_id)
        return [g.to_dict() for g in groups]

    def _get_groups(self, archive_id: str) -> list:
        """
        Получить все группы архива (с кэшированием в памяти).

        Кэш хранится в self._groups_cache[archive_id]. Сбрасывается
        при reindex или close_archive.
        """
        from .groups import GroupsBuilder
        with self._lock:
            if not hasattr(self, "_groups_cache"):
                self._groups_cache: dict[str, list] = {}
            if archive_id in self._groups_cache:
                return self._groups_cache[archive_id]
            archive = self.open_archive(archive_id)
            parser_db, _ = self._get_dbs(archive_id)
            builder = GroupsBuilder(archive, parser_db)
            groups = builder.build_all()
            self._groups_cache[archive_id] = groups
            return groups

    def _invalidate_groups_cache(self, archive_id: str | None = None) -> None:
        """Сбросить кэш групп. archive_id=None — сбросить весь кэш."""
        if not hasattr(self, "_groups_cache"):
            return
        if archive_id is None:
            self._groups_cache.clear()
        else:
            self._groups_cache.pop(archive_id, None)

    def top_terms(self, archive_id: str, limit: int = 8) -> list[str]:
        """
        Топ-термины архива для чипов-примеров (UI-спец. §3).
        Вычисляется на лету из FTS5-словаря librarian.db.
        """
        self.open_archive(archive_id)  # гарантирует, что индекс построен
        _, lib_db = self._get_dbs(archive_id)
        return lib_db.top_terms(limit=limit)

    def open_archive_with_card(self, archive_id: str) -> tuple[Archive, list[str]]:
        """
        Открыть архив и вернуть его вместе с динамическими чипами.
        Чипы считаются здесь, чтобы вызывающему коду (WS/HTTP) не нужно
        было знать про librarian_db.

        Дополнительно: подменяем passport.messages_count / transcriptions_count
        реальными значениями из parser.db. Это чинит "сообщений 0" в UI,
        когда паспорт пустой или неполный (частый случай для пользовательских
        архивов, где archive_passport.json отсутствует).
        """
        archive = self.open_archive(archive_id)
        # Реальные счётчики из parser.db
        try:
            parser_db, _ = self._get_dbs(archive_id)
            real_msgs = parser_db.count_messages()
            real_transc = parser_db.count_transcriptions()
            if real_msgs > 0:
                archive.passport.messages_count = real_msgs
            if real_transc > 0:
                archive.passport.transcriptions_count = real_transc
        except Exception:
            pass
        try:
            chips = self.top_terms(archive_id, limit=8)
        except Exception:
            # Если FTS по какой-то причине не построен — не падаем, отдаём без чипов.
            chips = []
        return archive, chips

    def archive_status(self, archive_id: str) -> dict:
        """
        Диагностическая информация об открытом архиве.

        Возвращает:
            - messages_in_parser_db / transcriptions_in_parser_db (count)
            - fts_doc_count / index_built / index_version (LibrarianDB)
            - parser_db_path / db_filename (для лога)
            - schema: результат parser_db.inspect_schema() — таблицы,
              колонки messages, user_version, альтернативные имена.
              ВАЖНО: если messages_in_parser_db == 0, именно schema
              объяснит почему (таблицы нет / называется иначе / пустая).

        Используется в live-логе при open_archive и для отладки "почему
        ничего не ищется". Все вызовы обёрнуты в try/except — диагностика
        не должна ронять открытие архива.
        """
        self.open_archive(archive_id)
        parser_db, lib_db = self._get_dbs(archive_id)
        # Схема — отдельным вызовом, обёрнутым в try/except (если БД
        # битая, inspect не должен ронять весь archive_status).
        schema: dict = {}
        try:
            schema = parser_db.inspect_schema()
        except Exception:
            schema = {"error": "inspect_schema failed"}
        return {
            "archive_id": archive_id,
            "parser_db_path": str(parser_db.path),
            "db_filename": parser_db.path.name,
            "messages_in_parser_db": parser_db.count_messages(),
            "transcriptions_in_parser_db": parser_db.count_transcriptions(),
            "fts_doc_count": lib_db.doc_count(),
            "index_built": lib_db.is_built(),
            "index_version": lib_db.index_version(),
            "schema": schema,
        }
