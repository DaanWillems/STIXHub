import logging
from pathlib import Path
from typing import List

from merging.most_recent_deduplicated import MostRecentDeduplicated
from pipeline.pipeline import (
    PipelineExecutionError,
    PipelineExecutor,
    PipelineValidationError,
)

logger = logging.getLogger(__name__)


class PipelineManager:
    def __init__(self, pipelines_dir: str = "pipelines"):
        self.pipelines_dir = pipelines_dir
        self._pipelines: List[PipelineExecutor] = []
        self._merge_strategies = [MostRecentDeduplicated()]
        self._load_pipelines()

    def _load_pipelines(self):
        pipelines_path = Path(self.pipelines_dir)

        if not pipelines_path.exists():
            logger.error(f"Pipeline directory does not exist {pipelines_path}")
            return

        yaml_files = list(pipelines_path.glob("*.yaml")) + list(
            pipelines_path.glob("*.yml")
        )

        if not yaml_files:
            return

        for yaml_file in yaml_files:
            try:
                pipeline = PipelineExecutor(yaml_path=str(yaml_file))
                self._pipelines.append(pipeline)
            except PipelineValidationError as e:
                logger.error(f"Pipeline {yaml_file} failed validation: {e}")
                raise e
            else:
                logger.info(f"Succesfully loaded pipeline {yaml_file}")

    def execute(self, stix_id: str, type: str):
        if not self._pipelines:
            return

        with SessionLocal() as session:
            repo = StixRepository(session)

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
