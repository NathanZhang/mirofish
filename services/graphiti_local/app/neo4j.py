"""Neo4j-only parameterized queries. No Cypher supplied by API callers."""
import base64
import json
from datetime import datetime, timezone

from graphiti_core.search.search_filters import SearchFilters, DateFilter, ComparisonOperator as Op
from .journal import StoreError

NODE_CORE = {"uuid", "name", "summary", "group_id", "created_at", "name_embedding", "labels"}
EDGE_CORE = {"uuid", "name", "fact", "group_id", "created_at", "valid_at", "invalid_at",
             "expired_at", "episodes", "fact_embedding", "source_node_uuid", "target_node_uuid"}


def json_value(value):
    if hasattr(value, "iso_format"):
        return value.iso_format()
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: json_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_value(v) for v in value]
    return value


def node_dto(props, labels):
    return {"uuid": props["uuid"], "name": props.get("name", ""), "labels": labels,
            "summary": props.get("summary", ""), "created_at": json_value(props.get("created_at")),
            "attributes": json_value({k: v for k, v in props.items() if k not in NODE_CORE})}


def edge_dto(props, source, target):
    return {"uuid": props["uuid"], "name": props.get("name", ""), "fact": props.get("fact", ""),
            "fact_type": props.get("name", ""), "source_node_uuid": source, "target_node_uuid": target,
            "episodes": props.get("episodes", []),
            **{field: json_value(props.get(field)) for field in ["created_at", "valid_at", "invalid_at", "expired_at"]},
            "attributes": json_value({k: v for k, v in props.items() if k not in EDGE_CORE})}


def temporal_filter(mode, as_of=None):
    if mode == "all_history":
        return SearchFilters()
    if mode not in {"current", "as_of"}:
        raise StoreError("invalid_temporal_mode", "Expected current, as_of or all_history", 400)
    if mode == "as_of" and as_of is None:
        raise StoreError("invalid_as_of", "as_of timestamp required", 400)
    at = as_of or datetime.now(timezone.utc)
    if at.tzinfo is None:
        raise StoreError("invalid_as_of", "as_of must include UTC offset", 400)
    filters = SearchFilters(
        valid_at=[[DateFilter(comparison_operator=Op.is_null)],
                  [DateFilter(date=at, comparison_operator=Op.less_than_equal)]],
        invalid_at=[[DateFilter(comparison_operator=Op.is_null)],
                    [DateFilter(date=at, comparison_operator=Op.greater_than)]])
    # expired_at is the transaction-time supersession marker. Historical as_of
    # uses the valid-time interval; all_history preserves both kinds of time.
    if mode == "current":
        filters.expired_at = [[DateFilter(comparison_operator=Op.is_null)]]
    return filters


class Neo4jQueries:
    def __init__(self, driver):
        self.driver = driver

    async def query(self, query, **params):
        rows, _, _ = await self.driver.execute_query(query, **params)
        return rows

    def cursor(self, graph, kind, value):
        if not value:
            return ""
        try:
            data = json.loads(base64.urlsafe_b64decode(value))
            if data[:2] != [graph["group_id"], kind]:
                raise ValueError()
            return str(data[2])
        except Exception:
            raise StoreError("invalid_cursor", "Cursor belongs to another graph version or collection", 400)

    async def page(self, graph, kind, cursor=None, limit=100):
        after = self.cursor(graph, kind, cursor)
        if kind == "nodes":
            rows = await self.query("MATCH (n:Entity) WHERE n.group_id=$g AND n.uuid>$after RETURN properties(n) AS props, labels(n) AS labels ORDER BY n.uuid LIMIT $limit",
                                    g=graph["group_id"], after=after, limit=limit+1)
            items = [node_dto(r["props"], r["labels"]) for r in rows]
        else:
            rows = await self.query("MATCH (a:Entity)-[r:RELATES_TO]->(b:Entity) WHERE r.group_id=$g AND a.group_id=$g AND b.group_id=$g AND r.uuid>$after RETURN properties(r) AS props,a.uuid AS source,b.uuid AS target ORDER BY r.uuid LIMIT $limit",
                                    g=graph["group_id"], after=after, limit=limit+1)
            items = [edge_dto(r["props"], r["source"], r["target"]) for r in rows]
        following = base64.urlsafe_b64encode(json.dumps([graph["group_id"], kind, items[limit-1]["uuid"]]).encode()).decode() if len(items) > limit else None
        return {"items": items[:limit], "next_cursor": following, "graph_id": graph["id"]}

    async def node(self, graph, node_id):
        rows = await self.query("MATCH (n:Entity) WHERE n.group_id=$g AND n.uuid=$id RETURN properties(n) AS props, labels(n) AS labels", g=graph["group_id"], id=node_id)
        if not rows:
            raise StoreError("node_not_found", "Node absent from this graph", 404)
        return node_dto(rows[0]["props"], rows[0]["labels"])

    async def neighbors(self, graph, node_id):
        await self.node(graph, node_id)
        rows = await self.query("MATCH (a:Entity)-[r:RELATES_TO]->(b:Entity) WHERE a.group_id=$g AND b.group_id=$g AND r.group_id=$g AND (a.uuid=$id OR b.uuid=$id) RETURN properties(r) AS props,a.uuid AS source,b.uuid AS target ORDER BY r.uuid", g=graph["group_id"], id=node_id)
        return [edge_dto(r["props"], r["source"], r["target"]) for r in rows]

    async def stats(self, graph):
        nodes = await self.query("MATCH (n:Entity) WHERE n.group_id=$g RETURN count(n) AS count", g=graph["group_id"])
        edges = await self.query("MATCH (a:Entity)-[r:RELATES_TO]->(b:Entity) WHERE r.group_id=$g AND a.group_id=$g AND b.group_id=$g RETURN count(r) AS count", g=graph["group_id"])
        return {"graph_id": graph["id"], "node_count": nodes[0]["count"], "edge_count": edges[0]["count"],
                "committed_watermark": graph["watermark"], "finalized": bool(graph["finalized"]), "status": graph["status"]}

    async def delete(self, graph):
        # Every internal node written by Graphiti carries the namespace group_id.
        await self.query("MATCH (n) WHERE n.group_id=$g DETACH DELETE n", g=graph["group_id"])
        rows = await self.query("MATCH (n) WHERE n.group_id=$g RETURN count(n) AS count", g=graph["group_id"])
        if rows[0]["count"]:
            raise StoreError("delete_uncertain", "Namespace deletion could not be verified")
