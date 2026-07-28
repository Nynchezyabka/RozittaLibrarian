"""
Полный smoke-тест WebSocket-операций UI-1+UI-2.
Подключается к ws://localhost:8012/ws, вызывает последовательно:
  list_archives → open_archive → list_shelves → stats → whats_new → scan_archives
и проверяет, что каждый ответ содержит ожидаемые поля.
"""
import asyncio
import json
import sys
from pathlib import Path

try:
    import websockets
except ImportError:
    print("Устанавливаю websockets...")
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "-q",
                           "--break-system-packages", "websockets"])
    import websockets


PORT = 8012
ARCHIVE_ID = "demo_philosophy_channel"


async def recv_until(ws, op_expected, timeout=10.0):
    """
    Получать сообщения из WS, пока не придёт {type:'result', op: op_expected}.
    Логи и hello игнорируем (но печатаем).
    Возвращает data ответа или raises TimeoutError.
    """
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=deadline - asyncio.get_event_loop().time())
        except asyncio.TimeoutError:
            break
        msg = json.loads(raw)
        t = msg.get("type")
        if t == "hello":
            print(f"  [hello] {msg.get('message')} (порт {msg.get('port')})")
        elif t == "log":
            level = msg.get("level", "info")
            print(f"  [log/{level}] {msg.get('message')}")
        elif t == "error":
            print(f"  [ERROR] {msg.get('message')}")
            raise RuntimeError(f"Server error: {msg.get('message')}")
        elif t == "result":
            if msg.get("op") == op_expected:
                return msg.get("data")
            # Чужой результат — пропускаем
            print(f"  [result/{msg.get('op')}] (не ждали, пропускаем)")
        else:
            print(f"  [{t}] {msg}")
    raise TimeoutError(f"Не дождались result op={op_expected} за {timeout} сек")


async def main():
    url = f"ws://localhost:{PORT}/ws"
    print(f"Подключение к {url} ...")
    async with websockets.connect(url) as ws:
        print("OK\n")

        # 1. list_archives
        print("→ list_archives")
        await ws.send(json.dumps({"op": "list_archives"}))
        data = await recv_until(ws, "list_archives")
        archives = data.get("archives", [])
        print(f"  Найдено архивов: {len(archives)}")
        assert len(archives) == 2, f"Ожидал 2 архива, получил {len(archives)}"
        a = next(x for x in archives if x["id"] == ARCHIVE_ID)
        print(f"  Карточка {ARCHIVE_ID}:")
        print(f"    emoji={a['emoji']}, title={a['title']!r}")
        print(f"    type_label={a['type_label']!r}, date_period={a['date_period']!r}")
        print(f"    messages_count={a['messages_count']}, transcriptions_count={a['transcriptions_count']}")
        print(f"    chips={a['chips']}")
        assert a["emoji"] == "📚"
        assert a["type_label"] == "канал"
        assert a["date_period"] == "15 сен 2024 — 1 окт 2024"
        assert "обесценивание" in a["chips"]
        print("  PASS\n")

        # 2. open_archive
        print(f"→ open_archive ({ARCHIVE_ID})")
        await ws.send(json.dumps({"op": "open_archive", "archive_id": ARCHIVE_ID}))
        data = await recv_until(ws, "open_archive")
        card = data.get("card", {})
        passport = data.get("passport", {})
        print(f"  card.title={card.get('title')!r}")
        print(f"  passport.passport.chat_id={passport.get('passport',{}).get('chat_id')}")
        assert card["id"] == ARCHIVE_ID
        assert passport["passport"]["chat_id"] == -1001234567890
        print("  PASS\n")

        # 3. list_shelves
        print(f"→ list_shelves ({ARCHIVE_ID})")
        await ws.send(json.dumps({"op": "list_shelves", "archive_id": ARCHIVE_ID}))
        data = await recv_until(ws, "list_shelves")
        shelves = data.get("shelves", [])
        print(f"  Полок: {len(shelves)}")
        for s in shelves:
            print(f"    {s['kind']}: {s['label']} ({s['count']})")
        assert len(shelves) >= 1
        kinds = [s["kind"] for s in shelves]
        assert "messages" in kinds
        print("  PASS\n")

        # 4. stats
        print(f"→ stats ({ARCHIVE_ID})")
        await ws.send(json.dumps({"op": "stats", "archive_id": ARCHIVE_ID}))
        data = await recv_until(ws, "stats")
        print(f"  total_messages={data.get('total_messages')}")
        print(f"  top_keys={list(data.keys())[:5]}")
        print("  PASS\n")

        # 5. whats_new (limit=3 для стартовой страницы)
        print(f"→ whats_new ({ARCHIVE_ID}, limit=3)")
        await ws.send(json.dumps({"op": "whats_new", "archive_id": ARCHIVE_ID, "args": {"limit": 3}}))
        data = await recv_until(ws, "whats_new")
        items = data.get("items", [])
        print(f"  Получено сообщений: {len(items)}")
        for i, it in enumerate(items):
            print(f"    [{i+1}] message_id={it.get('message_id')} author={it.get('author')!r} preview={it.get('text_preview','')[:60]!r}")
        assert len(items) == 3
        assert "text_preview" in items[0]
        print("  PASS\n")

        # 6. scan_archives
        print(f"→ scan_archives")
        await ws.send(json.dumps({"op": "scan_archives"}))
        data = await recv_until(ws, "scan_archives")
        archives2 = data.get("archives", [])
        print(f"  Найдено архивов после рескана: {len(archives2)}")
        assert len(archives2) == len(archives)
        print("  PASS\n")

    print("=== ВСЕ ТЕСТЫ ПРОЙДЕНЫ ===")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as e:
        print(f"\nОШИБКА: {e}")
        sys.exit(1)
