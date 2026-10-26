"""Durable local builds: request acceptance and status projection, no Flask worker."""
import hashlib
import json
from pathlib import Path
from datetime import datetime, timezone

from ...models.project import ProjectManager, ProjectStatus
from ..text_processor import TextProcessor
from . import get_graph_store
from .protocol import GraphBusy, GraphNotFound


def refresh_build(project):
    if not project.graph_id:
        return None
    store = get_graph_store(backend='graphiti_local')
    if not project.ingestion_job_id:
        metadata = store.request('GET', f'graphs/{project.graph_id}/metadata/details')
        accepted = [job for job in metadata['jobs'] if job['operation_key'].startswith('seed:')]
        if not accepted:
            return None
        project.ingestion_job_id = accepted[0]['id']
        project.graph_build_task_id = accepted[0]['id']
        ProjectManager.save_project(project)
    job = store.get_ingestion(project.ingestion_job_id)
    result = None
    status = job['status']
    if status == 'completed':
        stats = store.get_stats(project.graph_id)
        if not stats['node_count'] or not stats['edge_count']:
            status = 'failed'
            job['error'] = '摄入已结束，但实体或事实为空；请核对材料、本体和抽取输出'
        else:
            result = {'project_id': project.project_id, 'graph_id': project.graph_id,
                      'node_count': stats['node_count'], 'edge_count': stats['edge_count'],
                      'chunk_count': job['total'], 'ingestion_job_id': job['id'],
                      'communities': 'optional_not_built'}
            if project.status != ProjectStatus.GRAPH_COMPLETED:
                # Ensure completed publication includes readable evidence, not just a journal flag.
                data = store.export(project.graph_id)
                if any(not e.get('episodes') for e in data['edges']):
                    raise GraphBusy('事实边缺少原文 episode 来源，暂不发布图谱')
                store.search(project.graph_id, data['nodes'][0]['name'], scope='combined', limit=3)
                snapshot = Path(ProjectManager._get_project_dir(project.project_id)) / f'graph-{project.build_revision}.json'
                snapshot.write_text(json.dumps(data, ensure_ascii=False))
    mapped = ProjectStatus.GRAPH_COMPLETED if status == 'completed' else ProjectStatus.FAILED if status in {'failed','uncertain','cancelled'} else ProjectStatus.GRAPH_BUILDING
    if project.status != mapped or project.error != job.get('error'):
        project.status, project.error = mapped, job.get('error')
        ProjectManager.save_project(project)
    return {'task_id': job['id'], 'task_type': 'graph_build',
            'status': 'completed' if mapped == ProjectStatus.GRAPH_COMPLETED else 'failed' if mapped == ProjectStatus.FAILED else 'processing',
            'progress': job['progress'], 'message': f"本地 Graphiti：{job['committed']}/{job['total']} 块已提交；社区摘要为可选项",
            'result': result, 'error': job.get('error'), 'metadata': {'project_id': project.project_id, 'graph_id': project.graph_id}}


def start_build(project, data):
    store = get_graph_store(backend='graphiti_local')
    force = data.get('force', False)
    if not isinstance(force, bool):
        raise GraphBusy('force must be a JSON boolean')
    if project.graph_id and project.status == ProjectStatus.GRAPH_BUILDING and not project.ingestion_job_id:
        refresh_build(project)
    if project.ingestion_job_id:
        task = refresh_build(project)
        if task['status'] == 'processing' or (task['status'] == 'completed' and not force):
            return {'project_id': project.project_id, 'task_id': project.ingestion_job_id,
                    'graph_id': project.graph_id, 'reused': True}
        if not force:
            raise GraphBusy('原任务失败或写入不确定；使用 force 在新 namespace 重建，旧版本保留供核对')
    if force and project.graph_id:
        metadata = store.request('GET', f'graphs/{project.graph_id}/metadata/details')
        active_jobs = any(j['status'] in {'processing','cancelling'} or (j['status'] == 'queued' and metadata['status'] not in {'uncertain','failed'}) for j in metadata['jobs'])
        if metadata.get('leases') or active_jobs:
            raise GraphBusy('图谱仍有活动消费者或摄入任务，暂不能重建项目')
    text = ProjectManager.get_extracted_text(project.project_id)
    if not text or not project.ontology:
        raise GraphBusy('请先上传材料并生成本体')
    size, overlap = data.get('chunk_size', project.chunk_size), data.get('chunk_overlap', project.chunk_overlap)
    if type(size) is not int or type(overlap) is not int or not 0 <= overlap < size:
        raise GraphBusy('chunk 参数须满足 0 <= overlap < size')
    chunks = TextProcessor.split_text(text, chunk_size=size, overlap=overlap)
    if not chunks:
        raise GraphBusy('材料分块为空')
    now = datetime.now(timezone.utc).isoformat()
    spec = {'source_sha256': hashlib.sha256(text.encode()).hexdigest(), 'chunk_size': size,
            'chunk_overlap': overlap, 'reference_time': now, 'communities': 'optional_not_built'}
    if project.graph_id and project.status == ProjectStatus.GRAPH_BUILDING and not force:
        # HTTP acceptance may have completed before Flask saved the job ID.
        # Reuse the persisted graph and original reference time; never change
        # the operation payload while recovering a lost response.
        metadata = store.request('GET', f'graphs/{project.graph_id}/metadata/details')
        original = json.loads(metadata['build_spec'])
        if any(original.get(k) != spec[k] for k in ['source_sha256','chunk_size','chunk_overlap']):
            raise GraphBusy('待恢复图谱与当前输入不一致；请显式重建新版本')
        spec = original
        now = original['reference_time']
        created = project.graph_versions[project.graph_id]
    else:
        created = store.create_graph(project.project_id, project.ontology, spec)
    project.graph_id = created['graph_id']
    project.build_revision = created['build_revision']
    project.embedding_profile_id = created['embedding_profile_id']
    project.graph_schema_version = created['graph_schema_version']
    project.chunk_size, project.chunk_overlap = size, overlap
    project.graph_versions[project.graph_id] = {**created, 'graph_backend': 'graphiti_local'}
    project.status, project.error = ProjectStatus.GRAPH_BUILDING, None
    # Save graph identity before accepting ingestion so reset/restart can always find it.
    ProjectManager.save_project(project)
    operation_key = 'seed:' + spec['source_sha256']
    items = [{'item_id': f'chunk:{i}', 'body': chunk, 'reference_time': now,
              'source_description': f'MiroFish seed {project.project_id} chunk {i}',
              'event_count': 0, 'metadata': {'chunk_index': i, 'sha256': hashlib.sha256(chunk.encode()).hexdigest()}}
             for i, chunk in enumerate(chunks)]
    job = store.submit_ingestion(project.graph_id, items, operation_key)
    project.ingestion_job_id = job['id']
    project.graph_build_task_id = job['id']
    ProjectManager.save_project(project)
    return {'project_id': project.project_id, 'graph_id': project.graph_id, 'task_id': job['id'], 'ingestion_job_id': job['id']}


def get_display_graph(graph_id):
    store = get_graph_store(graph_id)
    stats = store.get_stats(graph_id)
    if stats['status'] == 'ingesting':
        for project in ProjectManager.find_projects_by_graph_id(graph_id):
            path = Path(ProjectManager._get_project_dir(project.project_id)) / f'graph-{project.build_revision}.json'
            if path.exists():
                return {**json.loads(path.read_text()), 'snapshot': True,
                        'snapshot_label': '显示建图完成时的快照；模拟记忆写入中'}
    return store.export(graph_id)
