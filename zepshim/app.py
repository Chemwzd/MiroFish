"""A local, Zep-Cloud-compatible HTTP service.

Implements the subset of the Zep Cloud v2 REST contract that MiroFish actually
calls, backed by SQLite plus an OpenAI-compatible LLM for entity/edge
extraction. Run it next to MiroFish and point the Zep client at this service:

    MIROFISH_ZEP_BASE_URL=http://127.0.0.1:8100/api/v2
"""

from __future__ import annotations

import concurrent.futures
import hashlib
import json
import logging
import os
import re
import threading
from typing import Any

from fastapi import Body, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from extract import (
    LLMClient,
    extraction_cache_key,
    extract as run_extraction,
    normalize_extraction,
)
from search import build_search_response
from store import Store, now_iso

logging.basicConfig(
    level=os.environ.get("ZEPSHIM_LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("zepshim")

DB_PATH = os.environ.get("ZEPSHIM_DB", os.path.join(os.path.dirname(__file__), "data", "zepshim.sqlite3"))
LLM_WORKERS = int(os.environ.get("ZEPSHIM_LLM_WORKERS", "4"))
FAIL_OPEN = os.environ.get("ZEPSHIM_FAIL_OPEN", "1") not in {"0", "false", "False"}
MAX_ENTITIES = int(os.environ.get("ZEPSHIM_MAX_ENTITIES", "30"))
API_KEY = os.environ.get("ZEPSHIM_API_KEY", "").strip()

store = Store(DB_PATH)
llm = LLMClient(
    base_url=os.environ.get("ZEPSHIM_LLM_BASE_URL", ""),
    api_key=os.environ.get("ZEPSHIM_LLM_API_KEY", "not-needed"),
    model=os.environ.get("ZEPSHIM_LLM_MODEL", ""),
    timeout=float(os.environ.get("ZEPSHIM_LLM_TIMEOUT", "180")),
    max_retries=int(os.environ.get("ZEPSHIM_LLM_RETRIES", "3")),
)

pool = concurrent.futures.ThreadPoolExecutor(max_workers=LLM_WORKERS, thread_name_prefix="zepshim-llm")
_batch_tracker: dict[str, set[concurrent.futures.Future]] = {}
_tracker_lock = threading.Lock()
_stats = {"episodes_ingested": 0, "extraction_failures": 0, "cache_hits": 0}
_stats_lock = threading.Lock()

app = FastAPI(title="zepshim", version="0.1.0")


# --------------------------------------------------------------------- helpers
def _bump(key: str, amount: int = 1) -> None:
    with _stats_lock:
        _stats[key] = _stats.get(key, 0) + amount


def name_key(name: str) -> str:
    """Normalise an entity name so the same actor merges across mentions."""

    normalized = (name or "").strip().lower()
    normalized = re.sub(r"[\s\u3000]+", "", normalized)
    normalized = re.sub(r"[^\w\u4e00-\u9fff]+", "", normalized)
    return normalized or (name or "").strip().lower()


# --------------------------------------------------------------------------
# 实体别名归一：LLM 抽取时同一人的不同说法会变成多个节点，这里做确定性兜底。
# 表在 aliases.json，按修改时间热加载；可用 ZEPSHIM_ALIASES_FILE 覆盖路径。
# --------------------------------------------------------------------------
ALIAS_FILE = os.environ.get("ZEPSHIM_ALIASES_FILE",
                            os.path.join(os.path.dirname(os.path.abspath(__file__)), "aliases.json"))
_ALIAS_CACHE: dict = {"mtime": None, "map": {}, "strip": []}


def _alias_table() -> tuple[dict, list]:
    try:
        mtime = os.path.getmtime(ALIAS_FILE)
    except OSError:
        return {}, []
    if _ALIAS_CACHE["mtime"] != mtime:
        try:
            with open(ALIAS_FILE, encoding="utf-8") as handle:
                data = json.load(handle)
            table = {str(k).strip().lower(): str(v) for k, v in (data.get("aliases") or {}).items()}
            strip = [str(p) for p in (data.get("strip_prefixes") or [])]
        except Exception as exc:  # noqa: BLE001
            logger.warning("aliases.json 读取失败：%s", exc)
            table, strip = {}, []
        _ALIAS_CACHE.update({"mtime": mtime, "map": table, "strip": strip})
        logger.info("别名表已加载：%d 条", len(table))
    return _ALIAS_CACHE["map"], _ALIAS_CACHE["strip"]


def canonical_entity_name(name: str) -> str:
    """把同一实体的不同说法归并到唯一称呼（找不到映射则原样返回）。"""

    raw = (name or "").strip()
    if not raw:
        return raw
    table, strip = _alias_table()
    hit = table.get(raw.lower())
    if hit:
        return hit
    for prefix in strip:
        if raw.startswith(prefix) and len(raw) > len(prefix):
            hit = table.get(raw[len(prefix):].strip().lower())
            if hit:
                return hit
    return raw


def _check_auth(authorization: str | None, x_api_key: str | None) -> None:
    if not API_KEY:
        return
    presented = ""
    if authorization:
        # zep-cloud SDK 3.x 发的是 "Authorization: Api-Key <key>"；同时兼容 Bearer。
        scheme, _, value = authorization.partition(" ")
        if scheme.lower() in {"bearer", "api-key"}:
            presented = value.strip()
    if not presented and x_api_key:
        presented = x_api_key.strip()
    if presented != API_KEY:
        raise HTTPException(status_code=401, detail={"message": "invalid api key"})


def _not_found(message: str) -> HTTPException:
    return HTTPException(status_code=404, detail={"message": message})


def _apply_extraction(graph_id: str, episode_uuid: str, text: str, ontology: dict,
                      *, strict_ontology: bool, created_at: str | None) -> None:
    cache_key = extraction_cache_key(ontology, f"{'strict' if strict_ontology else 'open'}:{text}")
    cached = store.cache_get(cache_key)
    if cached is not None:
        _bump("cache_hits")
        result = cached
    else:
        result = run_extraction(llm, ontology, text, max_entities=MAX_ENTITIES)
        store.cache_put(cache_key, result)

    allowed_entities = {
        item.get("name") for item in (ontology.get("entity_types") or [])
        if isinstance(item, dict) and item.get("name")
    }
    allowed_edges = {
        item.get("name") for item in (ontology.get("edge_types") or [])
        if isinstance(item, dict) and item.get("name")
    }

    uuids: dict[str, str] = {}
    for entity in result.get("entities", []):
        entity_type = entity.get("type") or "Entity"
        if strict_ontology and allowed_entities and entity_type not in allowed_entities:
            continue
        display_name = canonical_entity_name(entity["name"])
        key = name_key(display_name)
        if not key:
            continue
        labels = [entity_type] if entity_type else []
        uuid_, _created = store.upsert_node(
            graph_id=graph_id,
            name=display_name,
            name_key=key,
            labels=labels,
            summary=entity.get("summary", ""),
            attributes=entity.get("attributes") or {},
            created_at=created_at,
        )
        uuids[key] = uuid_

    for edge in result.get("edges", []):
        edge_type = edge.get("type") or "RELATED_TO"
        if strict_ontology and allowed_edges and edge_type not in allowed_edges:
            continue
        source_name = canonical_entity_name(edge["source"])
        target_name = canonical_entity_name(edge["target"])
        source_key = name_key(source_name)
        target_key = name_key(target_name)
        source_uuid = uuids.get(source_key)
        target_uuid = uuids.get(target_key)
        if source_uuid is None:
            source_uuid, _ = store.upsert_node(
                graph_id=graph_id, name=source_name, name_key=source_key,
                labels=[], summary="", attributes={}, created_at=created_at,
            )
            uuids[source_key] = source_uuid
        if target_uuid is None:
            target_uuid, _ = store.upsert_node(
                graph_id=graph_id, name=target_name, name_key=target_key,
                labels=[], summary="", attributes={}, created_at=created_at,
            )
            uuids[target_key] = target_uuid
        store.upsert_edge(
            graph_id=graph_id, name=edge_type, fact=edge.get("fact", ""),
            source_uuid=source_uuid, target_uuid=target_uuid,
            attributes=edge.get("attributes") or {}, episode_uuid=episode_uuid,
            created_at=created_at,
        )


def ingest_episode(graph_id: str, episode_uuid: str, text: str, *, strict_ontology: bool = False,
                   created_at: str | None = None) -> None:
    """Extract and persist one episode. Never raises to the request thread."""

    try:
        ontology = store.get_ontology(graph_id)
        _apply_extraction(
            graph_id, episode_uuid, text, ontology,
            strict_ontology=strict_ontology, created_at=created_at,
        )
        store.mark_episode_processed(episode_uuid)
        _bump("episodes_ingested")
    except Exception as error:  # noqa: BLE001
        _bump("extraction_failures")
        logger.warning("extraction failed for episode %s: %s", episode_uuid, error)
        store.mark_episode_processed(episode_uuid, failed=True, error=str(error))
        if not FAIL_OPEN:
            raise


def _finalize_batch(batch_id: str) -> None:
    items = store.all_batch_items(batch_id)
    statuses = {item["status"] for item in items}
    if statuses <= {"succeeded", "skipped"}:
        store.set_batch_status(batch_id, "succeeded", completed=True)
    elif statuses & {"pending", "queued", "processing"}:
        store.set_batch_status(batch_id, "processing")
    elif "succeeded" in statuses or "skipped" in statuses:
        store.set_batch_status(batch_id, "partial", completed=True)
    else:
        store.set_batch_status(batch_id, "failed", completed=True)


def _track_batch_future(batch_id: str, future: concurrent.futures.Future) -> None:
    with _tracker_lock:
        _batch_tracker.setdefault(batch_id, set()).add(future)

    def _done(_future: concurrent.futures.Future, batch_id: str = batch_id) -> None:
        with _tracker_lock:
            pending = _batch_tracker.get(batch_id)
            if pending is not None:
                pending.discard(_future)
                remaining = len(pending)
            else:
                remaining = 0
        if remaining == 0:
            _finalize_batch(batch_id)

    future.add_done_callback(_done)


def _process_batch_item(item: dict) -> None:
    store.set_item_status(item["item_id"], "processing")
    try:
        if item["kind"] == "thread_message":
            store.set_item_status(item["item_id"], "skipped")
            return
        ingest_episode(
            item["graph_id"],
            item["episode_uuid"],
            item["data"] or "",
            strict_ontology=bool((item.get("metadata") or {}).get("strict_ontology")),
            created_at=item.get("created_at"),
        )
        store.set_item_status(item["item_id"], "succeeded")
    except Exception as error:  # noqa: BLE001
        logger.warning("batch item %s failed: %s", item["item_id"], error)
        store.set_item_status(item["item_id"], "failed", error={"message": str(error)})
        store.mark_episode_processed(item["episode_uuid"], failed=True, error=str(error))


# ---------------------------------------------------------------------- routes
@app.get("/health")
async def health() -> dict:
    return {
        "status": "ok",
        "service": "zepshim",
        "llm_configured": llm.configured,
        "llm_model": llm.model,
        "db": DB_PATH,
    }


@app.get("/stats")
async def stats() -> dict:
    with _stats_lock:
        counters = dict(_stats)
    return {"counters": counters, "llm_model": llm.model, "llm_configured": llm.configured}


@app.post("/api/v2/graph/create")
async def create_graph(
    payload: dict = Body(...),
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="x-api-key"),
) -> dict:
    _check_auth(authorization, x_api_key)
    graph_id = (payload.get("graph_id") or "").strip()
    if not graph_id:
        raise HTTPException(status_code=400, detail={"message": "graph_id is required"})
    graph = store.create_graph(
        graph_id, payload.get("name"), payload.get("description"), payload.get("time_zone")
    )
    return graph


@app.put("/api/v2/entity-types")
async def set_entity_types(
    payload: dict = Body(...),
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="x-api-key"),
) -> dict:
    _check_auth(authorization, x_api_key)
    graph_ids = [str(value) for value in (payload.get("graph_ids") or [])]
    if not graph_ids:
        raise HTTPException(
            status_code=400,
            detail={"message": "zepshim requires graph_ids in the ontology payload"},
        )
    store.set_ontology(graph_ids, {
        "entity_types": payload.get("entity_types") or [],
        "edge_types": payload.get("edge_types") or [],
    })
    logger.info("ontology set for %s (%d entity types, %d edge types)", graph_ids,
                len(payload.get("entity_types") or []), len(payload.get("edge_types") or []))
    return {"message": "ontology updated"}


@app.post("/api/v2/graph/search")
async def search_graph(
    payload: dict = Body(...),
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="x-api-key"),
) -> dict:
    _check_auth(authorization, x_api_key)
    graph_id = payload.get("graph_id")
    if not graph_id:
        raise HTTPException(status_code=400, detail={"message": "graph_id is required"})
    if store.get_graph(graph_id) is None:
        raise _not_found(f"graph {graph_id} not found")
    return build_search_response(
        store,
        graph_id=graph_id,
        query=str(payload.get("query") or ""),
        limit=int(payload.get("limit") or 10),
        scope=payload.get("scope"),
        max_characters=payload.get("max_characters"),
        search_filters=payload.get("search_filters") or {},
    )


@app.post("/api/v2/graph/node/graph/{graph_id}")
async def list_nodes(
    graph_id: str,
    payload: dict = Body(default={}),
    response: Response = None,  # type: ignore[assignment]
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="x-api-key"),
) -> list[dict]:
    _check_auth(authorization, x_api_key)
    if store.get_graph(graph_id) is None:
        raise _not_found(f"graph {graph_id} not found")
    limit = min(max(int(payload.get("limit") or 100), 1), 100)
    cursor = payload.get("cursor")
    offset = int(cursor) if cursor not in (None, "") else 0
    nodes = store.list_nodes(graph_id, limit=limit, offset=offset)
    total = store.count_nodes(graph_id)
    if offset + limit < total:
        response.headers["zep-next-cursor"] = str(offset + limit)
    return nodes


@app.post("/api/v2/graph/edge/graph/{graph_id}")
async def list_edges(
    graph_id: str,
    payload: dict = Body(default={}),
    response: Response = None,  # type: ignore[assignment]
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="x-api-key"),
) -> list[dict]:
    _check_auth(authorization, x_api_key)
    if store.get_graph(graph_id) is None:
        raise _not_found(f"graph {graph_id} not found")
    limit = min(max(int(payload.get("limit") or 100), 1), 100)
    cursor = payload.get("cursor")
    offset = int(cursor) if cursor not in (None, "") else 0
    edges = store.list_edges(graph_id, limit=limit, offset=offset)
    total = store.count_edges(graph_id)
    if offset + limit < total:
        response.headers["zep-next-cursor"] = str(offset + limit)
    return edges


@app.get("/api/v2/graph/episodes/graph/{graph_id}")
async def list_episodes(
    graph_id: str,
    lastn: int | None = None,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="x-api-key"),
) -> dict:
    _check_auth(authorization, x_api_key)
    if store.get_graph(graph_id) is None:
        raise _not_found(f"graph {graph_id} not found")
    return {"episodes": store.list_episodes(graph_id, lastn)}


@app.get("/api/v2/graph/episodes/{uuid_}")
async def get_episode(
    uuid_: str,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="x-api-key"),
) -> dict:
    _check_auth(authorization, x_api_key)
    episode = store.get_episode(uuid_)
    if episode is None:
        raise _not_found(f"episode {uuid_} not found")
    return episode


@app.get("/api/v2/graph/node/{uuid_}/entity-edges")
async def node_edges(
    uuid_: str,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="x-api-key"),
) -> list[dict]:
    _check_auth(authorization, x_api_key)
    if store.get_node(uuid_) is None:
        raise _not_found(f"node {uuid_} not found")
    return store.node_edges(uuid_)


@app.get("/api/v2/graph/node/{uuid_}")
async def get_node(
    uuid_: str,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="x-api-key"),
) -> dict:
    _check_auth(authorization, x_api_key)
    node = store.get_node(uuid_)
    if node is None:
        raise _not_found(f"node {uuid_} not found")
    return node


@app.get("/api/v2/graph/{graph_id}")
async def get_graph(
    graph_id: str,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="x-api-key"),
) -> dict:
    _check_auth(authorization, x_api_key)
    graph = store.get_graph(graph_id)
    if graph is None:
        raise _not_found(f"graph {graph_id} not found")
    return graph


@app.delete("/api/v2/graph/{graph_id}")
async def delete_graph(
    graph_id: str,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="x-api-key"),
) -> dict:
    _check_auth(authorization, x_api_key)
    existed = store.delete_graph(graph_id)
    if not existed:
        raise _not_found(f"graph {graph_id} not found")
    return {"message": f"graph {graph_id} deleted"}


@app.post("/api/v2/graph-batch")
async def add_batch(
    payload: dict = Body(...),
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="x-api-key"),
) -> list[dict]:
    """Batch episode ingestion (newer upstream call path).

    The current MiroFish image calls ``graph.add_batch`` instead of the older
    Batch API: it posts every chunk at once and then polls each returned
    episode for ``processed``. Semantics are the same as repeated ``graph``
    adds, only the envelope differs.
    """

    _check_auth(authorization, x_api_key)
    graph_id = payload.get("graph_id")
    if not graph_id:
        raise HTTPException(status_code=400, detail={"message": "graph_id is required"})
    if store.get_graph(graph_id) is None:
        raise _not_found(f"graph {graph_id} not found")

    episodes = payload.get("episodes")
    if not isinstance(episodes, list) or not episodes:
        raise HTTPException(
            status_code=400, detail={"message": "episodes must be a non-empty list"}
        )

    created: list[dict] = []
    for raw in episodes:
        if not isinstance(raw, dict):
            continue
        content = raw.get("data") or raw.get("content") or ""
        if not isinstance(content, str) or not content.strip():
            continue
        episode = store.add_episode(
            graph_id=graph_id,
            content=content,
            source_description=raw.get("source_description"),
            metadata=raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {},
            source=str(raw.get("type") or "text"),
            created_at=raw.get("created_at"),
        )
        created.append(episode)
        pool.submit(
            ingest_episode, graph_id, episode["uuid_"], content,
            strict_ontology=bool(raw.get("strict_ontology")),
            created_at=raw.get("created_at"),
        )

    if not created:
        raise HTTPException(
            status_code=400, detail={"message": "no usable episode in payload"}
        )
    logger.info("graph-batch: queued %d episodes for graph %s", len(created), graph_id)
    return created


@app.post("/api/v2/graph")
async def add_episode(
    payload: dict = Body(...),
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="x-api-key"),
) -> dict:
    _check_auth(authorization, x_api_key)
    graph_id = payload.get("graph_id")
    if not graph_id:
        raise HTTPException(status_code=400, detail={"message": "graph_id is required"})
    if store.get_graph(graph_id) is None:
        raise _not_found(f"graph {graph_id} not found")
    content = payload.get("data") or payload.get("content") or ""
    if not isinstance(content, str) or not content.strip():
        raise HTTPException(status_code=400, detail={"message": "data must be a non-empty string"})
    episode = store.add_episode(
        graph_id=graph_id,
        content=content,
        source_description=payload.get("source_description"),
        metadata=payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {},
        source=str(payload.get("type") or "text"),
        role=None,
        created_at=payload.get("created_at"),
    )
    pool.submit(
        ingest_episode, graph_id, episode["uuid_"], content,
        strict_ontology=bool(payload.get("strict_ontology")),
        created_at=payload.get("created_at"),
    )
    return episode


@app.post("/api/v2/batches")
async def create_batch(
    payload: dict = Body(default={}),
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="x-api-key"),
) -> dict:
    _check_auth(authorization, x_api_key)
    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    ignore_roles = payload.get("ignore_roles")
    return store.create_batch(metadata=metadata, ignore_roles=ignore_roles)


@app.get("/api/v2/batches")
async def list_batches(
    limit: int | None = None,
    cursor: int | None = None,
    status: str | None = None,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="x-api-key"),
) -> dict:
    _check_auth(authorization, x_api_key)
    page_size = min(max(int(limit or 100), 1), 100)
    offset = max(int(cursor or 0), 0)
    batches = store.list_batches(limit=page_size, offset=offset, status=status)
    total = store.count_batches(status)
    next_cursor = offset + page_size if offset + page_size < total else None
    return {"batches": batches, "next_cursor": next_cursor}


@app.get("/api/v2/batches/{batch_id}/items")
async def list_batch_items(
    batch_id: str,
    limit: int | None = None,
    cursor: int | None = None,
    status: str | None = None,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="x-api-key"),
) -> dict:
    _check_auth(authorization, x_api_key)
    if store.get_batch(batch_id) is None:
        raise _not_found(f"batch {batch_id} not found")
    page_size = min(max(int(limit or 100), 1), 100)
    offset = max(int(cursor or 0), 0)
    items = store.list_batch_items(batch_id, limit=page_size, offset=offset, status=status)
    total = len(store.all_batch_items(batch_id))
    next_cursor = offset + page_size if offset + page_size < total else None
    return {"items": items, "next_cursor": next_cursor}


@app.post("/api/v2/batches/{batch_id}/items")
async def add_batch_items(
    batch_id: str,
    payload: dict = Body(...),
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="x-api-key"),
) -> list[dict]:
    _check_auth(authorization, x_api_key)
    batch = store.get_batch(batch_id)
    if batch is None:
        raise _not_found(f"batch {batch_id} not found")
    if batch["status"] not in {"draft", "queued"}:
        raise HTTPException(
            status_code=409,
            detail={"message": f"batch {batch_id} is {batch['status']} and cannot accept items"},
        )

    prepared: list[dict] = []
    for raw in payload.get("items") or []:
        if not isinstance(raw, dict):
            continue
        kind = str(raw.get("type") or "graph_episode")
        graph_id = raw.get("graph_id")
        data = raw.get("data") or raw.get("content") or ""
        if kind == "graph_episode":
            if not graph_id:
                raise HTTPException(status_code=400, detail={"message": "graph_id is required"})
            episode = store.add_episode(
                graph_id=graph_id,
                content=data,
                source_description=raw.get("source_description"),
                metadata=raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {},
                source=str(raw.get("data_type") or "text"),
                created_at=raw.get("created_at"),
            )
            episode_uuid = episode["uuid_"]
        else:
            episode_uuid = None
        prepared.append({
            "kind": kind,
            "graph_id": graph_id,
            "data": data,
            "data_type": raw.get("data_type"),
            "source_description": raw.get("source_description"),
            "metadata": raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {},
            "episode_uuid": episode_uuid,
            "thread_id": raw.get("thread_id"),
            "user_id": raw.get("user_id"),
        })
    if not prepared:
        raise HTTPException(status_code=400, detail={"message": "items must not be empty"})
    return store.add_batch_items(batch_id, prepared)


@app.post("/api/v2/batches/{batch_id}/process")
async def process_batch(
    batch_id: str,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="x-api-key"),
) -> dict:
    _check_auth(authorization, x_api_key)
    batch = store.get_batch(batch_id)
    if batch is None:
        raise _not_found(f"batch {batch_id} not found")
    if batch["status"] not in {"draft", "queued"}:
        raise HTTPException(
            status_code=409,
            detail={"message": f"batch {batch_id} is already {batch['status']}"},
        )
    items = store.batch_items_for_processing(batch_id)
    if not items:
        raise HTTPException(status_code=400, detail={"message": "batch has no items"})
    store.set_batch_status(batch_id, "processing", processed=True)
    queued = 0
    for item in items:
        future = pool.submit(_process_batch_item, item)
        _track_batch_future(batch_id, future)
        queued += 1
    logger.info("batch %s processing %d items", batch_id, queued)
    return store.get_batch(batch_id) or {}


@app.get("/api/v2/batches/{batch_id}")
async def get_batch(
    batch_id: str,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="x-api-key"),
) -> dict:
    _check_auth(authorization, x_api_key)
    batch = store.get_batch(batch_id)
    if batch is None:
        raise _not_found(f"batch {batch_id} not found")
    return batch


@app.delete("/api/v2/batches/{batch_id}")
async def delete_batch(
    batch_id: str,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="x-api-key"),
) -> dict:
    _check_auth(authorization, x_api_key)
    if store.get_batch(batch_id) is None:
        raise _not_found(f"batch {batch_id} not found")
    store.set_batch_status(batch_id, "canceled", completed=True)
    return store.get_batch(batch_id) or {}


@app.exception_handler(HTTPException)
async def http_exception_handler(_request: Request, exc: HTTPException) -> JSONResponse:
    detail = exc.detail
    if isinstance(detail, dict):
        body = detail
    else:
        body = {"message": str(detail)}
    return JSONResponse(status_code=exc.status_code, content=body)


def _resume_pending_episodes() -> int:
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
