#!/usr/bin/env python3
"""给 zepshim 的抽取提示词加"同一实体别名归并"规则。

问题：原文里同一人会用多种说法（我/本人/作者/昵称/账号名/真名），
原提示词只写了 "Skip pronouns"，既没有要求把这些说法归并，也没要求
"一个实体只出现一次"，于是知识图谱里同一个人被拆成多个节点。

修法：
1) 规则 2 改为——代词不再一律丢弃，第一人称自称要保留为"我"这一个实体；
2) 新增规则 7——同一人物的不同说法必须归并为唯一实体，且不得把不同的人
   （如"我的父亲"与"女朋友的父亲"）混成笼统集合。
反复执行安全（已打过则跳过）。
"""
from __future__ import annotations

import pathlib

OLD_RULE2 = (
    "2. Entities: real, meaningful, named or clearly identifiable actors/objects in the text.\n"
    "   Skip pronouns, generic words and pure numbers. At most {max_entities} entities.\n"
)
NEW_RULE2 = (
    "2. Entities: real, meaningful, named or clearly identifiable actors/objects in the text.\n"
    "   Skip generic words and pure numbers. Keep first-person self-reference (\"我\", \"本人\",\n"
    "   \"作者\", \"I\", \"me\") as ONE entity — do not drop it, and do not emit several entities\n"
    "   for it. At most {max_entities} entities.\n"
)

ANCHOR = "\nJSON schema:\n"
RULE7 = (
    "\n7. ONE ACTOR = ONE ENTITY. Different surface forms for the same actor (real name,\n"
    "   nickname, account handle, role/credit label, or a self-reference such as 我/本人/作者)\n"
    "   MUST be merged into a single entity. Name that entity with the most specific canonical\n"
    "   label available: an explicitly declared identity/alias block in the source text first,\n"
    "   then a full personal name, then the self label. Never emit two entities for one person,\n"
    "   and always use that same canonical name in \"edges\".\n"
    "8. DISTINCT PEOPLE STAY DISTINCT. Do not collapse different individuals into one vague\n"
    "   collective (e.g. \"我的父亲\" / \"我的母亲\" / \"女朋友的父亲\" / \"女朋友的母亲\" are four\n"
    "   different people — never merge them into \"双方父母\"). If the text only provides a vague\n"
    "   collective, prefer the explicit members when they are stated anywhere in the text.\n"
)

for path in [pathlib.Path('/Users/youzi/Desktop/wzd/Usage/MiroFish/zepshim/extract.py'),
             pathlib.Path('/Users/youzi/.hermes-web-ui/.ekko/workspace/default/mv9jxa7fqdb6tt/zepshim/extract.py')]:
    if not path.exists():
        print(f'  [跳过] {path} 不存在')
        continue
    text = path.read_text()
    if 'ONE ACTOR = ONE ENTITY' in text:
        print(f'  [已打过] {path}')
        continue
    if text.count(OLD_RULE2) != 1:
        print(f'  [警告] {path}: 规则 2 锚点命中 {text.count(OLD_RULE2)} 次，跳过')
        continue
    text = text.replace(OLD_RULE2, NEW_RULE2)
    if text.count(ANCHOR) != 1:
        print(f'  [警告] {path}: JSON schema 锚点命中 {text.count(ANCHOR)} 次，跳过')
        continue
    text = text.replace(ANCHOR, RULE7 + ANCHOR)
    path.write_text(text)
    print(f'  [ok]   {path} 已加入别名归并规则（规则 2 改写 + 新增 7/8）')
