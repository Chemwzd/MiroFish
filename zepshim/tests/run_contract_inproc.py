"""Run the shim contract test against an in-process uvicorn server.

Design goals:
  * no background/daemonised processes,
  * no sleep-and-hope orchestration,
  * one command in, one exit code out.

The uvicorn server lives in a daemon thread of this very process, so when the
contract test finishes the process ends and nothing is left listening.
"""

from __future__ import annotations

import os
import pathlib
import sys
import threading
import time

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))

# --- shim configuration -------------------------------------------------------
# Must be set before importing app.py: it reads the environment at import time.
os.environ.setdefault("ZEPSHIM_HOST", "127.0.0.1")
os.environ.setdefault("ZEPSHIM_PORT", "8123")
os.environ.setdefault("ZEPSHIM_DB", str(ROOT / "data" / "zepshim_selftest.sqlite3"))
os.environ.setdefault("ZEPSHIM_LOG_LEVEL", "WARNING")
os.environ.setdefault("ZEPSHIM_LLM_BASE_URL", "http://127.0.0.1:4000/v1")
os.environ.setdefault("ZEPSHIM_LLM_MODEL", "glm-5.3-flashx")

if not os.environ.get("ZEPSHIM_LLM_API_KEY"):
    env_file = pathlib.Path.home() / ".hermes" / "workspace" / "litellm-gateway" / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            if line.startswith("LITELLM_MASTER_KEY="):
                os.environ["ZEPSHIM_LLM_API_KEY"] = line.split("=", 1)[1].strip()
                break

PORT = int(os.environ["ZEPSHIM_PORT"])

import httpx  # noqa: E402
import uvicorn  # noqa: E402

import app as shim  # noqa: E402

server = uvicorn.Server(
    uvicorn.Config(shim.app, host="127.0.0.1", port=PORT, log_level="warning")
)
threading.Thread(target=server.run, daemon=True).start()

deadline = time.time() + 30
while time.time() < deadline:
    try:
        if httpx.get(f"http://127.0.0.1:{PORT}/health", timeout=1.0).status_code == 200:
            break
    except Exception:  # noqa: BLE001 - server not up yet
        time.sleep(0.2)
else:
    print("FAIL: in-process shim never became healthy")
    raise SystemExit(1)

os.environ["ZEPSHIM_BASE_URL"] = f"http://127.0.0.1:{PORT}/api/v2"

import test_contract  # noqa: E402

test_contract.main()
