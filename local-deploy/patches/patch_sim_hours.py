#!/usr/bin/env python3
"""修复 MiroFish 模拟"空跑"：让模拟从活跃时段开始。

问题：模拟固定从模拟时间 00:00 起跑（simulated_hour = round_num * minutes_per_round // 60 % 24），
而 time_config 的 off_peak_hours=[0..5]、off_peak_activity_multiplier=0.05，
且每个 Agent 的 active_hours 默认是 8-23。于是短轮次运行整段落在死区：
每轮候选 Agent 数为 0 → 该轮被 continue 跳过 → 模拟 0.0 秒"跑完"、0 个动作、
current_round 始终为 0。

修法：time_config 增加 start_hour（默认 20，即晚间高峰），三个 runner 用它做时间偏移。
"""
from __future__ import annotations

import pathlib

ROOT = pathlib.Path('/Users/youzi/Desktop/wzd/Usage/MiroFish')


def patch(path: pathlib.Path, pairs: list[tuple[str, str]]) -> None:
    text = path.read_text()
    hits = 0
    for old, new in pairs:
        n = text.count(old)
        if n == 0:
            print(f'  [skip] {path.name}: 目标片段未找到（可能已打过补丁）')
            continue
        text = text.replace(old, new)
        hits += n
    if hits:
        path.write_text(text)
    print(f'  [ok]   {path.name}: 替换 {hits} 处')


# --- A. 配置生成器：TimeSimulationConfig 新增 start_hour ---
gen = ROOT / 'backend/app/services/simulation_config_generator.py'
patch(gen, [
    (
        "    # 每轮代表的时间（模拟分钟）- 默认60分钟（1小时），加快时间流速\n"
        "    minutes_per_round: int = 60\n",
        "    # 每轮代表的时间（模拟分钟）- 默认60分钟（1小时），加快时间流速\n"
        "    minutes_per_round: int = 60\n"
        "\n"
        "    # 模拟起始时刻（0-23）。默认 20 点（晚间高峰）：若从 00:00 起跑，\n"
        "    # 短轮次运行会整段落在 off_peak_hours(0-5) 与 Agent active_hours(默认8-23)\n"
        "    # 之外，每轮激活 0 个 Agent，导致模拟空跑。\n"
        "    start_hour: int = 20\n",
    ),
    (
        '            minutes_per_round=result.get("minutes_per_round", 60),  # 默认每轮1小时\n',
        '            minutes_per_round=result.get("minutes_per_round", 60),  # 默认每轮1小时\n'
        '            start_hour=int(result.get("start_hour", 20)) % 24,\n',
    ),
])

# --- B. 三个 runner：按 start_hour 做偏移 ---
run = ROOT / 'backend/scripts'
twitter_repl = (
    "            simulated_minutes = round_num * minutes_per_round\n"
    "            simulated_hour = (simulated_minutes // 60) % 24\n",
    "            simulated_minutes = round_num * minutes_per_round\n"
    "            start_hour = int(self.config.get(\"time_config\", {}).get(\"start_hour\", 0)) % 24\n"
    "            simulated_hour = (start_hour + simulated_minutes // 60) % 24\n",
)
parallel_repl = (
    "        simulated_minutes = round_num * minutes_per_round\n"
    "        simulated_hour = (simulated_minutes // 60) % 24\n",
    "        simulated_minutes = round_num * minutes_per_round\n"
    "        start_hour = int((config.get(\"time_config\", {}) or {}).get(\"start_hour\", 0)) % 24\n"
    "        simulated_hour = (start_hour + simulated_minutes // 60) % 24\n",
)
patch(run / 'run_twitter_simulation.py', [twitter_repl])
patch(run / 'run_reddit_simulation.py', [twitter_repl])
patch(run / 'run_parallel_simulation.py', [parallel_repl])  # 两处循环

# --- C. E2E：报告阶段等待更长（图谱记忆写入慢，会返回 409） ---
e2e = ROOT / 'bin/e2e_sim.py'
patch(e2e, [("deadline_report = time.time() + 300", "deadline_report = time.time() + 1800")])

print('patch 完成')
