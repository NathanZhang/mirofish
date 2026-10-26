"""Synchronous HTTP adapter. Async drivers live only in the local service."""
import time
import uuid
import json
import sqlite3
from pathlib import Path
from datetime import datetime
from contextlib import contextmanager
from urllib.parse import quote
from urllib.parse import urlparse

import httpx

from ...config import Config
from .protocol import GraphNotFound, GraphBusy, IngestionUncertain, DependencyUnavailable


class GraphitiLocalStore:
    backend = "graphiti_local"

    def __init__(self, client=None):
        if not Config.GRAPHITI_SERVICE_TOKEN:
            raise DependencyUnavailable("GRAPHITI_SERVICE_TOKEN 未配置")
        endpoint = urlparse(Config.GRAPHITI_SERVICE_URL)
        if endpoint.scheme != 'http' or endpoint.hostname not in {'127.0.0.1','localhost','::1'}:
            raise DependencyUnavailable('GRAPHITI_SERVICE_URL 须为本机 HTTP 端点')
        self.client = client or httpx.Client(base_url=Config.GRAPHITI_SERVICE_URL.rstrip("/")+"/internal/v1/",
            headers={"Authorization": f"Bearer {Config.GRAPHITI_SERVICE_TOKEN}", "X-Graph-Protocol": "1"},
            timeout=60, trust_env=False)

    def request(self, method, path, **kwargs):
        try:
            response = self.client.request(method, path, headers={"X-Request-ID": str(uuid.uuid4())}, **kwargs)
        except httpx.TransportError as error:
            raise DependencyUnavailable("本地 Graphiti 服务连接失败；已受理写入请按操作键查询，不要更换键重放") from error
        if response.is_error:
            try:
                error = response.json().get("error", {})
                message = error.get("message", "Graphiti request failed")
                code = error.get("code", "")
            except (ValueError, AttributeError):
                message, code = "Graphiti request failed", ""
            cls = GraphNotFound if response.status_code == 404 else IngestionUncertain if code == "ingestion_uncertain" else GraphBusy if response.status_code in {400, 409, 422} else DependencyUnavailable
            raise cls(message)
        return response.json()

    def create_graph(self, project_id, ontology, build_spec):
        return self.request("POST", "graphs", json={"project_id": project_id, "ontology": ontology, "build_spec": build_spec})

    def submit_ingestion(self, graph_id, items, operation_key):
        return self.request("POST", f"graphs/{graph_id}/ingestions", json={"items": items, "operation_key": operation_key})

    def get_ingestion(self, job_id):
        return self.request("GET", f"jobs/{job_id}")

    def cancel_ingestion(self, job_id):
        return self.request('POST', f'jobs/{job_id}/cancel')

    def list_nodes(self, graph_id, cursor=None, limit=100):
        return self.request("GET", f"graphs/{graph_id}/nodes", params={"limit": limit, **({"cursor": cursor} if cursor else {})})

    def list_edges(self, graph_id, cursor=None, limit=100):
        return self.request("GET", f"graphs/{graph_id}/edges", params={"limit": limit, **({"cursor": cursor} if cursor else {})})

    def all_items(self, graph_id, kind):
        cursor, seen, items = None, set(), []
        while True:
            page = (self.list_nodes if kind == "nodes" else self.list_edges)(graph_id, cursor)
            for item in page["items"]:
                if item["uuid"] in seen:
                    raise DependencyUnavailable("Graphiti 分页出现重复 UUID；导出已中止")
                seen.add(item["uuid"])
                items.append(item)
            following = page.get("next_cursor")
            if not following:
                return items
            if following == cursor:
                raise DependencyUnavailable("Graphiti 分页游标未推进")
            cursor = following

    @contextmanager
    def lease(self, graph_id, owner=None, kind="export"):
        owner = owner or "read_"+uuid.uuid4().hex
        self.acquire_lease(graph_id, owner, kind)
        try:
            yield
        finally:
            self.release_lease(graph_id, owner)

    def acquire_lease(self, graph_id, owner, kind):
        return self.request("POST", f"graphs/{graph_id}/leases", json={"owner": owner, "kind": kind})

    def release_lease(self, graph_id, owner):
        return self.request("DELETE", f"graphs/{graph_id}/leases/{quote(owner, safe='')}")

    def export(self, graph_id):
        with self.lease(graph_id):
            nodes = self.all_items(graph_id, "nodes")
            edges = self.all_items(graph_id, "edges")
            stats = self.get_stats(graph_id)
            if len(nodes) != stats["node_count"] or len(edges) != stats["edge_count"]:
                raise DependencyUnavailable("图谱统计与完整分页不一致")
            return {"graph_id": graph_id, "nodes": nodes, "edges": edges,
                    "node_count": len(nodes), "edge_count": len(edges)}

    def get_node(self, graph_id, node_uuid):
        return self.request("GET", f"graphs/{graph_id}/nodes/{node_uuid}")

    def get_neighbors(self, graph_id, node_uuid):
        return self.request("GET", f"graphs/{graph_id}/nodes/{node_uuid}/neighbors")["edges"]

    def search(self, graph_id, query, scope="edges", temporal_mode="current", limit=10, as_of=None):
        if temporal_mode == 'current' and as_of is None:
            as_of = self._completed_simulation_time(graph_id)
        return self.request("POST", f"graphs/{graph_id}/search", json={"query": query, "scope": scope,
            "temporal_mode": temporal_mode, "limit": limit, "as_of": as_of})

    @staticmethod
    def _completed_simulation_time(graph_id):
        # A completed simulated day may be ahead of wall clock time. Query the
        # committed run clock, rather than hiding its facts until real hours pass.
        from ..simulation_manager import SimulationManager
        for state in SimulationManager().list_simulations():
            if state.graph_id != graph_id:
                continue
            root = Path(Config.OASIS_SIMULATION_DATA_DIR) / state.simulation_id
            if not (root/'memory-receipt.json').exists() or not (root/'memory-outbox.sqlite').exists():
                continue
            receipt = json.loads((root/'memory-receipt.json').read_text())
            if receipt.get('graph_id') == graph_id and receipt.get('simulation_reference_time'):
                return receipt['simulation_reference_time']
            with sqlite3.connect((root/'memory-outbox.sqlite').resolve().as_uri()+'?mode=ro',uri=True) as db:
                clocks = [item['reference_time'] for row in db.execute('SELECT items FROM batches')
                          for item in json.loads(row[0])]
            if clocks:
                return max(clocks,key=lambda value:datetime.fromisoformat(value))
        return None

    def get_stats(self, graph_id):
        return self.request("GET", f"graphs/{graph_id}/stats/summary")

    def finalize_memory(self, graph_id, expected_watermark):
        return self.request("POST", f"graphs/{graph_id}/finalize", json={"expected_watermark": expected_watermark})

    def delete_graph(self, graph_id):
        return self.request("DELETE", f"graphs/{graph_id}")

    def wait(self, job_id, timeout=None):
        deadline = time.monotonic() + (timeout or Config.GRAPH_JOB_DEADLINE_SECONDS)
        while time.monotonic() < deadline:
            job = self.get_ingestion(job_id)
            if job["status"] == "completed":
                return job
            if job["status"] in {"failed", "uncertain", "cancelled"}:
                raise IngestionUncertain(job.get("error") or "摄入失败或结果不确定；须在新 namespace 重建")
            time.sleep(1)
        raise GraphBusy("摄入仍未完成，请用原 job_id 继续查询")
