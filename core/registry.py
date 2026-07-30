"""
core/registry.py — реестр подключённых архивов.

Хранится в config/registry.toml рядом с программой. Каждая запись:
    [[archives]]
    id = "demo_philosophy_channel"          # slug, человеко-читаемый
    path = "/home/z/RozittaParser/output/demo_philosophy_channel"
    source_type = "telegram_archive"        # адаптер: telegram_archive | text_folder | ...
    added_at = "2026-07-29T11:50:00"

При первом запуске (реестр пуст + есть output/ с архивами) — миграция:
каждая папка-архив в output/ добавляется в реестр автоматически. Это
сохраняет демо-архивы и старые пользовательские архивы доступными без
ручной перерегистрации.

Этот модуль ничего не знает про FTS/SQLite/passport — только пути и метаданные.
"""
from __future__ import annotations

import datetime as _dt
import re
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


# Python 3.11+ имеет tomllib в stdlib. Для записи используем простой
# ручной сериализатор (TOML простой, зависимостей не тянем).
if sys.version_info >= (3, 11):
    import tomllib as _toml
else:  # pragma: no cover
    try:
        import tomli as _toml  # type: ignore
    except ImportError:
        _toml = None  # type: ignore


# Разрешаем Unicode-буквы (включая кириллицу), цифры, _, -, пробелы.
# ЗАПРЕЩАЕМ только опасные символы (файловая система / TOML / HTML-атрибуты):
#   / \ : * ? " < > | \' и control chars (\x00-\x1f).
# Это позволяет id="Мастер Группа" быть валидным.
# ВАЖНО: \s сюда НЕ включаем — иначе пробелы запретятся.
SLUG_RE = re.compile("^[^\\/\:*\\?\"<>|'\\x00-\\x1f]+$", re.UNICODE)
KNOWN_SOURCE_TYPES = ("telegram_archive", "text_folder")  # расширяется


@dataclass
class RegistryEntry:
    """Одна запись реестра — один подключённый архив."""
    id: str                       # уникальный slug
    path: str                     # абсолютный путь к папке архива
    source_type: str = "telegram_archive"
    added_at: str = ""            # ISO datetime

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "path": self.path,
            "source_type": self.source_type,
            "added_at": self.added_at,
        }


def _now_iso() -> str:
    return _dt.datetime.now().replace(microsecond=0).isoformat()


def _slugify(name: str) -> str:
    r"""
    Превратить имя папки в безопасный id.

    НЕ трогаем кириллицу и другие Unicode-буквы — они остаются как есть,
    чтобы id был человекочитаемым (например, "Мастер Группа Макеевой Виолетты").
    Заменяем только символы, опасные в файловой системе / TOML / HTML-атрибутах:
        / \ : * ? " < > | ' и любые control chars (\x00-\x1f)
    Сжимаем множественные '_' в один, обрезаем '_' по краям.
    Если после очистки строка пустая — fallback на 'archive'.
    """
    s = (name or "").strip()
    # Опасные символы → '_'
    s = re.sub(r"[\\/:*?\"<>|\']", "_", s)
    # Control chars → '_'
    s = re.sub(r"[\x00-\x1f]", "_", s)
    # Сжимаем множественные '_'
    s = re.sub(r"_+", "_", s).strip("_").strip()
    # Ограничиваем длину (TOML-строка + UI-атрибуты — 200 хватает с запасом)
    if len(s) > 200:
        s = s[:200].rstrip("_").strip()
    return s or "archive"


class ArchiveRegistry:
    """
    Реестр подключённых архивов.

    Потокобезопасный: одна RLock на все операции. Файл читается/пишется
    целиком — для однопользовательского приложения этого достаточно.
    """

    def __init__(self, config_path: Path | str):
        self.config_path = Path(config_path)
        self._lock = threading.RLock()
        self._entries: list[RegistryEntry] = []
        self._loaded = False

    # ------------------------------------------------------------------
    # Загрузка / сохранение
    # ------------------------------------------------------------------

    def _ensure_dir(self) -> None:
        self.config_path.parent.mkdir(parents=True, exist_ok=True)

    def load(self) -> None:
        """Прочитать TOML-файл. Если файла нет — пустой реестр."""
        with self._lock:
            self._entries = []
            if not self.config_path.exists():
                self._loaded = True
                return
            if _toml is None:
                # Нет tomllib/tomli — файл есть, но читать нечем.
                # Не падаем, работаем с пустым реестром (пользователь увидит
                # сообщение в UI при попытке добавить архив).
                self._loaded = True
                return
            try:
                with open(self.config_path, "rb") as f:
                    data = _toml.load(f)
            except Exception:
                # Битый TOML — не падаем, стартуем с пустым реестром.
                # Файл не перезаписываем (вдруг пользователь починит).
                self._loaded = True
                return
            for raw in data.get("archives", []):
                if not isinstance(raw, dict):
                    continue
                eid = str(raw.get("id", "")).strip()
                epath = str(raw.get("path", "")).strip()
                if not eid or not epath:
                    continue
                self._entries.append(RegistryEntry(
                    id=eid,
                    path=epath,
                    source_type=str(raw.get("source_type", "telegram_archive"))
                        or "telegram_archive",
                    added_at=str(raw.get("added_at", "")),
                ))
            self._loaded = True

    def save(self) -> None:
        """Записать TOML-файл (атомарно через temp+rename)."""
        with self._lock:
            self._ensure_dir()
            tmp = self.config_path.with_suffix(".toml.tmp")
            tmp.write_text(self._serialize(), encoding="utf-8")
            tmp.replace(self.config_path)

    def _serialize(self) -> str:
        """Простой TOML-сериализатор без зависимостей."""
        lines = [
            "# Rozitta Librarian — реестр подключённых архивов.",
            "# Автоматически управляется через UI (Добавить архив / Удалить).",
            "# Можно править вручную, пока сервер не запущен.",
            "",
        ]
        for e in self._entries:
            lines.append("[[archives]]")
            lines.append(f'id = "{_toml_escape(e.id)}"')
            lines.append(f'path = "{_toml_escape(e.path)}"')
            lines.append(f'source_type = "{_toml_escape(e.source_type)}"')
            if e.added_at:
                lines.append(f'added_at = "{_toml_escape(e.added_at)}"')
            lines.append("")
        return "\n".join(lines) + "\n"

    # ------------------------------------------------------------------
    # Доступ
    # ------------------------------------------------------------------

    def list(self) -> list[RegistryEntry]:
        """Список всех записей (копия)."""
        with self._lock:
            if not self._loaded:
                self.load()
            return list(self._entries)

    def has(self, archive_id: str) -> bool:
        with self._lock:
            return any(e.id == archive_id for e in self._entries)

    def get(self, archive_id: str) -> Optional[RegistryEntry]:
        with self._lock:
            for e in self._entries:
                if e.id == archive_id:
                    return e
            return None

    def add(self, *, id: str, path: str, source_type: str = "telegram_archive") -> RegistryEntry:
        """
        Добавить архив. Если id уже есть — ValueError.
        path проверяется на абсолютность (см. _validate_path).
        """
        id = id.strip()
        if not SLUG_RE.match(id):
            raise ValueError(
                f"Недопустимый id архива: {id!r}. "
                "Разрешены латинские буквы, цифры, _ и -."
            )
        _validate_path(path)
        if source_type not in KNOWN_SOURCE_TYPES:
            raise ValueError(
                f"Неизвестный source_type: {source_type!r}. "
                f"Допустимые: {', '.join(KNOWN_SOURCE_TYPES)}"
            )
        with self._lock:
            if not self._loaded:
                self.load()
            if self.has(id):
                raise ValueError(f"Архив с id {id!r} уже подключён")
            entry = RegistryEntry(
                id=id,
                path=str(Path(path).resolve()),
                source_type=source_type,
                added_at=_now_iso(),
            )
            self._entries.append(entry)
            self.save()
            return entry

    def remove(self, archive_id: str) -> bool:
        """Удалить запись по id. Возвращает True, если было что удалять."""
        with self._lock:
            if not self._loaded:
                self.load()
            before = len(self._entries)
            self._entries = [e for e in self._entries if e.id != archive_id]
            if len(self._entries) != before:
                self.save()
                return True
            return False

    # ------------------------------------------------------------------
    # Миграция
    # ------------------------------------------------------------------

    def migrate_from_output_dir(self, output_root: Path | str) -> int:
        """
        Если реестр пуст — найти архивы в output_root и добавить их.
        Возвращает количество добавленных записей.

        Это одноразовая миграция при первом запуске с новой версией.
        Если в реестре уже хоть что-то есть — миграция пропускается
        (не хотим плодить дубли при каждом запуске).
        """
        output_root = Path(output_root).resolve()
        with self._lock:
            if not self._loaded:
                self.load()
            if self._entries:
                return 0
            if not output_root.exists():
                return 0
            # Ищем БД парсера (как ArchiveDiscovery) — это маркер архива.
            # Поддерживаем оба имени: parser.db и telegram_archive.db.
            from .archive import PARSER_DB_ALIASES
            added = 0
            seen_ids: set[str] = set()
            all_db_paths = []
            for db_filename in PARSER_DB_ALIASES:
                all_db_paths.extend(output_root.rglob(db_filename))
            for db_path in sorted(all_db_paths):
                archive_root = db_path.parent.resolve()
                slug = _slugify(archive_root.name)
                # Если slug уже занят (две папки с одинаковым именем в разных
                # подпапках) — добавляем суффикс.
                base_slug = slug
                n = 2
                while slug in seen_ids:
                    slug = f"{base_slug}_{n}"
                    n += 1
                seen_ids.add(slug)
                try:
                    self.add(id=slug, path=str(archive_root),
                             source_type="telegram_archive")
                    added += 1
                except ValueError:
                    # Дубликаты id или проблемы с путём — пропускаем молча.
                    continue
            return added


# ---------------------------------------------------------------------------
# Утилиты
# ---------------------------------------------------------------------------

def _validate_path(path: str) -> None:
    """
    Проверить, что путь безопасен для записи в реестр:
    - абсолютный
    - не содержит .. (нельзя подсунуть системные папки)
    """
    p = Path(path)
    if not p.is_absolute():
        raise ValueError(
            f"Путь должен быть абсолютным: {path!r}. "
            "Укажите полный путь к папке архива."
        )
    # Проверка на .. в компонентах пути (не в имени файла).
    parts = p.parts
    if ".." in parts:
        raise ValueError(
            f"Путь не должен содержать '..': {path!r}."
        )


def _toml_escape(s: str) -> str:
    """Экранировать строку для TOML (минимальный набор)."""
    return s.replace("\\", "\\\\").replace('"', '\\"')
