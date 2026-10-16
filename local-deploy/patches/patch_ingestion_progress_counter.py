#!/usr/bin/env python3
"""让"图谱收尾进度"真正会动：把 zepshim 侧的 episode 处理计数接到监控徽标。

背景：updater 的 pending 列表在收尾时可能几分钟不变（要等一轮确认），
前端看着还是"不动"。zepshim 侧的 processed 计数会随每条 episode 抽取完成而增长，
是最真实的进度信号。

改动：
  1. zepshim/store.py：新增 episode_stats(graph_id) —— total/processed/pending/failed
  2. zepshim/app.py：新增 GET /api/v2/graph/{graph_id}/ingestion
  3. backend/app/api/simulation.py：run-status 里把该统计并入 graph_ingestion.graph_db
     （1.5s 超时 + 3s 缓存，失败静默）
  4. frontend/src/components/Step3Simulation.vue：徽标显示"图谱入库 M/N"，并按 5 条一档写日志
"""
from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path('/Users/youzi/Desktop/wzd/Usage/MiroFish')
STORE = ROOT/'zepshim/store.py'
SHIM_APP = ROOT/'zepshim/app.py'
API = ROOT/'backend/app/api/simulation.py'
VUE = ROOT/'frontend/src/components/Step3Simulation.vue'

STORE_ANCHOR = """    def pending_episodes(self) -> list[dict]:
        \"\"\"所有尚未处理完的 episode（含 graph_id 与正文），供启动时恢复入队。\"\"\"
        rows = self._fetchall(
            "SELECT uuid, graph_id, content FROM episodes WHERE processed=0 ORDER BY rowid"
        )
        return [
            {"uuid_": r["uuid"], "graph_id": r["graph_id"], "content": r["content"] or ""}
            for r in rows
        ]
"""
STORE_NEW = STORE_ANCHOR + """
    def episode_stats(self, graph_id: str) -> dict:
        \"\"\"按图统计 episode 处理情况（MiroFish 用它显示"图谱收尾进度"）。\"\"\"
        row = self._fetchone(
            "SELECT COUNT(*) AS total,"
            " SUM(CASE WHEN processed=1 THEN 1 ELSE 0 END) AS processed,"
            " SUM(CASE WHEN processed=0 THEN 1 ELSE 0 END) AS pending,"
            " SUM(CASE WHEN failed=1 THEN 1 ELSE 0 END) AS failed"
            " FROM episodes WHERE graph_id=?",
            (graph_id,),
        )
        return {
            "total": (row["total"] if row else 0) or 0,
            "processed": (row["processed"] if row else 0) or 0,
            "pending": (row["pending"] if row else 0) or 0,
            "failed": (row["failed"] if row else 0) or 0,
        }
"""

SHIM_ANCHOR = """@app.exception_handler(HTTPException)
"""
SHIM_NEW = '''@app.get("/api/v2/graph/{graph_id}/ingestion")
async def graph_ingestion(
    graph_id: str,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="x-api-key"),
) -> dict:
    """该图的 episode 处理统计（供 MiroFish 展示"图谱收尾进度"）。"""

    _check_auth(authorization, x_api_key)
    if store.get_graph(graph_id) is None:
        raise _not_found(f"graph {graph_id} not found")
    return store.episode_stats(graph_id)


@app.exception_handler(HTTPException)
'''

API_ANCHOR = """@simulation_bp.route('/<simulation_id>/run-status', methods=['GET'])
"""
API_NEW = '''_GRAPH_DB_STATS_CACHE: dict[str, tuple[float, dict]] = {}


def _shim_graph_ingestion(graph_id: str | None, ttl: float = 3.0) -> dict | None:
    """向本地 zepshim 查该图的 episode 处理统计（短超时 + 短缓存，失败静默返回 None）。

    Patched: updater 的 pending 列表在收尾时可能长时间不变，zepshim 侧 processed
    计数才是"看得见在动"的进度信号。
    """

    if not graph_id:
        return None
    cached = _GRAPH_DB_STATS_CACHE.get(graph_id)
    if cached and (time.time() - cached[0]) < ttl:
        return cached[1]
    try:
        import httpx

        from ..config import Config
        from ..utils.zep import ZEP_CLOUD_BASE_URL

        url = f"{ZEP_CLOUD_BASE_URL.rstrip('/')}/graph/{graph_id}/ingestion"
        response = httpx.get(
            url,
            headers={"Authorization": f"Api-Key {Config.ZEP_API_KEY or ''}"},
            timeout=1.5,
        )
        if response.status_code == 200:
            stats = response.json()
            _GRAPH_DB_STATS_CACHE[graph_id] = (time.time(), stats)
            return stats
    except Exception:  # noqa: BLE001
        return None
    return None


@simulation_bp.route('/<simulation_id>/run-status', methods=['GET'])
'''

API_PAYLOAD_ANCHOR = """            updater = ZepGraphMemoryManager.get_updater(simulation_id)
            payload["graph_ingestion"] = updater.get_stats() if updater else None
"""
API_PAYLOAD_NEW = """            updater = ZepGraphMemoryManager.get_updater(simulation_id)
            if updater is None:
                payload["graph_ingestion"] = None
            else:
                stats = updater.get_stats()
                db_stats = _shim_graph_ingestion(getattr(updater, "graph_id", None))
                if db_stats:
                    stats["graph_db"] = db_stats
                payload["graph_ingestion"] = stats
"""

VUE_TEXT_ANCHOR = """const graphIngestText = computed(() => {
  const g = graphIngest.value
  if (!g) return ''
  const parts = []
  const pending = g.pending_episode_count || 0
  if (pending > 0) {
    parts.push(`图谱收尾 ${pending} 条待写入`)
  } else {
    parts.push(`图谱已写入 ${g.items_sent || 0} 条 / ${g.batches_sent || 0} 批`)
  }
  if ((g.failed_count || 0) > 0) parts.push(`失败 ${g.failed_count} 批`)
  return parts.join(' · ')
})
"""
VUE_TEXT_NEW = """const graphIngestText = computed(() => {
  const g = graphIngest.value
  if (!g) return ''
  const db = g.graph_db || {}
  const pending = g.pending_episode_count || 0
  const parts = []
  if (db.total) parts.push(`图谱入库 ${db.processed || 0}/${db.total}`)
  if (pending > 0) {
    parts.push(`待写入 ${pending} 条`)
  } else {
    parts.push(`已写入 ${g.items_sent || 0} 条 / ${g.batches_sent || 0} 批`)
  }
  const failed = Math.max(g.failed_count || 0, db.failed || 0)
  if (failed > 0) parts.push(`失败 ${failed}`)
  return parts.join(' · ')
})
"""

VUE_POLL_ANCHOR = """        const pendingNow = data.graph_ingestion.pending_episode_count || 0
        if (pendingNow !== lastPendingLogged.value) {
          lastPendingLogged.value = pendingNow
          if (pendingNow > 0) {
            addLog(`[图谱收尾] 还有 ${pendingNow} 条 episode 正在写入图谱…`)
          } else if (data.runner_status === 'stopping' || data.runner_status === 'completed') {
            addLog('[图谱收尾] 图谱写入完成 ✓')
          }
        }
"""
VUE_POLL_NEW = """        const pendingNow = data.graph_ingestion.pending_episode_count || 0
        const dbStats = data.graph_ingestion.graph_db || {}
        // 每 5 条入库记一次，既能看出"在动"又不刷屏
        const processedBucket = Math.floor((dbStats.processed || 0) / 5) * 5
        if (pendingNow !== lastPendingLogged.value || processedBucket !== lastProcessedLogged.value) {
          if (processedBucket !== lastProcessedLogged.value) lastProcessedLogged.value = processedBucket
          lastPendingLogged.value = pendingNow
          if (pendingNow > 0 && dbStats.total) {
            addLog(`[图谱收尾] 已入库 ${dbStats.processed || 0}/${dbStats.total} 条，待写入 ${pendingNow} 条…`)
          } else if (pendingNow > 0) {
            addLog(`[图谱收尾] 还有 ${pendingNow} 条 episode 正在写入图谱…`)
          } else if (data.runner_status === 'stopping' || data.runner_status === 'completed') {
            addLog('[图谱收尾] 图谱写入完成 ✓')
          }
        }
"""

VUE_REF_ANCHOR = """const graphIngest = ref(null)
const lastPendingLogged = ref(-1)
"""
VUE_REF_NEW = """const graphIngest = ref(null)
const lastPendingLogged = ref(-1)
const lastProcessedLogged = ref(-1)
"""

VUE_RESET_ANCHOR = """  graphIngest.value = null
  lastPendingLogged.value = -1
"""
VUE_RESET_NEW = """  graphIngest.value = null
  lastPendingLogged.value = -1
  lastProcessedLogged.value = -1
"""

API_TIME_ANCHOR = 'import os\nimport traceback\n'
API_TIME_NEW = 'import os\nimport time\nimport traceback\n'

EDITS = [
    (API, API_TIME_ANCHOR, API_TIME_NEW, 'import time\nimport traceback'),
    (STORE, STORE_ANCHOR, STORE_NEW, 'def episode_stats'),
    (SHIM_APP, SHIM_ANCHOR, SHIM_NEW, '/ingestion'),
    (API, API_ANCHOR, API_NEW, '_shim_graph_ingestion'),
    (API, API_PAYLOAD_ANCHOR, API_PAYLOAD_NEW, 'stats["graph_db"]'),
    (VUE, VUE_REF_ANCHOR, VUE_REF_NEW, 'lastProcessedLogged'),
    (VUE, VUE_TEXT_ANCHOR, VUE_TEXT_NEW, '图谱入库'),
    (VUE, VUE_POLL_ANCHOR, VUE_POLL_NEW, '已入库 ${dbStats.processed'),
    (VUE, VUE_RESET_ANCHOR, VUE_RESET_NEW, 'lastProcessedLogged.value = -1'),
]


def main() -> int:
    srcs: dict[pathlib.Path, str] = {}
    for path, old, new, marker in EDITS:
        src = srcs.get(path, path.read_text())
        if marker in src and marker not in ('_shim_graph_ingestion', 'import time\nimport traceback'):
            print(f'[已有] {path.name}: {marker}')
            srcs[path] = src
            continue
        if src.count(old) != 1:
            print(f'[失败] {path.name} 锚点命中 {src.count(old)} 次: {old[:60]!r}')
            return 1
        srcs[path] = src.replace(old, new, 1)
        print(f'[已改] {path.name}: {marker}')
    for path, src in srcs.items():
        backup = path.with_suffix(path.suffix + '.bak-ingestprog')
        if not backup.exists():
            backup.write_text(path.read_text())
        path.write_text(src)
    return 0


if __name__ == '__main__':
    sys.exit(main())
