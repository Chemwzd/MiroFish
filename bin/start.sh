#!/bin/sh
# 启动 MiroFish 本地栈：本地图谱 zepshim(8100) -> 后端 API(5001) -> 前端 UI(3000)
# 全部 127.0.0.1，全部本机；任何进程都不占住当前终端。
set -e
ROOT=$(cd "$(dirname "$0")/.." && pwd)
LOGS="$ROOT/var/logs"
DAEMON="$ROOT/bin/daemon.py"
PY=$(command -v python3 || echo /Users/youzi/anaconda3/bin/python3)
mkdir -p "$LOGS"

port_busy() { lsof -nP -iTCP:"$1" -sTCP:LISTEN >/dev/null 2>&1; }
wait_http() {  # wait_http <url> <秒>
  i=0
  while [ "$i" -lt "$2" ]; do
    [ "$(curl -s -o /dev/null -w '%{http_code}' -m 2 "$1" 2>/dev/null)" = "200" ] && return 0
    i=$((i + 1)); sleep 1
  done
  return 1
}

# 1) 本地图谱兼容层
if port_busy 8100; then
  echo "[skip] zepshim 已在 8100 运行"
else
  "$PY" "$DAEMON" --log "$LOGS/zepshim.log" --cwd "$ROOT/zepshim" -- \
    /bin/sh -c 'set -a; [ -f ./.env ] && . ./.env; set +a; exec ./run.sh'
fi
wait_http http://127.0.0.1:8100/health 25 || echo "[warn] zepshim 未在 25s 内就绪，看 $LOGS/zepshim.log"

# 2) 后端 API
if port_busy 5001; then
  echo "[skip] backend 已在 5001 运行"
else
  "$PY" "$DAEMON" --log "$LOGS/backend.log" --cwd "$ROOT/backend" -- \
    "$ROOT/backend/.venv/bin/python" run.py
fi
wait_http http://127.0.0.1:5001/health 40 || echo "[warn] backend 未在 40s 内就绪，看 $LOGS/backend.log"

# 3) 前端 UI
if port_busy 3000; then
  echo "[skip] frontend 已在 3000 运行"
else
  "$PY" "$DAEMON" --log "$LOGS/frontend.log" --cwd "$ROOT/frontend" -- \
    "$(command -v npm)" run dev
fi
wait_http http://127.0.0.1:3000/ 40 || echo "[warn] frontend 未在 40s 内就绪，看 $LOGS/frontend.log"

echo
"$ROOT/bin/status.sh"
