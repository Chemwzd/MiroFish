# MiroFish 怎么玩（本地版）

打开 <http://127.0.0.1:3000>（打不开就先 `./bin/start.sh`）。

一句话说明它干什么：**丢给它一段真实事件/新闻，它自动抽取关键主体 → 建成知识图谱 → 生成一批"网民/机构"智能体 → 让它们在微博式平台上互相影响地跑若干轮 → 给出舆情走向预测报告。**

---

## 一、四步主流程（含本机实测耗时）

### 第 1 步：生成图谱
1. 新建项目 → 粘贴文本（300~2000 字最好）。样例：`var/seed_sample.txt`。
2. **生成本体**：本机实测 **26s**，得到 10 个实体类型 + 10 个关系类型。
3. **构建图谱**：本机实测 **20s**，样例产出 **11 节点 / 11 关系**（实体如 武汉大学、张平文、教育部、微博…）。
   - 数据落在 `var/graph/zepshim.sqlite3`，全程本地。

### 第 2 步：配置模拟
1. 选平台（twitter 风格 = 微博；还有 reddit/小红书风格）。
2. 设**轮数**（★见第三节：轮数 = 模拟小时数）。
3. 点 **准备模拟**：LLM 为每个实体生成人设档案 + 双平台配置。本机实测 **约 2 分钟**（11 个人设）。

### 第 3 步：运行模拟
- Agent 按轮行动（发帖/评论/转发/点赞/关注…）。本机实测 Agent 真的在动：
  `like_post`、`quote_post`、`repost`、`follow`、`create_post`。
- 每轮会先刷新推荐系统（本地 `twhin-bert` 模型，首次加载会慢十几秒）。
- **轮数跑完后，模拟进程会停在"等待命令"模式**（等你访谈/生成报告），页面上仍显示 running —— 这是正常的，不是卡死。

### 第 4 步：生成报告
- **⚠️ 必须先让模拟进入终态**：报告接口在"模拟或图谱写入还在进行中"时会返回
  `409 Simulation or Zep graph ingestion is still active`。
  所以顺序是：**轮次跑完 → 停止模拟（等终态）→ 生成报告**。
- 报告 Agent 会**去本地图谱检索事实**（本机实测：检索 26 次，InsightForge 返回 22 条事实 / 11 实体 / 11 关系），再逐章写作。
- 本机实测：4 章、3~6 分钟；产物在 `backend/uploads/reports/<report_id>/`（`section_*.md`、`meta.json`、`outline.json`、`console_log.txt`）。

---

## 二、命令行端到端（已封装好，可直接验收）

```bash
cd /Users/youzi/Desktop/wzd/Usage/MiroFish

# 只有图谱链：本体 + 建图（约 1 分钟）
./bin/e2e_graph.py

# 完整链：建图 → prepare → 模拟 → 停止 → 报告
./bin/e2e_sim.py --rounds 12 --platform twitter
```

脚本已内置环境变量（`MIROFISH_URL`、`ZEPSHIM_URL`、`ZEPSHIM_API_KEY`），且会在**轮次跑满后自动 stop 再生成报告**，避免上面那个 409。

日志：`var/logs/e2e_graph.log`、`var/logs/e2e_sim*.log`。

---

## 三、★轮数 = 模拟小时数（最容易踩的坑）

- 每轮代表 **1 个模拟小时**（`minutes_per_round` 默认 60），第 N 轮对应 **第 N 小时 % 24**。
- 每个 Agent 有自己的 `active_hours`，例如 `[9,10,11,14,15,16,17]`；深夜时段乘数只有 0.05，直接用 0 个 Agent。
- 实测（12 轮）每轮选人情况：
  ```
  hour 0-5 : target=0, candidates=0        → 深夜，无人上线
  hour 6-7 : target>0, candidates=0        → 仍在上班前
  hour 8   : active=2  (Agent 4, 9)
  hour 9   : active=3
  hour 10  : active=2
  hour 11  : active=5
  ```
- 结论：**只跑 1~2 轮基本看不到动作**（正好是凌晨）。想看动态至少跑到第 9 轮之后；覆盖完整一天用 **24 轮**；默认任务配置为 72 小时 = 72 轮。

---

## 四、实操建议

| 事项 | 建议 |
|---|---|
| 输入文本 | 300~2000 字；太短主体少，太长建图慢且贵 |
| 模型 | `deepseek-flash`（LiteLLM 别名 → DeepSeek 官方；走本机 LiteLLM，最便宜），它是"先推理后回答"，慢但便宜 |
| 轮数 | 冒烟测试 12 轮；正式分析 24~72 轮 |
| 成本 | 主要花在 ①本体/建图抽取 ②prepare 人设 ③报告；模拟本身较轻 |
| 并发 | 一次只跑一个模拟（LiteLLM 上游 30 rpm 共享配额） |
| 图谱备份 | `cp var/graph/zepshim.sqlite3* ~/backup.bak`（WAL 模式三个文件一起） |
| 图谱重置 | `./bin/stop.sh && rm var/graph/zepshim.sqlite3* && ./bin/start.sh` |

---

## 五、已知限制（上游版本自身的问题，非本次部署引入）

**模拟期的"动作列表"和"图谱记忆回写"当前不生效。**

- 现象：界面动作列表为空；后端日志里记忆更新器停止时报告 `total_activities=0, items_sent=0`。
- 根因：`simulation_runner.py` 与 `zep_graph_memory_updater.py` 都读取
  `<sim_dir>/twitter/actions.jsonl`（还会用它检测 `simulation_end` 事件），
  但本仓库版本的 `backend/scripts/run_twitter_simulation.py` **并不生成这个文件**（实测模拟目录里只有 `log/`、`twitter_simulation.db`，没有 `twitter/` 目录）。
- 影响面：模拟过程本身正常（Agent 确实在发帖互动，可通过帖子/节点接口看到），报告也仍会检索图谱（建图阶段的数据）；只是"把模拟动态写回图谱"和界面动作流不可用。
- 需要的话可以补一个桥接：把 OASIS 的 `log/social.agent.log` + `twitter_simulation.db` 转成 `twitter/actions.jsonl`，即可让记忆回写和动作列表一起工作。

---

## 六、常见问题

| 现象 | 处理 |
|---|---|
| 模拟一直 running 但没动作 | 轮数没覆盖 Agent 活跃时段：轮数=小时数，Agent 多在 9-23 点活跃，至少 9 轮（建议 24 轮） |
| 报告 409 `still active` | 先停模拟并等 `run-status` 变成 `stopped/completed` 再生成报告 |
| 页面打开但接口全空 | 前端代理必须指 `127.0.0.1:5001`（见 DEPLOY.md 第 5 节第 3 条） |
| 建图 401 | 图谱服务鉴权头是 `Authorization: Api-Key <key>`（见 DEPLOY.md 第 5 节第 6 条） |
| 抽取结果为空 | `max_tokens` 太小（deepseek 先推理后回答），见 DEPLOY.md 第 5 节第 4 条 |
| 模拟阶段 import 报错 | 确认 `mcp==1.24.0`（DEPLOY.md 第 5 节第 5 条） |
| 首次模拟很慢 | 推荐系统要加载本地 `twhin-bert` 模型，首次十几秒属正常 |
