from typing import Tuple

from merging.merge import merge_entities_with_priority
from merging.merge_strategy import MergeStrategy
from models.domain import StixEntity


class MostRecentDeduplicated(MergeStrategy):
    name = "most_recent"

    def _update_entities(
        self, stix_objects
    ) -> StixEntity | None:
        for stix_row in stix_objects:
            if stix_row.platform_modified <= stix_row.platform_modified:
                stix_row.object = merge_entities_with_priority(
                    stix_row.object, stix_row.object
                )
            else:
                stix_row.object = merge_entities_with_priority(
                    stix_row.object, stix_row.object
                )
        if stix_row is None:
            return None
        stix_row.merged_timestamp = None
        return stix_row

    def merge(self, objects, type: str) -> StixEntity | None:
        return self._update_entities(object)