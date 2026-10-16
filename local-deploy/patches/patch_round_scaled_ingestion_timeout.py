#!/usr/bin/env python3
"""把"图谱收尾等待上限"从固定 600s 改成随模拟轮数放大。

背景：MiroFish 收尾时要等 zepshim 把本次模拟产生的全部活动 episode 抽完；
固定 600s 只够短模拟，50 轮会产生几百条批次，必然超时并被误判为 failed
（failed 又不算"成功终态"，报告闸门会拒）。

公式（可用环境变量覆盖）：
    timeout = clamp( max(600s, 轮数 × 90s), ≤ 14400s )
    ZEP_INGESTION_WAIT_BASE_SECONDS      默认 600
    ZEP_INGESTION_WAIT_PER_ROUND_SECONDS 默认 90
    ZEP_INGESTION_WAIT_MAX_SECONDS       默认 14400（4 小时）

生效点：updater.stop()、_wait_for_pending_episodes()、runner 里等待监控线程的 join。
"""
from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path('/Users/youzi/Desktop/wzd/Usage/MiroFish/backend/app')
ZEP_UTILS = ROOT / 'utils' / 'zep.py'
UPDATER = ROOT / 'services' / 'zep_graph_memory_updater.py'
RUNNER = ROOT / 'services' / 'simulation_runner.py'

EDITS: dict[pathlib.Path, list[tuple[str, str]]] = {
    ZEP_UTILS: [
        (
            "ZEP_INGESTION_WAIT_TIMEOUT_SECONDS = 600\nMAX_ZEP_SEARCH_QUERY_CHARS = 400\n",
            "ZEP_INGESTION_WAIT_TIMEOUT_SECONDS = 600\n"
            "# Patched: 收尾（图谱写入）等待上限随模拟轮数放大——轮数越多，Agent 活动批次越多，\n"
            "# 本地 zepshim 逐条抽取所需时间越长；固定 600s 会让长模拟必然超时并被误判失败。\n"
            "# 可用环境变量覆盖：ZEP_INGESTION_WAIT_BASE_SECONDS / _PER_ROUND_SECONDS / _MAX_SECONDS\n"
            "ZEP_INGESTION_WAIT_BASE_SECONDS = float(os.environ.get(\"ZEP_INGESTION_WAIT_BASE_SECONDS\", \"600\"))\n"
            "ZEP_INGESTION_WAIT_PER_ROUND_SECONDS = float(os.environ.get(\"ZEP_INGESTION_WAIT_PER_ROUND_SECONDS\", \"90\"))\n"
            "ZEP_INGESTION_WAIT_MAX_SECONDS = float(os.environ.get(\"ZEP_INGESTION_WAIT_MAX_SECONDS\", \"14400\"))\n"
            "MAX_ZEP_SEARCH_QUERY_CHARS = 400\n"
            "\n"
            "\n"
            "def ingestion_wait_timeout_seconds(rounds: Any = None) -> float:\n"
            "    \"\"\"收尾等待上限 = max(保底, 轮数 × 每轮预算)，并受总上限约束。\n"
            "\n"
            "    Args:\n"
            "        rounds: 本次模拟的总轮数；未知时按保底值处理。\n"
            "    \"\"\"\n"
            "    budget = ZEP_INGESTION_WAIT_BASE_SECONDS\n"
            "    try:\n"
            "        count = int(rounds) if rounds is not None else 0\n"
            "    except (TypeError, ValueError):\n"
            "        count = 0\n"
            "    if count > 0:\n"
            "        budget = max(budget, count * ZEP_INGESTION_WAIT_PER_ROUND_SECONDS)\n"
            "    return min(budget, ZEP_INGESTION_WAIT_MAX_SECONDS)\n",
        ),
    ],
    UPDATER: [
        (
            "from ..utils.zep import (\n"
            "    ZEP_INGESTION_WAIT_TIMEOUT_SECONDS,\n"
            "    call_zep_read_with_retry,\n"
            "    get_zep_client,\n"
            ")\n",
            "from ..utils.zep import (\n"
            "    ZEP_INGESTION_WAIT_TIMEOUT_SECONDS,\n"
            "    call_zep_read_with_retry,\n"
            "    get_zep_client,\n"
            "    ingestion_wait_timeout_seconds,\n"
            ")\n"
            "\n"
            "\n"
            "def _simulation_rounds(simulation_id: Optional[str]) -> Optional[int]:\n"
            "    \"\"\"取本次模拟的总轮数（用于放大收尾等待上限）；拿不到就返回 None。\"\"\"\n"
            "    if not simulation_id or simulation_id == \"unknown\":\n"
            "        return None\n"
            "    try:\n"
            "        # 延迟导入，避免与 simulation_runner 形成循环依赖\n"
            "        from .simulation_runner import SimulationRunner\n"
            "\n"
            "        state = SimulationRunner.get_run_state(simulation_id)\n"
            "        return getattr(state, \"total_rounds\", None) if state else None\n"
            "    except Exception:  # noqa: BLE001\n"
            "        return None\n",
        ),
        (
            "    def stop(self):\n"
            "        \"\"\"Drain the worker, flush tail events, and wait for Cloud ingestion.\"\"\"\n"
            "        deadline = time.time() + ZEP_INGESTION_WAIT_TIMEOUT_SECONDS\n",
            "    def stop(self):\n"
            "        \"\"\"Drain the worker, flush tail events, and wait for Cloud ingestion.\"\"\"\n"
            "        rounds = _simulation_rounds(self.simulation_id)\n"
            "        wait_seconds = ingestion_wait_timeout_seconds(rounds)\n"
            "        logger.info(\n"
            "            f\"图谱收尾等待上限 {wait_seconds:.0f}s\"\n"
            "            f\"（simulation={self.simulation_id}, rounds={rounds}）\"\n"
            "        )\n"
            "        deadline = time.time() + wait_seconds\n",
        ),
        (
            "        if deadline is None:\n"
            "            deadline = time.time() + ZEP_INGESTION_WAIT_TIMEOUT_SECONDS\n",
            "        if deadline is None:\n"
            "            deadline = time.time() + ingestion_wait_timeout_seconds(\n"
            "                _simulation_rounds(self.simulation_id)\n"
            "            )\n",
        ),
    ],
    RUNNER: [
        (
            "    ZEP_HTTP_REQUEST_TIMEOUT_SECONDS,\n"
            "    ZEP_INGESTION_WAIT_TIMEOUT_SECONDS,\n",
            "    ZEP_HTTP_REQUEST_TIMEOUT_SECONDS,\n"
            "    ZEP_INGESTION_WAIT_TIMEOUT_SECONDS,\n"
            "    ingestion_wait_timeout_seconds,\n",
        ),
        (
            "            wait_timeout = max(\n"
            "                30.0,\n"
            "                ZEP_INGESTION_WAIT_TIMEOUT_SECONDS\n"
            "                + ZEP_HTTP_REQUEST_TIMEOUT_SECONDS\n"
            "                + 5,\n"
            "            )\n",
            "            wait_timeout = max(\n"
            "                30.0,\n"
            "                ingestion_wait_timeout_seconds(getattr(state, \"total_rounds\", None))\n"
            "                + ZEP_HTTP_REQUEST_TIMEOUT_SECONDS\n"
            "                + 5,\n"
            "            )\n",
        ),
    ],
}


def main() -> int:
    for path, edits in EDITS.items():
        if not path.exists():
            print(f'[跳过] 不存在 {path}')
            return 1
        src = path.read_text()
        if 'ingestion_wait_timeout_seconds' in src:
            print(f'[已打过] {path.name}')
            continue
        for old, new in edits:
            if src.count(old) != 1:
                print(f'[失败] {path.name} 锚点命中 {src.count(old)} 次（应为 1）:\n{old[:70]!r}')
                return 1
            src = src.replace(old, new, 1)
        backup = path.with_suffix(path.suffix + '.bak-roundtimeout')
        if not backup.exists():
            backup.write_text(path.read_text())
        path.write_text(src)
        print(f'[已改写] {path.name}（备份 {backup.name}）')
    return 0


if __name__ == '__main__':
    sys.exit(main())
