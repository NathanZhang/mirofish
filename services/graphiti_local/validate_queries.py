"""Real Neo4j synthetic fixtures for pagination/isolation, separate from user data."""
import asyncio
import json
import uuid
from pathlib import Path
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / 'deploy/graphiti-local/.env')
from app.clients import make_graphiti
from app.neo4j import Neo4jQueries


async def main():
    graphiti = make_graphiti()
    groups = ['pagination_' + uuid.uuid4().hex, 'isolation_' + uuid.uuid4().hex]
    queries = Neo4jQueries(graphiti.driver)
    evidence = {'groups': groups, 'synthetic_fixture': True}
    try:
        if getattr(graphiti.driver, '_init_task', None):
            await graphiti.driver._init_task
        for group in groups:
            await queries.query('UNWIND range(0,250) AS i CREATE (:Entity {uuid:$g+toString(i),group_id:$g,name: CASE WHEN i=0 THEN "周先生" ELSE "fixture"+toString(i) END,summary:"synthetic pagination fixture"})',g=group)
            await queries.query('MATCH (a:Entity),(b:Entity) WHERE a.group_id=$g AND b.group_id=$g AND a.uuid=$g+"0" AND b.uuid<>a.uuid CREATE (a)-[:RELATES_TO {uuid:b.uuid,group_id:$g,name:"FIXTURE",fact:"synthetic relationship",episodes:[]}]->(b)',g=group)
        g = {'id':'synthetic-graph','group_id':groups[0],'watermark':0,'finalized':0,'status':'ready'}
        for kind,expected in [('nodes',251),('edges',250)]:
            seen,cursor,pages=set(),None,0
            while True:
                page=await queries.page(g,kind,cursor,100)
                for item in page['items']:
                    assert item['uuid'] not in seen
                    assert item['uuid'].startswith(groups[0])
                    seen.add(item['uuid'])
                pages+=1;cursor=page['next_cursor']
                if not cursor:break
            assert len(seen)==expected and pages==3
            evidence[kind]={'count':len(seen),'pages':pages,'duplicates':0}
        stats=await queries.stats(g)
        assert stats['node_count']==251 and stats['edge_count']==250
        wrong=await queries.query('MATCH (n:Entity) WHERE n.group_id=$g AND n.name="周先生" RETURN n.uuid AS id',g=groups[1])
        try:
            await queries.node(g,wrong[0]['id'])
            raise AssertionError('Cross-namespace node accepted')
        except Exception as error:
            assert getattr(error,'status',None)==404
        assert len(await queries.neighbors(g,groups[0]+'0'))==250
        await queries.delete(g)
        other={'id':'other','group_id':groups[1],'watermark':0,'finalized':0,'status':'ready'}
        assert (await queries.stats(other))['node_count']==251
        evidence.update(status='passed',cross_namespace_node='404',other_project_after_delete=251,adjacent_edges=250)
    finally:
        for group in groups:
            await queries.delete({'group_id':group})
        await graphiti.close()
        out=ROOT.parent/'artifacts/mirofish-20261024/graphiti-local-acceptance-20261026/query-validation.json'
        out.write_text(json.dumps(evidence,ensure_ascii=False,indent=2))
    print(json.dumps(evidence,ensure_ascii=False))


if __name__=='__main__':asyncio.run(main())
