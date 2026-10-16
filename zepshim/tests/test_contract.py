"""Contract test: drive the local shim through the real ``zep_cloud`` SDK.

Run with the MiroFish backend virtualenv, which already has ``zep-cloud``:

    ZEPSHIM_BASE_URL=http://127.0.0.1:8100/api/v2 \
      backend/.venv/bin/python zepshim/tests/test_contract.py

It mirrors exactly what MiroFish does: create graph -> set ontology ->
Batch API ingestion -> poll -> paginated node/edge reads -> node edges ->
search -> episode reads -> graph.add -> delete. Any deviation from the Zep
Cloud contract fails the assertions.
"""

from __future__ import annotations

import os
import sys
import time
import uuid
from typing import Optional

from pydantic import Field
from zep_cloud import BatchAddItem, EntityEdgeSourceTarget, NotFoundError, Zep
from zep_cloud.external_clients.ontology import EdgeModel, EntityModel, EntityText

BASE_URL = os.environ.get("ZEPSHIM_BASE_URL", "http://127.0.0.1:8100/api/v2")
API_KEY = os.environ.get("ZEPSHIM_API_KEY", "local-shim-key")

CHUNKS = [
    "武汉大学近日发布通报，称该校一名学生因学术不端行为被撤销博士学位。校长张平文表示，"
    "学校将全面加强学风与学术规范建设，并在全校范围内开展自查自纠。",
    "教育部就此事作出回应，要求各高校严肃处理学术不端问题，并完善学位授予质量保障机制。"
    "教育部同时强调，导师在学位论文质量把关中负有不可推卸的责任。",
    "该事件在微博上引发大量讨论，相关话题阅读量超过一亿次。许多网友支持严查学术不端，"
    "也有网友认为应当区分个案与普遍现象。武汉大学位于湖北省武汉市，是教育部直属重点大学。",
]

STATUS_TERMINAL = {"succeeded", "partial", "failed", "invalid", "canceled"}


def _fail(message: str) -> None:
    print(f"FAIL: {message}")
    sys.exit(1)


def _check(condition: bool, message: str) -> None:
    if not condition:
        _fail(message)
    print(f"  ok: {message}")


def build_ontology(client: Zep, graph_id: str) -> None:
    class Organization(EntityModel):
        """A university, ministry, company or other institution."""

        location: Optional[EntityText] = Field(description="Where the organization is located", default=None)

    class Person(EntityModel):
        """A named individual such as an official or a student."""

        role: Optional[EntityText] = Field(description="The person's role or title", default=None)

    class Platform(EntityModel):
        """An online platform or media outlet."""

        kind: Optional[EntityText] = Field(description="The kind of platform", default=None)

    class WorksAt(EdgeModel):
        """Employment or affiliation between a person and an organization."""

        position: Optional[EntityText] = Field(description="The position held", default=None)

    class InvolvedIn(EdgeModel):
        """An organization or person is involved in an event or topic."""

        role: Optional[EntityText] = Field(description="The role played in the event", default=None)

    class DiscussedOn(EdgeModel):
        """A topic is discussed on a platform."""

        volume: Optional[EntityText] = Field(description="Scale of the discussion", default=None)

    client.graph.set_ontology(
        graph_ids=[graph_id],
        entities={"Organization": Organization, "Person": Person, "Platform": Platform},
        edges={
            "WORKS_AT": (WorksAt, [
                EntityEdgeSourceTarget(source="Person", target="Organization"),
            ]),
            "INVOLVED_IN": (InvolvedIn, [
                EntityEdgeSourceTarget(source="Organization", target="Organization"),
            ]),
            "DISCUSSED_ON": (DiscussedOn, [
                EntityEdgeSourceTarget(source="Organization", target="Platform"),
            ]),
        },
    )


def ingest(client: Zep, graph_id: str) -> list[str]:
    batch = client.batch.create(metadata={"test_operation": "contract"})
    _check(bool(getattr(batch, "batch_id", None)), "batch.create returned batch_id")
    _check(getattr(batch, "status", None) == "draft", "new batch status is 'draft'")

    items = [
        BatchAddItem(
            type="graph_episode",
            graph_id=graph_id,
            data=chunk,
            data_type="text",
            source_description="contract test chunk",
            metadata={"chunk_index": index},
        )
        for index, chunk in enumerate(CHUNKS)
    ]
    details = client.batch.add(batch_id=batch.batch_id, items=items)
    _check(len(details) == len(items), "batch.add echoed every item")
    _check(
        {item.sequence_index for item in details} == set(range(len(items))),
        "batch.add assigned contiguous sequence_index values",
    )
    _check(
        all(item.episode_uuid for item in details),
        "batch.add pre-assigned episode_uuid to every item",
    )
    _check(
        all(item.source_uuid == item.episode_uuid for item in details),
        "batch.add source_uuid matches episode_uuid",
    )

    summary = client.batch.process(batch_id=batch.batch_id)
    _check(getattr(summary, "status", None) != "draft", "batch.process left the draft state")

    deadline = time.time() + 300
    status = None
    while time.time() < deadline:
        summary = client.batch.get(batch_id=batch.batch_id)
        status = getattr(summary, "status", None)
        if status in STATUS_TERMINAL:
            break
        time.sleep(3)
    _check(status == "succeeded", f"batch finished as '{status}'")
    progress = getattr(summary, "progress", None)
    _check(progress is not None and progress.percent_complete == 100,
           "batch progress reported 100%")

    page = client.batch.list_items(batch_id=batch.batch_id, limit=100)
    _check(len(page.items or []) == len(CHUNKS), "batch.list_items returned every item")
    _check(all(item.status == "succeeded" for item in page.items), "every batch item succeeded")
    _check(getattr(page, "next_cursor", None) is None, "batch item pagination terminated")

    listed = client.batch.list(limit=100)
    _check(any(b.batch_id == batch.batch_id for b in (listed.batches or [])),
           "batch.list contains the batch with metadata")

    episode_uuids = [item.episode_uuid for item in details]
    for episode_uuid in episode_uuids:
        episode = client.graph.episode.get(uuid_=episode_uuid)
        _check(bool(episode.processed), f"episode {episode_uuid[:10]} reports processed=true")
        _check(bool(episode.content), "episode.content is populated")
    return episode_uuids


def read_graph(client: Zep, graph_id: str) -> None:
    nodes = []
    cursor = None
    while True:
        kwargs = {"limit": 2}
        if cursor is not None:
            kwargs["cursor"] = cursor
        response = client.graph.node.with_raw_response.get_by_graph_id(graph_id, **kwargs)
        nodes.extend(response.data or [])
        cursor = response.headers.get("zep-next-cursor")
        if cursor is None:
            break
    _check(len(nodes) > 2, f"pagination via zep-next-cursor returned {len(nodes)} nodes")
    for node in nodes:
        _check(bool(node.uuid_) and node.created_at is not None, "node has uuid_ and created_at")
        _check(isinstance(node.labels or [], list), "node.labels is a list")
        _check(node.name is not None, "node.name present")
        break

    edges = []
    cursor = None
    while True:
        kwargs = {"limit": 2}
        if cursor is not None:
            kwargs["cursor"] = cursor
        response = client.graph.edge.with_raw_response.get_by_graph_id(graph_id, **kwargs)
        edges.extend(response.data or [])
        cursor = response.headers.get("zep-next-cursor")
        if cursor is None:
            break
    _check(len(edges) > 0, f"edge pagination returned {len(edges)} edges")
    edge = edges[0]
    _check(bool(edge.fact) and bool(edge.name), "edge has fact and name")
    _check(bool(edge.source_node_uuid) and bool(edge.target_node_uuid),
           "edge has source/target node uuids")

    node = client.graph.node.get(uuid_=nodes[0].uuid_)
    _check(node.uuid_ == nodes[0].uuid_, "node.get round-trips by uuid")

    node_edges = client.graph.node.get_edges(node_uuid=nodes[0].uuid_)
    _check(isinstance(node_edges, list), "node.get_edges returns a list")
    _check(
        any(item.uuid_ for item in node_edges),
        "node.get_edges returned edges for a node that has any",
    )

    episodes = client.graph.episode.get_by_graph_id(graph_id=graph_id, lastn=10)
    _check(len(episodes.episodes or []) == len(CHUNKS),
           "episode.get_by_graph_id returned the ingested episodes")


def search_graph(client: Zep, graph_id: str) -> None:
    results = client.graph.search(
        graph_id=graph_id, query="教育部 学术不端", limit=10, scope="edges",
        reranker="cross_encoder",
    )
    _check(bool(results.context), "search returned a context string")
    _check(isinstance(results.edges, list) and len(results.edges) > 0, "search returned edges")
    _check(any(edge.fact for edge in results.edges), "search edges carry facts")
    _check(isinstance(results.nodes, list) and len(results.nodes) > 0, "search returned nodes")
    _check(all(node.name for node in results.nodes), "search nodes carry names")

    node_results = client.graph.search(
        graph_id=graph_id, query="武汉大学", limit=5, scope="nodes",
    )
    _check(isinstance(node_results.nodes, list), "scope='nodes' returns nodes")


def incremental_add(client: Zep, graph_id: str) -> None:
    episode = client.graph.add(
        graph_id=graph_id,
        type="text",
        data="模拟活动中，张平文校长在上海会见了教育部官员，双方讨论了学风建设。",
        source_description="contract test simulation activity",
        metadata={"source": "contract_test"},
    )
    _check(bool(episode.uuid_), "graph.add returned an episode uuid")

    deadline = time.time() + 180
    processed = False
    while time.time() < deadline:
        current = client.graph.episode.get(uuid_=episode.uuid_)
        if current.processed:
            processed = True
            break
        time.sleep(2)
    _check(processed, "single-episode graph.add is processed asynchronously")


def main() -> None:
    client = Zep(api_key=API_KEY, base_url=BASE_URL)
    graph_id = f"mirofish_contract_{uuid.uuid4().hex[:10]}"
    print(f"== shim contract test against {BASE_URL} (graph {graph_id}) ==")

    print("[1/6] graph.create")
    graph = client.graph.create(graph_id=graph_id, name="Contract Test",
                               description="MiroFish contract test")
    _check(graph.graph_id == graph_id, "graph.create returned the requested graph_id")

    try:
        print("[2/6] graph.set_ontology")
        build_ontology(client, graph_id)

        print("[3/6] Batch API ingestion")
        ingest(client, graph_id)

        print("[4/6] paginated reads")
        read_graph(client, graph_id)

        print("[5/6] graph.search")
        search_graph(client, graph_id)

        print("[6/6] graph.add (incremental episode)")
        incremental_add(client, graph_id)
    finally:
        print("[cleanup] graph.delete")
        client.graph.delete(graph_id=graph_id)
        try:
            client.graph.get(graph_id)
            _fail("deleted graph is still readable")
        except NotFoundError:
            print("  ok: deleted graph raises NotFoundError")

    print("\nALL CONTRACT CHECKS PASSED")


if __name__ == "__main__":
    main()
