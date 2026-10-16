#!/usr/bin/env python3
"""给 SIMULATION MONITOR 加上"图谱写入/收尾"进度。

背景：模拟跑满轮次后进入 stopping（等图谱写入），此时前端只显示 running、
看不到收尾在推进，用户以为卡住了。

改动：
  1. backend/app/api/simulation.py
     run-status 里增返 graph_ingestion（更新器的实时统计：已发批次/条数、
     待写入 episode 数、失败批次数）——数据源是既有的 updater.get_stats()。
  2. frontend/src/components/Step3Simulation.vue
     - SIMULATION MONITOR 标题旁增加进度徽标（收尾中显示"还有 N 条待写入"）
     - 轮询到数值变化时写入日志，收尾过程可见
     - 徽标配色：进行中/失败/已完成
"""
from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path('/Users/youzi/Desktop/wzd/Usage/MiroFish')
API = ROOT/'backend/app/api/simulation.py'
VUE = ROOT/'frontend/src/components/Step3Simulation.vue'

EDITS = [
    (API,
     """                    "total_actions_count": 0,
                }
            })
        
        return jsonify({
            "success": True,
            "data": run_state.to_dict()
        })
""",
     """                    "total_actions_count": 0,
                }
            })
        
        payload = run_state.to_dict()
        # Patched: 把"图谱记忆写入 / 收尾"进度一并返回，供前端 SIMULATION MONITOR 展示。
        # 收尾(stopping)期间若只回 runner_status，前端只能看到 running、看不到进度在动。
        try:
            from ..services.zep_graph_memory_updater import ZepGraphMemoryManager

            updater = ZepGraphMemoryManager.get_updater(simulation_id)
            payload["graph_ingestion"] = updater.get_stats() if updater else None
        except Exception as error:  # noqa: BLE001
            logger.warning(f"获取图谱写入进度失败: {error}")
            payload["graph_ingestion"] = None
        return jsonify({
            "success": True,
            "data": payload
        })
"""),
    (VUE,
     """      <div class="log-header">
        <span class="log-title">SIMULATION MONITOR</span>
        <span class="log-id">{{ simulationId || 'NO_SIMULATION' }}</span>
      </div>
""",
     """      <div class="log-header">
        <span class="log-title">SIMULATION MONITOR</span>
        <span v-if="graphIngestText" class="log-ingest" :class="graphIngestClass">{{ graphIngestText }}</span>
        <span class="log-id">{{ simulationId || 'NO_SIMULATION' }}</span>
      </div>
"""),
    (VUE,
     """const isStopping = ref(false)
""",
     """const isStopping = ref(false)

// Patched: 图谱记忆写入 / 收尾进度（来自 run-status 的 graph_ingestion）。
// 目的：收尾(stopping)期间不再只看到 running，而是能看到"还有 N 条待写入"在减少。
const graphIngest = ref(null)
const lastPendingLogged = ref(-1)

const graphIngestText = computed(() => {
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

const graphIngestClass = computed(() => {
  const g = graphIngest.value
  if (!g) return ''
  if ((g.failed_count || 0) > 0) return 'fail'
  return (g.pending_episode_count || 0) > 0 ? 'pending' : 'done'
})
"""),
    (VUE,
     """      runStatus.value = data
""",
     """      runStatus.value = data

      // Patched: 同步图谱写入/收尾进度（数值变化才写日志，避免刷屏）
      if (data.graph_ingestion) {
        graphIngest.value = data.graph_ingestion
        const pendingNow = data.graph_ingestion.pending_episode_count || 0
        if (pendingNow !== lastPendingLogged.value) {
          lastPendingLogged.value = pendingNow
          if (pendingNow > 0) {
            addLog(`[图谱收尾] 还有 ${pendingNow} 条 episode 正在写入图谱…`)
          } else if (data.runner_status === 'stopping' || data.runner_status === 'completed') {
            addLog('[图谱收尾] 图谱写入完成 ✓')
          }
        }
      }
"""),
    (VUE,
     """  runStatus.value = {}
""",
     """  runStatus.value = {}
  graphIngest.value = null
  lastPendingLogged.value = -1
"""),
    (VUE,
     """.log-msg { color: #BBB; word-break: break-all; }
""",
     """.log-msg { color: #BBB; word-break: break-all; }

/* Patched: 图谱写入/收尾进度徽标 */
.log-ingest {
  margin-left: 12px;
  padding: 1px 8px;
  border: 1px solid #444;
  border-radius: 10px;
  font-size: 10px;
  color: #999;
  letter-spacing: 0.3px;
}
.log-ingest.pending { color: #e6b450; border-color: #6b5528; }
.log-ingest.fail { color: #e06c75; border-color: #6b3238; }
.log-ingest.done { color: #7ec699; border-color: #2f5c3f; }
"""),
]


def main() -> int:
    by_file: dict[pathlib.Path, str] = {}
    for path, old, new in EDITS:
        src = by_file.get(path) or path.read_text()
        if new.strip().splitlines()[0] in src and 'Patched' in new:
            if new in src or (new.split('\n')[1] in src and 'log-ingest' in src):
                print(f'[跳过] 已改过: {path.name}')
                continue
        n = src.count(old)
        if n != 1:
            print(f'[失败] {path.name} 锚点命中 {n} 次（应为 1）:\n   {old[:80]!r}')
            return 1
        by_file[path] = src.replace(old, new, 1)
    for path, src in by_file.items():
        backup = path.with_suffix(path.suffix + '.bak-monitor')
        if not backup.exists():
            backup.write_text(path.read_text())
        path.write_text(src)
        print(f'[已改写] {path.relative_to(ROOT)}（备份 {backup.name}）')
    return 0


if __name__ == '__main__':
    sys.exit(main())
