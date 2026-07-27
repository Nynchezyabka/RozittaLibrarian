"""
core — внутренности Librarian.

Архитектура (librarian_рабочий_план.md §2):
- Parser DB — только чтение (parser_db.py).
- Своя база librarian.db + FTS5 (librarian_db.py).
- Пять инструментов (tools.py), не знающих про LLM.
- Архивы ищутся в output/ (archive.py).

Импорты снаружи — только через __init__, чтобы пути были короткие:
    from core import LibrarianCore
"""
from .librarian_core import LibrarianCore  # noqa: F401
