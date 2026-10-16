#!/usr/bin/env python3
"""Continue the MiroFish E2E: simulation create -> prepare -> run -> report.

Reuses an existing project/graph (no rebuild), so it is cheap to re-run.

    python3 e2e_simulation.py --rounds 2 --platform twitter
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE = os.environ.get("MIROFISH_URL", "http://127.0.0.1:3000")
FAILURES: list[str] = []
OK_STATES = {"completed", "complete", "success", "succeeded", "done", "finished", "ready"}
BAD_STATES = {"failed", "error", "cancelled", "canceled", "timeout"}


def log(message: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


def check(label: str, condition: bool, detail: str = "") -> None:
    if not condition:
        FAILURES.append(label)
    print(f"{'ok  ' if condition else 'FAIL'} {label} {detail}", flush=True)


def api(method: str, path: str, body: dict | None = None, timeout: float = 300.0):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(f"{BASE}{path}", data=data, method=method)
    request.add_header("Accept", "application/json")
    if data is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode()
            return response.status, (json.loads(raw) if raw.strip() else None)
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode()[:600]
    except Exception as error:  # noqa: BLE001
        return 0, str(error)


def find_key(node, key):
    if isinstance(node, dict):
        if key in node and node[key] not in (None, "", [], {}):
            return node[key]
        for value in node.values():
            found = find_key(value, key)
            if found is not None:
                return found
    elif isinstance(node, list):
        for item in node:
            found = find_key(item, key)
            if found is not None:
                return found
    return None


def state_of(payload) -> str:
    for key in ("status", "state", "phase", "stage"):
        value = find_key(payload, key)
        if isinstance(value, str):
            return value.lower()
    return ""


def poll(label: str, call, timeout: float, interval: float = 5.0):
    started = time.time()
    last = None
    while True:
        code, payload = call()
        last = payload
        state = state_of(payload)
        if state in OK_STATES:
            log(f"{label}: {state}")
            return payload
        if state in BAD_STATES:
            raise RuntimeError(f"{label} -> {state}: {str(payload)[:400]}")
        done_flag = find_key(payload, "completed") or find_key(payload, "ready")
        if done_flag is True:
            log(f"{label}: completed flag")
            return payload
        if time.time() - started > timeout:
            raise RuntimeError(f"{label} timed out after {timeout:.0f}s; last={str(last)[:300]}")
        if int(time.time() - started) % 15 < interval:
            log(f"{label}: {state or 'running'} ({int(time.time() - started)}s)")
        time.sleep(interval)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-id", default="")
    parser.add_argument("--graph-id", default="")
    parser.add_argument("--rounds", type=int, default=12)
    parser.add_argument("--platform", default="twitter", choices=["twitter", "reddit", "parallel"])
    parser.add_argument("--prepare-timeout", type=float, default=900)
    parser.add_argument("--run-timeout", type=float, default=2400)
    parser.add_argument("--report-timeout", type=float, default=1200)
    args = parser.parse_args()

    log(f"target {BASE}")
    project_id, graph_id = args.project_id, args.graph_id
    if not project_id:
        code, listing = api("GET", "/api/graph/project/list")
        projects = find_key(listing, "projects") or listing.get("data") or []
        if isinstance(projects, dict):
            projects = projects.get("projects") or []
        if not projects:
            raise SystemExit("no project found; run e2e_mirofish.py first")
        latest = sorted(projects, key=lambda p: str(p.get("created_at") or ""))[-1]
        project_id = latest.get("project_id") or latest.get("id")
        graph_id = graph_id or latest.get("graph_id")
        log(f"reusing project={project_id} graph={graph_id}")

    log("1/4 simulation/create")
    code, payload = api("POST", "/api/simulation/create", {
        "project_id": project_id,
        "graph_id": graph_id,
        "enable_twitter": args.platform in {"twitter", "parallel"},
        "enable_reddit": args.platform in {"reddit", "parallel"},
    }, timeout=180)
    simulation_id = find_key(payload, "simulation_id")
    check("simulation/create", code == 200 and bool(simulation_id),
          f"HTTP {code} simulation_id={simulation_id}")
    if not simulation_id:
        print("     response:", str(payload)[:500])
        return finish()

    log("2/4 simulation/prepare (agent 人设 + 双平台配置，LLM 重头戏)")
    code, payload = api("POST", "/api/simulation/prepare", {
        "simulation_id": simulation_id,
        "use_llm_for_profiles": True,
        "parallel_profile_count": 4,
    }, timeout=300)
    check("simulation/prepare accepted", code == 200, f"HTTP {code} {str(payload)[:200]}")
    # prepare is asynchronous and returns a task_id immediately; the status
    # endpoint tracks THAT task, not the simulation id (polling by simulation_id
    # forever reports not_started).
    prepare_task = find_key(payload, "task_id")
    log(f"    prepare task_id={prepare_task}")
    try:
        poll("prepare", lambda: api("POST", "/api/simulation/prepare/status",
                                    {"task_id": prepare_task,
                                     "simulation_id": simulation_id}),
             args.prepare_timeout, interval=8)
    except RuntimeError as error:
        check("prepare finished", False, str(error)[:300])
        return finish()

    profiles_code, profiles = api("GET", f"/api/simulation/{simulation_id}/profiles")
    count = profiles_code == 200 and bool(profiles)
    check("agent profiles exist", count, f"HTTP {profiles_code}")

    log(f"3/4 simulation/start (platform={args.platform}, max_rounds={args.rounds})")
    code, payload = api("POST", "/api/simulation/start", {
        "simulation_id": simulation_id,
        "platform": args.platform,
        "max_rounds": args.rounds,
        "enable_graph_memory_update": True,
    }, timeout=300)
    check("simulation/start", code == 200, f"HTTP {code} {str(payload)[:200]}")
    if code != 200:
        return finish()
    # 轮次跑完后 OASIS 子进程会进入"等待命令"模式而不退出（后端未传 --no-wait），
    # 因此 run-status 会一直停在 running。这里直接读子进程自报的 env_status.json：
    # status=alive 表示轮次已跑完、正在等待访谈/报告命令。
    sim_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "backend", "uploads", "simulations", simulation_id)
    env_status_file = os.path.join(sim_dir, "env_status.json")
    rounds_done = False
    deadline = time.time() + args.run_timeout
    while time.time() < deadline:
        child_state = ""
        try:
            with open(env_status_file, encoding="utf-8") as fh:
                child_state = str(json.load(fh).get("status") or "")
        except Exception:
            child_state = ""
        code, payload = api("GET", f"/api/simulation/{simulation_id}/run-status")
        data = (payload or {}).get("data") or {}
        runner = str(data.get("runner_status") or "")
        log(f"simulation: child={child_state or 'starting'} runner={runner or '?'} "
            f"round={data.get('current_round')}/{data.get('total_rounds')}")
        if child_state in {"alive", "stopped", "completed", "failed"} \
                or runner in {"completed", "stopped", "failed", "error"}:
            rounds_done = True
            break
        time.sleep(8)
    check("simulation rounds finished", rounds_done,
          "" if rounds_done else f"超过 {args.run_timeout}s 未跑完")

    # 轮次跑完后子进程停在"等待命令"模式，run-status 不会是终态；
    # 而报告生成要求"终态 + 图谱写入落盘"，所以先主动 stop 并等待终态。
    if rounds_done:
        code, payload = api("POST", "/api/simulation/stop", {"simulation_id": simulation_id})
        check("simulation/stop", code == 200, f"HTTP {code} {str(payload)[:150]}")
        terminal = False
        runner = ""
        deadline_stop = time.time() + 180
        while time.time() < deadline_stop:
            code, payload = api("GET", f"/api/simulation/{simulation_id}/run-status")
            runner = str(((payload or {}).get("data") or {}).get("runner_status") or "")
            if runner in {"stopped", "completed", "failed", "error"}:
                terminal = True
                break
            time.sleep(5)
        check("simulation terminal", terminal, f"runner_status={runner}")

    actions_code, actions = api("GET", f"/api/simulation/{simulation_id}/actions?limit=5")
    posts_code, posts = api("GET", f"/api/simulation/{simulation_id}/posts?limit=5")
    check("simulation produced actions/posts",
          actions_code == 200 or posts_code == 200,
          f"actions HTTP {actions_code}, posts HTTP {posts_code}")

    log("4/4 report/generate")
    report_id = None
    code, payload = 0, None
    deadline_report = time.time() + 1800
    while time.time() < deadline_report:
        code, payload = api("POST", "/api/report/generate", {"simulation_id": simulation_id}, timeout=300)
        report_id = find_key(payload, "report_id")
        if code == 200 and report_id:
            break
        if code == 409:
            time.sleep(6)
            continue
        break
    check("report/generate", code == 200 and bool(report_id), f"HTTP {code} report_id={report_id}")
    if not report_id:
        print("     response:", str(payload)[:400])
        return finish()

    report_task = find_key(payload, "task_id")
    try:
        poll("report", lambda: api("POST", "/api/report/generate/status",
                                   {"task_id": report_task, "simulation_id": simulation_id}),
             args.report_timeout, interval=5)
    except RuntimeError as error:
        check("report finished", False, str(error)[:300])
        return finish()

    code, report = api("GET", f"/api/report/{report_id}")
    text = json.dumps(report, ensure_ascii=False) if report else ""
    check("report has content", code == 200 and len(text) > 500, f"HTTP {code} chars={len(text)}")
    if report:
        print("     report keys:", list(report)[:12])
        print("     preview:", text[:300].replace("\\n", " "))

    print()
    ui = BASE.replace(":5001", ":3000")
    log("SIMULATION E2E " + ("PASS" if not FAILURES else "FAIL " + ", ".join(FAILURES)))
    print(f"  模拟: {ui}/simulation/{simulation_id}")
    print(f"  报告: {ui}/report/{report_id}")
    print(f"  互动: {ui}/interaction/{report_id}")
    return finish()


def finish() -> int:
    if FAILURES:
        print("E2E FAIL: " + ", ".join(FAILURES), flush=True)
        return 1
    print("E2E PASS", flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as error:
        log(f"E2E FAIL: {error}")
        raise SystemExit(1)
