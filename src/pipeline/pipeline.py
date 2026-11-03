import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any

from models.domain import (
    PipelineBooleanType,
    PipelineConditionType,
    PipelineConfig,
    PipelineExpression,
    StixEntity,
)
from models.domain import (
    PipelineCondition as DomainPipelineCondition,
)
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
    Set = "set"
    Delete = "delete"


class FieldKind(Enum):
    LIST = "list"
    SCALAR = "scalar"


@dataclass(frozen=True)
class FieldSpec:
    kind: FieldKind
    actions: frozenset[Action]


# Fields pipelines may modify, with their STIX shape. `add` only applies to
# list fields and `set` only to scalar fields; validated at startup.
FIELDS: dict[str, FieldSpec] = {
    "labels": FieldSpec(FieldKind.LIST, frozenset({Action.Add, Action.Delete})),
    "object_marking_refs": FieldSpec(
        FieldKind.LIST, frozenset({Action.Add, Action.Delete})
    ),
    "confidence": FieldSpec(FieldKind.SCALAR, frozenset({Action.Set, Action.Delete})),
    "created_by_ref": FieldSpec(
        FieldKind.SCALAR, frozenset({Action.Set, Action.Delete})
    ),
}


# Expression types _evaluate_condition implements; others are rejected at startup.
_SUPPORTED_CONDITIONS = frozenset(
    {
        PipelineConditionType.eq,
        PipelineConditionType.not_eq,
        PipelineConditionType.contains,
        PipelineConditionType.not_contains,
    }
)


class PipelineAction:
    def execute(self, object: StixEntity, field: str, value: str) -> str | None:
        raise NotImplementedError()


class PipelineSetAction(PipelineAction):
    """Set a scalar field, whether or not it already exists."""

    def execute(self, object: StixEntity, field: str, value: str) -> str | None:
        old_val = object.object.get(field, "N.A")
        object.object[field] = value
        return f"Set {field} from {old_val} to {value}"


class PipelineDeleteAction(PipelineAction):
    """Remove a value from a list field, or remove a scalar field."""

    def execute(self, object: StixEntity, field: str, value: str) -> str | None:
        if field not in object.object:
            return None
        current = object.object[field]
        if FIELDS[field].kind is FieldKind.LIST:
            if not isinstance(current, list):
                raise PipelineExecutionError(
                    f"Cannot delete from '{field}': expected a list, "
                    f"got {type(current).__name__}"
                )
            if value not in current:
                return None
            current.remove(value)
            return f"Deleted '{value}' from {field}"
        del object.object[field]
        return f"Deleted {field}"


class PipelineAddAction(PipelineAction):
    """Append a value to a list field, creating the list if it is missing."""

    def execute(self, object: StixEntity, field: str, value: str) -> str | None:
        current = object.object.get(field)
        if current is None:
            object.object[field] = [value]
            return f"Added '{value}' to {field}"
        if not isinstance(current, list):
            raise PipelineExecutionError(
                f"Cannot add to '{field}': expected a list, got {type(current).__name__}"
            )
        # Lists are treated as sets: no duplicates.
        if value in current:
            return None
        current.append(value)
        return f"Added '{value}' to {field}"


class PipelineExecutor:
    """Executes data processing pipelines defined in YAML"""

    def __init__(self, pipeline_definition: PipelineConfig):
        self.actions = {
            Action.Set: PipelineSetAction(),
            Action.Delete: PipelineDeleteAction(),
            Action.Add: PipelineAddAction(),
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
    ) -> None:
        if isinstance(condition, DomainPipelineCondition):
            for c in condition.condition:
                self._validate_condition(c)
            return
        # PipelineExpression
        if condition.type not in _SUPPORTED_CONDITIONS:
            supported = ", ".join(t.value for t in _SUPPORTED_CONDITIONS)
            raise PipelineValidationError(
                f"Condition type '{condition.type.value}' is not supported "
                f"(expected one of: {supported})"
            )

    def _evaluate_condition(
        self,
        object: dict[str, Any],
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
        valid_actions = ", ".join(a.value for a in Action)
        for step in self.pipeline_definition.steps:
            try:
                action = Action(step.action)
            except ValueError:
                raise PipelineValidationError(
                    f"Unknown action '{step.action}' (expected one of: {valid_actions})"
                ) from None
            spec = FIELDS.get(step.field)
            if spec is None:
                raise PipelineValidationError(
                    f"Field '{step.field}' cannot be modified by pipelines"
                )
            if action not in spec.actions:
                raise PipelineValidationError(
                    f"Action {step.action} is not allowed on {spec.kind.value} "
                    f"field {step.field}"
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
