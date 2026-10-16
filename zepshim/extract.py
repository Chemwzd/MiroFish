"""LLM-backed entity/edge extraction used as a local stand-in for Zep's
cloud-side graph ingestion.

The extraction contract mirrors what Zep Cloud produces for a text episode:
a set of typed entities (nodes) plus typed relationships (edges) whose
properties follow the ontology registered through ``PUT /api/v2/entity-types``.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from typing import Any

import httpx

MAX_INPUT_CHARS = int(os.environ.get("ZEPSHIM_MAX_INPUT_CHARS", "12000"))

SYSTEM_PROMPT = (
    "You are the ingestion engine of a temporal knowledge graph. "
    "You read one piece of source text and reply with JSON only."
)

USER_TEMPLATE = """Extract entities and relationships from the SOURCE TEXT.

ONTOLOGY
Entity types (use the exact `name` when the text contains such an entity):
{entity_types}

Relationship types (use the exact `name`; respect allowed source/target types):
{edge_types}

SOURCE TEXT
\"\"\"
{text}
\"\"\"

Rules:
1. Reply with a single JSON object, no markdown fences, no commentary.
2. Entities: real, meaningful, named or clearly identifiable actors/objects in the text.
   Skip generic words and pure numbers. Keep first-person self-reference ("我", "本人",
   "作者", "I", "me") as ONE entity — do not drop it, and do not emit several entities
   for it. At most {max_entities} entities.
3. Every entity needs: name (short, canonical), type (an ontology entity type; use
   "Entity" only when nothing fits), summary (one factual sentence), attributes (object
   using only ontology attribute names, omit unknown ones, use "" for unknown values).
4. Relationships: only what the text actually states. Every relationship needs:
   source (entity name), target (entity name), type (ontology relationship name, else
   uppercase snake case), fact (one sentence, self-contained, in the same language as
   the source text), attributes (object).
5. Prefer relations between entities you listed in "entities".
6. Language: write names, summaries and facts in the source text's language.

7. ONE ACTOR = ONE ENTITY. Different surface forms for the same actor (real name,
   nickname, account handle, role/credit label, or a self-reference such as 我/本人/作者)
   MUST be merged into a single entity. Name that entity with the most specific canonical
   label available: an explicitly declared identity/alias block in the source text first,
   then a full personal name, then the self label. Never emit two entities for one person,
   and always use that same canonical name in "edges".
8. DISTINCT PEOPLE STAY DISTINCT. Do not collapse different individuals into one vague
   collective (e.g. "我的父亲" / "我的母亲" / "女朋友的父亲" / "女朋友的母亲" are four
   different people — never merge them into "双方父母"). If the text only provides a vague
   collective, prefer the explicit members when they are stated anywhere in the text.

JSON schema:
{{"entities":[{{"name":"","type":"","summary":"","attributes":{{}}}}],
  "edges":[{{"source":"","target":"","type":"","fact":"","attributes":{{}}}}]}}"""


def _strip_code_fence(raw: str) -> str:
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z0-9_-]*\s*", "", text)
        text = re.sub(r"```\s*$", "", text)
    return text.strip()


def parse_json_object(raw: str) -> dict[str, Any]:
    """Parse a model reply that should contain exactly one JSON object."""

    text = _strip_code_fence(raw or "")
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except (TypeError, ValueError):
        pass

    start = text.find("{")
    if start >= 0:
        depth = 0
        in_string = False
        escape = False
        for index in range(start, len(text)):
            char = text[index]
            if in_string:
                if escape:
                    escape = False
                elif char == "\\":
                    escape = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    candidate = text[start:index + 1]
                    try:
                        parsed = json.loads(candidate)
                        if isinstance(parsed, dict):
                            return parsed
                    except (TypeError, ValueError):
                        break
    raise ValueError("model reply did not contain a JSON object")


def _format_entity_types(ontology: dict) -> str:
    entity_types = ontology.get("entity_types") or []
    if not entity_types:
        return "- Entity: any notable person, organisation, place, event, product or concept."
    lines = []
    for item in entity_types:
        if not isinstance(item, dict) or not item.get("name"):
            continue
        attributes = item.get("properties") or item.get("attributes") or []
        attribute_names = [
            attribute.get("name")
            for attribute in attributes
            if isinstance(attribute, dict) and attribute.get("name")
        ]
        suffix = f" attributes: {', '.join(attribute_names)}" if attribute_names else ""
        lines.append(f"- {item['name']}: {item.get('description', '')}{suffix}")
    return "\n".join(lines) or "- Entity: any notable entity in the text."


def _format_edge_types(ontology: dict) -> str:
    edge_types = ontology.get("edge_types") or []
    if not edge_types:
        return "- UPPER_SNAKE_CASE relationship between two entities."
    lines = []
    for item in edge_types:
        if not isinstance(item, dict) or not item.get("name"):
            continue
        pairs = []
        for target in item.get("source_targets") or []:
            if isinstance(target, dict):
                pairs.append(f"{target.get('source', 'Entity')}->{target.get('target', 'Entity')}")
        suffix = f" ({'; '.join(pairs)})" if pairs else ""
        lines.append(f"- {item['name']}: {item.get('description', '')}{suffix}")
    return "\n".join(lines) or "- UPPER_SNAKE_CASE relationship between two entities."


def build_user_prompt(ontology: dict, text: str, *, max_entities: int = 30) -> str:
    return USER_TEMPLATE.format(
        entity_types=_format_entity_types(ontology),
        edge_types=_format_edge_types(ontology),
        text=text[:MAX_INPUT_CHARS],
        max_entities=max_entities,
    )


class LLMClient:
    """Minimal OpenAI-compatible chat client with retries and JSON mode."""

    def __init__(self, base_url: str, api_key: str, model: str, *, timeout: float = 180.0,
                 max_retries: int = 3):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.max_retries = max_retries
        self._local = threading.local()

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.model)

    def _client(self) -> httpx.Client:
        client = getattr(self._local, "client", None)
        if client is None:
            client = httpx.Client(
                timeout=httpx.Timeout(self.timeout, connect=30.0),
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
            )
            self._local.client = client
        return client

    def complete_json(self, system: str, user: str, *, temperature: float = 0.0) -> dict:
        if not self.configured:
            raise RuntimeError("ZEPSHIM LLM endpoint is not configured")

        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": temperature,
            "response_format": {"type": "json_object"},
        }
        last_error: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            try:
                response = self._client().post(
                    f"{self.base_url}/chat/completions", json=payload
                )
                if response.status_code == 400 and "response_format" in payload:
                    # Some proxies reject json_object mode; retry without it.
                    payload.pop("response_format", None)
                    continue
                response.raise_for_status()
                body = response.json()
                content = body["choices"][0]["message"]["content"]
                return parse_json_object(content)
            except Exception as error:  # noqa: BLE001 - surfaced to caller after retries
                last_error = error
                if attempt < self.max_retries:
                    time.sleep(min(2 ** attempt, 8))
        raise RuntimeError(f"LLM extraction failed after {self.max_retries} attempts: {last_error}")


def extraction_cache_key(ontology: dict, text: str) -> str:
    digest = hashlib.sha256()
    digest.update(json.dumps(ontology, sort_keys=True, ensure_ascii=False).encode("utf-8"))
    digest.update(b"\0")
    digest.update(text.encode("utf-8"))
    return digest.hexdigest()


def normalize_extraction(raw: dict[str, Any]) -> dict[str, list[dict]]:
    """Coerce a model reply into the canonical extraction shape."""

    entities: list[dict] = []
    seen_names: set[str] = set()
    for item in raw.get("entities") or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name or name.lower() in {"none", "null", "n/a"}:
            continue
        key = name.lower()
        if key in seen_names:
            continue
        seen_names.add(key)
        attributes = item.get("attributes")
        entities.append({
            "name": name,
            "type": str(item.get("type") or item.get("entity_type") or "Entity").strip() or "Entity",
            "summary": str(item.get("summary") or item.get("description") or "").strip(),
            "attributes": attributes if isinstance(attributes, dict) else {},
        })

    edges: list[dict] = []
    seen_edges: set[tuple[str, str, str]] = set()
    for item in raw.get("edges") or raw.get("relationships") or []:
        if not isinstance(item, dict):
            continue
        source = str(item.get("source") or item.get("source_name") or "").strip()
        target = str(item.get("target") or item.get("target_name") or "").strip()
        if not source or not target:
            continue
        edge_name = str(item.get("type") or item.get("name") or "RELATED_TO").strip() or "RELATED_TO"
        edge_name = re.sub(r"[^0-9A-Za-z_\u4e00-\u9fff]+", "_", edge_name).strip("_").upper() or "RELATED_TO"
        key = (source.lower(), target.lower(), edge_name)
        if key in seen_edges:
            continue
        seen_edges.add(key)
        attributes = item.get("attributes")
        edges.append({
            "source": source,
            "target": target,
            "type": edge_name,
            "fact": str(item.get("fact") or item.get("summary") or "").strip(),
            "attributes": attributes if isinstance(attributes, dict) else {},
        })

    return {"entities": entities, "edges": edges}


def extract(llm: LLMClient, ontology: dict, text: str, *, max_entities: int = 30) -> dict:
    reply = llm.complete_json(SYSTEM_PROMPT, build_user_prompt(ontology, text, max_entities=max_entities))
    return normalize_extraction(reply)
