#!/bin/sh
# Start zepshim in the foreground.
#
#   ZEPSHIM_LLM_BASE_URL=http://127.0.0.1:4000/v1 \
#   ZEPSHIM_LLM_API_KEY=... ZEPSHIM_LLM_MODEL=gpt-6-astra ./run.sh
#
# When ZEPSHIM_LLM_API_KEY is unset the script tries to reuse the local
# LiteLLM master key from ~/.hermes/workspace/litellm-gateway/.env.
set -e

HERE=$(cd "$(dirname "$0")" && pwd)

if [ -z "${ZEPSHIM_LLM_API_KEY:-}" ] && [ -f "$HOME/.hermes/workspace/litellm-gateway/.env" ]; then
    ZEPSHIM_LLM_API_KEY=$(grep '^LITELLM_MASTER_KEY=' "$HOME/.hermes/workspace/litellm-gateway/.env" | cut -d= -f2-)
    export ZEPSHIM_LLM_API_KEY
fi

: "${ZEPSHIM_LLM_BASE_URL:=http://127.0.0.1:4000/v1}"
: "${ZEPSHIM_LLM_MODEL:=gpt-6-astra}"
: "${ZEPSHIM_DB:=$HERE/data/zepshim.sqlite3}"
: "${ZEPSHIM_PORT:=8100}"
export ZEPSHIM_LLM_BASE_URL ZEPSHIM_LLM_MODEL ZEPSHIM_DB ZEPSHIM_PORT

if [ -x "$HERE/.venv/bin/python" ]; then
    PYTHON="$HERE/.venv/bin/python"
else
    PYTHON=$(command -v python3)
fi

exec "$PYTHON" "$HERE/app.py"
