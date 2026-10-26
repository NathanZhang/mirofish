"""Durable acceptance ledger. Graphiti and SQLite are deliberately not atomic."""
import hashlib
import json
import sqlite3
import time
import uuid
from datetime import datetime
from contextlib import contextmanager
from pathlib import Path


class StoreError(RuntimeError):
    def __init__(self, code, message, status=409):
        super().__init__(message)
        self.code, self.status = code, status


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def fingerprint(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


class Journal:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS graphs (
          id TEXT PRIMARY KEY, project_id TEXT NOT NULL, group_id TEXT UNIQUE NOT NULL,
          ontology TEXT NOT NULL, build_spec TEXT NOT NULL, status TEXT NOT NULL,
          fingerprint TEXT NOT NULL, created_at REAL NOT NULL, watermark INTEGER NOT NULL DEFAULT 0,
          finalized INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS jobs (
          id TEXT PRIMARY KEY, graph_id TEXT NOT NULL REFERENCES graphs(id),
          operation_key TEXT NOT NULL, payload_hash TEXT NOT NULL, status TEXT NOT NULL,
          created_at REAL NOT NULL, error TEXT, UNIQUE(graph_id, operation_key));
        CREATE TABLE IF NOT EXISTS items (
          job_id TEXT NOT NULL REFERENCES jobs(id), sequence INTEGER NOT NULL,
          episode_uuid TEXT UNIQUE NOT NULL, payload TEXT NOT NULL, status TEXT NOT NULL,
          error TEXT, PRIMARY KEY(job_id, sequence));
        CREATE TABLE IF NOT EXISTS leases (
          graph_id TEXT NOT NULL REFERENCES graphs(id), owner TEXT NOT NULL,
          kind TEXT NOT NULL, created_at REAL NOT NULL, PRIMARY KEY(graph_id, owner));
        """)

    @contextmanager
    def transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def recover(self):
        with self.transaction():
            self.db.execute("UPDATE items SET status='uncertain', error='Worker stopped during Graphiti write; rebuild namespace' WHERE status='processing'")
            self.db.execute("UPDATE jobs SET status='uncertain', error='Interrupted write requires reconciliation or namespace rebuild' WHERE status!='uncertain' AND id IN (SELECT job_id FROM items WHERE status='uncertain')")
            self.db.execute("UPDATE graphs SET status='uncertain' WHERE id IN (SELECT graph_id FROM jobs WHERE status='uncertain')")
            # No write was in flight for the remaining processing jobs.
            self.db.execute("UPDATE jobs SET status='queued' WHERE status='processing'")
            for row in self.db.execute("SELECT id FROM jobs WHERE status='cancelling'").fetchall():
                self._cancel_locked(row['id'])
        # Durable leases survive restart; no expiry-based destructive cleanup.

    def graph(self, graph_id):
        row = self.db.execute("SELECT * FROM graphs WHERE id=?", (graph_id,)).fetchone()
        if not row or row["status"] == "deleted":
            raise StoreError("graph_not_found", "Graph not found", 404)
        return dict(row)

    def create_graph(self, project_id, ontology, build_spec, profile):
        graph_id, group = "graph_" + uuid.uuid4().hex, "mf_" + uuid.uuid4().hex
        self.db.execute("INSERT INTO graphs(id,project_id,group_id,ontology,build_spec,status,fingerprint,created_at) VALUES(?,?,?,?,?,'created',?,?)",
                        (graph_id, project_id, group, canonical(ontology), canonical(build_spec), profile, time.time()))
        return self.graph(graph_id)

    def submit(self, graph_id, items, operation_key):
        digest = fingerprint(items)
        with self.transaction():
            graph = self.graph(graph_id)
            existing = self.db.execute("SELECT id,payload_hash FROM jobs WHERE graph_id=? AND operation_key=?", (graph_id, operation_key)).fetchone()
            if existing:
                if existing["payload_hash"] != digest:
                    raise StoreError("operation_conflict", "Operation key already used with different payload")
                return self.job(existing["id"])
            if graph["finalized"] or graph["status"] in {"uncertain", "failed", "deleting"}:
                raise StoreError("graph_busy", "Graph is finalized, failed, uncertain, or deleting; rebuild before writing")
            if self.db.execute("SELECT count(*) FROM leases WHERE graph_id=? AND kind='export'", (graph_id,)).fetchone()[0]:
                raise StoreError("graph_busy", "A complete graph export is in progress")
            if not items:
                raise StoreError("invalid_input", "No ingestion items", 400)
            job_id = "job_" + uuid.uuid4().hex
            self.db.execute("INSERT INTO jobs VALUES(?,?,?,?,?,?,NULL)",
                            (job_id, graph_id, operation_key, digest, "queued", time.time()))
            for sequence, payload in enumerate(items):
                episode = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{graph_id}:{operation_key}:{payload['item_id']}"))
                self.db.execute("INSERT INTO items VALUES(?,?,?,?,?,NULL)",
                                (job_id, sequence, episode, canonical(payload), "queued"))
            self.db.execute("UPDATE graphs SET status='ingesting' WHERE id=?", (graph_id,))
            return self.job(job_id)

    def job(self, job_id):
        row = self.db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            raise StoreError("job_not_found", "Job not found", 404)
        result = dict(row)
        result["items"] = [dict(r) for r in self.db.execute("SELECT sequence,episode_uuid,status,error FROM items WHERE job_id=? ORDER BY sequence", (job_id,))]
        result["committed"] = sum(i["status"] == "committed" for i in result["items"])
        result["total"] = len(result["items"])
        result["progress"] = int(100 * result["committed"] / result["total"]) if result["total"] else 0
        return result

    def claim(self):
        with self.transaction():
            row = self.db.execute("SELECT j.id FROM jobs j JOIN graphs g ON g.id=j.graph_id WHERE j.status='queued' AND g.status NOT IN ('uncertain','failed','deleting','deleted') ORDER BY j.created_at,j.id LIMIT 1").fetchone()
            if not row:
                return None
            self.db.execute("UPDATE jobs SET status='processing' WHERE id=?", (row["id"],))
            return self.job(row["id"])

    def next_item(self, job_id):
        if self.job(job_id)['status'] == 'cancelling':
            with self.transaction():
                self._cancel_locked(job_id)
            return None
        row = self.db.execute("SELECT * FROM items WHERE job_id=? AND status='queued' ORDER BY sequence LIMIT 1", (job_id,)).fetchone()
        if not row:
            return None
        self.db.execute("UPDATE items SET status='processing' WHERE job_id=? AND sequence=?", (job_id, row["sequence"]))
        return dict(row)

    def commit_item(self, job_id, sequence):
        with self.transaction():
            item = self.db.execute("SELECT payload,status FROM items WHERE job_id=? AND sequence=?", (job_id, sequence)).fetchone()
            if item["status"] != "processing":
                raise StoreError("invalid_state", "Can only commit a processing item")
            self.db.execute("UPDATE items SET status='committed' WHERE job_id=? AND sequence=?", (job_id, sequence))
            weight = json.loads(item["payload"]).get("event_count", 0)
            self.db.execute("UPDATE graphs SET watermark=watermark+? WHERE id=(SELECT graph_id FROM jobs WHERE id=?)", (weight, job_id))
            if weight:
                graph_id = self.job(job_id)['graph_id']
                graph = self.graph(graph_id)
                spec = json.loads(graph['build_spec'])
                candidate = json.loads(item['payload'])['reference_time']
                previous = spec.get('memory_reference_time')
                if previous is None or datetime.fromisoformat(candidate) > datetime.fromisoformat(previous):
                    spec['memory_reference_time'] = candidate
                    self.db.execute('UPDATE graphs SET build_spec=? WHERE id=?', (canonical(spec), graph_id))

    def finish_job(self, job_id):
        with self.transaction():
            job = self.job(job_id)
            if job['status'] in {'cancelling','cancelled'}:
                self._cancel_locked(job_id)
                return
            if job["committed"] != job["total"]:
                raise StoreError("ingestion_uncertain", "Cannot finish uncommitted job")
            self.db.execute("UPDATE jobs SET status='completed' WHERE id=?", (job_id,))
            active = self.db.execute("SELECT count(*) FROM jobs WHERE graph_id=? AND status!='completed'", (job["graph_id"],)).fetchone()[0]
            if not active:
                self.db.execute("UPDATE graphs SET status='ready' WHERE id=?", (job["graph_id"],))

    def _cancel_locked(self, job_id):
        self.db.execute("UPDATE items SET status='cancelled' WHERE job_id=? AND status='queued'", (job_id,))
        self.db.execute("UPDATE jobs SET status='cancelled',error='Cancelled; committed content retained, rebuild required' WHERE id=?", (job_id,))
        self.db.execute("UPDATE graphs SET status='failed' WHERE status!='uncertain' AND id=(SELECT graph_id FROM jobs WHERE id=?)", (job_id,))

    def cancel(self, job_id):
        with self.transaction():
            job = self.job(job_id)
            if job['status'] == 'queued':
                self._cancel_locked(job_id)
            elif job['status'] == 'processing':
                self.db.execute("UPDATE jobs SET status='cancelling' WHERE id=?", (job_id,))
            return self.job(job_id)

    def fail_job(self, job_id, error, uncertain=True):
        status = "uncertain" if uncertain else "failed"
        with self.transaction():
            job = self.job(job_id)
            self.db.execute("UPDATE items SET status=?,error=? WHERE job_id=? AND status='processing'", (status, error, job_id))
            self.db.execute("UPDATE jobs SET status=?,error=? WHERE id=?", (status, error, job_id))
            self.db.execute("UPDATE graphs SET status=? WHERE id=?", (status, job["graph_id"]))

    def lease(self, graph_id, owner, kind):
        with self.transaction():
            graph = self.graph(graph_id)
            if graph["status"] == "deleting":
                raise StoreError("graph_busy", "Graph deletion in progress")
            if kind == 'export' and graph['status'] != 'ready':
                raise StoreError("graph_busy", "Exports require a completed graph version")
            self.db.execute("INSERT OR IGNORE INTO leases VALUES(?,?,?,?)", (graph_id, owner, kind, time.time()))

    def claim_run(self, graph_id, run_id):
        with self.transaction():
            graph = self.graph(graph_id)
            spec = json.loads(graph['build_spec'])
            busy = self.db.execute('SELECT count(*) FROM leases WHERE graph_id=?', (graph_id,)).fetchone()[0]
            if graph['status'] != 'ready' or spec.get('run_id') or busy:
                raise StoreError('graph_busy', '首期每张图仅允许一次模拟；重跑须先重建图谱')
            spec['run_id'] = run_id
            self.db.execute('UPDATE graphs SET build_spec=? WHERE id=?', (canonical(spec), graph_id))
            self.db.execute('INSERT INTO leases VALUES(?,?,?,?)', (graph_id, run_id, 'simulation', time.time()))
            return {'run_id': run_id, 'source_graph_id': graph_id, 'run_graph_id': graph_id, 'single_run': True}

    def release(self, graph_id, owner):
        self.db.execute("DELETE FROM leases WHERE graph_id=? AND owner=?", (graph_id, owner))

    def finalize(self, graph_id, expected):
        with self.transaction():
            graph = self.graph(graph_id)
            active = self.db.execute("SELECT count(*) FROM jobs WHERE graph_id=? AND status!='completed'", (graph_id,)).fetchone()[0]
            if active or graph["watermark"] != expected or graph["status"] != "ready":
                raise StoreError("ingestion_uncertain", "Memory barrier incomplete", 409)
            self.db.execute("UPDATE graphs SET finalized=1 WHERE id=?", (graph_id,))
            return {"expected_watermark": expected, "committed_watermark": graph["watermark"], "finalized": True}

    def begin_delete(self, graph_id):
        with self.transaction():
            graph = self.graph(graph_id)
            leases = self.db.execute("SELECT owner FROM leases WHERE graph_id=?", (graph_id,)).fetchall()
            active = self.db.execute("SELECT count(*) FROM jobs WHERE graph_id=? AND status IN ('queued','processing','cancelling')", (graph_id,)).fetchone()[0]
            if leases or active:
                raise StoreError("graph_busy", "Graph has active leases or ingestion tasks")
            self.db.execute("UPDATE graphs SET status='deleting' WHERE id=?", (graph_id,))
            return graph
