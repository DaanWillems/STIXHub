from models.domain import StixEntity


class MergeStrategy:
    name: str

    def merge(
        self, objects: list[object]
    ) -> StixEntity | None:
        return None
