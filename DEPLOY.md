# MiroFish 本地部署说明

部署位置：`/Users/youzi/Desktop/wzd/Usage/MiroFish`
形态：**全部本机原生进程**，无 docker、无外部 SaaS 依赖（图谱层为本地 zepshim）。

## 1. 组件与端口

| 组件 | 端口 | 说明 | 进程 |
|---|---|---|---|
| 前端 UI | 3000 | Vite dev server，代理 `/api` → `127.0.0.1:5001` | `frontend/.venv` 无关，node |
| 后端 API | 5001 | Flask，`backend/run.py` | `backend/.venv/bin/python run.py` |
| 本地图谱 zepshim | 8100 | Zep Cloud v2 兼容层（FastAPI + SQLite） | `zepshim/.venv/bin/python app.py`（经 `zepshim/run.sh`） |
| LLM 网关（外部依赖） | 4000 | 本机 LiteLLM，launchd `com.workbuddy.litellm-gateway` | 已有服务 |

三者均绑定 `127.0.0.1`，不对外暴露。

## 2. 目录结构

```
MiroFish/
├── backend/                     # 后端源码 + .venv
├── frontend/                    # 前端源码 + node_modules
├── zepshim/                     # 本地图谱服务（我们的 Zep Cloud 兼容层）
│   ├── app.py extract.py store.py search.py
│   ├── run.sh                   # 启动入口（自带 .env 注入）
│   └── .env
├── var/
│   ├── graph/zepshim.sqlite3    # ★ 图谱数据库（全部图谱数据，需备份）
│   └── logs/{zepshim,backend,frontend,deps,start,e2e_*}.log
├── bin/
│   ├── daemon.py                # 分离式启动器（让终端立刻返回，不挂住）
│   ├── start.sh / stop.sh / status.sh
│   ├── e2e_graph.py             # 命令行端到端：本体 → 建图
│   └── e2e_sim.py               # 命令行端到端：模拟 → 报告
├── .env                         # 主配置（LLM / 图谱 / Flask），权限 600
└── DEPLOY.md  HOWTO-PLAY.md
```

## 3. 日常操作

```bash
cd /Users/youzi/Desktop/wzd/Usage/MiroFish

./bin/start.sh     # 拉起三件套（幂等，已在跑的会跳过）并打印状态
./bin/status.sh    # 健康检查（三个端口 + 图谱统计 + LLM 网关）
./bin/stop.sh      # 停栈（按端口精确杀，不影响别的服务）

tail -f var/logs/backend.log     # 后端日志
tail -f var/logs/zepshim.log     # 图谱服务日志
tail -f var/logs/frontend.log    # 前端日志
```

浏览器打开 <http://127.0.0.1:3000>。

## 4. 配置（.env）

```ini
# LLM：走本机 LiteLLM 网关（模型 deepseek/deepseek-flash）
LLM_API_KEY=<LITELLM_MASTER_KEY>
LLM_BASE_URL=http://127.0.0.1:4000/v1
LLM_MODEL_NAME=deepseek-flash   # LiteLLM 别名 → DeepSeek 官方(api.deepseek.com)
# 另一个可用别名: deepseek/deepseek-flash → 腾讯 TokenHub(二者并存)

# 图谱：指向本地 zepshim
ZEP_API_KEY=local-zepshim
MIROFISH_ZEP_BASE_URL=http://127.0.0.1:8100/api/v2

FLASK_HOST=127.0.0.1
FLASK_PORT=5001
FLASK_DEBUG=false
OASIS_DEFAULT_MAX_ROUNDS=10
```

`zepshim/.env` 里 `ZEPSHIM_API_KEY` 必须与上面的 `ZEP_API_KEY` 一致；`ZEPSHIM_LLM_*` 同样走 LiteLLM 网关。

> LLM 相关**全部**经由 LiteLLM（含 zepshim 的图谱抽取），key 只从 `~/.hermes/workspace/litellm-gateway/.env` 取 `LITELLM_MASTER_KEY`。

## 5. 相对上游的改动（重新安装时务必带上）

| # | 文件 | 改动 | 目的 |
|---|---|---|---|
| 1 | `backend/app/utils/zep.py` | base_url 改为读 `MIROFISH_ZEP_BASE_URL` | 指向本地图谱服务 |
| 2 | `backend/app/config.py` | 不再拒绝 `ZEP_API_URL` | 允许自定义图谱端点 |
| 3 | `frontend/vite.config.js` | 代理目标 `127.0.0.1:5001`、`open:false` | Node 会把 localhost 解析成 ::1，Flask 只听 IPv4 |
| 4 | `backend/app/services/zep_tools.py` | `max_tokens` 800 → 4096 | deepseek 系"先推理后回答"，预算小会返回空 |
| 5 | `backend/requirements.txt` | 追加 `mcp==1.24.0` | mcp 2.x 里 `FastMCP` 换位置，`import oasis` 会失败 |
| 6 | `zepshim/app.py` | 鉴权支持 `Authorization: Api-Key <key>` | zep-cloud SDK 3.25 用的就是 `Api-Key` 方案（只认 Bearer 会 401） |

`git -C <根目录> status --porcelain` 可看到 1-4 的改动。

## 6. 图谱数据：备份 / 重置

```bash
# 备份（三件一起，WAL 模式下缺一不可）
cp var/graph/zepshim.sqlite3* ~/Desktop/zepshim-backup-$(date +%F).sqlite3.bak

# 重置为全空
./bin/stop.sh
rm -f var/graph/zepshim.sqlite3*
./bin/start.sh
```

## 7. 故障排查

| 现象 | 原因 / 处理 |
|---|---|
| 建图 401 `invalid api key` | 两边 key 不一致，或走的是只认 `Bearer` 的旧 shim（装 5 节第 6 条补丁） |
| 前端页面接口全空响应 | vite 代理指向了 `localhost`（必须 `127.0.0.1`） |
| 建图/抽取内容为空 | `max_tokens` 太小（deepseek 先推理后回答），确认 5 节第 4 条补丁还在 |
| 后端启动报 `LLM_API_KEY 未配置` | `.env` 没读到；确认在根目录，且 LiteLLM 网关在 4000 上活着 |
| 模拟阶段 import 报错 | 检查 `mcp` 版本是否为 1.24.0 |
| 端口被占 | `./bin/status.sh` 看是谁；`./bin/stop.sh` 清理 |


## 8. 模拟阶段补丁（第 7 条）

| # | 文件 | 改动 | 目的 |
|---|---|---|---|
| 7 | `backend/app/services/simulation_config_generator.py`（新增 `time_config.start_hour`，默认 20）<br>`backend/scripts/run_{twitter,reddit,parallel}_simulation.py`（`simulated_hour = (start_hour + round_num*minutes_per_round//60) % 24`） | 让模拟从活跃时段起跑 | 否则模拟固定从模拟时间 00:00 开始，短轮次运行会整段落在 `off_peak_hours`(0-5) 且 Agent `active_hours`(默认 8-23) 之外 → 每轮激活 **0 个 Agent** → 模拟 0.0 秒空跑、0 动作 |

一键应用（幂等）：`deploy/patch_sim_hours.py`

## 9. 已验证 + 已知限制

**验证（2026-10-16，模型 `deepseek/deepseek-flash` 经 LiteLLM）**
- 本体生成 26s（10 实体/10 关系）→ 建图 20s → 图谱 **11 节点 / 11 关系**
- 模拟 3 轮：模拟时刻 20/21/22 点，各 **4/4/5 个活跃 Agent**；动作 post=24 like=13 repost=22
- 报告产出：`backend/uploads/reports/report_*/full_report.md`（24KB / 4 章，约 215s）

**已知限制（不影响主流程跑通）**
1. **模拟记忆未回写图谱**：`ZepGraphMemoryUpdater` 收尾报 `total_activities=0`，图谱仍是建图时的 11 节点/11 关系——模拟中产生的新事实不会自动进图谱（上游喂数据的 IPC 链路在本版本未生效）。
2. **`run_state.json` 计数不刷新**：`current_round / total_actions_count` 恒为 0，页面进度可能不准；真实进度看 `<simdir>/round_trace.log` 与 `twitter_simulation.db`。
3. **日志噪音**：`simulation.log` 偶发 `UnicodeEncodeError: surrogates not allowed`（Agent 文本含 emoji），只影响日志文本，不影响动作与报告。
