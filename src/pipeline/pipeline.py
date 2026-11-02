import logging
from enum import Enum
from typing import Any

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
from pipeline.refs import RefResolver


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
            if Action(step.action) not in self.valid_field_actions.get(step.field, []):
                raise PipelineValidationError(
                    f"Action {step.action} is not allowed on field {step.field}"
                )
            if step.condition is None:
                continue
            self._validate_condition(step.condition)

    async def execute_pipeline(
        self, objects: list[StixEntity], resolver: RefResolver
    ) -> list[StixEntity]:
        """Run the pipeline steps on copies of ``objects``.

        Ref fields are translated to readable values before conditions and
        steps run, and back to refs afterwards. ``objects`` themselves are
        never modified.
        """
        audit_actions = []
        processed_objects = []

        await resolver.prefetch(objects)

        for source in objects:
            resolved = resolver.to_values(source)
            obj = resolved.entity

            for step in self.pipeline_definition.steps:
                if not self._evaluate_condition(
                    object=obj.object, condition=step.condition
                ):
                    continue
                audit_actions.append(
                    self.actions[Action(step.action)].execute(
                        object=obj, field=step.field, value=step.value
                    )
                )

            processed_objects.append(resolver.to_refs(resolved))

        return processed_objects
