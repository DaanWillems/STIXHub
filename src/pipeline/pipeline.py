import logging
from enum import Enum
from typing import Any, Dict

import yaml

from database.repositories.bucket import BucketRepository
from models.domain import StixEntity
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

    def __init__(self, yaml_path: str):
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

        self.yaml_path = yaml_path
        self.pipeline_definition: Dict[Any, Any] = None

        self._load_pipeline()
        self._validate_pipeline()

        self.merge_strategy = self.pipeline_definition.get("merge_strategy", "")
        self.output_name = self.pipeline_definition.get("output_name", "Default")
        self.max_tlp = self.pipeline_definition.get("max_tlp", None)  # TODO: Validate

        self.reference_cache: dict = {}

    def _load_pipeline(self):
        """Load pipeline definition from YAML file or use default."""
        if self.yaml_path and self.yaml_path.strip():
            try:
                with open(self.yaml_path, "r") as f:
                    self.pipeline_definition = yaml.safe_load(f)
            except FileNotFoundError:
                logger.error(
                    f"Pipeline YAML file not found: {self.yaml_path}, using default"
                )
            except yaml.YAMLError as e:
                logger.error(
                    f"Error parsing YAML file {self.yaml_path}: {e}, using default"
                )

    def _validate_condition(self, condition: dict) -> PipelineCondition:
        match PipelineCondition(condition.get("type", "invalid")):
            case PipelineCondition.AND | PipelineCondition.OR:
                for c in condition.get("condition", []):
                    result = self._validate_condition(c)
                    if result is not bool:
                        raise PipelineValidationError(
                            f"Sub expression of the AND / OR filter must evaluate to boolean. {condition}"
                        )
                return bool
            case (
                PipelineCondition.EQ
                | PipelineCondition.NOT_EQ
                | PipelineCondition.LESS_EQUAL
                | PipelineCondition.MORE_EQUAL
            ):
                if type(condition["value"]) not in [int, str]:
                    raise PipelineValidationError()
                return bool
            case PipelineCondition.IN:
                return bool
            case _:
                pass

    def _evaluate_condition(self, object: dict, condition: dict) -> bool:
        if condition is None:
            return True
        match PipelineCondition(condition.get("type", "invalid")):
            case PipelineCondition.AND:
                return all(
                    [
                        self._evaluate_condition(object, c)
                        for c in condition.get("condition", [])
                    ]
                )
            case PipelineCondition.OR:
                return any(
                    [
                        self._evaluate_condition(object, c)
                        for c in condition.get("condition", [])
                    ]
                )
            case PipelineCondition.EQ:
                return object.get(condition["field"], None) == condition["value"]
            case PipelineCondition.IN:
                return condition["value"] in object.get(condition["field"], "")
            case _:
                pass

    def _validate_pipeline(self):
        if (
            self.pipeline_definition.get("merge_strategy", "")
            not in self.valid_merge_strategies
        ):
            raise PipelineValidationError("Invalid merge strategy")

        scope = self.pipeline_definition.get("scope", "").lower().split(",")

        for type in scope:
            try:
                StixType(type)
            except ValueError:
                raise PipelineValidationError(
                    f"Scope contains invalid STIX 2.1 type: {type}"
                )

        for step in self.pipeline_definition.get("steps", []):
            if Action(step.get("action", "")) not in self.valid_field_actions.get(
                step.get("field", ""), []
            ):
                raise PipelineValidationError(
                    f"Action {step.get('action', '')} is not allowed on field {step.get('field', '')}"
                )

            # Validate condition
            conditions = step.get("condition", None)
            if conditions is None:
                continue
            self._validate_condition(conditions)

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

            for step in self.pipeline_definition.get("steps", []):
                if not self._evaluate_condition(
                    object=obj.object, condition=step.get("condition", None)
                ):
                    continue
                audit_actions.append(
                    self.actions[Action(step.get("action"))].execute(
                        object=obj, field=step["field"], value=step["value"]
                    )
                )

            processed_objects.append(self._transform_values_to_refs(obj, repo))

        return processed_objects