"""Execute the script's actual loop against a deterministic SQLite environment."""
import ast
import asyncio
import __future__
import json
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPTS=Path(__file__).resolve().parents[1]/'scripts'
sys.path.insert(0,str(SCRIPTS))
from action_logger import PlatformActionLogger
from action_records import log_committed_actions


@pytest.mark.parametrize('platform,expected_initial',[('twitter',1),('reddit',2)])
def test_initial_posts_and_first_round_match_committed_trace(tmp_path,platform,expected_initial):
    source=ast.parse((SCRIPTS/'run_parallel_simulation.py').read_text())
    keep={'fetch_new_actions_from_db','get_agent_names_from_config',f'run_{platform}_simulation'}
    module=ast.Module(body=[n for n in source.body if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef)) and n.name in keep],type_ignores=[])
    agent=object()
    graph=SimpleNamespace(get_agent=lambda _:agent,get_agents=lambda:[(0,agent)])

    class Environment:
        def __init__(self,database_path):self.path=database_path;self.agent_graph=graph
        async def reset(self):
            with sqlite3.connect(self.path) as db:db.execute('CREATE TABLE trace(user_id INTEGER,action TEXT,info TEXT)')
        async def step(self,actions):
            with sqlite3.connect(self.path) as db:
                for payload in actions.values():
                    for action in payload if isinstance(payload,list) else [payload]:
                        args=action.action_args if hasattr(action,'action_args') else {'content':'真实自主发帖'}
                        db.execute('INSERT INTO trace VALUES(?,?,?)',(0,'create_post',json.dumps(args)))

    async def generate(**kwargs):return graph
    namespace={'PlatformSimulation':SimpleNamespace,'create_model':lambda *a,**k:None,
        f'generate_{platform}_agent_graph':generate,'TWITTER_ACTIONS':[],'REDDIT_ACTIONS':[],
        'os':os,'datetime':datetime,'sqlite3':sqlite3,'json':json,
        'oasis':SimpleNamespace(make=lambda **k:Environment(k['database_path']),DefaultPlatformType=SimpleNamespace(TWITTER='twitter',REDDIT='reddit')),
        'ManualAction':lambda **kwargs:SimpleNamespace(**kwargs),'ActionType':SimpleNamespace(CREATE_POST='create_post'),
        'LLMAction':object,'_shutdown_event':None,'get_active_agents_for_round':lambda *a:[(0,agent)],
        'log_committed_actions':log_committed_actions,'FILTERED_ACTIONS':{'sign_up','refresh'},
        'ACTION_TYPE_MAP':{'create_post':'CREATE_POST'},'_enrich_action_context':lambda *a:None}
    exec(compile(module,str(SCRIPTS/'run_parallel_simulation.py'),'exec',flags=__future__.annotations.compiler_flag),namespace)
    (tmp_path/('twitter_profiles.csv' if platform=='twitter' else 'reddit_profiles.json')).write_text('fixture')
    config={'time_config':{'total_simulation_hours':1,'minutes_per_round':60},
            'agent_configs':[{'agent_id':0,'entity_name':'小林'}],
            'event_config':{'initial_posts':[{'poster_agent_id':0,'content':'第一个预设帖'},{'poster_agent_id':0,'content':'第二个预设帖'}]}}
    log=PlatformActionLogger(platform,str(tmp_path))
    result=asyncio.run(namespace[f'run_{platform}_simulation'](config,str(tmp_path),log,max_rounds=1))
    actions=[json.loads(line) for line in Path(log.log_path).read_text().splitlines() if 'event_type' not in json.loads(line)]
    assert len(actions)==expected_initial+1
    assert [row['round'] for row in actions].count(0)==expected_initial
    assert len({row['source_record']['trace_rowid'] for row in actions})==len(actions)
    assert actions[-1]['source_record']['origin']=='agent_action'
    assert result.total_actions==len(actions)
