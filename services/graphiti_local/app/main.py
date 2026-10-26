"""Versioned loopback API and one persistent, serial ingestion worker."""
import asyncio
import fcntl
import json
import logging
import os
import secrets
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
from fastapi import FastAPI, Request, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator

ROOT = Path(__file__).resolve().parents[3]
load_dotenv(ROOT / "deploy/graphiti-local/.env")
os.environ["GRAPH_AUDIT_PATH"] = str(ROOT / "data/graphiti-local/model-calls.jsonl")

from graphiti_core.nodes import EpisodeType, EpisodicNode
from graphiti_core.search.search_config_recipes import COMBINED_HYBRID_SEARCH_RRF, EDGE_HYBRID_SEARCH_RRF, NODE_HYBRID_SEARCH_RRF
from .clients import make_graphiti
from .journal import Journal, StoreError, fingerprint
from .neo4j import Neo4jQueries, node_dto, edge_dto, temporal_filter
from .ontology import compile_ontology

logger = logging.getLogger(__name__)


async def worker(app):
    journal, graphiti = app.state.journal, app.state.graphiti
    while not app.state.stopping:
        job = journal.claim()
        if not job:
            await asyncio.sleep(0.25)
            continue
        try:
            graph = journal.graph(job["graph_id"])
            if graph["fingerprint"] != app.state.profile:
                raise StoreError("configuration_mismatch", "Model configuration changed; rebuild graph")
            entities, edges, mapping = compile_ontology(json.loads(graph["ontology"]))
            deadline = time.monotonic() + int(os.environ.get("GRAPH_JOB_DEADLINE_SECONDS", "1800"))
            while item := journal.next_item(job["id"]):
                payload = json.loads(item["payload"])
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("Ingestion job deadline exceeded")
                # In 0.31.2 uuid= means "load an existing episode", rather than
                # "create using this UUID". Save the deterministic source first.
                reference = datetime.fromisoformat(payload["reference_time"].replace("Z", "+00:00"))
                previous = await graphiti.retrieve_episodes(reference, group_ids=[graph["group_id"]], source=EpisodeType.text)
                episode = EpisodicNode(uuid=item["episode_uuid"], name=payload["item_id"],
                    group_id=graph["group_id"], labels=[], source=EpisodeType.text,
                    content=payload["body"], source_description=payload["source_description"],
                    created_at=datetime.now(timezone.utc), valid_at=reference)
                await episode.save(graphiti.driver)
                await asyncio.wait_for(graphiti.add_episode(
                    name=payload["item_id"], episode_body=payload["body"],
                    source_description=payload["source_description"],
                    reference_time=datetime.fromisoformat(payload["reference_time"].replace("Z", "+00:00")),
                    source=EpisodeType.text, group_id=graph["group_id"], uuid=item["episode_uuid"],
                    previous_episode_uuids=[e.uuid for e in previous],
                    entity_types=entities or None, edge_types=edges or None, edge_type_map=mapping or None,
                    custom_extraction_instructions="保留中文实体名称与中文事实。只提取有来源依据的事实。"), remaining)
                # Verify the source record before advancing SQLite's commit watermark.
                rows = await app.state.queries.query("MATCH (e:Episodic) WHERE e.group_id=$g AND e.uuid=$id RETURN e.uuid AS uuid", g=graph["group_id"], id=item["episode_uuid"])
                if not rows:
                    raise StoreError("ingestion_uncertain", "Episode absent after Graphiti returned")
                journal.commit_item(job["id"], item["sequence"])
            if journal.job(job['id'])['status'] == 'cancelled':
                continue
            await app.state.queries.stats(journal.graph(job["graph_id"]))
            journal.finish_job(job["id"])
        except BaseException as error:
            journal.fail_job(job["id"], f"{type(error).__name__}: rebuild required", uncertain=True)
            logger.exception("Ingestion %s became uncertain", job["id"])
            if isinstance(error, asyncio.CancelledError):
                raise


@asynccontextmanager
async def lifespan(app):
    data_dir = Path(os.environ.get("GRAPH_DATA_DIR", str(ROOT / "data/graphiti-local")))
    data_dir.mkdir(parents=True, exist_ok=True)
    lock = (data_dir / "worker.lock").open("a")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    if not os.environ.get("GRAPHITI_SERVICE_TOKEN"):
        raise RuntimeError("GRAPHITI_SERVICE_TOKEN is required")
    journal = Journal(data_dir / "journal.sqlite")
    journal.recover()
    graph = make_graphiti()
    if getattr(graph.driver, "_init_task", None):
        await graph.driver._init_task
    await graph.build_indices_and_constraints()
    app.state.graphiti, app.state.journal = graph, journal
    app.state.queries = Neo4jQueries(graph.driver)
    profile = {k: os.environ.get(k) for k in ["GRAPH_LLM_MODEL", "GRAPH_LLM_SMALL_MODEL", "GRAPH_LLM_BASE_URL", "GRAPH_LLM_STRUCTURED_OUTPUT_MODE", "GRAPH_EMBEDDING_MODEL", "GRAPH_EMBEDDING_DIM", "GRAPH_EMBEDDING_BASE_URL"]}
    provenance = ROOT / "data/graphiti-local/bge-m3/provenance.json"
    profile["embedding_provenance"] = json.loads(provenance.read_text()) if provenance.exists() else os.environ.get("GRAPH_EMBEDDING_REVISION")
    app.state.profile = fingerprint(profile)
    app.state.stopping = False
    task = asyncio.create_task(worker(app))
    try:
        yield
    finally:
        app.state.stopping = True
        # Finish the current accepted item where possible. Forced termination
        # is recovered as uncertain on the next boot.
        try:
            await asyncio.wait_for(asyncio.shield(task), 30)
        except TimeoutError:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await graph.close()
        await graph.llm_client.client.close()
        await graph.embedder.client.close()
        journal.db.close()
        lock.close()


app = FastAPI(lifespan=lifespan)


@app.middleware("http")
async def protocol(request: Request, call_next):
    request_id = request.headers.get("X-Request-ID", str(uuid.uuid4()))
    if request.url.path.startswith("/internal/"):
        expected = "Bearer " + os.environ.get("GRAPHITI_SERVICE_TOKEN", "")
        if not os.environ.get("GRAPHITI_SERVICE_TOKEN") or not secrets.compare_digest(request.headers.get("Authorization", ""), expected):
            return JSONResponse({"error": {"code": "unauthorized", "message": "Invalid service credential"}, "request_id": request_id}, status_code=401)
        if request.headers.get("X-Graph-Protocol", "1") != "1":
            return JSONResponse({"error": {"code": "protocol_mismatch", "message": "Expected protocol 1"}}, status_code=400)
    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    response.headers["X-Graph-Protocol"] = "1"
    return response


@app.exception_handler(StoreError)
async def store_error(request, error):
    return JSONResponse({"error": {"code": error.code, "message": str(error)}}, status_code=error.status)


@app.exception_handler(Exception)
async def dependency_error(request, error):
    logger.exception('Local graph dependency failed')
    return JSONResponse({'error': {'code': 'dependency_unavailable', 'message': '本地图谱依赖调用失败，请检查服务日志与原任务状态'}}, status_code=503)


@app.get("/healthz")
async def health():
    return {"status": "alive", "protocol_version": 1}


@app.get("/internal/v1/readyz")
async def ready():
    try:
        await app.state.queries.query("RETURN 1 AS ok")
        models = await app.state.graphiti.llm_client.client.models.list()
        if os.environ["GRAPH_LLM_MODEL"] not in {m.id for m in models.data}:
            raise ValueError("Configured LLM alias absent")
        vector = await app.state.graphiti.embedder.create("就绪检查")
        app.state.journal.db.execute("SELECT count(*) FROM jobs").fetchone()
        return {"ready": True, "embedding_dimension": len(vector), "embedding_profile_id": app.state.profile}
    except Exception as error:
        return JSONResponse({"ready": False, "error": type(error).__name__}, status_code=503)


class GraphRequest(BaseModel):
    project_id: str = Field(min_length=1)
    ontology: dict
    build_spec: dict = Field(default_factory=dict)


class IngestionItem(BaseModel):
    item_id: str = Field(min_length=1)
    body: str = Field(min_length=1, max_length=20000)
    reference_time: datetime
    source_description: str = Field(min_length=1)
    event_count: int = Field(default=0, ge=0)
    metadata: dict = Field(default_factory=dict)

    @field_validator("reference_time")
    @classmethod
    def timezone_required(cls, value):
        if value.tzinfo is None:
            raise ValueError("reference_time must contain UTC offset")
        return value


class IngestionRequest(BaseModel):
    operation_key: str = Field(min_length=1, max_length=300)
    items: list[IngestionItem] = Field(min_length=1, max_length=1000)


@app.post("/internal/v1/graphs", status_code=201)
async def create_graph(body: GraphRequest):
    try:
        compile_ontology(body.ontology)
    except (ValueError, KeyError) as error:
        raise StoreError("invalid_ontology", str(error), 400)
    result = app.state.journal.create_graph(body.project_id, body.ontology, body.build_spec, app.state.profile)
    return {"graph_id": result["id"], "build_revision": result["group_id"], "embedding_profile_id": result["fingerprint"], "graph_schema_version": 1}


@app.post("/internal/v1/graphs/{graph_id}/ingestions", status_code=202)
async def ingest(graph_id: str, body: IngestionRequest):
    ids = [item.item_id for item in body.items]
    if len(ids) != len(set(ids)):
        raise StoreError("duplicate_item", "item_id must be unique within operation", 400)
    graph = app.state.journal.graph(graph_id)
    if graph["fingerprint"] != app.state.profile:
        raise StoreError("configuration_mismatch", "Graph model configuration differs; rebuild")
    return app.state.journal.submit(graph_id, [i.model_dump(mode="json") for i in body.items], body.operation_key)


@app.get("/internal/v1/jobs/{job_id}")
async def job(job_id: str):
    return app.state.journal.job(job_id)


@app.post('/internal/v1/jobs/{job_id}/cancel', status_code=202)
async def cancel(job_id: str):
    return app.state.journal.cancel(job_id)


def readable(graph_id, export=False):
    graph = app.state.journal.graph(graph_id)
    if graph["status"] in {"deleting", "uncertain", "failed"} or (export and graph["status"] != "ready"):
        raise StoreError("graph_busy", "Graph is not a readable completed version")
    if graph["fingerprint"] != app.state.profile:
        raise StoreError("configuration_mismatch", "Configured model differs from graph index")
    return graph


@app.get("/internal/v1/graphs/{graph_id}/{kind}")
async def page(graph_id: str, kind: Literal["nodes", "edges"], cursor: str | None = None, limit: int = Query(100, ge=1, le=1000)):
    return await app.state.queries.page(readable(graph_id, export=True), kind, cursor, limit)


@app.get("/internal/v1/graphs/{graph_id}/nodes/{node_id}")
async def node(graph_id: str, node_id: str):
    return await app.state.queries.node(readable(graph_id), node_id)


@app.get("/internal/v1/graphs/{graph_id}/nodes/{node_id}/neighbors")
async def neighbors(graph_id: str, node_id: str):
    return {"edges": await app.state.queries.neighbors(readable(graph_id), node_id)}


@app.get("/internal/v1/graphs/{graph_id}/stats/summary")
async def stats(graph_id: str):
    return await app.state.queries.stats(app.state.journal.graph(graph_id))


class SearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=4000)
    scope: Literal["edges", "nodes", "combined"] = "edges"
    temporal_mode: Literal["current", "as_of", "all_history"] = "current"
    as_of: datetime | None = None
    limit: int = Field(default=10, ge=1, le=100)


@app.post("/internal/v1/graphs/{graph_id}/search")
async def search(graph_id: str, body: SearchRequest):
    graph = readable(graph_id)
    reference = body.as_of
    if reference is None and body.temporal_mode == 'current':
        clock = json.loads(graph['build_spec']).get('memory_reference_time')
        reference = datetime.fromisoformat(clock) if clock else None
    recipes = {"edges": EDGE_HYBRID_SEARCH_RRF, "nodes": NODE_HYBRID_SEARCH_RRF, "combined": COMBINED_HYBRID_SEARCH_RRF}
    config = recipes[body.scope].model_copy(deep=True)
    config.limit = body.limit
    result = await app.state.graphiti.search_(body.query, config=config, group_ids=[graph["group_id"]], search_filter=temporal_filter(body.temporal_mode, reference))
    if any(n.group_id != graph["group_id"] for n in result.nodes) or any(e.group_id != graph["group_id"] for e in result.edges):
        raise StoreError("namespace_violation", "Search returned data from another namespace", 503)
    nodes = [node_dto({**n.attributes, **n.model_dump(exclude={"attributes", "name_embedding"})}, n.labels) for n in result.nodes]
    edges = [edge_dto({**e.attributes, **e.model_dump(exclude={"attributes", "fact_embedding"})}, e.source_node_uuid, e.target_node_uuid) for e in result.edges]
    return {"graph_id": graph_id, "query": body.query, "facts": [e["fact"] for e in edges], "nodes": nodes, "edges": edges, "total_count": len(nodes)+len(edges)}


class FinalizeRequest(BaseModel):
    expected_watermark: int = Field(ge=0)


class RunRequest(BaseModel):
    run_id: str = Field(min_length=1)


@app.post("/internal/v1/graphs/{graph_id}/claim-run")
async def claim_run(graph_id: str, body: RunRequest):
    return app.state.journal.claim_run(graph_id, body.run_id)


@app.get("/internal/v1/graphs/{graph_id}/metadata/details")
async def metadata(graph_id: str):
    graph = app.state.journal.graph(graph_id)
    return {**graph,
        'leases': [dict(r) for r in app.state.journal.db.execute('SELECT owner,kind,created_at FROM leases WHERE graph_id=?', (graph_id,))],
        'jobs': [dict(r) for r in app.state.journal.db.execute('SELECT id,operation_key,status FROM jobs WHERE graph_id=? ORDER BY created_at', (graph_id,))]}


@app.post("/internal/v1/graphs/{graph_id}/finalize")
async def finalize(graph_id: str, body: FinalizeRequest):
    return app.state.journal.finalize(graph_id, body.expected_watermark)


class LeaseRequest(BaseModel):
    owner: str = Field(min_length=1, max_length=200)
    kind: Literal["report", "simulation", "export"]


@app.post("/internal/v1/graphs/{graph_id}/leases")
async def lease(graph_id: str, body: LeaseRequest):
    app.state.journal.lease(graph_id, body.owner, body.kind)
    return {"acquired": True}


@app.delete("/internal/v1/graphs/{graph_id}/leases/{owner}")
async def release(graph_id: str, owner: str):
    app.state.journal.release(graph_id, owner)
    return {"released": True}


@app.delete("/internal/v1/graphs/{graph_id}")
async def delete(graph_id: str):
    graph = app.state.journal.begin_delete(graph_id)
    await app.state.queries.delete(graph)
    app.state.journal.db.execute("UPDATE graphs SET status='deleted' WHERE id=?", (graph_id,))
    return {"deleted": True, "graph_id": graph_id}
