from typing import List
from server.platform.materialized_view.most_recent_deduplicated import (
    MergeStrategy,
)


class ViewManager:
    views: List[MergeStrategy] = []

    def register_view(self: "ViewManager", new_view: MergeStrategy):
        if any([v.name == new_view.name for v in self.views]):
            return
        self.views.append(new_view)

    def update_views(self: "ViewManager", type: str, stix_id: str):
        for view in self.views:
            view.update_view(stix_id=stix_id, type=type)

    def reprocess_view(self: "ViewManager", view_name: str):
        for view in self.views:
            if view.name == view_name:
                view.reprocess_view()
