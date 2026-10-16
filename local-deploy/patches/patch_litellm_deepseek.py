#!/usr/bin/env python3
"""在 LiteLLM 网关 config.yaml 里新增 DeepSeek 官方上游：model_name = deepseek-flash。

用法：
    python3 patch_litellm_deepseek.py [配置文件路径]
    LITELLM_CONFIG=/path/to/config.yaml python3 patch_litellm_deepseek.py

默认路径 ~/.hermes/workspace/litellm-gateway/config.yaml；幂等（已存在则跳过）。
"""
from __future__ import annotations

import os
import pathlib
import sys

default = pathlib.Path.home() / '.hermes/workspace/litellm-gateway/config.yaml'
CFG = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else os.environ.get('LITELLM_CONFIG', default))

ANCHOR = '  # ---------- 上游 B: tencent-vod (text-aigc.vod-qcloud.com) ----------'

BLOCK = '''  # ---------- 上游 C: DeepSeek 官方 (api.deepseek.com) ----------
  # 客户端 model=deepseek-flash → DeepSeek 官方（DeepSeek-V4.1-Flash，原生支持文本+图片输入）
  # 与上游 A 的 deepseek/deepseek-flash（腾讯 TokenHub）并存，互不影响。
  - model_name: deepseek-flash
    litellm_params:
      model: openai/deepseek-flash
      api_base: https://api.deepseek.com
      api_key: os.environ/DEEPSEEK_KEY
      tpm: 2000000
      rpm: 1000
      default_api_key_tpm_limit: 2000000
      default_api_key_rpm_limit: 1000
      timeout: 300
    model_info:
      supports_vision: true
      max_input_tokens: 1048576
      max_output_tokens: 393216

'''

text = CFG.read_text()
if '\n  - model_name: deepseek-flash\n' in text:
    print(f'{CFG}: 已包含 deepseek-flash，跳过')
else:
    if text.count(ANCHOR) != 1:
        raise SystemExit(f'{CFG}: 锚点命中 {text.count(ANCHOR)} 次，需人工处理')
    CFG.write_text(text.replace(ANCHOR, BLOCK + ANCHOR))
    print(f'{CFG}: 已新增 deepseek-flash（DeepSeek 官方，多模态）')
