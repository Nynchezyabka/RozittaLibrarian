"""
Полный smoke-тест WebSocket-операций UI-1 + UI-2 + UI-3 + UI-4.
Подключается к ws://localhost:8012/ws, вызывает последовательно:
  list_archives → open_archive → list_shelves → stats → whats_new → scan_archives
  → top_terms → search (с комментариями) → get_message (пост + голосовое)
и проверяет, что каждый ответ содержит ожидаемые поля.
"""
import asyncio
import json
import sys

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
TECH_ARCHIVE_ID = "demo_tech_forum"


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
        print(f"    chips={a['chips']}  (на экране 1 — пусто, без открытия архива)")
        assert a["emoji"] == "📚"
        assert a["type_label"] == "канал"
        assert a["date_period"] == "15 сен 2024 — 1 окт 2024"
        # Чипы на home-экране НЕ вычисляются (без открытия librarian.db)
        assert a["chips"] == [], f"На home чипов быть не должно, got {a['chips']}"
        print("  PASS\n")

        # 2. open_archive — чипы появляются динамически из FTS
        print(f"→ open_archive ({ARCHIVE_ID})")
        await ws.send(json.dumps({"op": "open_archive", "archive_id": ARCHIVE_ID}))
        data = await recv_until(ws, "open_archive")
        card = data.get("card", {})
        passport = data.get("passport", {})
        print(f"  card.title={card.get('title')!r}")
        print(f"  card.chips (dynamic)={card.get('chips')}")
        print(f"  passport.passport.chat_id={passport.get('passport',{}).get('chat_id')}")
        assert card["id"] == ARCHIVE_ID
        assert passport["passport"]["chat_id"] == -1001234567890
        # Чипы должны быть вычислены (top_terms из FTS)
        assert len(card["chips"]) > 0, "Чипы должны быть вычислены после open_archive"
        # Проверяем, что в чипах есть осмысленные слова из архива
        chips_str = " ".join(card["chips"]).lower()
        assert "обесценивани" in chips_str or "обесценива" in chips_str, \
            f"В чипах должно быть слово про обесценивание, got {card['chips']}"
        print("  PASS\n")

        # 3. top_terms — отдельная операция
        print(f"→ top_terms ({ARCHIVE_ID})")
        await ws.send(json.dumps({"op": "top_terms", "archive_id": ARCHIVE_ID, "args": {"limit": 5}}))
        data = await recv_until(ws, "top_terms")
        terms = data.get("terms", [])
        print(f"  terms: {terms}")
        assert len(terms) > 0
        assert len(terms) <= 5
        print("  PASS\n")

        # 4. search — должен находить и посты, и комментарии
        print(f"→ search «обесценив» (должен найти посты и комментарии)")
        await ws.send(json.dumps({
            "op": "search", "archive_id": ARCHIVE_ID,
            "args": {"query": "обесценив"}
        }))
        data = await recv_until(ws, "search")
        hits = data.get("hits", [])
        print(f"  count: {data.get('count')}")
        comment_hits = [h for h in hits if h.get("is_comment")]
        post_hits = [h for h in hits if not h.get("is_comment")]
        print(f"  постов: {len(post_hits)}, комментариев: {len(comment_hits)}")
        for h in hits[:3]:
            tag = f" [comment to #{h['post_message_id']}]" if h.get("is_comment") else ""
            print(f"    #{h['message_id']}{tag} @{h['author']}: {h['snippet'][:60]}…")
        assert data["count"] > 0, "Должен найти хотя бы одно совпадение"
        assert len(comment_hits) > 0, "Должен найти хотя бы один комментарий"
        # Проверяем структуру hit с is_comment
        ch = comment_hits[0]
        assert ch["post_message_id"] is not None, "У комментария должен быть post_message_id"
        assert ch["url"].startswith(f"#/a/{ARCHIVE_ID}/m/")
        assert "?c=" in ch["url"], "URL комментария должен содержать ?c="
        print("  PASS\n")

        # 5. search — расширенный фильтр по автору
        print(f"→ search «обесценив» с фильтром author=@anna_p")
        await ws.send(json.dumps({
            "op": "search", "archive_id": ARCHIVE_ID,
            "args": {"query": "обесценив", "author": "@anna_p"}
        }))
        data = await recv_until(ws, "search")
        hits = data.get("hits", [])
        print(f"  count: {data.get('count')}")
        for h in hits:
            print(f"    @{h['author']}: {h['snippet'][:60]}…")
            assert h["author"] == "@anna_p"
        print("  PASS\n")

        # 6. get_message — обычный пост с комментариями
        print(f"→ get_message 400 (пост с комментариями)")
        await ws.send(json.dumps({
            "op": "get_message", "archive_id": ARCHIVE_ID,
            "args": {"message_id": 400}
        }))
        data = await recv_until(ws, "get_message")
        post = data.get("post", {})
        comments = data.get("comments", {})
        neighbors = data.get("neighbors", {})
        tg = data.get("telegram_link")
        print(f"  post.author={post.get('author')}")
        print(f"  post.text (превью): {post.get('text','')[:60]}…")
        print(f"  comments.total={comments.get('total')}")
        print(f"  neighbors: prev={neighbors.get('prev')}, next={neighbors.get('next')}")
        print(f"  telegram_link={tg}")
        print(f"  is_voice={data.get('is_voice')}")
        assert post["message_id"] == 400
        assert post["author"] == "@philosophy_daily"
        assert comments["total"] >= 1, "У поста 400 должны быть комментарии"
        assert neighbors["prev"] is not None, "Должен быть предыдущий пост"
        assert neighbors["next"] is not None, "Должен быть следующий пост"
        assert tg == "https://t.me/philosophy_daily/400"
        assert data["is_voice"] is False
        print("  PASS\n")

        # 7. get_message — голосовое сообщение с транскрипцией
        print(f"→ get_message 300 (голосовое с транскрипцией)")
        await ws.send(json.dumps({
            "op": "get_message", "archive_id": ARCHIVE_ID,
            "args": {"message_id": 300}
        }))
        data = await recv_until(ws, "get_message")
        post = data.get("post", {})
        tr = data.get("transcription")
        print(f"  post.author={post.get('author')}")
        print(f"  is_voice={data.get('is_voice')}")
        print(f"  transcription.text (превью): {(tr.get('text') if tr else '')[:60]}…")
        print(f"  comments.total={data.get('comments',{}).get('total')}")
        assert data["is_voice"] is True
        assert tr is not None, "У голосового должна быть транскрипция"
        assert "обесценива" in tr["text"].lower()
        print("  PASS\n")

        # 8. list_shelves
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

        # 9. stats
        print(f"→ stats ({ARCHIVE_ID})")
        await ws.send(json.dumps({"op": "stats", "archive_id": ARCHIVE_ID}))
        data = await recv_until(ws, "stats")
        print(f"  total_messages={data.get('messages_count')}")
        print("  PASS\n")

        # 10. whats_new (limit=3 для стартовой страницы)
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

        # 11. scan_archives
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
