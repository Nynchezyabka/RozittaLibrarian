"""
scripts/ws_smoke_test.py — сквозной smoke-тест через WebSocket.

Подключается к /ws, открывает архив, делает поиск «обесценива»,
читает первый найденный пост. Печатает все события в лог.
"""
import asyncio
import json
import sys
from pathlib import Path

import websockets


async def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8012
    url = f"ws://localhost:{port}/ws"
    print(f"→ Подключение к {url}")
    async with websockets.connect(url) as ws:
        # hello
        msg = json.loads(await ws.recv())
        print(f"  ← {msg['type']}: {msg.get('message')}")

        # 1) list_archives
        print("→ list_archives")
        await ws.send(json.dumps({"op": "list_archives", "args": {}}))
        await drain_until_result(ws, "list_archives")

        # 2) open_archive
        print("→ open_archive: demo_philosophy_channel")
        await ws.send(json.dumps({"op": "open_archive",
                                  "archive_id": "demo_philosophy_channel",
                                  "args": {}}))
        await drain_until_result(ws, "open_archive")

        # 3) search "обесценива"
        print("→ search: обесценива")
        await ws.send(json.dumps({"op": "search",
                                  "archive_id": "demo_philosophy_channel",
                                  "args": {"query": "обесценива"}}))
        result = await drain_until_result(ws, "search")
        if result and result["data"]["count"] > 0:
            first = result["data"]["hits"][0]
            print(f"  ★ первый хит: {first['author']} / msg {first['message_id']}")
            print(f"    snippet: {first['snippet'][:120]}")

            # 4) read_post
            print(f"→ read_post: chat={first['chat_id']} msg={first['message_id']}")
            await ws.send(json.dumps({"op": "read_post",
                                      "archive_id": "demo_philosophy_channel",
                                      "args": {"chat_id": first["chat_id"],
                                               "message_id": first["message_id"]}}))
            rp = await drain_until_result(ws, "read_post")
            if rp:
                post = rp["data"]["post"]
                print(f"  ★ автор: {post['author']}, дата: {post['date']}")
                print(f"    текст: {post['text'][:200]}…")
                print(f"    комментариев: {rp['data']['comments']['total']}")
                if rp["data"]["transcription"]:
                    print(f"    транскрипция есть ({len(rp['data']['transcription']['text'])} симв.)")

        # 5) stats
        print("→ stats overview")
        await ws.send(json.dumps({"op": "stats",
                                  "archive_id": "demo_philosophy_channel",
                                  "args": {"kind": "overview"}}))
        await drain_until_result(ws, "stats")

        # 6) whats_new
        print("→ whats_new")
        await ws.send(json.dumps({"op": "whats_new",
                                  "archive_id": "demo_philosophy_channel",
                                  "args": {"limit": 5}}))
        await drain_until_result(ws, "whats_new")

        # 7) list_shelves
        print("→ list_shelves")
        await ws.send(json.dumps({"op": "list_shelves",
                                  "archive_id": "demo_philosophy_channel",
                                  "args": {}}))
        await drain_until_result(ws, "list_shelves")

    print("\n✓ Smoke-тест пройден.")


async def drain_until_result(ws, expected_op, timeout=10.0):
    """Принимает сообщения, пока не придёт 'result' для ожидаемой op."""
    while True:
        raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
        msg = json.loads(raw)
        if msg["type"] == "log":
            print(f"    [{msg.get('level', 'info')}] {msg['message']}")
        elif msg["type"] == "result":
            assert msg["op"] == expected_op, f"Ожидали {expected_op}, получили {msg['op']}"
            return msg
        elif msg["type"] == "error":
            print(f"  ✗ ОШИБКА: {msg['message']}")
            return None
        else:
            print(f"    ? {msg}")


if __name__ == "__main__":
    asyncio.run(main())
