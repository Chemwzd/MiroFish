"""Lexical hybrid search over the local graph store.

Zep Cloud ranks results with embeddings + BM25 + a cross-encoder reranker.
Without an embedding backend we approximate that with a deterministic scorer:
exact substring hits, Latin word overlap and CJK bigram overlap, weighted per
field (names > facts > summaries > attribute values).
"""

from __future__ import annotations

import re
import time
from typing import Any

_LATIN_RE = re.compile(r"[a-z0-9_]+")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")


def tokenize(text: str) -> set[str]:
    lowered = (text or "").lower()
    latin = set(_LATIN_RE.findall(lowered))
    cjk = _CJK_RE.findall(lowered)
    bigrams = {cjk[i] + cjk[i + 1] for i in range(len(cjk) - 1)}
    return latin | bigrams | set(cjk)


def _score(query_lower: str, query_tokens: set[str], text: str, weight: float) -> float:
    if not text:
        return 0.0
    lowered = text.lower()
    score = 0.0
    if query_lower and query_lower in lowered:
        score += 40.0
    if query_tokens:
        overlap = len(query_tokens & tokenize(lowered))
        if overlap:
            score += 10.0 * (overlap / max(len(query_tokens), 1)) * min(overlap, 8)
    return score * weight


def _attributes_text(attributes: Any) -> str:
    if not isinstance(attributes, dict):
        return ""
    return " ".join(str(value) for value in attributes.values() if isinstance(value, (str, int, float)))


def _passes_filters(labels: list[str], filters: dict, *, is_edge: bool, name: str) -> bool:
    if not isinstance(filters, dict) or not filters:
        return True
    if is_edge:
        included = filters.get("edge_types")
        excluded = filters.get("exclude_edge_types")
    else:
        included = filters.get("node_labels")
        excluded = filters.get("exclude_node_labels")
    if included and name not in included and not (set(labels) & set(included)):
        return False
    if excluded and (name in excluded or (set(labels) & set(excluded))):
        return False
    return True


def build_search_response(
    store,
    *,
    graph_id: str,
    query: str,
    limit: int,
    scope: str | None,
    max_characters: int | None,
    search_filters: dict | None,
) -> dict:
    started = time.perf_counter()
    query_lower = (query or "").strip().lower()
    query_tokens = tokenize(query_lower)
    filters = search_filters if isinstance(search_filters, dict) else {}
    limit = max(1, min(int(limit or 10), 50))
    scope_normalized = (scope or "edges").lower()

    nodes = store.all_nodes(graph_id)
    edges = store.all_edges(graph_id)
    node_index = {node["uuid_"]: node for node in nodes}

    scored_nodes: list[tuple[float, dict]] = []
    for node in nodes:
        labels = node.get("labels") or []
        label_text = " ".join(labels)
        if not _passes_filters(labels, filters, is_edge=False, name=label_text):
            continue
        score = (
            _score(query_lower, query_tokens, node.get("name", ""), 3.0)
            + _score(query_lower, query_tokens, node.get("summary", ""), 1.0)
            + _score(query_lower, query_tokens, label_text, 1.5)
            + _score(query_lower, query_tokens, _attributes_text(node.get("attributes")), 0.5)
        )
        if score > 0:
            scored_nodes.append((score, node))
    scored_nodes.sort(key=lambda pair: pair[0], reverse=True)

    scored_edges: list[tuple[float, dict]] = []
    for edge in edges:
        if not _passes_filters([], filters, is_edge=True, name=edge.get("name", "")):
            continue
        source = node_index.get(edge.get("source_node_uuid"), {})
        target = node_index.get(edge.get("target_node_uuid"), {})
        score = (
            _score(query_lower, query_tokens, edge.get("fact", ""), 2.0)
            + _score(query_lower, query_tokens, edge.get("name", ""), 1.5)
            + _score(query_lower, query_tokens, source.get("name", ""), 1.0)
            + _score(query_lower, query_tokens, target.get("name", ""), 1.0)
            + _score(query_lower, query_tokens, _attributes_text(edge.get("attributes")), 0.5)
        )
        if score > 0:
            scored_edges.append((score, edge))
    scored_edges.sort(key=lambda pair: pair[0], reverse=True)

    top_nodes = [] if scope_normalized == "edges" else [
        {**node, "score": round(score, 4), "relevance": round(score, 4)}
        for score, node in scored_nodes[:limit]
    ]
    if scope_normalized == "edges":
        # Zep returns the entities that anchor the matched edges as well.
        anchor_uuids: list[str] = []
        for _score_value, edge in scored_edges[:limit]:
            for uuid_ in (edge.get("source_node_uuid"), edge.get("target_node_uuid")):
                if uuid_ and uuid_ not in anchor_uuids:
                    anchor_uuids.append(uuid_)
        top_nodes = [
            {**node_index[uuid_], "score": None, "relevance": None}
            for uuid_ in anchor_uuids if uuid_ in node_index
        ]

    top_edges = [
        {**edge, "score": round(score, 4), "relevance": round(score, 4)}
        for score, edge in scored_edges[:limit]
    ]

    episodes: list[dict] = []
    if scope_normalized in {"episodes", "auto", "episode"} or not top_edges:
        scored_episodes: list[tuple[float, dict]] = []
        for episode in store.list_episodes(graph_id):
            score = _score(query_lower, query_tokens, episode.get("content", ""), 1.0)
            if score > 0:
                scored_episodes.append((score, episode))
        scored_episodes.sort(key=lambda pair: pair[0], reverse=True)
        episodes = [
            {**episode, "score": round(score, 4), "relevance": round(score, 4)}
            for score, episode in scored_episodes[:3]
        ]

    lines: list[str] = []
    if top_edges:
        lines.append("FACTS")
        for edge in top_edges:
            source = node_index.get(edge.get("source_node_uuid"), {}).get("name", "")
            target = node_index.get(edge.get("target_node_uuid"), {}).get("name", "")
            lines.append(f"- {source} -> {target} ({edge.get('name')}): {edge.get('fact')}")
    if top_nodes:
        lines.append("ENTITIES")
        for node in top_nodes:
            labels = ", ".join(node.get("labels") or [])
            suffix = f" [{labels}]" if labels else ""
            lines.append(f"- {node.get('name')}{suffix}: {node.get('summary')}")
    if episodes:
        lines.append("EPISODES")
        for episode in episodes:
            lines.append(f"- {episode.get('content', '')[:400]}")
    context = "\n".join(lines)
    if max_characters and len(context) > int(max_characters):
        context = context[: int(max_characters)]

    latency_ms = int((time.perf_counter() - started) * 1000)
    return {
        "context": context,
        "nodes": top_nodes,
        "edges": top_edges,
        "episodes": episodes,
        "observations": [],
        "thread_summaries": [],
        "response": {"server_latency_ms": latency_ms},
    }
