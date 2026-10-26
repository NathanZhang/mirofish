import json
from pathlib import Path

import pytest

from app.config import Config
from app.services.graph_store import memory
from app.services.graph_store.protocol import DependencyUnavailable


class Store:
    def __init__(self):
        self.jobs, self.calls = {}, []
        self.lose_response = True

    def submit_ingestion(self, graph_id, items, key):
        self.calls.append((key, items))
        if key in self.jobs:
            assert self.jobs[key]['items'] == items
        else:
            self.jobs[key] = {'id':str(len(self.jobs)), 'items':items}
        if self.lose_response:
            self.lose_response = False
            raise DependencyUnavailable('response lost after acceptance')
        return self.jobs[key]

    def wait(self, job_id, timeout):
        return {'status':'completed'}

    def finalize_memory(self, graph_id, expected):
        self.expected = expected
        return {'expected_watermark':expected,'committed_watermark':expected,'finalized':True}

    def release_lease(self, *args):
        pass


def test_response_loss_preserves_batch_boundary_and_distinct_identical_posts(tmp_path, monkeypatch):
    sim='sim-outbox'
    root=Path(Config.OASIS_SIMULATION_DATA_DIR)/sim
    (root/'twitter').mkdir(parents=True)
    (root/'simulation_config.json').write_text('{}')
    path=root/'twitter/actions.jsonl'
    action={'round':1,'agent_id':1,'agent_name':'小林','action_type':'CREATE_POST','action_args':{'content':'我愿意参加'},'success':True}
    path.write_text(''.join(json.dumps(action,ensure_ascii=False)+'\n' for _ in range(10)))
    store=Store();monkeypatch.setattr(memory,'get_graph_store',lambda _:store)
    updater=memory.GraphitiMemoryUpdater('graph',sim);updater.start()
    with path.open() as f:
        for i in range(10):
            offset=f.tell();row=json.loads(f.readline());row.update(_source_path=str(path),_source_offset=offset)
            if i==9:
                with pytest.raises(DependencyUnavailable):updater.add_activity_from_dict(row,'twitter')
            else:updater.add_activity_from_dict(row,'twitter')
    assert len(store.jobs)==1
    # Recreate the updater as after a Flask restart; the exact partial HTTP
    # transaction is recovered from the outbox, without a new operation key.
    updater.db.close()
    recovered=memory.GraphitiMemoryUpdater('graph',sim);recovered.start();recovered.stop()
    assert store.expected==10
    assert len(store.jobs)==1
    assert store.calls[0] == store.calls[1]
    assert recovered.db.execute('SELECT count(*) FROM events').fetchone()[0]==10
    receipt=json.loads((root/'memory-receipt.json').read_text())
    assert receipt['event_count']==10 and receipt['committed_watermark']==10


def test_log_identity_conflict_is_not_silently_deduplicated(monkeypatch):
    root=Path(Config.OASIS_SIMULATION_DATA_DIR)/'sim-identity';root.mkdir(parents=True)
    (root/'simulation_config.json').write_text('{}')
    store=Store();store.lose_response=False
    monkeypatch.setattr(memory,'get_graph_store',lambda _:store)
    updater=memory.GraphitiMemoryUpdater('graph','sim-identity');updater.start()
    action={'action_type':'CREATE_POST','action_args':{'content':'旧内容'},'_source_path':'actions.jsonl','_source_offset':0}
    updater.add_activity_from_dict(action,'twitter')
    # The monitor and recovery code use different relative-path spellings.
    updater.add_activity_from_dict({**action,'_source_path':'unused/../actions.jsonl'},'twitter')
    assert updater.db.execute('SELECT count(*) FROM events').fetchone()[0] == 1
    with pytest.raises(Exception,match='different content'):
        updater.add_activity_from_dict({**action,'action_args':{'content':'被覆盖内容'}},'twitter')
