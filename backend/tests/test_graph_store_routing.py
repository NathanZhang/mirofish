from app.config import Config
from app.models.project import Project, ProjectStatus, ProjectManager
from app.services.graph_store import backend_for_graph


def test_default_only_affects_new_projects_and_old_versions_keep_backend(monkeypatch):
    monkeypatch.setattr(Config,'GRAPH_BACKEND','graphiti_local')
    new=ProjectManager.create_project('local')
    assert new.graph_backend=='graphiti_local'
    historical=Project.from_dict({'project_id':'old','graph_id':'cloud','status':'graph_completed'})
    assert historical.graph_backend=='zep_cloud'
    historical.created_at='2026-10-01';historical.updated_at=historical.created_at
    ProjectManager._get_project_dir('old')
    import os
    os.makedirs(ProjectManager._get_project_dir('old'))
    ProjectManager.save_project(historical)
    new.graph_id='active-local'
    new.graph_versions={'archived-local':{'graph_backend':'graphiti_local'}}
    ProjectManager.save_project(new)
    monkeypatch.setattr(Config,'GRAPH_BACKEND','zep_cloud')
    assert backend_for_graph('active-local')=='graphiti_local'
    assert backend_for_graph('archived-local')=='graphiti_local'
    assert backend_for_graph('cloud')=='zep_cloud'
