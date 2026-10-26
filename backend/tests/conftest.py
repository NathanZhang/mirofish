"""Unit tests must never inspect or mutate a running user's saved projects."""
import pytest


@pytest.fixture(autouse=True)
def isolate_persisted_projects(tmp_path, monkeypatch):
    from app.models.project import ProjectManager
    from app.config import Config
    from app.services.simulation_manager import SimulationManager
    from app.services.simulation_runner import SimulationRunner
    from app.services.report_agent import ReportManager
    monkeypatch.setattr(ProjectManager, 'PROJECTS_DIR', str(tmp_path / 'projects'))
    monkeypatch.setattr(Config, 'OASIS_SIMULATION_DATA_DIR', str(tmp_path / 'simulations'))
    monkeypatch.setattr(SimulationManager, 'SIMULATION_DATA_DIR', str(tmp_path / 'simulations'))
    monkeypatch.setattr(SimulationRunner, 'RUN_STATE_DIR', str(tmp_path / 'simulations'))
    monkeypatch.setattr(ReportManager, 'REPORTS_DIR', str(tmp_path / 'reports'))
