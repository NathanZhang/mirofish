"""Recovery must reject invented or changed events before submitting any graph write."""
import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest

from app.services.graph_store.protocol import GraphBusy

spec = importlib.util.spec_from_file_location(
    'rebuild_graphiti_run', Path(__file__).resolve().parents[2] / 'scripts/rebuild_graphiti_run.py')
rebuild = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rebuild)


@pytest.mark.parametrize('change', ['none', 'content', 'missing_trace', 'unfinished'])
def test_rebuild_verifies_committed_source_before_replay(tmp_path, change):
    root = tmp_path / 'sim-recovery'; (root / 'twitter').mkdir(parents=True)
    with sqlite3.connect(root / 'memory-outbox.sqlite') as db:
        db.executescript('CREATE TABLE meta(key TEXT,value TEXT); CREATE TABLE batches(operation_key TEXT,items TEXT); CREATE TABLE events(event_id TEXT,seq INTEGER,payload TEXT);')
        db.execute('INSERT INTO meta VALUES(?,?)', ('reference_time', '2026-10-26T00:00:00+00:00'))
    with sqlite3.connect(root / 'twitter_simulation.db') as db:
        db.execute('CREATE TABLE trace(user_id INTEGER, action TEXT, info TEXT)')
        db.execute('INSERT INTO trace VALUES(?,?,?)', (1, 'create_post', json.dumps({'content':'实际讨论'})))
    row = {'agent_id':1, 'agent_name':'小林', 'round':1, 'action_type':'CREATE_POST',
           'action_args':{'content':'实际讨论'}, 'source_record':{'trace_rowid':1}, 'success':True}
    if change == 'content': row['action_args']['content'] = '凭空改写'
    if change == 'missing_trace': row.pop('source_record')
    records = [row] + ([] if change == 'unfinished' else [{'event_type':'simulation_end'}])
    (root / 'twitter/actions.jsonl').write_text(''.join(json.dumps(r,ensure_ascii=False)+'\n' for r in records))
    if change != 'none':
        with pytest.raises(GraphBusy): rebuild.replay_inputs(root, {})
    else:
        items, count = rebuild.replay_inputs(root, {})
        assert count == 1 and len(items) == 1
        assert '实际讨论' in items[0]['body']
        assert items[0]['metadata']['source_record']['trace_rowid'] == 1
