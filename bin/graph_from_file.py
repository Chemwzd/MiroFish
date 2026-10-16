#!/usr/bin/env python3
"""用某个文本文件跑一遍 MiroFish 图谱链，并审计"同一人是否被拆成多节点"。

用法：
    python3 bin/graph_from_file.py <文件.txt> [--name 项目名] [--requirement 需求]

审计规则：图谱里若出现下列"别名/歧义"节点名，即视为未归一：
    本人、作者、主人公、用户、<你的真名>、<你的账号名>、双方父母、老板、家人、女友、对象
（"我"节点本身是允许的；"共一"等身份标签也不应单独成节点。）
"""
from __future__ import annotations

import os

import json
import pathlib
import sys
import time
import urllib.error
import urllib.request
import uuid

BASE = 'http://127.0.0.1:5001'

ALIAS_NODES = ['本人', '作者', '主人公', '用户', '双方父母', '老板', '家人', '女友', '爱人',
               '岳父', '岳母', '亲家', '共同第一作者', '第一完成人', '主要完成人']

# 项目特有的别名（真名、账号名等）不写死在代码里：从 zepshim/aliases.json 自动并入。
# 该文件属于部署数据、不入库（格式见 zepshim/aliases.example.json）。
_ALIAS_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           'zepshim', 'aliases.json')
if os.path.exists(_ALIAS_FILE):
    try:
        with open(_ALIAS_FILE, 'r', encoding='utf-8') as fh:
            ALIAS_NODES += [k for k in (json.load(fh).get('aliases') or {})
                            if k not in ALIAS_NODES]
    except Exception:  # noqa: BLE001
        pass


def request(method: str, url: str, *, data=None, headers=None, timeout: int = 300):
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode()
            return resp.status, (json.loads(raw) if raw.strip() else None)
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode()[:2000]


def api(method: str, path: str, *, data=None, headers=None, timeout: int = 300):
    return request(method, BASE + path, data=data, headers=headers, timeout=timeout)


def multipart(fields: dict, files: list) -> tuple[bytes, str]:
    boundary = '----mfp' + uuid.uuid4().hex
    chunks: list[bytes] = []
    for name, value in fields.items():
        chunks.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
    for name, filename, content in files:
        chunks.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
            f'Content-Type: text/plain; charset=utf-8\r\n\r\n'.encode()
        )
        chunks.append(content.encode() + b'\r\n')
    chunks.append(f'--{boundary}--\r\n'.encode())
    return b''.join(chunks), f'multipart/form-data; boundary={boundary}'


def find_key(obj, key: str):
    if isinstance(obj, dict):
        if obj.get(key):
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


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith('--')]
    if not args:
        print(__doc__)
        return 2
    path = pathlib.Path(args[0]).expanduser()
    text = path.read_text()
    name = path.stem

    print(f'== 用 {path.name}（{len(text)} 字符）跑图谱链 ==', flush=True)
    body, content_type = multipart(
        {
            'simulation_requirement': f'基于{name}推演我未来五年的人生走向',
            'project_name': f'审核-{name}',
            'additional_context': '实体别名归一审核',
        },
        [('files', path.name, text)],
    )
    started = time.time()
    code, payload = api('POST', '/api/graph/ontology/generate', data=body,
                        headers={'Content-Type': content_type}, timeout=900)
    project_id = find_key(payload, 'project_id')
    print(f'  本体生成: HTTP {code} project={project_id} ({time.time() - started:.0f}s)', flush=True)
    if not project_id:
        print('  失败:', str(payload)[:400])
        return 1

    code, task = api('POST', '/api/graph/build', data=json.dumps(
        {'project_id': project_id, 'graph_name': f'review-{name}'}).encode(),
        headers={'Content-Type': 'application/json'}, timeout=120)
    task_id = find_key(task, 'task_id')
    deadline = time.time() + 600
    status = ''
    while time.time() < deadline:
        _, st = api('GET', f'/api/graph/task/{task_id}', timeout=60)
        status = str(find_key(st, 'status') or '')
        if status in {'completed', 'failed'}:
            break
        time.sleep(5)
    print(f'  建图: {status}', flush=True)

    _, proj = api('GET', f'/api/graph/project/{project_id}', timeout=60)
    graph_id = find_key(proj, 'graph_id')
    _, data = api('GET', f'/api/graph/data/{graph_id}', timeout=120)
    payload_data = (data or {}).get('data') if isinstance(data, dict) else {}
    nodes = (payload_data or {}).get('nodes') or []
    edges = (payload_data or {}).get('edges') or []
    names = [str((n or {}).get('name') or (n or {}).get('label') or '') for n in nodes]

    print(f'\n--- 图谱结果: {len(nodes)} 节点 / {len(edges)} 关系 ---')
    print('  节点:', '、'.join(names))
    low = [n.lower() for n in names]
    bad = [n for n in names if any(a == n.lower() or a in n.lower() for a in ALIAS_NODES)]
    print('\n--- 审计 ---')
    if bad:
        print(f'  ✗ 仍存在别名/歧义节点: {bad}')
    else:
        print('  ✓ 未发现别名节点（本人/作者/用户/账号名/双方父母/老板 等）')
    for expected in ['我', '导师', '女朋友', '父亲', '母亲', '同门']:
        hit = [n for n in names if expected in n]
        print(f'  {expected:6s} → {hit if hit else "（无）"}')
    print(f'\n  UI: http://127.0.0.1:3000/graph/{graph_id}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
