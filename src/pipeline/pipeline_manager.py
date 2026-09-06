import logging
from pathlib import Path
from typing import List

from merging.most_recent_deduplicated import MostRecentDeduplicated
from models.domain import PlatformConfig
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

    def _load_pipelines(self):
        for pipeline in self.platform_config.pipelines:
            try:
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
