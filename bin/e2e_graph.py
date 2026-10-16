#!/usr/bin/env python3
"""Drive MiroFish's own HTTP API end to end: seed material -> ontology -> graph.

This is the real integration test: it goes through MiroFish's Flask API, which
talks to the local Zep-compatible shim for ingestion and search. Stdlib only.

    python3 e2e_mirofish.py            # defaults to http://127.0.0.1:3000
"""

from __future__ import annotations

import json
import os
import pathlib
import sys
import time
import urllib.error
import urllib.request
import uuid

BASE = os.environ.get("MIROFISH_URL", "http://127.0.0.1:3000")
SHIM = os.environ.get("ZEPSHIM_URL", "http://127.0.0.1:8100")
SHIM_KEY = os.environ.get("ZEPSHIM_API_KEY", "")
SHIM_HDR = {"Authorization": f"Bearer {SHIM_KEY}"} if SHIM_KEY else {}
FAILURES: list[str] = []

SEED = """武汉大学舆情事件材料（测试用）

2026 年 9 月，武汉大学发布通报，称该校一名博士研究生的学位论文存在严重学术不端行为，
经校学术委员会调查核实，决定撤销其博士学位，并对其导师作出停止招生的处理。
校长张平文在全校学风建设会议上表示，学校将全面加强学术规范教育，并在所有院系开展
学位论文质量自查自纠，绝不姑息任何学术不端行为。

教育部随后就此事向媒体作出回应，要求各高校严肃处理学术不端问题，完善学位授予质量
保障机制，并强调导师在学位论文质量把关中负有不可推卸的责任。教育部相关负责人表示，
将把学术不端纳入高校评估的重要指标。

事件在社交平台上引发广泛讨论。微博相关话题阅读量超过三亿次，大量网友支持从严查处
学术不端，也有网友担心个案被过度放大。多家媒体发表评论，呼吁建立更透明的学术监督
机制。部分高校教师在接受采访时表示，现行评价体系过度依赖论文数量，是学术不端的重要
诱因之一。

武汉大学位于湖北省武汉市，是教育部直属的全国重点大学。湖北省教育厅表示将配合教育部
开展专项检查。该事件也被视为对高校学术治理能力的一次检验。
"""


def check(label: str, condition: bool, detail: str = "") -> None:
    if not condition:
        FAILURES.append(label)
    print(f"{'ok  ' if condition else 'FAIL'} {label} {detail}", flush=True)


def request(method: str, url: str, *, data=None, headers=None, timeout: int = 300):
    req = urllib.request.Request(url, data=data, method=method)
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            raw = response.read().decode()
            return response.status, (json.loads(raw) if raw.strip() else None)
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode()[:20000]
    except Exception as error:  # noqa: BLE001
        return 0, str(error)


def api(method: str, path: str, *, data=None, headers=None, timeout: int = 300):
    return request(method, BASE + path, data=data, headers=headers, timeout=timeout)


def multipart(fields: dict, files: list) -> tuple[bytes, str]:
    boundary = "----mirofish" + uuid.uuid4().hex
    chunks: list[bytes] = []
    for name, value in fields.items():
        chunks.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'
            f"{value}\r\n".encode()
        )
    for name, filename, content in files:
        chunks.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}";'
            f' filename="{filename}"\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n'.encode()
        )
        chunks.append(content.encode() + b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode())
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def find_key(obj, key: str):
    """Depth-first search for the first value stored under `key`."""

    if isinstance(obj, dict):
        if key in obj and obj[key]:
            return obj[key]
        for value in obj.values():
            found = find_key(value, key)
            if found:
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = find_key(item, key)
            if found:
                return found
    return None


def main() -> None:
    print(f"== MiroFish E2E against {BASE} (shim {SHIM}) ==", flush=True)

    _, shim_before = request("GET", f"{SHIM}/stats", headers=SHIM_HDR, timeout=20)
    ingested_before = ((shim_before or {}).get("counters") or {}).get("episodes_ingested", 0)

    # --- step 1: upload seed + generate ontology -------------------------------
    body, content_type = multipart(
        {
            "simulation_requirement": "预测该舆情事件未来三天的舆论走向与关键转折点",
            "project_name": "e2e-武汉大学舆情",
            "additional_context": "端到端集成测试，材料为虚构测试数据",
        },
        [("files", "seed.txt", SEED)],
    )
    started = time.time()
    code, payload = api(
        "POST", "/api/graph/ontology/generate", data=body,
        headers={"Content-Type": content_type}, timeout=600,
    )
    project_id = find_key(payload, "project_id")
    ontology = find_key(payload, "ontology")
    check("POST /api/graph/ontology/generate", code == 200 and bool(project_id),
          f"HTTP {code} project={project_id} ({time.time() - started:.0f}s)")
    if not project_id:
        pathlib.Path("/tmp/e2e_ontology_error.txt").write_text(str(payload))
        tail = str(payload)[-700:]
        print(f"     full error -> /tmp/e2e_ontology_error.txt ({len(str(payload))} chars)")
        print("     tail of error:", tail.replace(chr(10), " "))
        return finish()
    entity_types = (ontology or {}).get("entity_types") or []
    edge_types = (ontology or {}).get("edge_types") or []
    check("ontology generated", bool(entity_types) and bool(edge_types),
          f"entities={len(entity_types)} edges={len(edge_types)}")
    print("     entity types:", ", ".join(str(t.get("name")) for t in entity_types[:6]))

    # --- step 2: build the knowledge graph ------------------------------------
    started = time.time()
    code, task_payload = api(
        "POST", "/api/graph/build",
        data=json.dumps({"project_id": project_id, "graph_name": "e2e-graph"}).encode(),
        headers={"Content-Type": "application/json"}, timeout=120,
    )
    task_id = find_key(task_payload, "task_id")
    check("POST /api/graph/build", code == 200 and bool(task_id), f"HTTP {code} task={task_id}")
    if not task_id:
        print("     response:", str(task_payload)[:400])
        return finish()

    status = None
    detail = None
    deadline = time.time() + 900
    while time.time() < deadline:
        _, task = api("GET", f"/api/graph/task/{task_id}", timeout=30)
        status = find_key(task, "status")
        detail = task
        if str(status).lower() in {"completed", "success", "failed", "error"}:
            break
        time.sleep(5)
    check("graph build task finished", str(status).lower() in {"completed", "success"},
          f"status={status} ({time.time() - started:.0f}s)")
    if str(status).lower() not in {"completed", "success"}:
        print("     task payload:", str(detail)[:600])

    _, project = api("GET", f"/api/graph/project/{project_id}", timeout=30)
    graph_id = find_key(project, "graph_id") or find_key(detail, "graph_id")
    check("project exposes graph_id", bool(graph_id), f"graph_id={graph_id}")

    # --- step 3: read the graph back through MiroFish -------------------------
    if graph_id:
        code, data = api("GET", f"/api/graph/data/{graph_id}", timeout=60)
        node_count = len(find_key(data, "nodes") or [])
        edge_count = len(find_key(data, "edges") or [])
        check("GET /api/graph/data/{graph_id}", code == 200 and node_count > 0 and edge_count > 0,
              f"HTTP {code} nodes={node_count} edges={edge_count}")
        nodes = find_key(data, "nodes") or []
        print("     sample nodes:", ", ".join(str(n.get("name"))[:14] for n in nodes[:8]))
        edges = find_key(data, "edges") or []
        print("     sample facts:", " | ".join(str(e.get("fact"))[:40] for e in edges[:3]))

    # --- step 4: confirm the shim really did the ingestion --------------------
    _, shim_after = request("GET", f"{SHIM}/stats", headers=SHIM_HDR, timeout=20)
    ingested_after = ((shim_after or {}).get("counters") or {}).get("episodes_ingested", 0)
    failures_after = ((shim_after or {}).get("counters") or {}).get("extraction_failures", 0)
    check("shim ingested the episodes", ingested_after > ingested_before,
          f"{ingested_before} -> {ingested_after} (extraction failures={failures_after})")

    finish()


def finish() -> None:
    print("E2E", "PASS" if not FAILURES else "FAIL " + ", ".join(FAILURES), flush=True)
    sys.exit(0 if not FAILURES else 1)


if __name__ == "__main__":
    main()
