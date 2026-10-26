"""Rebuild a stopped run in a fresh namespace; publish only after full verification."""
import argparse
import hashlib
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'backend'))
from app.config import Config
from app.models.project import ProjectManager, ProjectStatus
from app.services.graph_store import get_graph_store
from app.services.graph_store.protocol import GraphBusy
from app.services.simulation_manager import SimulationManager
from app.services.simulation_runner import SimulationRunner, RunnerStatus
from app.services.report_agent import ReportManager
from app.services.text_processor import TextProcessor
from app.services.zep_graph_memory_updater import AgentActivity


def atomic_json(path, value):
    temporary=path.with_suffix(path.suffix+'.tmp')
    with temporary.open('w') as stream:
        json.dump(value,stream,ensure_ascii=False,indent=2);stream.flush();os.fsync(stream.fileno())
    temporary.replace(path)


def replay_inputs(root, config):
    """Reuse accepted batch payloads exactly; supplement only unbatched raw events."""
    with sqlite3.connect((root/'memory-outbox.sqlite').resolve().as_uri()+'?mode=ro',uri=True) as db:
        db.row_factory=sqlite3.Row
        base=datetime.fromisoformat(db.execute("SELECT value FROM meta WHERE key='reference_time'").fetchone()[0])
        batches=[dict(row) for row in db.execute('SELECT operation_key,items FROM batches ORDER BY rowid')]
        recorded={row['event_id']:dict(row) for row in db.execute('SELECT * FROM events')}
    items=[item for batch in batches for item in json.loads(batch['items'])]
    covered=[event_id for item in items for event_id in item.get('metadata',{}).get('event_ids',[])]
    if len(set(covered))!=len(covered):raise GraphBusy('Original batch boundaries overlap; reconcile before rebuilding')
    covered=set(covered);eligible=set();sequence=max([row['seq'] for row in recorded.values()]+[0])
    run_id=root.name
    for platform in ['twitter','reddit']:
        path=root/platform/'actions.jsonl'
        if not path.exists():continue
        trace_ids=set();ended=False
        native = sqlite3.connect((root/f'{platform}_simulation.db').resolve().as_uri()+'?mode=ro',uri=True)
        with path.open() as stream:
            while True:
                offset=stream.tell();line=stream.readline()
                if not line:break
                if not line.strip():continue
                row=json.loads(line)
                if 'event_type' in row:
                    ended=ended or row['event_type']=='simulation_end';continue
                trace=(row.get('source_record') or {}).get('trace_rowid')
                if trace is None or trace in trace_ids:raise GraphBusy('Raw action log lacks unique committed trace identities')
                trace_ids.add(trace)
                committed = native.execute('SELECT user_id,action,info FROM trace WHERE rowid=?',(trace,)).fetchone()
                if not committed or committed[0] != row.get('agent_id') or committed[1].upper() != row.get('action_type'):
                    raise GraphBusy('Action log identity does not match its committed database record')
                native_args=json.loads(committed[2]) if committed[2] else {}
                if any(k in native_args and native_args[k] != row.get('action_args',{}).get(k)
                       for k in ['content','post_id','comment_id','query','follow_id']):
                    raise GraphBusy('Action content differs from the committed database record')
                if row.get('success',True) is False or row.get('action_type')=='DO_NOTHING':continue
                event_id=f'{run_id}:{platform}:actions.jsonl:{offset}';eligible.add(event_id)
                if event_id in recorded:
                    durable=json.loads(recorded[event_id]['payload'])
                    durable['_source_path']=str(Path(durable['_source_path']).resolve())
                    canonical={**row,'_source_path':str(path.resolve()),'_source_offset':offset}
                    if durable != canonical:raise GraphBusy('Raw log differs from its persisted outbox payload')
                if event_id in covered:continue
                sequence+=1
                index=recorded[event_id]['seq'] if event_id in recorded else sequence
                reference=base+timedelta(minutes=int(row.get('round',0))*config.get('time_config',{}).get('minutes_per_round',30),microseconds=index)
                activity=AgentActivity(platform,row.get('agent_id',0),row.get('agent_name',''),row['action_type'],row.get('action_args',{}),row.get('round',0),reference.isoformat())
                items.append({'item_id':'rebuild-tail:'+event_id,'body':activity.to_episode_text(),
                    'reference_time':reference.isoformat(),'source_description':f'Rebuild from committed trace {trace}, {platform}/actions.jsonl offset {offset}',
                    'event_count':1,'metadata':{'run_id':run_id,'event_ids':[event_id],'source_record':row['source_record']}})
        if not ended:raise GraphBusy(f'{platform} has no confirmed simulation_end; reconcile process outcome first')
        native.close()
    if not covered.issubset(eligible):raise GraphBusy('Accepted batches reference events absent from the raw logs')
    if sum(item.get('event_count',0) for item in items)!=len(eligible):raise GraphBusy('Rebuild event count mismatch')
    return items,len(eligible)


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('simulation_id')
    parser.add_argument('--publish',action='store_true',help='Switch the failed run after successful rebuild; completed published runs are protected')
    args=parser.parse_args()
    manager=SimulationManager();state=manager.get_simulation(args.simulation_id)
    if not state or state.graph_backend!='graphiti_local':raise GraphBusy('A persisted local simulation is required')
    root=Path(Config.OASIS_SIMULATION_DATA_DIR)/args.simulation_id
    run=SimulationRunner.get_run_state(args.simulation_id)
    if run and run.process_pid and Path(f'/proc/{run.process_pid}').exists():
        raise GraphBusy('Recorded simulation PID still exists; stop and verify it before rebuilding')
    if args.publish and ReportManager.get_report_by_simulation(args.simulation_id):
        raise GraphBusy('A published report already exists; retain this immutable run and use staging verification only')
    project=ProjectManager.get_project(state.project_id)
    if not project:raise GraphBusy('Owning project is missing')
    store=get_graph_store(backend='graphiti_local')
    original=store.request('GET',f'graphs/{state.graph_id}/metadata/details')
    if any(job['status'] in {'processing','cancelling'} for job in original['jobs']):raise GraphBusy('Original namespace still has an in-flight write')
    if any(lease['kind']=='report' for lease in original.get('leases',[])):raise GraphBusy('Original graph has active report readers')
    config=json.loads((root/'simulation_config.json').read_text())
    memory,expected=replay_inputs(root,config)
    spec=json.loads(original['build_spec']);ontology=json.loads(original['ontology'])
    source=ProjectManager.get_extracted_text(project.project_id)
    if not source or hashlib.sha256(source.encode()).hexdigest()!=spec.get('source_sha256'):
        raise GraphBusy('Original seed source fingerprint does not match')
    spec.update(rebuild_of=state.graph_id,run_id=args.simulation_id)
    spec.pop('memory_reference_time',None)
    rebuilt=store.create_graph(project.project_id,ontology,spec)
    graph_id=rebuilt['graph_id'];work=root/'rebuilds'/graph_id;work.mkdir(parents=True)
    atomic_json(work/'rebuild.json',{'source_graph_id':state.graph_id,**rebuilt,'phase':'accepted'})
    chunks=TextProcessor.split_text(source,chunk_size=spec['chunk_size'],overlap=spec['chunk_overlap'])
    seeds=[{'item_id':f'chunk:{i}','body':text,'reference_time':spec['reference_time'],
            'source_description':f'Rebuilt original seed {project.project_id} chunk {i}','event_count':0,'metadata':{'chunk_index':i}} for i,text in enumerate(chunks)]
    seed_job=store.submit_ingestion(graph_id,seeds,'seed:'+spec['source_sha256']);store.wait(seed_job['id'])
    seed_snapshot=store.export(graph_id)
    if not seed_snapshot['nodes'] or not seed_snapshot['edges']:raise GraphBusy('Rebuilt seed lacks readable entities or facts')
    if memory:
        digest=hashlib.sha256(json.dumps(memory,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
        memory_job=store.submit_ingestion(graph_id,memory,'rebuild-memory:'+digest);store.wait(memory_job['id'])
    receipt=store.finalize_memory(graph_id,expected)
    clocks=[item['reference_time'] for item in memory]
    receipt.update(graph_id=graph_id,source_graph_id=state.source_graph_id or state.graph_id,run_graph_id=graph_id,
        run_id=args.simulation_id,event_count=expected,
        simulation_reference_time=max(clocks,key=datetime.fromisoformat) if clocks else spec['reference_time'])
    exported=store.export(graph_id)
    store.search(graph_id,'共享阅读室实际模拟讨论',temporal_mode='all_history',limit=10)
    atomic_json(work/'memory-receipt.json',receipt)
    atomic_json(work/'graph-data.json',exported)
    result={'source_graph_id':state.graph_id,**rebuilt,'phase':'verified','published':False,
            'memory_watermark':expected,'node_count':exported['node_count'],'edge_count':exported['edge_count'],'seed_job_id':seed_job['id']}
    if args.publish:
        # The original failed namespace and original source files remain intact.
        if (root/'memory-receipt.json').exists():
            archive=root/'memory-receipts';archive.mkdir(exist_ok=True)
            atomic_json(archive/f'{state.graph_id}.json',json.loads((root/'memory-receipt.json').read_text()))
        atomic_json(root/'memory-receipt.json',receipt)
        project.graph_versions[graph_id]={**rebuilt,'graph_backend':'graphiti_local'}
        project.graph_id=graph_id;project.build_revision=rebuilt['build_revision'];project.embedding_profile_id=rebuilt['embedding_profile_id']
        project.ingestion_job_id=seed_job['id'];project.graph_build_task_id=seed_job['id'];project.status=ProjectStatus.GRAPH_COMPLETED;project.error=None
        ProjectManager.save_project(project)
        atomic_json(Path(ProjectManager._get_project_dir(project.project_id))/f"graph-{rebuilt['build_revision']}.json",seed_snapshot)
        state.graph_id=graph_id;state.run_graph_id=graph_id;state.memory_watermark=expected;state.error=None
        manager._save_simulation_state(state)
        if run:
            run.runner_status=RunnerStatus.COMPLETED;run.error=None;SimulationRunner._save_run_state(run)
            SimulationRunner._sync_simulation_status(args.simulation_id,RunnerStatus.COMPLETED)
        store.release_lease(result['source_graph_id'],args.simulation_id)
        result['published']=True
    atomic_json(work/'rebuild.json',result)
    print(json.dumps(result,ensure_ascii=False,indent=2))
    if args.publish:print('Restart Flask to refresh any cached failed run state before generating a new report.')


if __name__=='__main__':main()
