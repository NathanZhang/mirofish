"""Offline coordinated backup and a real restore check in an isolated volume."""
import argparse
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from dotenv import dotenv_values

ROOT=Path(__file__).resolve().parents[2]
IMAGE='neo4j:5.26.0-community@sha256:69d0bc3286a613bdf6fe0165392f77f5459081f2ddaa04da8cbf6fa7286a38ce'
VOLUME='mirofish-graphiti_neo4j-data'


def docker(*args):
    return subprocess.check_output(['docker',*map(str,args)],text=True).strip()


def listener(port):
    try:
        with socket.create_connection(('127.0.0.1',port),timeout=1):return True
    except OSError:return False


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('action',choices=['create','verify'])
    parser.add_argument('--backup',type=Path)
    parser.add_argument('--graph-id')
    parser.add_argument('--expected-nodes',type=int)
    parser.add_argument('--expected-edges',type=int)
    args=parser.parse_args()
    config=dotenv_values(ROOT/'deploy/graphiti-local/.env')
    backup=args.backup or ROOT/'data/backups'/datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')
    if args.action=='create':
        if any(listener(p) for p in [5001,5101,7687]):
            raise RuntimeError('Stop Flask, Graphiti and Neo4j before taking a coordinated backup')
        if docker('inspect','--format','{{.State.Running}}','mirofish-graphiti-neo4j-1') != 'false':
            raise RuntimeError('Wait until the Neo4j container has fully stopped before copying its volume')
        backup.mkdir(parents=True,exist_ok=False);backup.chmod(0o700)
        shutil.copytree(ROOT/'backend/uploads',backup/'uploads')
        shutil.copytree(ROOT/'data/graphiti-local',backup/'graphiti-local',ignore=shutil.ignore_patterns('*.log','processes.json','worker.lock'))
        shutil.copytree(ROOT,backup/'source',ignore=shutil.ignore_patterns('.git','.venv','node_modules','data','uploads','logs','__pycache__','.pytest_cache','dist','.env'))
        credentials=backup/'credentials';credentials.mkdir(mode=0o700)
        for src,name in [(ROOT/'.env','mirofish.env'),(ROOT/'deploy/graphiti-local/.env','graphiti.env')]:
            shutil.copy2(src,credentials/name);(credentials/name).chmod(0o600)
        script=f'tar -C /source -cf /backup/neo4j-data.tar . && chown {os.getuid()}:{os.getgid()} /backup/neo4j-data.tar && chmod 600 /backup/neo4j-data.tar'
        docker('run','--rm','--network','none','--user','root','--entrypoint','sh',
               '-v',VOLUME+':/source:ro','-v',str(backup.resolve())+':/backup',IMAGE,'-c',script)
        (backup/'manifest.json').write_text(json.dumps({'created_at':datetime.now(timezone.utc).isoformat(),
            'neo4j_image':IMAGE,'database_volume':VOLUME,'graph_id':args.graph_id,'expected_nodes':args.expected_nodes,'expected_edges':args.expected_edges,
            'source_commit':subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip()},indent=2))
        print(backup);return
    if not args.backup:raise ValueError('--backup is required for verify')
    config=dotenv_values(backup/'credentials/graphiti.env')
    manifest=json.loads((backup/'manifest.json').read_text())
    graph_id=args.graph_id or manifest['graph_id']
    if not graph_id:raise ValueError('graph ID required for count/watermark verification')
    if listener(17687) or listener(17474):raise RuntimeError('Restore verification ports are occupied')
    suffix=uuid.uuid4().hex[:10];volume='mirofish-restore-'+suffix;container=volume
    auth=backup/'credentials/restore.env'
    auth.write_text('NEO4J_AUTH=neo4j/'+config['NEO4J_PASSWORD']+'\nNEO4J_server_memory_heap_initial__size=256M\nNEO4J_server_memory_heap_max__size=512M\nNEO4J_server_memory_pagecache_size=256M\n');auth.chmod(0o600)
    docker('volume','create',volume)
    try:
        docker('run','--rm','--network','none','--user','root','--entrypoint','sh',
               '-v',volume+':/restore','-v',str(backup.resolve())+':/backup:ro',IMAGE,
               '-c','tar -C /restore -xf /backup/neo4j-data.tar')
        docker('run','-d','--name',container,'--memory','1536m','--cpus','2',
               '--env-file',auth,'-p','127.0.0.1:17687:7687','-p','127.0.0.1:17474:7474','-v',volume+':/data',IMAGE)
        from neo4j import GraphDatabase
        driver=GraphDatabase.driver('bolt://127.0.0.1:17687',auth=('neo4j',config['NEO4J_PASSWORD']))
        deadline=time.monotonic()+120
        while True:
            try:driver.verify_connectivity();break
            except Exception:
                if time.monotonic()>deadline:raise
                time.sleep(2)
        with sqlite3.connect(backup/'graphiti-local/journal.sqlite') as db:
            db.row_factory=sqlite3.Row
            graph=dict(db.execute('SELECT * FROM graphs WHERE id=?',(graph_id,)).fetchone())
            jobs=[dict(r) for r in db.execute('SELECT id,status FROM jobs WHERE graph_id=?',(graph_id,))]
        nodes=driver.execute_query('MATCH (n:Entity) WHERE n.group_id=$g RETURN count(n) AS count',g=graph['group_id']).records[0]['count']
        edges=driver.execute_query('MATCH (a:Entity)-[r:RELATES_TO]->(b:Entity) WHERE r.group_id=$g AND a.group_id=$g AND b.group_id=$g RETURN count(r) AS count',g=graph['group_id']).records[0]['count']
        if manifest.get('expected_nodes') is not None:assert nodes==manifest['expected_nodes']
        if manifest.get('expected_edges') is not None:assert edges==manifest['expected_edges']
        episodes=driver.execute_query('MATCH (e:Episodic) WHERE e.group_id=$g RETURN count(e) AS count',g=graph['group_id']).records[0]['count']
        receipt=json.loads(next((backup/'uploads/simulations').glob('*/memory-receipt.json')).read_text())
        # Choose the receipt belonging to the specified restored graph.
        for path in (backup/'uploads/simulations').glob('*/memory-receipt.json'):
            candidate=json.loads(path.read_text())
            if candidate['graph_id']==graph_id:receipt=candidate;break
        assert graph['finalized'] and graph['watermark']==receipt['committed_watermark']==receipt['expected_watermark']
        assert all(j['status']=='completed' for j in jobs)
        result={'status':'passed','graph_id':graph_id,'node_count':nodes,'edge_count':edges,'episode_count':episodes,
                'memory_watermark':graph['watermark'],'jobs':jobs,'isolated_restore_volume':volume,'image':IMAGE}
        (backup/'restore-verification.json').write_text(json.dumps(result,indent=2))
        driver.close();print(json.dumps(result,indent=2))
    finally:
        # These names are freshly generated above and cannot name user volumes.
        subprocess.run(['docker','stop','--time','30',container],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        subprocess.run(['docker','rm',container],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        subprocess.run(['docker','volume','rm',volume],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)


if __name__=='__main__':main()
