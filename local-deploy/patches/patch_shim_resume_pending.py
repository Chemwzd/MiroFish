#!/usr/bin/env python3
"""让 zepshim 在启动时把"排队未开始"的 episode 重新入队。

问题：抽取任务只活在进程内线程池里。zepshim 一重启，排队中的任务就丢了，
这些 episode 永远停在 processed=0；而 MiroFish 的收尾（等图谱写入完成）
会一直轮询它们，直到超时并报 failed。

改动：
  store.py  新增 pending_episodes()（带 graph_id，供恢复用）
  app.py    新增 _resume_pending_episodes()，在 __main__ 启动 uvicorn 前调用
"""
from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path('/Users/youzi/Desktop/wzd/Usage/MiroFish/zepshim')
STORE = ROOT / 'store.py'
APP = ROOT / 'app.py'

STORE_ANCHOR = """    def pending_episode_uuids(self) -> list[str]:
        rows = self._fetchall("SELECT uuid FROM episodes WHERE processed=0 ORDER BY rowid")
        return [r["uuid"] for r in rows]
"""
STORE_NEW = STORE_ANCHOR + """
    def pending_episodes(self) -> list[dict]:
        \"\"\"所有尚未处理完的 episode（含 graph_id 与正文），供启动时恢复入队。\"\"\"
        rows = self._fetchall(
            "SELECT uuid, graph_id, content FROM episodes WHERE processed=0 ORDER BY rowid"
        )
        return [
            {"uuid_": r["uuid"], "graph_id": r["graph_id"], "content": r["content"] or ""}
            for r in rows
        ]
"""

APP_ANCHOR = """if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=os.environ.get("ZEPSHIM_HOST", "0.0.0.0"),
                port=int(os.environ.get("ZEPSHIM_PORT", "8100")))
"""
APP_NEW = '''def _resume_pending_episodes() -> int:
    """启动时把上次排队未开始（processed=0）的 episode 重新入队。

    抽取任务只存在于进程内线程池：重启会丢掉排队中的任务，这些 episode 会
    永远停在 processed=0，而 MiroFish 的收尾会一直等它们直到超时（failed）。
    """
    try:
        pending = store.pending_episodes()
    except Exception as error:  # noqa: BLE001
        logger.warning("恢复未处理 episode 失败: %s", error)
        return 0
    for item in pending:
        pool.submit(ingest_episode, item["graph_id"], item["uuid_"], item["content"])
    if pending:
        logger.info("已重新入队 %d 条未处理的 episode", len(pending))
    return len(pending)


if __name__ == "__main__":
    import uvicorn

    _resume_pending_episodes()
    uvicorn.run(app, host=os.environ.get("ZEPSHIM_HOST", "0.0.0.0"),
                port=int(os.environ.get("ZEPSHIM_PORT", "8100")))
'''


def patch(path: pathlib.Path, anchor: str, new: str, marker: str) -> bool:
    src = path.read_text()
    if marker in src:
        print(f'[已打过] {path.name}')
        return True
    if src.count(anchor) != 1:
        print(f'[失败] {path.name} 锚点命中 {src.count(anchor)} 次')
        return False
    backup = path.with_suffix(path.suffix + '.bak-resume')
    if not backup.exists():
        backup.write_text(path.read_text())
    path.write_text(src.replace(anchor, new, 1))
    print(f'[已改写] {path.name}（备份 {backup.name}）')
    return True


if __name__ == '__main__':
    ok = patch(STORE, STORE_ANCHOR, STORE_NEW, 'def pending_episodes') and \
         patch(APP, APP_ANCHOR, APP_NEW, '_resume_pending_episodes')
    sys.exit(0 if ok else 1)
