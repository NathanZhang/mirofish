"""Durable action outbox; the service, rather than Flask threads, extracts facts."""
import hashlib
import json
import os
import sqlite3
import threading
import time
from dataclasses import asdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

from ...config import Config
from . import get_graph_store
from .protocol import GraphBusy


class GraphitiMemoryUpdater:
    BATCH_SIZE = 10

    def __init__(self, graph_id, simulation_id):
        self.graph_id, self.simulation_id = graph_id, simulation_id
        self.store = get_graph_store(graph_id)
        self.path = Path(Config.OASIS_SIMULATION_DATA_DIR) / simulation_id
        self.path.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._running, self._worker_thread = False, None
        self.db = sqlite3.connect(self.path / 'memory-outbox.sqlite', check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.executescript('''
          CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT);
          CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY AUTOINCREMENT,event_id TEXT UNIQUE,payload TEXT,platform TEXT,eligible INTEGER,batch_key TEXT);
          CREATE TABLE IF NOT EXISTS batches(operation_key TEXT PRIMARY KEY,items TEXT,job_id TEXT);
        ''')
        now = datetime.now(timezone.utc).isoformat()
        self.db.execute("INSERT OR IGNORE INTO meta VALUES('reference_time',?)", (now,))
        self.db.commit()
        self.reference = datetime.fromisoformat(self.db.execute("SELECT value FROM meta WHERE key='reference_time'").fetchone()[0])
        config = json.loads((self.path / 'simulation_config.json').read_text())
        self.minutes_per_round = config.get('time_config', {}).get('minutes_per_round', 30)

    def start(self):
        self._running = True

    def add_activity_from_dict(self, data, platform):
        if 'event_type' in data:
            return
        source = data.get('_source_path')
        offset = data.get('_source_offset')
        if source is None or offset is None:
            raise ValueError('Local memory requires a durable action log path and offset')
        data = {**data, '_source_path': str(Path(source).resolve())}
        event_id = f'{self.simulation_id}:{platform}:{Path(source).name}:{offset}'
        payload = json.dumps(data, ensure_ascii=False, sort_keys=True)
        eligible = data.get('success', True) is not False and data.get('action_type') != 'DO_NOTHING'
        with self._lock:
            if not self._running:
                raise GraphBusy('Memory outbox closed')
            existing = self.db.execute('SELECT payload FROM events WHERE event_id=?', (event_id,)).fetchone()
            if existing:
                previous = json.loads(existing['payload'])
                previous['_source_path'] = str(Path(previous['_source_path']).resolve())
                if json.dumps(previous, ensure_ascii=False, sort_keys=True) != payload:
                    raise GraphBusy('Action log identity reused with different content')
            self.db.execute('INSERT OR IGNORE INTO events(event_id,payload,platform,eligible) VALUES(?,?,?,?)', (event_id,payload,platform,int(eligible)))
            self.db.commit()
            self._flush(tail=False)

    def add_activity(self, activity):
        raise ValueError('Use add_activity_from_dict with source identity for local graphs')

    def _flush(self, tail):
        from ..zep_graph_memory_updater import AgentActivity
        # Persist the exact batch boundary before any HTTP request, including a
        # final partial batch. A lost response is resubmitted with the same key.
        for platform in ['twitter', 'reddit']:
            while True:
                rows = self.db.execute('SELECT * FROM events WHERE eligible=1 AND batch_key IS NULL AND platform=? ORDER BY seq LIMIT ?', (platform,self.BATCH_SIZE)).fetchall()
                if not rows or (not tail and len(rows) < self.BATCH_SIZE):
                    break
                texts, ids = [], []
                for row in rows:
                    data = json.loads(row['payload'])
                    reference = self.reference + timedelta(minutes=int(data.get('round',0))*self.minutes_per_round, microseconds=row['seq'])
                    activity = AgentActivity(platform=platform, agent_id=data.get('agent_id',0), agent_name=data.get('agent_name',''), action_type=data.get('action_type',''), action_args=data.get('action_args',{}), round_num=data.get('round',0), timestamp=reference.isoformat())
                    texts.append(activity.to_episode_text());ids.append(row['event_id'])
                key = f'memory:{self.simulation_id}:' + hashlib.sha256('\n'.join(ids).encode()).hexdigest()
                items = [{'item_id': key, 'body': '\n'.join(texts), 'reference_time': reference.isoformat(),
                          'source_description': f'MiroFish run={self.simulation_id} platform={platform} events={ids}',
                          'event_count': len(rows), 'metadata': {'run_id': self.simulation_id, 'event_ids': ids, 'source': 'actions.jsonl', 'reference_time_semantics': 'simulation_clock'}}]
                if len(items[0]['body']) > 20000:
                    raise GraphBusy('Action batch exceeds ingestion limit; source preserved for rebuild')
                with self.db:
                    self.db.execute('INSERT OR IGNORE INTO batches VALUES(?,?,NULL)',(key,json.dumps(items,ensure_ascii=False)))
                    self.db.executemany('UPDATE events SET batch_key=? WHERE event_id=?',[(key,event_id) for event_id in ids])
        for row in self.db.execute('SELECT * FROM batches WHERE job_id IS NULL ORDER BY rowid').fetchall():
            job = self.store.submit_ingestion(self.graph_id,json.loads(row['items']),row['operation_key'])
            with self.db:
                self.db.execute('UPDATE batches SET job_id=? WHERE operation_key=?',(job['id'],row['operation_key']))

    def stop(self):
        with self._lock:
            # Re-read the actual files to include tail events and recover any
            # monitor interruption. Offset identities make this idempotent.
            self._running = True
            for platform in ['twitter', 'reddit']:
                file = self.path / platform / 'actions.jsonl'
                if not file.exists():
                    continue
                with file.open() as f:
                    while True:
                        offset = f.tell(); line = f.readline()
                        if not line:
                            break
                        if not line.strip():
                            continue
                        data = json.loads(line)
                        data.update(_source_path=str(file), _source_offset=offset)
                        self.add_activity_from_dict(data, platform)
            self._running = False
            self._flush(tail=True)
            expected = self.db.execute('SELECT count(*) FROM events WHERE eligible=1').fetchone()[0]
            jobs = self.db.execute('SELECT job_id FROM batches ORDER BY rowid').fetchall()
        deadline = time.monotonic() + Config.GRAPH_JOB_DEADLINE_SECONDS
        for row in jobs:
            self.store.wait(row['job_id'], timeout=max(1, deadline-time.monotonic()))
        receipt = self.store.finalize_memory(self.graph_id, expected)
        receipt.update(graph_id=self.graph_id, run_id=self.simulation_id, source_graph_id=self.graph_id,
                       run_graph_id=self.graph_id, ingestion_job_ids=[r['job_id'] for r in jobs],
                       event_count=expected, reference_time=self.reference.isoformat())
        clocks = [item['reference_time'] for row in self.db.execute('SELECT items FROM batches')
                  for item in json.loads(row['items'])]
        receipt['simulation_reference_time'] = max(clocks, key=lambda value:datetime.fromisoformat(value)) if clocks else self.reference.isoformat()
        temporary = self.path / 'memory-receipt.json.tmp'
        with temporary.open('w') as f:
            json.dump(receipt, f,ensure_ascii=False,indent=2); f.flush();os.fsync(f.fileno())
        temporary.replace(self.path / 'memory-receipt.json')
        from ..simulation_manager import SimulationManager
        manager = SimulationManager()
        state = manager.get_simulation(self.simulation_id)
        if state:
            state.memory_watermark = expected
            manager._save_simulation_state(state)
        self.store.release_lease(self.graph_id, self.simulation_id)

    def get_stats(self):
        with self._lock:
            total = self.db.execute('SELECT count(*) FROM events WHERE eligible=1').fetchone()[0]
            batches = self.db.execute('SELECT count(*) FROM batches').fetchone()[0]
            return {'graph_id':self.graph_id,'total_activities':total,'batches_sent':batches,
                    'running':self._running,'backend':'graphiti_local','batch_size':self.BATCH_SIZE,
                    'outbox':str(self.path / 'memory-outbox.sqlite')}
