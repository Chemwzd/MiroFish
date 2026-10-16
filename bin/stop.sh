#!/bin/sh
# 停止 MiroFish 本地栈（按端口精确杀，不动别的服务）
ROOT=$(cd "$(dirname "$0")/.." && pwd)

for spec in "3000:前端 UI" "5001:后端 API" "8100:本地图谱 zepshim"; do
  port=${spec%%:*}
  name=${spec#*:}
  pids=$(lsof -ti tcp:"$port" 2>/dev/null | tr '\n' ' ' | sed 's/ *$//')
  if [ -n "$pids" ]; then
    echo "[stop] $name (:$port) pid=$pids"
    kill $pids 2>/dev/null || true
  else
    echo "[skip] $name (:$port) 未运行"
  fi
done

# 有界等待端口释放（最多 10s）
i=0
while [ "$i" -lt 20 ]; do
  busy=""
  for p in 3000 5001 8100; do
    lsof -ti tcp:"$p" >/dev/null 2>&1 && busy="$busy $p"
  done
  [ -z "$busy" ] && break
  i=$((i + 1)); sleep 0.5
done
[ -n "$busy" ] && echo "[warn] 仍被占用:$busy（可再执行一次或用 kill -9）"
echo "[done] 停栈完成"
