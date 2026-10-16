#!/usr/bin/env python3
"""MiroFish 历史清理工具（项目 / 模拟 / 报告 / 图谱）。

背景：MiroFish 前端没有删除按钮；后端只有
  DELETE /api/graph/project/<project_id>
  DELETE /api/graph/delete/<graph_id>
  DELETE /api/report/<report_id>
三个接口，而且没有删除"模拟(simulation)"的接口——模拟只能删目录。

用法（默认只列出，不删任何东西）：
  python3 bin/cleanup_history.py                    # 列出全部历史
  python3 bin/cleanup_history.py --keep-latest 1    # 预览：只留最新 1 个项目
  python3 bin/cleanup_history.py --keep-latest 1 --yes   # 真正执行
  python3 bin/cleanup_history.py --keep proj_xxx sim_yyy --yes   # 精确保留

安全规则：正在运行/暂停/准备中的模拟及其项目、图谱，一律跳过不删。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
UPLOADS = ROOT / "backend" / "uploads"
PROJECTS = UPLOADS / "projects"
SIMS = UPLOADS / "simulations"
REPORTS = UPLOADS / "reports"
GRAPH_DB = ROOT / "var" / "graph" / "zepshim.sqlite3"
BACKEND = os.environ.get("MIROFISH_BACKEND", "http://127.0.0.1:5001")
SHIM = os.environ.get("MIROFISH_SHIM", "http://127.0.0.1:8100")

ACTIVE_STATUS = {"starting", "running", "paused", "stopping", "preparing"}


def _shim_key() -> str:
    env = ROOT / "zepshim" / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            if line.strip().startswith("ZEPSHIM_API_KEY="):
                return line.split("=", 1)[1].strip()
    return ""


def _http(method: str, url: str, key: str = "", body: dict | None = None) -> tuple[int, str]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if key:
        req.add_header("Authorization", f"Api-Key {key}")
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, resp.read().decode(errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")
    except Exception as e:  # noqa: BLE001
        return 0, str(e)


def _ts(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime).strftime("%m-%d %H:%M")


def _du(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def load_projects() -> list[dict]:
    out = []
    if not PROJECTS.exists():
        return out
    for d in sorted(PROJECTS.iterdir(), key=lambda p: p.stat().st_mtime):
        meta = {}
        j = d / "project.json"
        if j.exists():
            try:
                meta = json.loads(j.read_text())
            except Exception:  # noqa: BLE001
                meta = {}
        out.append(
            {
                "id": d.name,
                "dir": d,
                "name": meta.get("name") or "",
                "status": meta.get("status") or "",
                "graph_id": meta.get("graph_id"),
                "req": (meta.get("simulation_requirement") or "").replace("\n", " ")[:40],
                "files": [(f.get("original_filename") or f.get("filename") or "") for f in (meta.get("files") or [])],
                "mtime": d.stat().st_mtime,
                "size": _du(d),
            }
        )
    return out


def load_sims() -> list[dict]:
    out = []
    if not SIMS.exists():
        return out
    for d in sorted(SIMS.iterdir(), key=lambda p: p.stat().st_mtime):
        cfg_p, st_p = d / "simulation_config.json", d / "state.json"
        cfg = json.loads(cfg_p.read_text()) if cfg_p.exists() else {}
        st = json.loads(st_p.read_text()) if st_p.exists() else {}
        status = str(st.get("status") or st.get("runner_status") or "").lower()
        out.append(
            {
                "id": d.name,
                "dir": d,
                "project_id": cfg.get("project_id"),
                "hours": (cfg.get("time_config") or {}).get("total_simulation_hours"),
                "agents": len(cfg.get("agent_configs") or []),
                "status": status,
                "mtime": d.stat().st_mtime,
                "size": _du(d),
            }
        )
    return out


def load_reports() -> list[dict]:
    out = []
    if not REPORTS.exists():
        return out
    for d in sorted(REPORTS.iterdir(), key=lambda p: p.stat().st_mtime):
        meta = {}
        m = d / "meta.json"
        if m.exists():
            try:
                meta = json.loads(m.read_text())
            except Exception:  # noqa: BLE001
                meta = {}
        out.append(
            {
                "id": d.name,
                "dir": d,
                "simulation_id": meta.get("simulation_id") or meta.get("sim_id"),
                "project_id": meta.get("project_id"),
                "mtime": d.stat().st_mtime,
                "size": _du(d),
            }
        )
    return out


def graph_rows() -> list[tuple[str, str, int, int, int]]:
    if not GRAPH_DB.exists():
        return []
    con = sqlite3.connect(f"file:{GRAPH_DB}?mode=ro", uri=True)
    try:
        rows = con.execute("select graph_id, name, created_at from graphs order by created_at").fetchall()
        out = []
        for gid, name, created in rows:
            n = con.execute("select count(*) from nodes where graph_id=?", (gid,)).fetchone()[0]
            e = con.execute("select count(*) from edges where graph_id=?", (gid,)).fetchone()[0]
            ep = con.execute("select count(*) from episodes where graph_id=?", (gid,)).fetchone()[0]
            out.append((gid, f"{name or ''} | {str(created)[:19]}", n, e, ep))
        return out
    finally:
        con.close()


def mb(n: int) -> str:
    return f"{n / 1e6:.1f}MB" if n >= 1e6 else f"{n / 1e3:.0f}KB"


def main() -> int:
    ap = argparse.ArgumentParser(description="MiroFish 历史清理")
    ap.add_argument("--keep", nargs="*", default=None, help="要保留的 id（项目/模拟/报告/图谱）")
    ap.add_argument("--keep-latest", type=int, default=None, help="保留最新 N 个项目（连同其模拟/报告/图谱）")
    ap.add_argument("--only-tests", action="store_true", help="只清理名字带 e2e-/审核- 的测试项目及其产物")
    ap.add_argument("--yes", action="store_true", help="真正执行删除（默认只预览）")
    args = ap.parse_args()

    projects, sims, reports, graphs = load_projects(), load_sims(), load_reports(), graph_rows()
    keep: set[str] = set(args.keep or [])
    if args.keep is None and args.keep_latest is None and not args.only_tests:
        args.keep_latest = 1  # 默认：保留最新 1 个项目，其余按历史处理

    if args.only_tests:
        for p in projects:
            if not (p["name"].startswith("e2e-") or p["name"].startswith("审核-")):
                keep.add(p["id"])
    elif args.keep_latest is not None:
        for p in projects[-args.keep_latest:] if args.keep_latest > 0 else []:
            keep.add(p["id"])

    # 正在跑的模拟 + 其项目/图谱 永不删
    protected_sims = {s["id"] for s in sims if s["status"] in ACTIVE_STATUS}
    protected_projects = {s["project_id"] for s in sims if s["id"] in protected_sims and s["project_id"]}
    for s in sims:
        if s["id"] in protected_sims:
            keep.add(s["id"])
    keep |= protected_projects
    keep |= {r["id"] for r in reports if r["id"] in keep}

    del_projects = [p for p in projects if p["id"] not in keep]
    del_sims = [s for s in sims if s["id"] not in keep and s["id"] not in protected_sims]
    sim_project = {s["id"]: s.get("project_id") for s in sims}
    del_reports = [
        r for r in reports
        if r["id"] not in keep
        and r.get("simulation_id") not in keep
        and sim_project.get(r.get("simulation_id")) not in keep
    ]
    keep_graphs = {p["graph_id"] for p in projects if p["graph_id"] and p["id"] in keep}
    del_graphs = [g for g in graphs if g[0] not in keep_graphs]

    print("=" * 78)
    print(f"项目 {len(projects)} 个 | 模拟 {len(sims)} 个 | 报告 {len(reports)} 个 | 图谱 {len(graphs)} 个")
    print("=" * 78)
    print("\n[保留]")
    for p in projects:
        if p["id"] in keep:
            tag = "（运行中）" if p["id"] in protected_projects else ""
            print(f"  项目 {p['id']}  {_ts(p['dir'])}  {p['name'][:24]:24s} graph={p['graph_id']} {tag}")
    for s in sims:
        if s["id"] in keep:
            print(f"  模拟 {s['id']}  {_ts(s['dir'])}  status={s['status']}")

    print("\n[待删除]")
    total = 0
    for p in del_projects:
        total += p["size"]
        print(f"  项目 {p['id']}  {_ts(p['dir'])}  {p['name'][:24]:24s} graph={p['graph_id']}  {mb(p['size'])}")
    for s in del_sims:
        total += s["size"]
        print(f"  模拟 {s['id']}  {_ts(s['dir'])}  project={s['project_id']}  hours={s['hours']}  {mb(s['size'])}")
    for r in del_reports:
        total += r["size"]
        print(f"  报告 {r['id']}  {_ts(r['dir'])}  {mb(r['size'])}")
    for gid, info, n, e, ep in del_graphs:
        print(f"  图谱 {gid}  {info}  {n}节点/{e}关系/{ep}episode")
    print(f"\n  预计释放 ≈ {mb(total)}（图谱库另算）")

    if not args.yes:
        print("\n以上为预览。加 --yes 才会真正删除。")
        return 0

    if not (del_projects or del_sims or del_reports or del_graphs):
        print("\n没有需要删除的内容。")
        return 0

    print("\n开始删除 ——")
    key = _shim_key()
    for p in del_projects:
        code, body = _http("DELETE", f"{BACKEND}/api/graph/project/{p['id']}")
        ok = code == 200
        print(f"  项目 {p['id']}: 接口 {code} {'✓' if ok else body[:80]}")
        if p["dir"].exists():          # 接口删不干净时兜底
            shutil.rmtree(p["dir"], ignore_errors=True)
    for s in del_sims:
        shutil.rmtree(s["dir"], ignore_errors=True)
        print(f"  模拟 {s['id']}: 目录已删除")
    for r in del_reports:
        _http("DELETE", f"{BACKEND}/api/report/{r['id']}")
        if r["dir"].exists():
            shutil.rmtree(r["dir"], ignore_errors=True)
        print(f"  报告 {r['id']}: 已删除")
    for gid, info, *_ in del_graphs:
        code, body = _http("DELETE", f"{SHIM}/api/v2/graph/{gid}", key=key)
        print(f"  图谱 {gid}: 接口 {code} {'✓' if code == 200 else body[:60]}")

    time.sleep(1)
    print("\n清理后：")
    for p in load_projects():
        print(f"  项目 {p['id']}  {_ts(p['dir'])}  {p['name'][:24]}")
    for s in load_sims():
        print(f"  模拟 {s['id']}  {_ts(s['dir'])}  status={s['status']}")
    print(f"  图谱 {len(graph_rows())} 个；图谱库 {mb(GRAPH_DB.stat().st_size) if GRAPH_DB.exists() else '-'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
