"""
main.py — точка входа Rozitta Librarian.

FastAPI + одна страница + WebSocket, порт 8011. Скелет как у RozittaTranscriber.

Маршруты:
    GET  /                       → index.html
    GET  /api/archives           → список найденных архивов
    POST /api/archives/{id}/open → открыть архив + построить индекс
    GET  /api/archives/{id}/shelves → list_shelves()
    GET  /api/archives/{id}/stats  → stats()
    WS   /ws                     → оркестратор:
        in:  {"op": "search"|"read_post"|"whats_new"|"stats"|"list_shelves",
              "archive_id": "...", "args": {...}}
        out: поток сообщений с live-логом шагов и финальным ответом
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from core import LibrarianCore
from core.tools import ToolError


# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
OUTPUT_ROOT = BASE_DIR / "output"
# Порт по спеке — 8011. Можно переопределить через окружение LIBRARIAN_PORT,
# если 8011 уже занят (например, другой копией Librarian в этом же контейнере).
PORT = int(os.environ.get("LIBRARIAN_PORT", "8011"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("librarian")

app = FastAPI(title="Rozitta Librarian", version="0.1.0-stage1")
core = LibrarianCore(OUTPUT_ROOT)

# Static files (favicon, etc.) — но index.html отдаём вручную, чтобы
# добавить no-cache и убедиться, что корень — это всегда свежий UI.
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# ---------------------------------------------------------------------------
# HTTP routes
# ---------------------------------------------------------------------------

@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html", media_type="text/html")


@app.get("/api/health")
async def health():
    return {
        "status": "ok",
        "service": "rozitta_librarian",
        "port": PORT,
        "stage": "1-reading-hall",
        "archives_found": len(core.list_archives()),
    }


@app.get("/api/archives")
async def api_list_archives():
    """Список архивов в output/. Не открывает базы. Возвращает карточки (UI-спец. §2)."""
    return {"archives": core.list_archives_as_cards()}


@app.post("/api/archives/{archive_id}/open")
async def api_open_archive(archive_id: str):
    try:
        archive = core.open_archive(archive_id)
        return archive.to_dict()
    except Exception as e:
        log.exception("open_archive failed")
        return JSONResponse({"error": str(e)}, status_code=400)


@app.get("/api/archives/{archive_id}/card")
async def api_archive_card(archive_id: str):
    """Карточка архива для экрана 2 (UI-спец. §3): метаданные без открытия баз."""
    archives = core.list_archives()
    archive = next((a for a in archives if a.id == archive_id), None)
    if archive is None:
        return JSONResponse({"error": f"Архив не найден: {archive_id}"}, status_code=404)
    return archive.to_card_dict()


@app.post("/api/archives/{archive_id}/reindex")
async def api_reindex(archive_id: str):
    try:
        stats = core.ensure_index(archive_id, force=True)
        return {"archive_id": archive_id, "reindex": stats}
    except Exception as e:
        log.exception("reindex failed")
        return JSONResponse({"error": str(e)}, status_code=400)


@app.get("/api/archives/{archive_id}/shelves")
async def api_shelves(archive_id: str):
    try:
        return core.list_shelves(archive_id)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@app.get("/api/archives/{archive_id}/stats")
async def api_stats(archive_id: str, kind: str = "overview", top_authors: int = 20):
    try:
        return core.stats(archive_id, kind=kind, top_authors=top_authors)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)


# ---------------------------------------------------------------------------
# WebSocket orchestrator
# ---------------------------------------------------------------------------

async def _ws_send(ws: WebSocket, type_: str, **payload) -> None:
    """Безопасная отправка JSON-фрейма в вебсокет."""
    try:
        await ws.send_json({"type": type_, **payload})
    except Exception:
        log.debug("ws send failed", exc_info=True)


async def _ws_log(ws: WebSocket, message: str, level: str = "info") -> None:
    """Live-лог шага — показывается в правой колонке UI."""
    await _ws_send(ws, "log", level=level, message=message)


@app.websocket("/ws")
async def ws_main(ws: WebSocket):
    """
    Финальная версия. _handle_op — корутина, и WebSocket-сообщения уходят
    в основном event loop; долгие SQL-операции уходят в threadpool через
    asyncio.to_thread внутри самой корутины (см. ниже).
    """
    await ws.accept()
    await _ws_send(ws, "hello", message="Rozitta Librarian готов к работе", port=PORT)
    log.info("WS connected")
    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                await _ws_send(ws, "error", message="Ожидался JSON")
                continue

            op = msg.get("op")
            archive_id = msg.get("archive_id", "")
            args = msg.get("args", {}) or {}

            if not op:
                await _ws_send(ws, "error", message="Нет поля op")
                continue

            try:
                result = await _handle_op_sync(ws, op, archive_id, args)
            except ToolError as e:
                await _ws_send(ws, "error", message=str(e))
                continue
            except Exception as e:
                log.exception("op failed: %s", op)
                await _ws_send(ws, "error", message=f"Внутренняя ошибка: {e}")
                continue

            if result is not None:
                await _ws_send(ws, "result", op=op, archive_id=archive_id, data=result)
    except WebSocketDisconnect:
        log.info("WS disconnected")
    except Exception:
        log.exception("WS error")


async def _handle_op_sync(ws: WebSocket, op: str, archive_id: str, args: dict) -> Optional[dict]:
    """
    Синхронная обёртка вокруг логики операций. Долгие SQL-запросы уходят в
    threadpool через asyncio.to_thread, чтобы event loop не блокировался
    и ws.send_json мог отправлять live-логи параллельно.
    """
    args = args or {}

    if op == "list_archives":
        await _ws_log(ws, "Сканирую папку output/ …")
        cards = await asyncio.to_thread(core.list_archives_as_cards)
        await _ws_log(ws, f"Найдено архивов: {len(cards)}",
                      level="success" if cards else "warning")
        return {"archives": cards}

    if op == "scan_archives":
        # Пересканировать output/ — детект новых архивов и изменений.
        # Сейчас просто пересканируем discovery; дельта-переиндексация
        # появится, когда будет хранение mtime в librarian.db.
        await _ws_log(ws, "Пересканирую папку output/ …")
        cards = await asyncio.to_thread(core.list_archives_as_cards)
        await _ws_log(ws, f"Найдено архивов: {len(cards)}",
                      level="success" if cards else "warning")
        return {"archives": cards, "scanned_at": core.output_root.name}

    if op == "open_archive":
        await _ws_log(ws, f"Открываю архив «{archive_id}» …")
        try:
            archive, chips = await asyncio.to_thread(core.open_archive_with_card, archive_id)
        except Exception as e:
            await _ws_log(ws, f"Не удалось открыть: {e}", level="error")
            raise
        await _ws_log(ws, f"Архив открыт: {archive.passport.title}", level="success")
        # Карточка с динамическими чипами + полный паспорт (для chat_id и т.п.)
        return {
            "card": archive.to_card_dict(chips=chips),
            "passport": archive.to_dict(),
        }

    if op == "top_terms":
        # Топ-термины архива для чипов (UI-спец. §9).
        # Можно вызвать отдельно, если чипы нужно обновить без переоткрытия.
        limit = int(args.get("limit", 8))
        terms = await asyncio.to_thread(core.top_terms, archive_id, limit)
        return {"archive_id": archive_id, "terms": terms}

    if op == "search":
        query = (args.get("query") or "").strip()
        if not query:
            await _ws_send(ws, "error", message="Пустой поисковый запрос")
            return None
        await _ws_log(ws, f"Ищу: «{query}» …")
        # Прокидываем расширенные фильтры (UI-3 «Точный поиск»)
        kwargs = {k: v for k, v in args.items() if k != "query"}
        result = await asyncio.to_thread(core.search, archive_id, query, **kwargs)
        n = result["count"]
        await _ws_log(
            ws,
            f"Найдено: {n} (лимит 20)",
            level="success" if n > 0 else "warning",
        )
        return result

    if op == "get_message":
        # UI-4 ридер: полное сообщение + комментарии + соседи + t.me одним запросом.
        message_id = args.get("message_id")
        if message_id is None:
            await _ws_send(ws, "error", message="Нужен message_id")
            return None
        await _ws_log(ws, f"Открываю сообщение {message_id} …")
        try:
            result = await asyncio.to_thread(
                core.get_message, archive_id,
                message_id=int(message_id),
                chat_id=args.get("chat_id"),
                comment_limit=args.get("comment_limit", 200),
                comment_offset=args.get("comment_offset", 0),
            )
        except ToolError as e:
            await _ws_log(ws, str(e), level="error")
            raise
        await _ws_log(
            ws,
            f"Пост от {result['post']['author']} · комментариев: {result['comments']['total']}",
            level="success",
        )
        return result

    if op == "read_post":
        # Оставлено для совместимости / отладки — UI-4 использует get_message.
        chat_id = args.get("chat_id")
        message_id = args.get("message_id")
        if chat_id is None or message_id is None:
            await _ws_send(ws, "error", message="Нужны chat_id и message_id")
            return None
        await _ws_log(ws, f"Читаю пост {chat_id}/{message_id} …")
        result = await asyncio.to_thread(
            core.read_post, archive_id,
            chat_id=int(chat_id), message_id=int(message_id),
            comment_limit=args.get("comment_limit", 200),
            comment_offset=args.get("comment_offset", 0),
        )
        c = result["comments"]["total"]
        await _ws_log(
            ws,
            f"Пост от {result['post']['author']} · комментариев: {c}",
            level="success",
        )
        return result

    if op == "stats":
        await _ws_log(ws, "Собираю статистику …")
        return await asyncio.to_thread(core.stats, archive_id, **args)

    if op == "whats_new":
        await _ws_log(ws, "Что нового …")
        return await asyncio.to_thread(core.whats_new, archive_id, **args)

    if op == "list_shelves":
        await _ws_log(ws, "Полки архива …")
        return await asyncio.to_thread(core.list_shelves, archive_id)

    await _ws_send(ws, "error", message=f"Неизвестная операция: {op}")
    return None


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def _open_browser_after_delay(url: str, delay: float = 1.5) -> None:
    """
    Открыть дефолтный браузер на странице приложения через `delay` секунд.
    Запускается в отдельном потоке — uvicorn.run блокирующий, поэтому
    поток нужен, чтобы таймер сработал уже после того, как сервер начал слушать.
    """
    import threading
    import webbrowser

    def _open():
        import time
        time.sleep(delay)
        try:
            webbrowser.open(url, new=2, autoraise=True)
        except Exception:
            # Браузер может быть недоступен (безголовая система, контейнер).
            # Не страшно — пользователь сам откроет URL из консоли.
            log.warning("Не удалось открыть браузер автоматически. URL: %s", url)

    t = threading.Thread(target=_open, name="browser-opener", daemon=True)
    t.start()


if __name__ == "__main__":
    import uvicorn
    url = f"http://localhost:{PORT}"
    print(f"\n  Rozitta Librarian — порт {PORT}")
    print(f"  output root: {OUTPUT_ROOT}")
    print(f"  {url}")
    print(f"  (браузер откроется автоматически через 1.5 сек)\n")
    _open_browser_after_delay(url, delay=1.5)
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=PORT,
        reload=False,
        log_level="info",
    )
