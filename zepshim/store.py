"""SQLite persistence for the local Zep-Cloud-compatible shim.

Single writer lock + WAL so the async extraction workers and the HTTP handlers
can share one database file safely.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS graphs (
    graph_id    TEXT PRIMARY KEY,
    name        TEXT,
    description TEXT,
    time_zone   TEXT,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ontologies (
    graph_id   TEXT PRIMARY KEY,
    payload    TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS episodes (
    uuid               TEXT PRIMARY KEY,
    graph_id           TEXT NOT NULL,
    content            TEXT,
    source_description TEXT,
    metadata           TEXT,
    source             TEXT DEFAULT 'text',
    role               TEXT,
    created_at         TEXT NOT NULL,
    processed          INTEGER DEFAULT 0,
    failed             INTEGER DEFAULT 0,
    error              TEXT
);
CREATE INDEX IF NOT EXISTS idx_episodes_graph ON episodes(graph_id, created_at);

CREATE TABLE IF NOT EXISTS nodes (
    uuid        TEXT PRIMARY KEY,
    graph_id    TEXT NOT NULL,
    name        TEXT NOT NULL,
    name_key    TEXT NOT NULL,
    labels      TEXT,
    summary     TEXT,
    attributes  TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_nodes_key ON nodes(graph_id, name_key);
CREATE INDEX IF NOT EXISTS idx_nodes_graph ON nodes(graph_id);

CREATE TABLE IF NOT EXISTS edges (
    uuid         TEXT PRIMARY KEY,
    graph_id     TEXT NOT NULL,
    name         TEXT NOT NULL,
    fact         TEXT,
    source_uuid  TEXT NOT NULL,
    target_uuid  TEXT NOT NULL,
    attributes   TEXT,
    created_at   TEXT NOT NULL,
    valid_at     TEXT,
    invalid_at   TEXT,
    expired_at   TEXT,
    episodes     TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_edges_key
    ON edges(graph_id, source_uuid, target_uuid, name);
CREATE INDEX IF NOT EXISTS idx_edges_graph ON edges(graph_id);

CREATE TABLE IF NOT EXISTS batches (
    batch_id     TEXT PRIMARY KEY,
    status       TEXT NOT NULL,
    metadata     TEXT,
    ignore_roles TEXT,
    item_count   INTEGER DEFAULT 0,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    processed_at TEXT,
    completed_at TEXT
);

CREATE TABLE IF NOT EXISTS batch_items (
    item_id          TEXT PRIMARY KEY,
    batch_id         TEXT NOT NULL,
    sequence_index   INTEGER NOT NULL,
    kind             TEXT,
    graph_id         TEXT,
    data             TEXT,
    data_type        TEXT,
    source_description TEXT,
    metadata         TEXT,
    status           TEXT NOT NULL,
    episode_uuid     TEXT,
    error            TEXT,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_items_batch ON batch_items(batch_id, sequence_index);

CREATE TABLE IF NOT EXISTS extraction_cache (
    cache_key  TEXT PRIMARY KEY,
    payload    TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _dumps(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False)


def _loads(value: Any, default: Any = None) -> Any:
    if value in (None, ""):
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


class Store:
    def __init__(self, path: str):
        self.path = path
        self._local = threading.local()
        self._write_lock = threading.RLock()
        directory = os.path.dirname(os.path.abspath(path))
        os.makedirs(directory, exist_ok=True)
        with self._write_lock:
            conn = self._conn()
            conn.executescript(SCHEMA)
            conn.commit()

    # ---------------------------------------------------------------- plumbing
    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, check_same_thread=False, timeout=30.0)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA busy_timeout=30000")
            conn.execute("PRAGMA foreign_keys=ON")
            self._local.conn = conn
        return conn

    def _write(self, sql: str, params: Iterable[Any] = ()) -> None:
        with self._write_lock:
            conn = self._conn()
            conn.execute(sql, tuple(params))
            conn.commit()

    def _fetchall(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        return self._conn().execute(sql, tuple(params)).fetchall()

    def _fetchone(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
        return self._conn().execute(sql, tuple(params)).fetchone()

    # ------------------------------------------------------------------ graphs
    def create_graph(self, graph_id: str, name: str | None, description: str | None,
                     time_zone: str | None = None) -> dict:
        created = now_iso()
        with self._write_lock:
            conn = self._conn()
            row = conn.execute(
                "SELECT graph_id FROM graphs WHERE graph_id=?", (graph_id,)
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO graphs(graph_id,name,description,time_zone,created_at)"
                    " VALUES(?,?,?,?,?)",
                    (graph_id, name, description, time_zone, created),
                )
            else:
                conn.execute(
                    "UPDATE graphs SET name=COALESCE(?,name),"
                    " description=COALESCE(?,description),"
                    " time_zone=COALESCE(?,time_zone) WHERE graph_id=?",
                    (name, description, time_zone, graph_id),
                )
            conn.commit()
        return self.get_graph(graph_id)

    def get_graph(self, graph_id: str) -> dict | None:
        row = self._fetchone("SELECT * FROM graphs WHERE graph_id=?", (graph_id,))
        if row is None:
            return None
        return {
            "graph_id": row["graph_id"],
            "uuid_": row["graph_id"],
            "name": row["name"],
            "description": row["description"],
            "time_zone": row["time_zone"],
            "created_at": row["created_at"],
        }

    def delete_graph(self, graph_id: str) -> bool:
        with self._write_lock:
            conn = self._conn()
            existed = conn.execute(
                "SELECT 1 FROM graphs WHERE graph_id=?", (graph_id,)
            ).fetchone() is not None
            for table in ("edges", "nodes", "episodes", "ontologies"):
                conn.execute(f"DELETE FROM {table} WHERE graph_id=?", (graph_id,))
            conn.execute("DELETE FROM graphs WHERE graph_id=?", (graph_id,))
            conn.commit()
        return existed

    # -------------------------------------------------------------- ontology
    def set_ontology(self, graph_ids: list[str], payload: dict) -> None:
        stamp = now_iso()
        with self._write_lock:
            conn = self._conn()
            for graph_id in graph_ids:
                conn.execute(
                    "INSERT INTO ontologies(graph_id,payload,updated_at) VALUES(?,?,?)"
                    " ON CONFLICT(graph_id) DO UPDATE SET payload=excluded.payload,"
                    " updated_at=excluded.updated_at",
                    (graph_id, _dumps(payload), stamp),
                )
            conn.commit()

    def get_ontology(self, graph_id: str | None) -> dict:
        if not graph_id:
            return {}
        row = self._fetchone("SELECT payload FROM ontologies WHERE graph_id=?", (graph_id,))
        if row is None:
            return {}
        return _loads(row["payload"], {}) or {}

    # -------------------------------------------------------------- episodes
    def add_episode(self, *, graph_id: str, content: str, source_description: str | None,
                    metadata: dict | None, source: str = "text", role: str | None = None,
                    created_at: str | None = None, uuid_: str | None = None,
                    processed: bool = False) -> dict:
        episode_uuid = uuid_ or f"ep_{os.urandom(12).hex()}"
        stamp = created_at or now_iso()
        self._write(
            "INSERT OR REPLACE INTO episodes(uuid,graph_id,content,source_description,"
            "metadata,source,role,created_at,processed,failed) VALUES(?,?,?,?,?,?,?,?,?,0)",
            (episode_uuid, graph_id, content, source_description, _dumps(metadata),
             source, role, stamp, 1 if processed else 0),
        )
        return self.get_episode(episode_uuid) or {}

    def get_episode(self, uuid_: str) -> dict | None:
        row = self._fetchone("SELECT * FROM episodes WHERE uuid=?", (uuid_,))
        if row is None:
            return None
        return {
            "uuid_": row["uuid"],
            "uuid": row["uuid"],
            "content": row["content"] or "",
            "created_at": row["created_at"],
            "processed": bool(row["processed"]),
            "metadata": _loads(row["metadata"], {}) or {},
            "source_description": row["source_description"],
            "source": row["source"] or "text",
            "role": row["role"],
            "role_type": row["role"] or "norole",
            "score": None,
            "relevance": None,
        }

    def list_episodes(self, graph_id: str, lastn: int | None = None) -> list[dict]:
        sql = "SELECT uuid FROM episodes WHERE graph_id=? ORDER BY created_at ASC, rowid ASC"
        rows = self._fetchall(sql, (graph_id,))
        uuids = [r["uuid"] for r in rows]
        if lastn is not None and lastn > 0:
            uuids = uuids[-lastn:]
        return [self.get_episode(u) for u in uuids]

    def mark_episode_processed(self, uuid_: str, *, failed: bool = False,
                               error: str | None = None) -> None:
        self._write(
            "UPDATE episodes SET processed=1, failed=?, error=? WHERE uuid=?",
            (1 if failed else 0, error, uuid_),
        )

    def pending_episode_uuids(self) -> list[str]:
        rows = self._fetchall("SELECT uuid FROM episodes WHERE processed=0 ORDER BY rowid")
        return [r["uuid"] for r in rows]

    def pending_episodes(self) -> list[dict]:
        """所有尚未处理完的 episode（含 graph_id 与正文），供启动时恢复入队。"""
        rows = self._fetchall(
            "SELECT uuid, graph_id, content FROM episodes WHERE processed=0 ORDER BY rowid"
        )
        return [
            {"uuid_": r["uuid"], "graph_id": r["graph_id"], "content": r["content"] or ""}
            for r in rows
        ]

    def episode_stats(self, graph_id: str) -> dict:
        """按图统计 episode 处理情况（MiroFish 用它显示"图谱收尾进度"）。"""
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

    # ----------------------------------------------------------------- nodes
    def upsert_node(self, *, graph_id: str, name: str, name_key: str, labels: list[str],
                    summary: str, attributes: dict, created_at: str | None = None) -> tuple[str, bool]:
        stamp = now_iso()
        with self._write_lock:
            conn = self._conn()
            row = conn.execute(
                "SELECT * FROM nodes WHERE graph_id=? AND name_key=?", (graph_id, name_key)
            ).fetchone()
            if row is None:
                uuid_ = f"n_{os.urandom(12).hex()}"
                conn.execute(
                    "INSERT INTO nodes(uuid,graph_id,name,name_key,labels,summary,attributes,"
                    "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (uuid_, graph_id, name, name_key, _dumps(labels), summary,
                     _dumps(attributes), created_at or stamp, stamp),
                )
                conn.commit()
                return uuid_, True

            merged_labels = sorted({*(_loads(row["labels"], []) or []), *(labels or [])})
            existing_attributes = _loads(row["attributes"], {}) or {}
            for key, value in (attributes or {}).items():
                if value not in (None, "", [], {}) and existing_attributes.get(key) in (None, "", [], {}):
                    existing_attributes[key] = value
            new_summary = row["summary"] or ""
            if summary and len(summary) > len(new_summary):
                new_summary = summary
            conn.execute(
                "UPDATE nodes SET name=?, labels=?, summary=?, attributes=?, updated_at=?"
                " WHERE uuid=?",
                (row["name"] or name, _dumps(merged_labels), new_summary,
                 _dumps(existing_attributes), stamp, row["uuid"]),
            )
            conn.commit()
            return row["uuid"], False

    def node_by_key(self, graph_id: str, name_key: str) -> dict | None:
        row = self._fetchone(
            "SELECT * FROM nodes WHERE graph_id=? AND name_key=?", (graph_id, name_key)
        )
        return self._node_row(row) if row else None

    def get_node(self, uuid_: str) -> dict | None:
        row = self._fetchone("SELECT * FROM nodes WHERE uuid=?", (uuid_,))
        return self._node_row(row) if row else None

    @staticmethod
    def _node_row(row: sqlite3.Row) -> dict:
        return {
            "uuid_": row["uuid"],
            "uuid": row["uuid"],
            "name": row["name"],
            "labels": _loads(row["labels"], []) or [],
            "summary": row["summary"] or "",
            "attributes": _loads(row["attributes"], {}) or {},
            "created_at": row["created_at"],
        }

    def list_nodes(self, graph_id: str, *, limit: int, offset: int) -> list[dict]:
        rows = self._fetchall(
            "SELECT * FROM nodes WHERE graph_id=? ORDER BY created_at ASC, rowid ASC"
            " LIMIT ? OFFSET ?",
            (graph_id, limit, offset),
        )
        return [self._node_row(r) for r in rows]

    def all_nodes(self, graph_id: str) -> list[dict]:
        rows = self._fetchall(
            "SELECT * FROM nodes WHERE graph_id=? ORDER BY created_at ASC, rowid ASC", (graph_id,)
        )
        return [self._node_row(r) for r in rows]

    def count_nodes(self, graph_id: str) -> int:
        row = self._fetchone("SELECT COUNT(*) AS c FROM nodes WHERE graph_id=?", (graph_id,))
        return int(row["c"]) if row else 0

    # ----------------------------------------------------------------- edges
    def upsert_edge(self, *, graph_id: str, name: str, fact: str, source_uuid: str,
                    target_uuid: str, attributes: dict, episode_uuid: str | None,
                    created_at: str | None = None, valid_at: str | None = None) -> tuple[str, bool]:
        stamp = now_iso()
        with self._write_lock:
            conn = self._conn()
            row = conn.execute(
                "SELECT * FROM edges WHERE graph_id=? AND source_uuid=? AND target_uuid=? AND name=?",
                (graph_id, source_uuid, target_uuid, name),
            ).fetchone()
            if row is None:
                uuid_ = f"e_{os.urandom(12).hex()}"
                conn.execute(
                    "INSERT INTO edges(uuid,graph_id,name,fact,source_uuid,target_uuid,attributes,"
                    "created_at,valid_at,invalid_at,expired_at,episodes) VALUES(?,?,?,?,?,?,?,?,?,NULL,NULL,?)",
                    (uuid_, graph_id, name, fact, source_uuid, target_uuid, _dumps(attributes),
                     created_at or stamp, valid_at or created_at or stamp,
                     _dumps([episode_uuid] if episode_uuid else [])),
                )
                conn.commit()
                return uuid_, True

            existing_fact = row["fact"] or ""
            merged_fact = existing_fact
            if fact and fact.strip() and fact.strip() not in existing_fact:
                merged_fact = f"{existing_fact}; {fact}".strip("; ") if existing_fact else fact
            episodes = _loads(row["episodes"], []) or []
            if episode_uuid and episode_uuid not in episodes:
                episodes.append(episode_uuid)
            existing_attributes = _loads(row["attributes"], {}) or {}
            for key, value in (attributes or {}).items():
                if value not in (None, "", [], {}) and existing_attributes.get(key) in (None, "", [], {}):
                    existing_attributes[key] = value
            conn.execute(
                "UPDATE edges SET fact=?, attributes=?, episodes=? WHERE uuid=?",
                (merged_fact, _dumps(existing_attributes), _dumps(episodes), row["uuid"]),
            )
            conn.commit()
            return row["uuid"], False

    @staticmethod
    def _edge_row(row: sqlite3.Row) -> dict:
        return {
            "uuid_": row["uuid"],
            "uuid": row["uuid"],
            "name": row["name"],
            "fact": row["fact"] or "",
            "source_node_uuid": row["source_uuid"],
            "target_node_uuid": row["target_uuid"],
            "attributes": _loads(row["attributes"], {}) or {},
            "created_at": row["created_at"],
            "valid_at": row["valid_at"],
            "invalid_at": row["invalid_at"],
            "expired_at": row["expired_at"],
            "episodes": _loads(row["episodes"], []) or [],
        }

    def get_edge(self, uuid_: str) -> dict | None:
        row = self._fetchone("SELECT * FROM edges WHERE uuid=?", (uuid_,))
        return self._edge_row(row) if row else None

    def list_edges(self, graph_id: str, *, limit: int, offset: int) -> list[dict]:
        rows = self._fetchall(
            "SELECT * FROM edges WHERE graph_id=? ORDER BY created_at ASC, rowid ASC"
            " LIMIT ? OFFSET ?",
            (graph_id, limit, offset),
        )
        return [self._edge_row(r) for r in rows]

    def all_edges(self, graph_id: str) -> list[dict]:
        rows = self._fetchall(
            "SELECT * FROM edges WHERE graph_id=? ORDER BY created_at ASC, rowid ASC", (graph_id,)
        )
        return [self._edge_row(r) for r in rows]

    def count_edges(self, graph_id: str) -> int:
        row = self._fetchone("SELECT COUNT(*) AS c FROM edges WHERE graph_id=?", (graph_id,))
        return int(row["c"]) if row else 0

    def node_edges(self, node_uuid: str) -> list[dict]:
        rows = self._fetchall(
            "SELECT * FROM edges WHERE source_uuid=? OR target_uuid=?"
            " ORDER BY created_at ASC, rowid ASC",
            (node_uuid, node_uuid),
        )
        return [self._edge_row(r) for r in rows]

    # --------------------------------------------------------------- batches
    def create_batch(self, *, metadata: dict | None, ignore_roles: list | None) -> dict:
        batch_id = f"b_{os.urandom(12).hex()}"
        stamp = now_iso()
        self._write(
            "INSERT INTO batches(batch_id,status,metadata,ignore_roles,item_count,created_at,"
            "updated_at) VALUES(?,?,?,?,0,?,?)",
            (batch_id, "draft", _dumps(metadata), _dumps(ignore_roles), stamp, stamp),
        )
        return self.get_batch(batch_id) or {}

    def get_batch(self, batch_id: str) -> dict | None:
        row = self._fetchone("SELECT * FROM batches WHERE batch_id=?", (batch_id,))
        return self._batch_row(row) if row else None

    def _batch_row(self, row: sqlite3.Row) -> dict:
        grouped: dict[str, int] = {}
        rows = self._fetchall(
            "SELECT status, COUNT(*) AS c FROM batch_items WHERE batch_id=? GROUP BY status",
            (row["batch_id"],),
        )
        for item in rows:
            grouped[item["status"]] = int(item["c"])
        total = row["item_count"] or sum(grouped.values())
        succeeded = grouped.get("succeeded", 0)
        failed = grouped.get("failed", 0)
        canceled = grouped.get("canceled", 0)
        skipped = grouped.get("skipped", 0)
        processing = grouped.get("processing", 0)
        queued = grouped.get("queued", 0) + grouped.get("pending", 0)
        percent = (100.0 * (succeeded + failed + canceled + skipped) / total) if total else 0.0
        return {
            "batch_id": row["batch_id"],
            "status": row["status"],
            "metadata": _loads(row["metadata"], {}) or {},
            "ignore_roles": _loads(row["ignore_roles"], []) or [],
            "item_count": total,
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "processed_at": row["processed_at"],
            "completed_at": row["completed_at"],
            "progress": {
                "total_items": total,
                "succeeded_items": succeeded,
                "failed_items": failed,
                "canceled_items": canceled,
                "skipped_items": skipped,
                "processing_items": processing,
                "queued_items": queued,
                "percent_complete": round(percent, 2),
            },
        }

    def list_batches(self, *, limit: int, offset: int, status: str | None) -> list[dict]:
        if status:
            rows = self._fetchall(
                "SELECT * FROM batches WHERE status=? ORDER BY created_at ASC, rowid ASC"
                " LIMIT ? OFFSET ?",
                (status, limit, offset),
            )
        else:
            rows = self._fetchall(
                "SELECT * FROM batches ORDER BY created_at ASC, rowid ASC LIMIT ? OFFSET ?",
                (limit, offset),
            )
        return [self._batch_row(r) for r in rows]

    def count_batches(self, status: str | None = None) -> int:
        if status:
            row = self._fetchone("SELECT COUNT(*) AS c FROM batches WHERE status=?", (status,))
        else:
            row = self._fetchone("SELECT COUNT(*) AS c FROM batches")
        return int(row["c"]) if row else 0

    def add_batch_items(self, batch_id: str, items: list[dict]) -> list[dict]:
        stamp = now_iso()
        created: list[dict] = []
        with self._write_lock:
            conn = self._conn()
            row = conn.execute(
                "SELECT COALESCE(MAX(sequence_index),-1) AS m FROM batch_items WHERE batch_id=?",
                (batch_id,),
            ).fetchone()
            next_index = int(row["m"]) + 1
            for offset, item in enumerate(items):
                item_id = f"it_{os.urandom(10).hex()}"
                episode_uuid = item.get("episode_uuid")
                sequence_index = next_index + offset
                conn.execute(
                    "INSERT INTO batch_items(item_id,batch_id,sequence_index,kind,graph_id,data,"
                    "data_type,source_description,metadata,status,episode_uuid,error,"
                    "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,NULL,?,?)",
                    (item_id, batch_id, sequence_index, item.get("kind"), item.get("graph_id"),
                     item.get("data"), item.get("data_type"), item.get("source_description"),
                     _dumps(item.get("metadata")), "pending", episode_uuid, stamp, stamp),
                )
                created.append({
                    "item_id": item_id,
                    "batch_id": batch_id,
                    "sequence_index": sequence_index,
                    "kind": item.get("kind") or "graph_episode",
                    "graph_id": item.get("graph_id"),
                    "episode_uuid": episode_uuid,
                    "source_uuid": episode_uuid,
                    "thread_id": item.get("thread_id"),
                    "user_id": item.get("user_id"),
                    "user_uuid": None,
                    "graph_uuid": item.get("graph_id"),
                    "status": "pending",
                    "error": None,
                    "created_at": stamp,
                    "updated_at": stamp,
                })
            conn.execute(
                "UPDATE batches SET item_count=item_count+?, updated_at=? WHERE batch_id=?",
                (len(items), stamp, batch_id),
            )
            conn.commit()
        return created

    def list_batch_items(self, batch_id: str, *, limit: int, offset: int,
                         status: str | None) -> list[dict]:
        if status:
            rows = self._fetchall(
                "SELECT * FROM batch_items WHERE batch_id=? AND status=? ORDER BY sequence_index"
                " LIMIT ? OFFSET ?",
                (batch_id, status, limit, offset),
            )
        else:
            rows = self._fetchall(
                "SELECT * FROM batch_items WHERE batch_id=? ORDER BY sequence_index"
                " LIMIT ? OFFSET ?",
                (batch_id, limit, offset),
            )
        return [self._item_row(r) for r in rows]

    def all_batch_items(self, batch_id: str) -> list[dict]:
        rows = self._fetchall(
            "SELECT * FROM batch_items WHERE batch_id=? ORDER BY sequence_index", (batch_id,)
        )
        return [self._item_row(r) for r in rows]

    def batch_items_for_processing(self, batch_id: str) -> list[dict]:
        """Internal view of batch items: includes payload fields the API hides."""

        rows = self._fetchall(
            "SELECT * FROM batch_items WHERE batch_id=? ORDER BY sequence_index", (batch_id,)
        )
        return [
            {
                "item_id": row["item_id"],
                "batch_id": row["batch_id"],
                "graph_id": row["graph_id"],
                "kind": row["kind"] or "graph_episode",
                "data": row["data"] or "",
                "metadata": _loads(row["metadata"], {}) or {},
                "episode_uuid": row["episode_uuid"],
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    @staticmethod
    def _item_row(row: sqlite3.Row) -> dict:
        return {
            "item_id": row["item_id"],
            "batch_id": row["batch_id"],
            "sequence_index": row["sequence_index"],
            "kind": row["kind"],
            "graph_id": row["graph_id"],
            "episode_uuid": row["episode_uuid"],
            "source_uuid": row["episode_uuid"],
            "thread_id": None,
            "user_id": None,
            "user_uuid": None,
            "graph_uuid": row["graph_id"],
            "status": row["status"],
            "error": _loads(row["error"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def set_batch_status(self, batch_id: str, status: str, *, processed: bool = False,
                         completed: bool = False) -> None:
        stamp = now_iso()
        self._write(
            "UPDATE batches SET status=?, updated_at=?,"
            " processed_at=CASE WHEN ? THEN ? ELSE processed_at END,"
            " completed_at=CASE WHEN ? THEN ? ELSE completed_at END WHERE batch_id=?",
            (status, stamp, 1 if processed else 0, stamp,
             1 if completed else 0, stamp, batch_id),
        )

    def set_item_status(self, item_id: str, status: str, *, error: dict | None = None) -> None:
        self._write(
            "UPDATE batch_items SET status=?, error=?, updated_at=? WHERE item_id=?",
            (status, _dumps(error), now_iso(), item_id),
        )

    # ------------------------------------------------------- extraction cache
    def cache_get(self, cache_key: str) -> dict | None:
        row = self._fetchone(
            "SELECT payload FROM extraction_cache WHERE cache_key=?", (cache_key,)
        )
        if row is None:
            return None
        return _loads(row["payload"], {}) or {}

    def cache_put(self, cache_key: str, payload: dict) -> None:
        self._write(
            "INSERT OR REPLACE INTO extraction_cache(cache_key,payload,created_at) VALUES(?,?,?)",
            (cache_key, _dumps(payload), now_iso()),
        )
