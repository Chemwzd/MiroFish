#!/usr/bin/env python3
"""方案 A 补丁：跑满轮次后自动结束模拟进程并发布 COMPLETED。

问题：后端启动模拟子进程时没有传 --no-wait，OASIS 跑完最后一轮会停在
"等待指令"模式而不退出；simulation_runner._monitor_simulation 的
`while process.poll() is None:` 便永远等不到终态，于是
  - run_status.runner_status 永远是 running
  - 前端 phase 到不了 2 → "开始生成报告"一直灰
  - POST /api/report/generate 一直 409 ingestion_pending

补丁：监控线程检测到所有平台都写出了 simulation_end
（state.twitter_completed / reddit_completed）后，主动优雅结束子进程，
从而让既有的收尾逻辑正常发布 COMPLETED；并把 SIGTERM 造成的非零退出码
排除在 FAILED 判定之外（否则会被误判为失败）。

代价（用户已知并接受）：跑完后环境即关闭，"采访智能体"不可再用。
"""
from __future__ import annotations

import pathlib
import sys

TARGETS = [
    pathlib.Path('/Users/youzi/Desktop/wzd/Usage/MiroFish/backend/app/services/simulation_runner.py'),
]

P1_OLD = """        monitor_error: Exception | None = None
        exit_code: int | None = None
        try:
            while process.poll() is None:  # 进程仍在运行
"""
P1_NEW = """        monitor_error: Exception | None = None
        exit_code: int | None = None
        auto_completed = False  # 所有平台跑满后由监控线程主动收尾（方案 A）
        try:
            while process.poll() is None:  # 进程仍在运行
"""

P2_OLD = """                # 更新状态
                cls._save_run_state(state)
                time.sleep(2)
"""
P2_NEW = """                # 更新状态
                cls._save_run_state(state)

                # 方案 A：所有平台都已写出 simulation_end。子进程此时会停在
                # "等待指令"模式而不退出（后端未传 --no-wait），监控线程将永远
                # 等不到进程结束、状态也就永远停在 running。这里主动优雅结束它，
                # 让下方收尾逻辑正常发布 COMPLETED。
                if not auto_completed and cls._check_all_platforms_completed(state):
                    logger.info(f"全部平台已跑完，自动结束模拟进程: {simulation_id}")
                    auto_completed = True
                    cls._terminate_process(process, simulation_id)
                    break

                time.sleep(2)
"""

P3_OLD = """                    elif not manual_stop and exit_code != 0:
"""
P3_NEW = """                    elif not manual_stop and exit_code != 0 and not auto_completed:
"""


def main() -> int:
    for path in TARGETS:
        if not path.exists():
            print(f"[跳过] 不存在: {path}")
            continue
        src = path.read_text()
        if '方案 A' in src:
            print(f"[已打过] {path.name}")
            continue
        edits = [(P1_OLD, P1_NEW), (P2_OLD, P2_NEW), (P3_OLD, P3_NEW)]
        for old, new in edits:
            n = src.count(old)
            if n != 1:
                print(f"[失败] 锚点出现 {n} 次（应为 1）:\n{old[:80]!r}")
                return 1
            src = src.replace(old, new, 1)
        backup = path.with_suffix(path.suffix + '.bak-planA')
        if not backup.exists():
            backup.write_text(path.read_text())
        path.write_text(src)
        print(f"[已改写] {path}\n   备份: {backup.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
