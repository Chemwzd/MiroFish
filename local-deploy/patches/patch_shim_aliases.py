#!/usr/bin/env python3
"""给 zepshim 加"确定性别名归一"：同一人物/角色的不同说法在入库前合并。

背景：提示词约束 LLM 并不足够——实测同一人仍会被写成
"我"+"<真名>/<账号名>"、"导师"+"我的导师"、"女朋友"+"我的女朋友"、
"论文盲审专家"+"盲审专家"、"期刊编辑"+"目标期刊的期刊编辑" 等多个节点。

做法：
1) zepshim/aliases.json —— 热加载的别名表（key 小写，value 规范名）；
2) app.py 里在 upsert_node / upsert_edge 之前调用 canonical_entity_name()，
   把名称先归一，再算 name_key，从而天然合并。
幂等：已打过则跳过。
"""
from __future__ import annotations

import pathlib

HELPERS = '''
# --------------------------------------------------------------------------
# 实体别名归一：LLM 抽取时同一人的不同说法会变成多个节点，这里做确定性兜底。
# 表在 aliases.json，按修改时间热加载；可用 ZEPSHIM_ALIASES_FILE 覆盖路径。
# --------------------------------------------------------------------------
ALIAS_FILE = os.environ.get("ZEPSHIM_ALIASES_FILE",
                            os.path.join(os.path.dirname(os.path.abspath(__file__)), "aliases.json"))
_ALIAS_CACHE: dict = {"mtime": None, "map": {}, "strip": []}


def _alias_table() -> tuple[dict, list]:
    try:
        mtime = os.path.getmtime(ALIAS_FILE)
    except OSError:
        return {}, []
    if _ALIAS_CACHE["mtime"] != mtime:
        try:
            with open(ALIAS_FILE, encoding="utf-8") as handle:
                data = json.load(handle)
            table = {str(k).strip().lower(): str(v) for k, v in (data.get("aliases") or {}).items()}
            strip = [str(p) for p in (data.get("strip_prefixes") or [])]
        except Exception as exc:  # noqa: BLE001
            logger.warning("aliases.json 读取失败：%s", exc)
            table, strip = {}, []
        _ALIAS_CACHE.update({"mtime": mtime, "map": table, "strip": strip})
        logger.info("别名表已加载：%d 条", len(table))
    return _ALIAS_CACHE["map"], _ALIAS_CACHE["strip"]


def canonical_entity_name(name: str) -> str:
    """把同一实体的不同说法归并到唯一称呼（找不到映射则原样返回）。"""

    raw = (name or "").strip()
    if not raw:
        return raw
    table, strip = _alias_table()
    hit = table.get(raw.lower())
    if hit:
        return hit
    for prefix in strip:
        if raw.startswith(prefix) and len(raw) > len(prefix):
            hit = table.get(raw[len(prefix):].strip().lower())
            if hit:
                return hit
    return raw


'''

ANCHOR_HELPERS = '\ndef _check_auth(authorization: str | None, x_api_key: str | None) -> None:\n'

ENTITY_OLD = '''        key = name_key(entity["name"])
        if not key:
            continue'''
ENTITY_NEW = '''        display_name = canonical_entity_name(entity["name"])
        key = name_key(display_name)
        if not key:
            continue'''

NAME_OLD = '            name=entity["name"],\n'
NAME_NEW = '            name=display_name,\n'

EDGE_OLD = '''        source_key = name_key(edge["source"])
        target_key = name_key(edge["target"])'''
EDGE_NEW = '''        source_name = canonical_entity_name(edge["source"])
        target_name = canonical_entity_name(edge["target"])
        source_key = name_key(source_name)
        target_key = name_key(target_name)'''

SRC_OLD = '                graph_id=graph_id, name=edge["source"], name_key=source_key,\n'
SRC_NEW = '                graph_id=graph_id, name=source_name, name_key=source_key,\n'
TGT_OLD = '                graph_id=graph_id, name=edge["target"], name_key=target_key,\n'
TGT_NEW = '                graph_id=graph_id, name=target_name, name_key=target_key,\n'

for path in [pathlib.Path('/Users/youzi/Desktop/wzd/Usage/MiroFish/zepshim/app.py'),
             pathlib.Path('/Users/youzi/.hermes-web-ui/.ekko/workspace/default/mv9jxa7fqdb6tt/zepshim/app.py')]:
    if not path.exists():
        print(f'  [跳过] {path} 不存在')
        continue
    text = path.read_text()
    if 'canonical_entity_name' in text:
        print(f'  [已打过] {path}')
        continue
    changed = 0
    for old, new in [(ANCHOR_HELPERS, HELPERS + ANCHOR_HELPERS.lstrip('\n')),
                     (ENTITY_OLD, ENTITY_NEW), (NAME_OLD, NAME_NEW),
                     (EDGE_OLD, EDGE_NEW), (SRC_OLD, SRC_NEW), (TGT_OLD, TGT_NEW)]:
        n = text.count(old)
        if n != 1:
            print(f'  [警告] {path.name}: 锚点命中 {n} 次，跳过整份文件')
            changed = -1
            break
        text = text.replace(old, new)
        changed += 1
    if changed > 0:
        path.write_text(text)
        print(f'  [ok]   {path} 已加入别名归一（{changed} 处改动）')
