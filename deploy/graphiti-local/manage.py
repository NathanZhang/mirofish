"""Start/stop only this stack; preserve volumes and verify recorded processes."""
import argparse
import fcntl
import json
import os
import signal
import socket
import subprocess
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT/'data/graphiti-local'
STATE = DATA/'processes.json'
COMPOSE = ['docker','compose','--env-file',str(ROOT/'deploy/graphiti-local/.env'),'-f',str(ROOT/'deploy/graphiti-local/compose.yaml')]


def configured_environment():
    # Use python-dotenv's parser rather than executing a shell .env file.
    from dotenv import dotenv_values
    env = os.environ.copy()
    env.update({k:v for k,v in dotenv_values(ROOT/'deploy/graphiti-local/.env').items() if v is not None})
    for key in ['ALL_PROXY','all_proxy','HTTP_PROXY','http_proxy','HTTPS_PROXY','https_proxy']:
        env.pop(key,None)
    env.update(HF_HUB_OFFLINE='1',TRANSFORMERS_OFFLINE='1',GRAPHITI_TELEMETRY_ENABLED='false',FLASK_HOST='127.0.0.1')
    return env


def alive(pid, command, cwd=None):
    try:
        current = Path(f'/proc/{pid}/cmdline').read_bytes().replace(b'\0',b' ').decode()
        if cwd and Path(f'/proc/{pid}/cwd').resolve() != Path(cwd).resolve():
            return False
        return command in current
    except (FileNotFoundError,PermissionError):
        return False


def probe(url, token=None, json_response=True):
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    request=urllib.request.Request(url,headers={'Authorization':f'Bearer {token}'} if token else {})
    with opener.open(request,timeout=10) as response:
        return json.loads(response.read()) if json_response else {'status': response.status}


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('action',choices=['start','stop','restart','status'])
    args=parser.parse_args()
    DATA.mkdir(parents=True,exist_ok=True)
    lifecycle_lock = (DATA/'lifecycle.lock').open('a')
    if args.action != 'status':
        try:
            fcntl.flock(lifecycle_lock,fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            parser.exit(1,'Another start/stop/restart is in progress; retry after it finishes.\n')
    state=json.loads(STATE.read_text()) if STATE.exists() else {}
    env=configured_environment()
    web_host=env.get('MIROFISH_WEB_HOST','127.0.0.1')
    if web_host not in {'127.0.0.1','0.0.0.0'}:
        parser.error('MIROFISH_WEB_HOST must be 127.0.0.1 (local) or 0.0.0.0 (LAN)')
    if args.action=='status':
        result={name:{'pid':p['pid'],'running':alive(p['pid'],p['identity'],p.get('cwd'))} for name,p in state.items()}
        containers=subprocess.run(COMPOSE+['ps','--all','--format','json'],capture_output=True,text=True,env=env)
        if containers.returncode:
            result['neo4j']={'running':False,'error':'docker compose ps failed; check Docker access and configuration'}
        else:
            output=containers.stdout.strip()
            rows=json.loads(output) if output.startswith('[') else [json.loads(line) for line in output.splitlines()]
            neo4j=next((row for row in rows if row.get('Service')=='neo4j'),{})
            result['neo4j']={'running':neo4j.get('State')=='running','state':neo4j.get('State','not_created'),'health':neo4j.get('Health','unknown')}
        for name,url in [('embedding','http://127.0.0.1:5102/health'),('graphiti','http://127.0.0.1:5101/internal/v1/readyz'),('backend','http://127.0.0.1:5001/ready'),('frontend','http://127.0.0.1:3000/')]:
            try:result.setdefault(name,{})['health']=probe(url,env['GRAPHITI_SERVICE_TOKEN'] if name=='graphiti' else None,json_response=name!='frontend')
            except Exception as error:result.setdefault(name,{})['error']=type(error).__name__
        print(json.dumps(result,ensure_ascii=False,indent=2));return
    if args.action in {'stop','restart'}:
        for name in ['frontend','backend','graphiti','embedding']:
            if name not in state:continue
            process=state[name]
            if alive(process['pid'],process['identity'],process.get('cwd')):
                os.killpg(process['pid'],signal.SIGINT)
                deadline=time.monotonic()+45
                while alive(process['pid'],process['identity'],process.get('cwd')) and time.monotonic()<deadline:time.sleep(0.5)
                if alive(process['pid'],process['identity'],process.get('cwd')):
                    raise RuntimeError(f'{name} still draining; keep dependencies running and retry stop later')
            state.pop(name);STATE.write_text(json.dumps(state,indent=2))
        subprocess.run(COMPOSE+['stop'],check=True,env=env)
        print('Stopped; database volumes, task journal, raw actions and reports preserved',flush=True)
    if args.action in {'start','restart'}:
        subprocess.run(COMPOSE+['up','-d','--wait'],check=True,env=env)
        components=[
            ('embedding',[str(ROOT/'services/embedding/.venv/bin/python'),'-m','uvicorn','app:app','--host','127.0.0.1','--port','5102'],ROOT/'services/embedding','app:app','http://127.0.0.1:5102/health'),
            ('graphiti',[str(ROOT/'services/graphiti_local/.venv/bin/python'),'-m','uvicorn','app.main:app','--host','127.0.0.1','--port','5101','--no-access-log'],ROOT/'services/graphiti_local','app.main:app','http://127.0.0.1:5101/internal/v1/readyz'),
            ('backend',[str(ROOT/'backend/.venv/bin/python'),str(ROOT/'backend/run.py')],ROOT/'backend',str(ROOT/'backend/run.py'),'http://127.0.0.1:5001/ready'),
            ('frontend',['npm','run','dev','--','--host',web_host,'--open','false'],ROOT/'frontend','npm run dev','http://127.0.0.1:3000/'),
        ]
        for name,command,cwd,identity,url in components:
            if name in state and alive(state[name]['pid'],state[name]['identity'],state[name].get('cwd')):
                probe(url,env['GRAPHITI_SERVICE_TOKEN'] if name=='graphiti' else None,json_response=name!='frontend')
                print(f'{name}: already running and ready',flush=True);continue
            # Refuse an unowned listener rather than starting a second process.
            from urllib.parse import urlparse
            address=urlparse(url)
            try:
                with socket.create_connection((address.hostname,address.port),timeout=1):
                    occupied=True
            except OSError:
                occupied=False
            if occupied:
                raise RuntimeError(f'{name} already serves this port outside this manager; stop it or use its existing lifecycle')
            log=(DATA/f'{name}.log').open('ab')
            process=subprocess.Popen(command,cwd=cwd,env=env,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
            log.close();state[name]={'pid':process.pid,'identity':identity,'cwd':str(cwd)}
            STATE.write_text(json.dumps(state,indent=2))
            deadline=time.monotonic()+120
            while time.monotonic()<deadline:
                if process.poll() is not None:raise RuntimeError(f'{name} exited; see {DATA}/{name}.log')
                try:probe(url,env['GRAPHITI_SERVICE_TOKEN'] if name=='graphiti' else None,json_response=name!='frontend');break
                except Exception:time.sleep(1)
            else:raise RuntimeError(f'{name} did not become ready; process retained for diagnosis')
            print(f'{name}: started pid={process.pid}',flush=True)
        print('Ready: http://127.0.0.1:3000  (API: http://127.0.0.1:5001)',flush=True)
        if web_host=='0.0.0.0':
            print('LAN access enabled: http://<this-machine-LAN-IP>:3000 (API proxied through /api)',flush=True)


if __name__=='__main__':main()
