#!/usr/bin/env python3
"""把 LiteLLM 的 deepseek-flash 接入 hermes / hermes-web-ui / WorkBuddy。

用法：
    python3 patch_clients_deepseek.py [家目录]
    CLIENT_HOME=/home/rtx4090 python3 patch_clients_deepseek.py

改动（全部幂等，缺文件则跳过）：
1) <home>/.hermes/config.yaml 与 <home>/.hermes/profiles/*/config.yaml
   - custom_providers 中 Litellm provider 的 models 增加 deepseek-flash: {}
   - providers.custom.models 增加 deepseek-flash（supports_vision + context_length）
   （纯文本插入，保留注释与原格式）
2) <home>/.hermes-web-ui/config.json → modelVisibility['custom:litellm'].models 增加 deepseek-flash
3) <home>/.workbuddy/models.json → 追加模型对象（url/key 复用同文件已有 litellm 条目）
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import sys

HOME = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else os.environ.get('CLIENT_HOME', pathlib.Path.home()))
HERMES = HOME / '.hermes'
MODEL = 'deepseek-flash'
CTX = 1048576

VISION_BLOCK = (
    f'      {MODEL}:\n'
    '        supports_vision: true\n'
    f'        context_length: {CTX}\n'
)


def patch_yaml(path: pathlib.Path) -> str:
    text = path.read_text()

    block_pat = re.compile(rf'(?m)^(      {re.escape(MODEL)}:\n)((?:        [^\n]*\n)*)')
    if block_pat.search(text):
        text = block_pat.sub(VISION_BLOCK, text, count=1)
        note_a = '元数据已刷新'
    else:
        anchor = '      deepseek/deepseek-flash:\n        supports_vision: true\n'
        if text.count(anchor) == 1:
            text = text.replace(anchor, anchor + VISION_BLOCK)
            note_a = '已加元数据'
        else:
            note_a = f'未找到元数据锚点(x{text.count(anchor)})'

    anchor2 = '    models:\n      gpt-6-luna: {}\n'
    if re.search(rf'(?m)^      {re.escape(MODEL)}: \{{\}}\n', text):
        note_b = '模型已在列表'
    elif text.count(anchor2) == 1:
        text = text.replace(anchor2, f'    models:\n      {MODEL}: {{}}\n      gpt-6-luna: {{}}\n')
        note_b = '已加入列表'
    else:
        note_b = f'未找到列表锚点(x{text.count(anchor2)})'

    path.write_text(text)
    return f'{note_a}; {note_b}'


root_cfg = HERMES / 'config.yaml'
targets = ([root_cfg] if root_cfg.exists() else [])
targets += sorted(p for p in HERMES.glob('profiles/*/config.yaml') if p.parent.name != '.deleted')

print('=== hermes YAML ===')
for path in targets:
    label = 'root' if path.parent == HERMES else path.parent.name
    print(f'  {label:20s} {patch_yaml(path)}')

# --- hermes-web-ui ---
webui = HOME / '.hermes-web-ui/config.json'
if webui.exists():
    data = json.loads(webui.read_text())
    mv = data.setdefault('modelVisibility', {})
    entry = mv.setdefault('custom:litellm', {'mode': 'include', 'models': []})
    entry.setdefault('mode', 'include')
    models = entry.setdefault('models', [])
    if MODEL in models:
        print('=== hermes-web-ui ===\n  模型已可见')
    else:
        models.append(MODEL)
        webui.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n')
        print(f'=== hermes-web-ui ===\n  已加入可见列表（现 {len(models)} 个模型）')
else:
    print('=== hermes-web-ui ===\n  (无 config.json，跳过)')

# --- WorkBuddy ---
wb = HOME / '.workbuddy/models.json'
if wb.exists():
    items = json.loads(wb.read_text())
    assert isinstance(items, list), 'models.json 结构不是列表'
    if any(str(i.get('id')) == MODEL for i in items):
        print('=== workbuddy ===\n  模型已存在')
    else:
        ref = next((i for i in items if str(i.get('url', '')).endswith(':4000/v1')), None)
        if ref is None:
            print('=== workbuddy ===\n  (没有 litellm 条目可复用，跳过)')
        else:
            items.append({
                'id': MODEL, 'name': MODEL, 'vendor': 'Custom',
                'url': ref['url'], 'apiKey': ref['apiKey'],
                'supportsToolCall': True, 'supportsImages': True, 'supportsReasoning': True,
                'useCustomProtocol': False,
                'reasoning': {'defaultEffort': 'high', 'supportedEfforts': ['low', 'high', 'max']},
            })
            wb.write_text(json.dumps(items, ensure_ascii=False, indent=2) + '\n')
            print(f'=== workbuddy ===\n  已追加（现 {len(items)} 个模型）')
else:
    print('=== workbuddy ===\n  (该机无 .workbuddy/models.json，跳过)')
