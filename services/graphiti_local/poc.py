"""Real P1 gate. Stores model output; never substitutes mock facts."""
import asyncio
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / "deploy/graphiti-local/.env")
os.environ["GRAPH_AUDIT_PATH"] = str(ROOT / "data/graphiti-local/model-calls.jsonl")
from app.clients import make_graphiti
from graphiti_core.nodes import EpisodeType
from graphiti_core.search.search_config_recipes import COMBINED_HYBRID_SEARCH_RRF
from app.neo4j import Neo4jQueries, temporal_filter


class Person(BaseModel):
    """A human participant in the community discussion."""
    role: str | None = Field(default=None, description="人物在社区中的角色")


async def main():
    graph = make_graphiti()
    group = "poc_" + uuid.uuid4().hex
    out = ROOT.parent / "artifacts/mirofish-20261024/graphiti-local-acceptance-20261026/poc.json"
    evidence = {"group_id": group, "started_at": datetime.now(timezone.utc).isoformat()}
    if "--resume" in sys.argv:
        evidence = json.loads(out.read_text())
        group = evidence["group_id"]
    try:
        if getattr(graph.driver, "_init_task", None):
            await graph.driver._init_task
        await graph.build_indices_and_constraints()
        print("indices ready", flush=True)
        vectors = await graph.embedder.create_batch(["周先生担忧噪声", "小林负责排班"])
        assert len(vectors) == 2 and all(len(v) == 1024 for v in vectors)
        evidence["embedding"] = {"batch": 2, "dimension": 1024, "distinct": vectors[0] != vectors[1]}
        for i, text in enumerate([
            "2026年10月24日，社区志愿者小林宣布社区书店的读书活动将在2026年10月31日周六开放。居民周先生担忧活动噪声。书店店主陈女士愿意赞助，小林负责志愿者排班。",
            "2026年10月25日，小林宣布变更：社区书店读书活动取消2026年10月31日周六开放，改为2026年11月1日周日开放。周先生表示如果控制噪声愿意参加。"
        ]):
            if f"episode_{i}" in evidence:
                continue
            print(f"ingesting {i+1}", flush=True)
            result = await graph.add_episode(name=f"poc-{i}", episode_body=text,
                source_description="P1 synthetic Chinese fixture", source=EpisodeType.text,
                reference_time=datetime(2026, 10, 24+i, tzinfo=timezone.utc),
                group_id=group, entity_types={"Person": Person},
                custom_extraction_instructions="保留原文中文实体名和中文事实；准确提取时间与人物角色。")
            evidence[f"episode_{i}"] = result.model_dump(mode="json")
            out.write_text(json.dumps(evidence, ensure_ascii=False, indent=2))
            print(f"committed {len(result.nodes)} nodes {len(result.edges)} edges", flush=True)
        config = COMBINED_HYBRID_SEARCH_RRF.model_copy(deep=True)
        config.limit = 10
        for question in ["谁担忧噪声？", "谁负责排班？", "书店愿意赞助吗？", "读书活动什么时候开放？"]:
            result = await graph.search_(question, config=config, group_ids=[group])
            evidence.setdefault("searches", {})[question] = result.model_dump(mode="json")
            assert result.edges or result.nodes, f"No evidence for {question}"
        communities, edges = await graph.build_communities(group_ids=[group])
        evidence["communities"] = [c.model_dump(mode="json") for c in communities]
        # Verify time history in actual DB; the business adapter adds explicit temporal filters.
        records, _, _ = await graph.driver.execute_query(
            "MATCH (a:Entity)-[r:RELATES_TO]->(b:Entity) WHERE r.group_id=$g RETURN properties(r) AS edge",
            g=group)
        evidence["all_history"] = [dict(r["edge"]) for r in records]
        for mode, at in [("current", None), ("as_of", datetime(2026, 10, 24, 12, tzinfo=timezone.utc))]:
            result = await graph.search_("读书活动什么时候开放？", config=config,
                group_ids=[group], search_filter=temporal_filter(mode, at))
            evidence[mode] = result.model_dump(mode="json")
        assert all(e["invalid_at"] is None and e["expired_at"] is None for e in evidence["current"]["edges"])
        assert any(e["invalid_at"] for e in evidence["as_of"]["edges"]), "Historical invalidated evidence missing"
        queries = Neo4jQueries(graph.driver)
        scratch, marker = "poc_delete_" + uuid.uuid4().hex, str(uuid.uuid4())
        await queries.query("CREATE (:Entity {uuid:$id,group_id:$g,name:'周先生',summary:'隔离删除标记'})", id=marker, g=scratch)
        before = await queries.query("MATCH (n) WHERE n.group_id=$g RETURN count(n) AS count", g=group)
        await queries.delete({"group_id": scratch})
        after = await queries.query("MATCH (n) WHERE n.group_id=$g RETURN count(n) AS count", g=group)
        assert before == after
        evidence["namespace_delete"] = {"deleted_group": scratch, "other_group_unchanged": True}
        evidence.pop("error", None)
        evidence.pop("error_type", None)
        evidence["status"] = "completed"
        print("P1 extraction/search/community complete", flush=True)
    except BaseException as error:
        evidence.update(status="failed", error_type=type(error).__name__, error=str(error)[:1500])
        raise
    finally:
        out.write_text(json.dumps(evidence, ensure_ascii=False, indent=2, default=str))
        await graph.close()


if __name__ == "__main__":
    asyncio.run(main())
