#!/bin/bash
# scripts/run_and_test.sh — запустить Librarian, прогнать WS smoke-тест, остановить.
set -e
cd "$(dirname "$0")/.."

PORT=${LIBRARIAN_PORT:-8012}
echo "→ Запуск Librarian на порту $PORT …"
LIBRARIAN_PORT=$PORT python3 main.py > /tmp/librarian.log 2>&1 &
SERVER_PID=$!
trap "kill $SERVER_PID 2>/dev/null; wait $SERVER_PID 2>/dev/null; echo '→ сервер остановлен'" EXIT

# Ждём, пока сервер не начнёт отвечать
for i in $(seq 1 20); do
    if curl -s "http://localhost:$PORT/api/health" > /dev/null 2>&1; then
        echo "✓ сервер поднялся (попытка $i)"
        break
    fi
    sleep 0.5
done

echo ""
echo "=== health ==="
curl -s "http://localhost:$PORT/api/health" | python3 -m json.tool
echo ""

echo "=== WS smoke test ==="
python3 "scripts/ws_smoke_test.py" "$PORT"
