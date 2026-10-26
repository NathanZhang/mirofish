"""Real local acceptance runner. Uses seed.txt; preserves every HTTP response."""
import argparse
import json
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_EVIDENCE = ROOT.parent / 'artifacts/mirofish-20261024/graphiti-local-acceptance-20261026'


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('phase', choices=['build', 'prepare', 'run', 'report', 'chat'])
    parser.add_argument('--evidence', type=Path, default=DEFAULT_EVIDENCE)
    parser.add_argument('--fixed-activity', action='store_true', help='Declare a 2-agent-per-platform activity fixture before running')
    parser.add_argument('--regenerate', action='store_true')
    parser.add_argument('--rebuild', action='store_true', help='Retain failed namespace and build a fresh version')
    args = parser.parse_args()
    out = args.evidence
    out.mkdir(parents=True, exist_ok=True)
    state_path = out / 'run.json'
    state = json.loads(state_path.read_text()) if state_path.exists() else {}
    client = httpx.Client(base_url='http://127.0.0.1:5001/api/', timeout=600, trust_env=False, headers={'Accept-Language':'zh'})

    def save():
        state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2))

    def call(method, path, name=None, **kwargs):
        started = time.monotonic()
        response = client.request(method, path, **kwargs)
        data = response.json()
        with (out/'http.jsonl').open('a') as f:
            f.write(json.dumps({'time':time.time(),'method':method,'path':path,'status':response.status_code,'seconds':round(time.monotonic()-started,3),'body':data},ensure_ascii=False)+'\n')
        if name:
            (out/f'{name}.json').write_text(json.dumps(data,ensure_ascii=False,indent=2))
        response.raise_for_status()
        if not data.get('success',True):
            raise RuntimeError(data)
        return data.get('data',data)

    def wait(path, body=None, terminal='completed', timeout=7200):
        deadline, previous = time.monotonic()+timeout, None
        while time.monotonic() < deadline:
            data = call('POST' if body is not None else 'GET',path,json=body) if body is not None else call('GET',path)
            status = data.get('status',data.get('runner_status'))
            progress = (status,data.get('progress'),data.get('message'))
            if progress != previous:
                print(progress,flush=True);previous=progress
            if status == terminal:
                return data
            if status in {'failed','uncertain'}:
                raise RuntimeError(data)
            time.sleep(5)
        raise TimeoutError(path)

    if args.phase == 'build':
        response = client.get('http://127.0.0.1:5001/ready');response.raise_for_status()
        (out/'readiness.json').write_text(response.text)
        if not state.get('project_id'):
            seed = ROOT.parent/'artifacts/mirofish-20261024/seed.txt'
            with seed.open('rb') as f:
                ontology = call('POST','graph/ontology/generate','ontology',files={'files':('seed.txt',f,'text/plain')},data={
                    'project_name':'Graphiti 本地替换真实验收',
                    'simulation_requirement':'以材料中的六方参与者为基础，选择6至10个有效参与者，在Twitter和Reddit模拟共享阅读室试行后的讨论，最多3轮，启用图谱记忆写回，识别支持、反对与改进建议。输出包含观察、分歧、建议和局限的中文报告。',
                    'additional_context':'实体类型采用Person和Organization及其具体角色类型；平台、地点、活动不作为模拟Agent。不要凭空添加人物。'})
            state['project_id']=ontology['project_id'];save()
        if not state.get('graph_task_id') or args.rebuild:
            job=call('POST','graph/build','graph-submission',json={'project_id':state['project_id'],'force':args.rebuild})
            state.update(graph_task_id=job['task_id'],graph_id=job['graph_id']);save()
        wait(f"graph/task/{state['graph_task_id']}")
        call('GET',f"graph/data/{state['graph_id']}",'graph-data')
        call('GET',f"graph/project/{state['project_id']}",'project')
    elif args.phase == 'prepare':
        if not state.get('simulation_id'):
            sim=call('POST','simulation/create','simulation',json={'project_id':state['project_id'],'enable_twitter':True,'enable_reddit':True})
            state['simulation_id']=sim['simulation_id'];save()
        import sys
        sys.path.insert(0,str(ROOT/'backend'))
        from app.config import Config
        ontology=json.loads((out/'ontology.json').read_text())['data']['ontology']
        actor_types=[spec['name'] for spec in ontology['entity_types'] if Config.is_simulatable_type(spec['name'])]
        job=call('POST','simulation/prepare','prepare-submission',json={'simulation_id':state['simulation_id'],'parallel_profile_count':2,'use_llm_for_profiles':True,'entity_types':actor_types,'force_regenerate':args.regenerate})
        if job.get('task_id'):
            wait('simulation/prepare/status',{'task_id':job['task_id']})
        call('GET',f"simulation/{state['simulation_id']}/profiles",'profiles')
        call('GET',f"simulation/{state['simulation_id']}/config",'simulation-config')
        if args.fixed_activity:
            path=ROOT/'backend/uploads/simulations'/state['simulation_id']/'simulation_config.json'
            generated=json.loads(path.read_text())
            (out/'simulation-config-generated.json').write_text(json.dumps(generated,ensure_ascii=False,indent=2))
            generated['time_config'].update(agents_per_hour_min=2,agents_per_hour_max=2,off_peak_activity_multiplier=1,peak_activity_multiplier=1)
            for agent in generated['agent_configs']:
                agent['active_hours']=list(range(24));agent['activity_level']=1
            path.write_text(json.dumps(generated,ensure_ascii=False,indent=2))
            (out/'simulation-config-fixed-fixture.json').write_text(path.read_text())
            (out/'simulation-fixture-rationale.json').write_text(json.dumps({'reason':'Declared three-round acceptance fixture: activate two real agents per platform per round, avoiding zero activation at hour 0. No output actions fabricated.'},indent=2))

    elif args.phase == 'run':
        sim=state['simulation_id']
        call('POST','simulation/start','run-start',json={'simulation_id':sim,'platform':'parallel','max_rounds':3,'enable_graph_memory_update':True})
        deadline=time.monotonic()+7200
        while time.monotonic()<deadline:
            status=call('GET',f'simulation/{sim}/run-status','run-status')
            print({k:status.get(k) for k in ['runner_status','current_round','twitter_completed','reddit_completed']},flush=True)
            if status.get('runner_status')=='failed':raise RuntimeError(status)
            if status.get('twitter_completed') and status.get('reddit_completed'):break
            time.sleep(10)
        else:raise TimeoutError('Dual-platform simulation')
        call('POST','simulation/interview/batch','agent-interview',json={'simulation_id':sim,'interviews':[{'agent_id':0,'prompt':'共享阅读室试行中你最关心什么？请根据刚才的讨论简短回答。'}],'platform':'reddit','timeout':180})
        call('POST','simulation/close-env','close-environment',json={'simulation_id':sim,'timeout':30})
        wait(f'simulation/{sim}/run-status')
    elif args.phase == 'report':
        job=call('POST','report/generate','report-submission',json={'simulation_id':state['simulation_id']})
        state['report_id']=job['report_id'];save()
        if job.get('task_id'):wait('report/generate/status',{'task_id':job['task_id']})
        call('GET',f"report/{state['report_id']}",'final-report')
    elif args.phase == 'chat':
        call('POST','report/chat','report-chat',json={'simulation_id':state['simulation_id'],'message':'根据本次真实模拟，参与者有哪些分歧？请引用实际证据，并说明不能得出的结论。','chat_history':[]})
    print(f'{args.phase} completed; evidence: {out}',flush=True)


if __name__ == '__main__':
    main()
