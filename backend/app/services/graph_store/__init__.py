"""Backend selection comes from persisted project metadata, never graph UUIDs."""
from .protocol import *
from functools import lru_cache


class LazyCloudClient:
    """Read services can be constructed before their graph backend is known."""
    @property
    def client(self):
        if getattr(self, '_client', None) is None:
            from ...utils.zep import get_zep_client
            self._client = get_zep_client(self.api_key)
        return self._client

    @client.setter
    def client(self, value):
        self._client = value


def backend_for_graph(graph_id):
    from ...models.project import ProjectManager
    for project in ProjectManager.list_projects(limit=None):
        if project.graph_id == graph_id:
            return project.graph_backend
        if graph_id in project.graph_versions:
            return project.graph_versions[graph_id]['graph_backend']
    # Missing backend on historical records means Cloud, independently of the
    # default used for newly created projects.
    return "zep_cloud"


def is_local_graph(graph_id):
    return bool(graph_id) and backend_for_graph(graph_id) == "graphiti_local"


def get_graph_store(graph_id=None, *, backend=None):
    selected = backend or backend_for_graph(graph_id)
    if selected == "graphiti_local":
        from ...config import Config
        return _local_store(Config.GRAPHITI_SERVICE_URL, Config.GRAPHITI_SERVICE_TOKEN)
    if selected == "zep_cloud":
        from .zep_cloud_store import ZepCloudStore
        return ZepCloudStore()
    raise ValueError(f"Unknown graph backend: {selected}")


@lru_cache(maxsize=4)
def _local_store(url, token):
    from .graphiti_local_store import GraphitiLocalStore
    return GraphitiLocalStore()
