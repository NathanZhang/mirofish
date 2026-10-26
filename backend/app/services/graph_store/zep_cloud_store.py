"""Cloud boundary reusing the pinned SDK and its existing batch implementation."""
from ...utils.zep import get_zep_client, call_zep_read_with_retry
from ...utils.zep_paging import fetch_all_nodes, fetch_all_edges


class ZepCloudStore:
    backend = "zep_cloud"

    def __init__(self):
        self.client = get_zep_client()

    def export(self, graph_id):
        from ..graph_builder import GraphBuilderService
        return GraphBuilderService().get_graph_data(graph_id)

    def search(self, graph_id, query, scope="edges", temporal_mode="current", limit=10):
        from ..zep_tools import ZepToolsService
        return ZepToolsService().search_graph(graph_id, query, limit, scope).to_dict()

    def delete_graph(self, graph_id):
        self.client.graph.delete(graph_id=graph_id)
