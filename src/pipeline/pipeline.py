import logging
from enum import Enum
from typing import Any

from database.repositories.bucket import BucketRepository
from models.domain import (
    PipelineBooleanType,
    PipelineCondition as DomainPipelineCondition,
    PipelineConditionType,
    PipelineConfig,
    PipelineExpression,
    PipelineStepConfig as PipelineStepConfigModel,
    StixEntity,
)
from models.models import StixType


logger = logging.getLogger(__name__)


class NotImplementedError(Exception):
    pass


class PipelineValidationError(Exception):
    pass


class PipelineExecutionError(Exception):
    pass


class PipelineCondition(Enum):
    EQ = "eq"
    NOT_EQ = "not_eq"
    AND = "and"
    OR = "or"
    LESS_EQUAL = "less_eq"
    MORE_EQUAL = "more_eq"
    BOOL = "bool"
    STR = "str"
    INT = "int"
    INVALID = "invalid"
    IN = "in"


class Action(Enum):
    Add = "add"
    Replace = "replace"
    Delete = "delete"


class PipelineAction:
    def execute(object: dict) -> str:
        return False


class PipelineReplaceAction(PipelineAction):
    def execute(self, object: dict, field: str, value: str) -> str | None:
        old_val = "N.A"
        if field in object.object:
            old_val = object.object[field]
        object.object[field] = value
        return f"Replaced {field} from {old_val} to {value}"


class PipelineDeleteAction(PipelineAction):
    def _execute_list(self, object: dict, field: str, value: str):
        # Assume lists can contain no duplicates
        if value in object.object[field]:
            object.object[field].remove(value)
            return "Deleted '{value}' from {field}"
        return None

    def _execute_dict(self, object: dict, field: str, value: str):
        raise NotImplementedError()

    def execute(self, object: dict, field: str, value: str) -> str:
        if field in object.object and type(object.object[field]) is list:
            self._execute_list(object, field, value)
        elif field in object.object and type(object.object[field]) is dict:
            self._execute_dict(object, field, value)
        elif field in object.object:
            object.object.pop(field, None)
            return f"Deleted '{value}' from {field}"
        return False


class PipelineAddAction(PipelineAction):
    def _execute_list(self, object: dict, field: str, value: str):
        # Assume lists can contain no duplicates
        if value not in object.object[field]:
            object.object[field].append(value)
        return f"Added {value} to {field}"

    def _execute_dict(self, object: dict, field: str, value: str):
        raise NotImplementedError()

    def execute(self, object: dict, field: str, value: str) -> str:
        if field in object.object and type(object.object[field]) is list:
            return self._execute_list(object, field, value)
        elif field in object.object and type(object.object[field]) is dict:
            return self._execute_dict(object, field, value)
        elif field not in object.object:
            object.object[field] = value
            return f"Added '{value}' to {field}"
        return None


class PipelineExecutor:
    """Executes data processing pipelines defined in YAML"""

    def __init__(self, pipeline_definition: PipelineConfig):
        self.actions = {
            Action.Replace: PipelineReplaceAction(),
            Action.Delete: PipelineDeleteAction(),
            Action.Add: PipelineAddAction(),
        }

        self.valid_field_actions = {
            "confidence": [Action.Replace, Action.Delete],
            "created_by_ref": [Action.Replace, Action.Delete],
            "labels": {Action.Delete, Action.Add},
            "object_marking_refs": {Action.Delete, Action.Add},
        }

        self.valid_merge_strategies = [
            "most_recent",
        ]

        self.valid_expressions_results = {
            PipelineCondition.AND: [bool],
            PipelineCondition.EQ: [
                int,
                str,
                bool,
            ],
        }

        self.pipeline_definition: PipelineConfig = pipeline_definition
        self._validate_pipeline()

        self.reference_cache: dict = {}

    def _validate_condition(
        self, condition: DomainPipelineCondition | PipelineExpression
    ) -> type:
        if isinstance(condition, DomainPipelineCondition):
            for c in condition.condition:
                result = self._validate_condition(c)
                if result is not bool:
                    raise PipelineValidationError(
                        f"Sub expression of the AND / OR filter must evaluate to boolean. {condition}"
                    )
            return bool
        # PipelineExpression
        if not isinstance(condition.value, (int, str)):
            raise PipelineValidationError()
        return bool

    def _evaluate_condition(
        self,
        object: dict,
        condition: DomainPipelineCondition | PipelineExpression | None,
    ) -> bool:
        if condition is None:
            return True
        if isinstance(condition, DomainPipelineCondition):
            if condition.type == PipelineBooleanType.AND:
                return all(
                    self._evaluate_condition(object, c) for c in condition.condition
                )
            return any(
                self._evaluate_condition(object, c) for c in condition.condition
            )
        # PipelineExpression
        match condition.type:
            case PipelineConditionType.eq:
                return object.get(condition.field) == condition.value
            case PipelineConditionType.not_eq:
                return object.get(condition.field) != condition.value
            case PipelineConditionType.contains:
                return condition.value in object.get(condition.field, "")
            case PipelineConditionType.not_contains:
                return condition.value not in object.get(condition.field, "")
            case _:
                return False

    def _validate_pipeline(self) -> None:
        for step in self.pipeline_definition.steps:
            self._validate_condition(step.condition)

    def _transform_refs_to_values(
        self: "PipelineExecutor", object: StixEntity, repo: BucketRepository
    ) -> StixEntity:
        # Resolve marking refs to human-readable "type:value" strings
        original_marking_refs = object.object.get("object_marking_refs", None)
        markings = []

        if original_marking_refs:
            for marking_ref in original_marking_refs:
                marking = repo.get_marking_by_stix_id(marking_ref)
                if marking is None:
                    logger.warning(
                        f"{marking_ref} does not exist in platform and cannot be resolved "
                        "in pipeline preventing correct evaluation of conditions"
                    )
                    continue
                markings.append(f"{marking.type}:{marking.value}")
                self.reference_cache[f"{marking.type}:{marking.value}"] = marking_ref
                logger.debug(
                    f"Writing marking ref {marking.type}:{marking.value}={marking_ref} to cache"
                )

            object.object["object_marking_refs"] = markings

        original_created_by_ref = object.object.get("created_by_ref", None)
        if original_created_by_ref:
            _, authors = repo.filter_processed_objects(
                ProcessedObjectFilter(stix_id=original_created_by_ref, limit=1)
            )
            if not authors:
                logger.warning(
                    f"{original_created_by_ref} does not exist in platform and cannot be resolved "
                    "in pipeline preventing correct evaluation of conditions"
                )
                return object
            author = authors[0]
            self.reference_cache[author.value] = original_created_by_ref
            logger.debug(
                f"Writing created by ref {author.value}={original_created_by_ref} to cache"
            )
            object.object["created_by_ref"] = author.value

        return object

    def _transform_values_to_refs(
        self: "PipelineExecutor", object: StixEntity, repo: BucketRepository
    ) -> StixEntity:
        # Resolve "type:value" strings back to marking STIX IDs
        original_marking_values = object.object.get("object_marking_refs", None)
        markings = []

        if original_marking_values is not None:
            for marking_value in original_marking_values:
                cache_ref = self.reference_cache.get(marking_value, None)
                if cache_ref:
                    logger.debug(f"Got marking ref {marking_value} from cache")
                    markings.append(cache_ref)
                    continue

                split_value = marking_value.split(":", 1)
                marking = repo.get_marking_by_type_value(split_value[0], split_value[1])
                if marking is None:
                    logger.warning(
                        f"{marking_value} does not exist in platform and cannot be resolved "
                        "in pipeline preventing correct evaluation of conditions"
                    )
                    continue
                markings.append(marking.stix_id)
            object.object["object_marking_refs"] = markings

        original_created_by_value = object.object.get("created_by_ref", None)
        if original_created_by_value:
            cache_ref = self.reference_cache.get(original_created_by_value, None)
            if cache_ref:
                logger.debug(f"Got author ref {original_created_by_value} from cache")
                object.object["created_by_ref"] = cache_ref
            else:
                _, authors = repo.filter_objects(ObjectFilter(value=original_created_by_value, limit=1))
                if not authors:
                    logger.warning(
                        f"{original_created_by_value} does not exist in platform and cannot be resolved "
                        "in pipeline preventing correct evaluation of conditions"
                    )
                    return object
                object.object["created_by_ref"] = authors[0].stix_id

        return object

    def execute_pipeline(
        self, stix_id: str, stix_type: StixType, objects: list[StixEntity], repo: BucketRepository
    ) -> list[StixEntity]:
        audit_actions = []
        processed_objects = []

        for obj in objects:
            obj = self._transform_refs_to_values(obj, repo)

            for step in self.pipeline_definition.steps:
                if not self._evaluate_condition(
                    object=obj.object, condition=step.condition
                ):
                    continue
                audit_actions.append(
                    self.actions[Action(step.action)].execute(object=obj)
                )

            processed_objects.append(self._transform_values_to_refs(obj, repo))

        return processed_objects
