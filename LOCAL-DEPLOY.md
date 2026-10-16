# MiroFish 本地化部署改动归档（local-deploy）

| 项 | 值 |
|---|---|
| 上游仓库 | https://github.com/666ghj/MiroFish |
| 上游基线 commit | `7657031`（chore: update Star History） |
| 本分支 | `local-deploy-macos` |
| 归档时间 | 2026-10-16 |
| 目标 | 在 macOS 上**完全本地化**运行：无 Docker、LLM 走本机 LiteLLM 网关、图谱走本地兼容层 |

---

## 1. 为什么要改

上游把"图谱"硬绑在 **Zep Cloud** 上——`backend/app/config.py` 直接拒绝 `ZEP_API_URL`，
而 Zep 社区版（CE）已停止维护，等于没有本地图谱方案。同时上游默认直连外部 LLM。

本分支做三类改动：

- **A. 本地 Zep 兼容层**：新增 `zepshim/`（FastAPI + SQLite + 本地 LLM 抽取），实现 Zep Cloud v2 的
  子集 API（graph / node / edge / episode / batch / 搜索），MiroFish 通过 `MIROFISH_ZEP_BASE_URL` 指向它。
- **B. LLM 出口统一**：所有 LLM 调用走本机 `127.0.0.1:4000`（LiteLLM 网关），
  由网关决定真实上游与限流，MiroFish 侧只保存一个 master key 与模型名。
- **C. 运行期修复**：让"跑满轮次后的图谱收尾"**可见、可自动完成、超时按轮数放宽**，
  并做实体别名归一，避免同一个人被抽成多个节点。

---

## 2. 运行拓扑（本机）

| 服务 | 端口 | 启动方式 | 备注 |
|---|---|---|---|
| 前端 UI（vite dev server） | 3000 | `bin/start.sh` | 代理 `/api` → `127.0.0.1:5001` |
| 后端 API（Flask） | 5001 | `bin/start.sh` | LLM 出口 = LiteLLM |
| zepshim（本地 Zep v2 兼容层） | 8100 | `bin/start.sh` → `zepshim/run.sh` | SQLite 存图：`var/graph/zepshim.sqlite3` |
| LiteLLM 网关 | 4000 | 外部依赖（launchd） | 统一上游 + 限流 |

相关配置（**均不入库**，权限 600）：

- `MiroFish/.env`：`LLM_API_KEY` / `LLM_BASE_URL` / `LLM_MODEL_NAME` / `ZEP_API_KEY` /
  `MIROFISH_ZEP_BASE_URL` / 代理设置（见 §3.12）
- `zepshim/.env`：`ZEPSHIM_DB` / `ZEPSHIM_API_KEY` / `ZEPSHIM_LLM_*`（指向 LiteLLM）

---

## 3. 改动清单

| # | 文件 / 目录 | 改动 | 目的 |
|---|---|---|---|
| 1 | `zepshim/`（新增） | 本地 Zep Cloud v2 兼容服务：graph/node/edge/episode/batch/搜索 + LLM 抽取 + 别名归并 + 启动恢复未处理 episode | 替代 Zep Cloud，实现本地图谱 |
| 2 | `backend/app/config.py` | 去掉"拒绝 `ZEP_API_URL`"的硬校验 | 允许本地/自建图谱端点 |
| 3 | `backend/app/utils/zep.py` | 支持 `MIROFISH_ZEP_BASE_URL`；新增按轮数放大的收尾超时 `ingestion_wait_timeout_seconds()` | 指向本地 shim；长模拟不再因收尾超时被误判失败 |
| 4 | `backend/app/services/zep_graph_memory_updater.py` | 收尾 deadline 按模拟轮数计算；新增 `_simulation_rounds()` | 同上（收尾主闸门） |
| 5 | `backend/app/services/simulation_runner.py` | ① 所有平台写出 `simulation_end` 后**自动优雅结束子进程**（否则永远停在 `running`）② 等待监控线程的 join 上限改为按轮数 | 跑满即自动 `completed`，报告按钮自动可点 |
| 6 | `backend/app/services/zep_tools.py` | LLM `max_tokens` 800 → 4096 | DeepSeek 系模型"先推理再出正文"，太小会返回空 |
| 7 | `backend/app/services/simulation_config_generator.py` | 时间配置新增 `start_hour` | 避免小轮数模拟全落在 0–5 点"无人活跃"区间 |
| 8 | `backend/scripts/run_{twitter,reddit,parallel}_simulation.py` | 使用 `start_hour`；twitter 侧增加 `round_trace.log` | 同上 + 便于诊断轮次/活跃人数 |
| 9 | `backend/app/api/simulation.py` | `run-status` 增返 `graph_ingestion`（更新器实时统计：已发批次/条数、待写入 episode 数、失败批次） | 前端可见"图谱收尾进度" |
| 10 | `frontend/src/components/Step3Simulation.vue` | SIMULATION MONITOR 标题旁新增图谱进度徽标 + 变化时写日志（进行中/失败/已完成三态配色） | 收尾期间不再只看到 running |
| 11 | `frontend/vite.config.js` | `open: false`；proxy target `localhost` → `127.0.0.1` | Node 会把 localhost 解析成 ::1，导致代理失败 |
| 12 | `backend/requirements.txt` | 追加 `mcp==1.24.0` | camel-ai/oasis 依赖 mcp<2（2.x 里 FastMCP 换位置，`import oasis` 会失败） |
| 13 | `MiroFish/.env`（不入库） | 见 §3.12 | 本地端点/密钥/代理 |

### 3.12 `.env` 关键项（模板见 `local-deploy/templates/`）

```dotenv
LLM_BASE_URL=http://127.0.0.1:4000/v1     # 本机 LiteLLM
LLM_MODEL_NAME=deepseek/deepseek-flash    # 网关里的模型别名
ZEP_API_KEY=local-zepshim                 # shim 的鉴权（非 LLM key）
MIROFISH_ZEP_BASE_URL=http://127.0.0.1:8100/api/v2
FLASK_HOST=127.0.0.1
FLASK_PORT=5001
# 外网走本机代理（Clash 等），本地服务直连
HTTP_PROXY=http://127.0.0.1:7897
HTTPS_PROXY=http://127.0.0.1:7897
http_proxy=http://127.0.0.1:7897
https_proxy=http://127.0.0.1:7897
NO_PROXY=127.0.0.1,localhost,::1,0.0.0.0
no_proxy=127.0.0.1,localhost,::1,0.0.0.0
```

> 说明：若把 `HTTP_PROXY` 指向本机代理，请**同时**保留 `NO_PROXY` 覆盖本地地址，
> 否则本地 8100/4000 调用会被送去代理，代理一挂就整条链路崩。
> 想完全不依赖代理，可改用 `NO_PROXY=*` + `HF_HUB_OFFLINE=1`（模型走本地缓存）。

---

## 4. 补丁脚本索引（`local-deploy/patches/`）

这些脚本是"改动是怎么落下去的"的可执行记录，可重复运行（幂等、带锚点校验）：

| 脚本 | 作用 |
|---|---|
| `patch_mirofish.py` | 早期一批：本地 Zep 端点支持、vite 代理等 |
| `patch_extract_alias_merge.py` | zepshim 抽取提示词：保留第一人称 + "一人一实体/不同人保持独立" |
| `patch_shim_aliases.py` | zepshim 入库前别名归并（`aliases.json` 热加载） |
| `patch_shim_resume_pending.py` | zepshim 启动时把排队未处理的 episode 重新入队（重启不丢队列） |
| `patch_sim_hours.py` | `start_hour` + 轮次诊断日志 |
| `patch_auto_complete_planA.py` | 方案 A：跑满自动收尾 |
| `patch_round_scaled_ingestion_timeout.py` | 收尾超时按轮数放大 |
| `patch_monitor_graph_ingestion.py` | 监控面板图谱收尾进度（本归档新增） |
| `patch_litellm_deepseek.py` / `patch_clients_deepseek.py` | LiteLLM 增加 deepseek 模型并接入各客户端 |
| `patch_e2e_local*.py` | 本地 e2e 联调脚本修缮 |

---

## 5. 运维脚本（`bin/`）

| 脚本 | 作用 |
|---|---|
| `start.sh` / `stop.sh` / `status.sh` | 启停查三件套（按端口精确操作，幂等） |
| `daemon.py` | 脱离终端的守护启动器（`start_new_session=True` + 关闭三条流） |
| `e2e_graph.py` / `e2e_sim.py` | 图谱链 / 模拟链端到端自检 |
| `graph_from_file.py` | 拿任意 txt 跑一遍建图并审计实体命名 |
| `cleanup_history.py` | 清理历史（项目/模拟/报告/图谱），默认预览，`--yes` 才删 |
| `patches/` / `templates/` | 见 §4 与 §3.12 |

---

## 6. 已知限制

- `zepshim` 只实现 MiroFish 用到的 Zep v2 子集，不是完整 Zep 替代品。
- 图谱抽取是**逐 episode 调 LLM**：模拟活动量大时收尾明显变慢；可按轮数放宽超时（本分支已做），
  或把 `ZEPSHIM_LLM_WORKERS` 调大。
- 模拟子进程启动时会校验 HuggingFace 上的 tokenizer 配置：网络不通会重试数分钟。
  建议配置代理（§3.12）或设置 `HF_HUB_OFFLINE=1` 走本地缓存。
- 前端"重新开始模拟"使用 `force=true`，会清掉上一次的运行日志；图谱/人设配置保留。

---

## 7. 溯源

- 上游基线：`7657031`
- 本分支：`local-deploy-macos`（fork 自上游，见仓库 remote `fork`）
- 备份：改动过的文件旁有 `*.bak-*` 原始副本（未入库，按需查阅）
- 归档生成：由本地部署过程整理，包含可执行补丁脚本，便于逐条复核/回退
