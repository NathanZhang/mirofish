import pytest

from app.journal import Journal, StoreError


def setup(tmp_path):
    journal = Journal(tmp_path / "jobs.sqlite")
    graph = journal.create_graph("project", {}, {}, "embedding-v1")
    return journal, graph["id"]


def item(text="原文", count=1):
    return {"item_id": "event-1", "body": text, "reference_time": "2026-10-24T00:00:00Z", "event_count": count}


def test_durable_idempotence_and_conflicting_payload(tmp_path):
    journal, graph_id = setup(tmp_path)
    job = journal.submit(graph_id, [item()], "run:platform:offset")
    journal.db.close()
    journal = Journal(tmp_path / "jobs.sqlite")
    assert journal.submit(graph_id, [item()], "run:platform:offset")["id"] == job["id"]
    with pytest.raises(StoreError, match="different payload"):
        journal.submit(graph_id, [item("改变的原文")], "run:platform:offset")
    assert journal.db.execute("SELECT count(*) FROM items").fetchone()[0] == 1


@pytest.mark.parametrize('graph_write_returned', [False, True])
def test_restart_inflight_is_uncertain_and_never_replayed(tmp_path, graph_write_returned):
    journal, graph_id = setup(tmp_path)
    job = journal.submit(graph_id, [item()], "op")
    assert journal.claim()["id"] == job["id"]
    journal.next_item(job["id"])
    if graph_write_returned:
        # Model the non-atomic boundary: the external store already committed,
        # but the worker dies before updating its SQLite item ledger.
        (tmp_path / 'external-graph-commit').write_text(job['items'][0]['episode_uuid'])
    journal.db.close()
    journal = Journal(tmp_path / "jobs.sqlite")
    journal.recover()
    assert journal.job(job["id"])["status"] == "uncertain"
    assert journal.claim() is None
    if graph_write_returned:
        assert (tmp_path / 'external-graph-commit').read_text() == job['items'][0]['episode_uuid']
    with pytest.raises(StoreError):
        journal.finalize(graph_id, 0)
    with pytest.raises(StoreError):
        journal.submit(graph_id, [item()], "retry-with-new-key")


def test_commit_before_job_update_recovers_without_duplicate_write(tmp_path):
    journal, graph_id = setup(tmp_path)
    job = journal.submit(graph_id, [item()], "op")
    journal.claim()
    journal.next_item(job["id"])
    journal.commit_item(job["id"], 0)
    journal.recover()
    assert journal.claim()["id"] == job["id"]
    assert journal.next_item(job["id"]) is None
    journal.finish_job(job["id"])
    assert journal.finalize(graph_id, 1)["committed_watermark"] == 1
    with pytest.raises(StoreError):
        journal.submit(graph_id, [item()], "late-event")


def test_delete_excludes_pending_jobs_and_durable_readers(tmp_path):
    journal, graph_id = setup(tmp_path)
    journal.lease(graph_id, "report-1", "report")
    journal.db.close()
    journal = Journal(tmp_path / "jobs.sqlite")
    with pytest.raises(StoreError):
        journal.begin_delete(graph_id)
    journal.release(graph_id, "report-1")
    job = journal.submit(graph_id, [item()], "op")
    with pytest.raises(StoreError):
        journal.begin_delete(graph_id)
    journal.claim()
    journal.next_item(job["id"])
    journal.commit_item(job["id"], 0)
    journal.finish_job(job["id"])
    journal.begin_delete(graph_id)
    with pytest.raises(StoreError):
        journal.lease(graph_id, "new-report", "report")


def test_watermark_requires_exact_committed_events(tmp_path):
    journal, graph_id = setup(tmp_path)
    job = journal.submit(graph_id, [item(count=4)], "op")
    journal.claim()
    journal.next_item(job["id"])
    with pytest.raises(StoreError):
        journal.finalize(graph_id, 4)
    journal.commit_item(job["id"], 0)
    journal.finish_job(job["id"])
    with pytest.raises(StoreError):
        journal.finalize(graph_id, 3)
    assert journal.finalize(graph_id, 4)["finalized"]


def test_accepted_job_survives_restart_before_worker_claim(tmp_path):
    journal, graph_id = setup(tmp_path)
    job = journal.submit(graph_id, [item()], 'accepted')
    journal.db.close()
    journal = Journal(tmp_path / 'jobs.sqlite')
    journal.recover()
    assert journal.claim()['id'] == job['id']


def test_single_run_claim_and_export_lease_exclude_mutations(tmp_path):
    journal, graph_id = setup(tmp_path)
    journal.db.execute("UPDATE graphs SET status='ready' WHERE id=?", (graph_id,))
    journal.claim_run(graph_id, 'run-1')
    with pytest.raises(StoreError, match='一次模拟'):
        journal.claim_run(graph_id, 'run-1')
    with pytest.raises(StoreError):
        journal.claim_run(graph_id, 'run-2')
    journal.release(graph_id, 'run-1')
    journal.lease(graph_id, 'snapshot', 'export')
    with pytest.raises(StoreError, match='export'):
        journal.submit(graph_id, [item()], 'mutate-during-export')


def test_cancellation_is_durable_and_never_publishes_partial_graph(tmp_path):
    journal, graph_id = setup(tmp_path)
    job = journal.submit(graph_id, [item()], 'cancel-queued')
    assert journal.cancel(job['id'])['status'] == 'cancelled'
    journal.recover()
    assert journal.claim() is None
    with pytest.raises(StoreError):journal.finalize(graph_id, 0)


def test_cancellation_waits_for_inflight_write_boundary(tmp_path):
    journal, graph_id = setup(tmp_path)
    job = journal.submit(graph_id, [item()], 'cancel-processing')
    journal.claim();journal.next_item(job['id'])
    assert journal.cancel(job['id'])['status'] == 'cancelling'
    with pytest.raises(StoreError):journal.begin_delete(graph_id)
    journal.commit_item(job['id'],0)
    assert journal.next_item(job['id']) is None
    assert journal.job(job['id'])['status'] == 'cancelled'
    assert journal.graph(graph_id)['watermark'] == 1
    with pytest.raises(StoreError):journal.finalize(graph_id,1)
