"""e2e_sim.py 补丁 v2：修"报告要求终态"的顺序问题。

- 轮次跑完（子进程进入等待命令模式）后：先 /api/simulation/stop 并等到终态
- 再调 /api/report/generate；若返回 409（图谱写入未落盘）则重试
"""
import pathlib

E2E = pathlib.Path("/Users/youzi/Desktop/wzd/Usage/MiroFish/bin/e2e_sim.py")
e = E2E.read_text()

# 1) 轮次跑完后先停模拟并等终态
anchor1 = (
    '    check("simulation rounds finished", rounds_done,\n'
    '          "" if rounds_done else f"超过 {args.run_timeout}s 未跑完")\n'
)
add1 = anchor1 + (
    '\n'
    '    # 轮次跑完后子进程停在"等待命令"模式，run-status 不会是终态；\n'
    '    # 而报告生成要求"终态 + 图谱写入落盘"，所以先主动 stop 并等待终态。\n'
    '    if rounds_done:\n'
    '        code, payload = api("POST", "/api/simulation/stop", {"simulation_id": simulation_id})\n'
    '        check("simulation/stop", code == 200, f"HTTP {code} {str(payload)[:150]}")\n'
    '        terminal = False\n'
    '        runner = ""\n'
    '        deadline_stop = time.time() + 180\n'
    '        while time.time() < deadline_stop:\n'
    '            code, payload = api("GET", f"/api/simulation/{simulation_id}/run-status")\n'
    '            runner = str(((payload or {}).get("data") or {}).get("runner_status") or "")\n'
    '            if runner in {"stopped", "completed", "failed", "error"}:\n'
    '                terminal = True\n'
    '                break\n'
    '            time.sleep(5)\n'
    '        check("simulation terminal", terminal, f"runner_status={runner}")\n'
)
assert e.count(anchor1) == 1, f"anchor1 x{e.count(anchor1)}"
e = e.replace(anchor1, add1)

# 2) 报告调用加 409 重试
anchor2 = (
    '    log("4/4 report/generate")\n'
    '    code, payload = api("POST", "/api/report/generate", {"simulation_id": simulation_id}, timeout=300)\n'
    '    report_id = find_key(payload, "report_id")\n'
    '    check("report/generate", code == 200 and bool(report_id), f"HTTP {code} report_id={report_id}")\n'
)
add2 = (
    '    log("4/4 report/generate")\n'
    '    report_id = None\n'
    '    code, payload = 0, None\n'
    '    deadline_report = time.time() + 300\n'
    '    while time.time() < deadline_report:\n'
    '        code, payload = api("POST", "/api/report/generate", {"simulation_id": simulation_id}, timeout=300)\n'
    '        report_id = find_key(payload, "report_id")\n'
    '        if code == 200 and report_id:\n'
    '            break\n'
    '        if code == 409:\n'
    '            time.sleep(6)\n'
    '            continue\n'
    '        break\n'
    '    check("report/generate", code == 200 and bool(report_id), f"HTTP {code} report_id={report_id}")\n'
)
assert e.count(anchor2) == 1, f"anchor2 x{e.count(anchor2)}"
e = e.replace(anchor2, add2)

# 3) 去掉之前在末尾插入的收尾 stop
anchor3 = (
    '    code, payload = api("POST", "/api/simulation/stop", {"simulation_id": simulation_id})\n'
    '    log(f"  收尾: simulation/stop HTTP {code}")\n'
    '\n'
)
assert e.count(anchor3) == 1, f"anchor3 x{e.count(anchor3)}"
e = e.replace(anchor3, "")

E2E.write_text(e)
print("e2e_sim.py 已更新：轮次跑完 → stop → 等终态 → 生成报告（409 重试）")
