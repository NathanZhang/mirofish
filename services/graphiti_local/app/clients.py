"""Explicit local model clients, output validation, and credential-free audit."""
import json
import math
import os
import time
from pathlib import Path
from urllib.parse import urlparse
from types import SimpleNamespace

os.environ["GRAPHITI_TELEMETRY_ENABLED"] = "false"

from openai import AsyncOpenAI, DefaultAsyncHttpxClient
from graphiti_core import Graphiti
from graphiti_core.cross_encoder.client import CrossEncoderClient
from graphiti_core.embedder.openai import OpenAIEmbedder, OpenAIEmbedderConfig
from graphiti_core.llm_client.config import LLMConfig
from graphiti_core.llm_client.openai_generic_client import OpenAIGenericClient


def local_url(name):
    value = os.environ[name]
    url = urlparse(value)
    if url.scheme not in {"http", "https", "bolt", "neo4j"} or url.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError(f"{name} must point to a loopback endpoint")
    return value


def audit(event):
    path = Path(os.environ.get("GRAPH_AUDIT_PATH", "../../data/graphiti-local/model-calls.jsonl"))
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps({"time": time.time(), **event}, ensure_ascii=False) + "\n")


class ValidatedLLM(OpenAIGenericClient):
    async def _generate_response(self, messages, response_model=None, *args, **kwargs):
        started = time.monotonic()
        event = {"kind": "llm", "endpoint": str(self.client.base_url),
                 "model": self.model, "schema": getattr(response_model, "__name__", None),
                 "structured_output_mode": self.structured_output_mode}
        try:
            result = await super()._generate_response(messages, response_model, *args, **kwargs)
            if response_model:
                result = response_model.model_validate(result).model_dump()
            event["status"] = "ok"
            return result
        except BaseException as error:
            event.update(status="error", error_type=type(error).__name__)
            raise
        finally:
            event["seconds"] = round(time.monotonic() - started, 3)
            audit(event)


class ValidatedEmbedder(OpenAIEmbedder):
    async def create_batch(self, input_data_list):
        if len(input_data_list) > 16:
            vectors = []
            for offset in range(0, len(input_data_list), 16):
                vectors.extend(await self.create_batch(input_data_list[offset:offset+16]))
            return vectors
        result = await self.client.embeddings.create(
            input=input_data_list, model=self.config.embedding_model, encoding_format="float")
        rows = sorted(result.data, key=lambda row: row.index)
        vectors = [row.embedding for row in rows]
        if ([row.index for row in rows] != list(range(len(input_data_list))) or
                any(len(v) != self.config.embedding_dim or
                    not all(math.isfinite(x) for x in v) for v in vectors)):
            raise ValueError("Embedding count, order, dimension or numeric validation failed")
        audit({"kind": "embedding", "endpoint": str(self.client.base_url),
               "model": self.config.embedding_model, "count": len(vectors), "status": "ok"})
        return vectors

    async def create(self, input_data):
        if isinstance(input_data, list) and len(input_data) == 1:
            input_data = input_data[0]
        if not isinstance(input_data, str):
            raise ValueError("Expected a single string for embedding")
        return (await self.create_batch([input_data]))[0]


class DisabledCrossEncoder(CrossEncoderClient):
    async def rank(self, query, passages):
        raise RuntimeError("CrossEncoder disabled; use explicit RRF search configurations")


class LocalChatClient:
    """Explicit optional vLLM chat-template setting, leaving Graphiti prompts intact."""
    def __init__(self, client):
        self.client = client
        self.chat = SimpleNamespace(completions=self)
        self.base_url = client.base_url
        self.models = client.models

    async def create(self, **kwargs):
        value = os.environ.get('GRAPH_LLM_ENABLE_THINKING')
        if value is not None:
            kwargs['extra_body'] = {'chat_template_kwargs': {'enable_thinking': value.lower() == 'true'}}
        response = await self.client.chat.completions.create(**kwargs)
        audit({'kind':'llm_http','endpoint':str(self.base_url),'model':kwargs.get('model'),
               'usage':response.usage.model_dump() if response.usage else None,
               'finish_reason':response.choices[0].finish_reason if response.choices else None,
               'enable_thinking':value})
        if response.choices and response.choices[0].finish_reason == 'length':
            raise ValueError('Local model output truncated before schema validation')
        return response

    async def close(self):
        await self.client.close()


def make_graphiti():
    if os.environ.get('GRAPH_DB_PROVIDER', 'neo4j') != 'neo4j':
        raise ValueError('Only the validated Neo4j backend is enabled in this release')
    if os.environ.get('GRAPH_SEARCH_RERANKER', 'rrf') != 'rrf':
        raise ValueError('Only explicit RRF search is enabled')
    if os.environ.get('GRAPH_LLM_STRUCTURED_OUTPUT_MODE', 'json_schema') not in {'json_schema','json_object'}:
        raise ValueError('Invalid structured output mode')
    if int(os.environ.get('GRAPH_INGEST_WORKERS', '1')) != 1:
        raise ValueError('This release requires one persistent ingestion worker')
    llm_url = local_url("GRAPH_LLM_BASE_URL")
    embed_url = local_url("GRAPH_EMBEDDING_BASE_URL")
    llm = ValidatedLLM(
        LLMConfig(api_key=os.environ["GRAPH_LLM_API_KEY"], base_url=llm_url,
                  model=os.environ["GRAPH_LLM_MODEL"],
                  small_model=os.environ["GRAPH_LLM_SMALL_MODEL"], temperature=0.1),
        client=LocalChatClient(AsyncOpenAI(api_key=os.environ["GRAPH_LLM_API_KEY"], base_url=llm_url,
                           timeout=180, max_retries=0,
                           http_client=DefaultAsyncHttpxClient(trust_env=False))),
        max_tokens=8192,
        structured_output_mode=os.environ.get("GRAPH_LLM_STRUCTURED_OUTPUT_MODE", "json_schema"))
    embedder = ValidatedEmbedder(OpenAIEmbedderConfig(
        api_key=os.environ["GRAPH_EMBEDDING_API_KEY"], base_url=embed_url,
        embedding_model=os.environ["GRAPH_EMBEDDING_MODEL"],
        embedding_dim=int(os.environ["GRAPH_EMBEDDING_DIM"])),
        client=AsyncOpenAI(api_key=os.environ["GRAPH_EMBEDDING_API_KEY"], base_url=embed_url,
                           timeout=180, max_retries=0,
                           http_client=DefaultAsyncHttpxClient(trust_env=False)))
    return Graphiti(local_url("NEO4J_URI"), os.environ["NEO4J_USER"],
                    os.environ["NEO4J_PASSWORD"], llm_client=llm, embedder=embedder,
                    cross_encoder=DisabledCrossEncoder(),
                    max_coroutines=int(os.environ.get('GRAPH_MAX_COROUTINES', '2')))
