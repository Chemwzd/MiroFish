"""给 MiroFish 本地部署打两个诊断/修复补丁（幂等）。

1) backend/scripts/run_twitter_simulation.py
   - 每轮记录 [round] 与 [select] 到 <sim_dir>/round_trace.log，
     用于查清"为什么某些轮次没有 Agent 动作"。
2) bin/e2e_sim.py
   - 轮次跑完后 OASIS 子进程会进入"等待命令"模式而不退出，
     run-status 永远停在 running。改为读子进程自报的 env_status.json。
   - 报告生成完再主动 stop。
"""
import pathlib

ROOT = pathlib.Path("/Users/youzi/Desktop/wzd/Usage/MiroFish")
SIM = ROOT / "backend/scripts/run_twitter_simulation.py"
E2E = ROOT / "bin/e2e_sim.py"

# ---------- 1) 子脚本：加诊断追踪 ----------
t = SIM.read_text()

sel_old = "        # 随机选择\n"
sel_new = (
    "        try:\n"
    "            open(os.path.join(os.path.dirname(self.config_path), \"round_trace.log\"), \"a\", encoding=\"utf-8\").write(\n"
    "                f\"[select] hour={current_hour} target={target_count} candidates={len(candidates)} selected_pool={candidates}\\n\")\n"
    "        except Exception:\n"
    "            pass\n"
    "        # 随机选择\n"
)
assert t.count(sel_old) == 1, f"select anchor x{t.count(sel_old)}"
t = t.replace(sel_old, sel_new)

loop_old = "            if not active_agents:\n                continue\n"
loop_new = (
    "            try:\n"
    "                open(os.path.join(os.path.dirname(self.config_path), \"round_trace.log\"), \"a\", encoding=\"utf-8\").write(\n"
    "                    f\"[round] n={round_num} hour={simulated_hour} active={len(active_agents)} ids={[a[0] for a in active_agents]}\\n\")\n"
    "            except Exception:\n"
    "                pass\n"
    "            if not active_agents:\n"
    "                continue\n"
)
assert t.count(loop_old) == 1, f"loop anchor x{t.count(loop_old)}"
t = t.replace(loop_old, loop_new)
SIM.write_text(t)
print("run_twitter_simulation.py: 已加入每轮诊断追踪")

# ---------- 2) E2E 客户端：完成判定 + 收尾 stop ----------
e = E2E.read_text()
old_block_start = '    # 注意：轮次跑完后，OASIS 子进程会进入"等待命令"模式而不退出（后端未传 --no-wait），'
old_block_end = '        check("simulation/stop", code == 200, f"HTTP {code} {str(payload)[:150]}")\n'
i = e.find(old_block_start)
j = e.find(old_block_end)
assert i != -1 and j != -1 and j > i, "e2e block not found"
block_end = j + len(old_block_end)
new_block = (
    '    # 轮次跑完后 OASIS 子进程会进入"等待命令"模式而不退出（后端未传 --no-wait），\n'
    '    # 因此 run-status 会一直停在 running。这里直接读子进程自报的 env_status.json：\n'
    '    # status=alive 表示轮次已跑完、正在等待访谈/报告命令。\n'
    '    sim_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),\n'
    '                           "backend", "uploads", "simulations", simulation_id)\n'
    '    env_status_file = os.path.join(sim_dir, "env_status.json")\n'
    '    rounds_done = False\n'
    '    deadline = time.time() + args.run_timeout\n'
    '    while time.time() < deadline:\n'
    '        child_state = ""\n'
    '        try:\n'
    '            with open(env_status_file, encoding="utf-8") as fh:\n'
    '                child_state = str(json.load(fh).get("status") or "")\n'
    '        except Exception:\n'
    '            child_state = ""\n'
    '        code, payload = api("GET", f"/api/simulation/{simulation_id}/run-status")\n'
    '        data = (payload or {}).get("data") or {}\n'
    '        runner = str(data.get("runner_status") or "")\n'
    '        log(f"simulation: child={child_state or \'starting\'} runner={runner or \'?\'} "\n'
    '            f"round={data.get(\'current_round\')}/{data.get(\'total_rounds\')}")\n'
    '        if child_state in {"alive", "stopped", "completed", "failed"} \\\n'
    '                or runner in {"completed", "stopped", "failed", "error"}:\n'
    '            rounds_done = True\n'
    '            break\n'
    '        time.sleep(8)\n'
    '    check("simulation rounds finished", rounds_done,\n'
    '          "" if rounds_done else f"超过 {args.run_timeout}s 未跑完")\n'
)
e = e[:i] + new_block + e[block_end:]

tail_old = '    print()\n    ui = BASE.replace(":5001", ":3000")\n'
tail_new = (
    '    code, payload = api("POST", "/api/simulation/stop", {"simulation_id": simulation_id})\n'
    '    log(f"  收尾: simulation/stop HTTP {code}")\n\n'
    '    print()\n    ui = BASE.replace(":5001", ":3000")\n'
)
assert e.count(tail_old) == 1, f"tail anchor x{e.count(tail_old)}"
e = e.replace(tail_old, tail_new)
E2E.write_text(e)
print("bin/e2e_sim.py: 完成判定改为读 env_status.json，报告后自动 stop")
