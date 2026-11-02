import logging
from pathlib import Path
from typing import List

from merging.most_recent_deduplicated import MostRecentDeduplicated
from models.domain import PipelineConfig, PlatformConfig
from permissions import get_effective_permissions
from pipeline.pipeline import (
    PipelineExecutionError,
    PipelineExecutor,
    PipelineValidationError,
)

logger = logging.getLogger(__name__)


class PipelineManager:
    def __init__(self, platform_config: PlatformConfig, pipelines_dir: str = "pipelines"):
        self.pipelines_dir = pipelines_dir
        self.platform_config = platform_config
        self._pipelines: List[PipelineExecutor] = []
        self._merge_strategies = [MostRecentDeduplicated()]
        self._load_pipelines()

    def pipelines(self) -> List[PipelineExecutor]:
        return self._pipelines

    def _validate_role(self, pipeline: PipelineConfig) -> None:
        """Check that the pipeline's role may read its sources and write its sinks."""
        if pipeline.role not in {role.name for role in self.platform_config.roles}:
            raise PipelineValidationError(
                f"Pipeline '{pipeline.name}' references unknown role '{pipeline.role}'"
            )
        can_read, can_write = get_effective_permissions(
            [pipeline.role], self.platform_config.roles
        )
        for source in pipeline.sources:
            if source not in can_read:
                raise PipelineValidationError(
                    f"Pipeline '{pipeline.name}': role '{pipeline.role}' "
                    f"cannot read source bucket '{source}'"
                )
        for sink in pipeline.sinks:
            if sink not in can_write:
                raise PipelineValidationError(
                    f"Pipeline '{pipeline.name}': role '{pipeline.role}' "
                    f"cannot write sink bucket '{sink}'"
                )

    def _load_pipelines(self):
        for pipeline in self.platform_config.pipelines:
            try:
                self._validate_role(pipeline)
                pipeline = PipelineExecutor(pipeline)
                self._pipelines.append(pipeline)
            except PipelineValidationError as e:
                logger.error(f"Pipeline failed validation: {e}")
                raise e
            else:
                logger.info(f"Succesfully loaded pipeline")

    def execute(self, stix_id: str, type: str):
        if not self._pipelines:
            return

        for pipeline in self._pipelines:
            merge_strategy = next(
                (
                    strategy
                    for strategy in self._merge_strategies
                    if strategy.name == pipeline.merge_strategy
                ),
                None,
            )

            if not merge_strategy:
                raise PipelineExecutionError(
                    f"No merge strategy found for pipeline: {pipeline.output_name}"
                )

            try:
                objects = pipeline.execute_pipeline(stix_id, StixType(type), repo)
                merged_object, _ = merge_strategy.merge(objects, type)
                if merged_object is None:
                    continue
                merged_object.view = pipeline.output_name

                if StixType(type) is StixType.Relationship:
                    repo.upsert_processed_relationship(merged_object)
                else:
                    repo.upsert_processed_object(merged_object)

            except Exception as e:
                raise e
