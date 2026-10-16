#!/bin/sh
# MiroFish 本地栈健康检查
ROOT=$(cd "$(dirname "$0")/.." && pwd)
echo "MiroFish @ $ROOT"
echo "-----------------------------------------------"

check() { # check <端口> <名称> <路径>
  code=$(curl -s -o /dev/null -w '%{http_code}' -m 3 "http://127.0.0.1:$1$3" 2>/dev/null)
  pid=$(lsof -ti tcp:"$1" 2>/dev/null | head -1)
  printf "  %-20s :%-5s http=%-4s pid=%s\n" "$2" "$1" "${code:-000}" "${pid:-none}"
}

check 3000 "前端 UI" "/"
check 5001 "后端 API" "/health"
check 8100 "本地图谱 zepshim" "/health"

printf "  %-20s " "图谱库统计"
curl -s -m 3 http://127.0.0.1:8100/stats || echo "(不可达)"
echo

# LiteLLM 网关：带上 master key 探测，避免"401 看起来像故障"
printf "  %-20s " "LLM 网关(:4000)"
GW_ENV="$HOME/.hermes/workspace/litellm-gateway/.env"
GW_KEY=""
[ -f "$GW_ENV" ] && GW_KEY=$(grep '^LITELLM_MASTER_KEY=' "$GW_ENV" | cut -d= -f2-)
MODELS_JSON="$ROOT/var/.models.json"
if [ -n "$GW_KEY" ]; then
  code=$(curl -s -o "$MODELS_JSON" -w '%{http_code}' -m 5 \
         -H "Authorization: Bearer $GW_KEY" http://127.0.0.1:4000/v1/models 2>/dev/null)
  n=$(grep -o '"id"' "$MODELS_JSON" 2>/dev/null | wc -l | tr -d ' ')
  echo "http=${code:-000}（带鉴权，可用模型 ${n:-?} 个）"
else
  echo "http=$(curl -s -o /dev/null -w '%{http_code}' -m 3 http://127.0.0.1:4000/v1/models 2>/dev/null)（未找到 master key，401 属正常）"
fi
rm -f "$MODELS_JSON"

echo "-----------------------------------------------"
echo "判定：3000/5001/8100 均为 200 且 extraction_failures 不增长 = 正常"
echo "日志目录: $ROOT/var/logs/"
