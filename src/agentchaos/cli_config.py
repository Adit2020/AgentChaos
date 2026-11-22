"""Versioned, data-only configuration for the local command-line interface."""

import json
from pathlib import Path
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    ValidationError,
    field_validator,
    model_validator,
)

from agentchaos.models import FaultSpec

CONFIG_SCHEMA_VERSION = 1


class ConfigurationError(ValueError):
    """A configuration file cannot be read or does not match this CLI version."""


class _ConfigModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ToolFaultConfig(_ConfigModel):
    fault_type: Literal["tool_error", "tool_timeout", "malformed_tool_output"]
    target_tool: str = Field(min_length=1)
    occurrence: int = Field(default=1, ge=1, strict=True)
    error_message: str | None = None
    parameters: dict[str, JsonValue] = Field(default_factory=dict)

    @field_validator("target_tool")
    @classmethod
    def check_tool_name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("target_tool must name a tool")
        return value

    @model_validator(mode="after")
    def check_fault_options(self) -> "ToolFaultConfig":
        allowed = {
            "tool_error": set(),
            "tool_timeout": {"timeout_ms"},
            "malformed_tool_output": {"payload"},
        }[self.fault_type]
        unknown = self.parameters.keys() - allowed
        if unknown:
            raise ValueError(
                f"unsupported parameters for {self.fault_type}: {sorted(unknown)}"
            )
        if (
            self.fault_type == "malformed_tool_output"
            and self.error_message is not None
        ):
            raise ValueError("malformed_tool_output does not use error_message")
        # Reuse the domain model's requirements and timeout bounds.
        FaultSpec.model_validate(self.model_dump())
        return self

    def to_fault_spec(self) -> FaultSpec:
        return FaultSpec.model_validate(self.model_dump())


class ExactEvaluationConfig(_ConfigModel):
    type: Literal["exact"]
    expected_output: JsonValue


class FactsEvaluationConfig(_ConfigModel):
    type: Literal["required_facts"]
    facts: dict[str, str] = Field(min_length=1)
    case_insensitive_fields: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def check_fields(self) -> "FactsEvaluationConfig":
        fields = self.case_insensitive_fields
        if len(fields) != len(set(fields)):
            raise ValueError("case_insensitive_fields must not contain duplicates")
        unknown = set(fields) - self.facts.keys()
        if unknown:
            raise ValueError(
                f"case_insensitive_fields are not required facts: {sorted(unknown)}"
            )
        return self


class ExperimentConfig(_ConfigModel):
    schema_version: Literal[1]
    task: str = Field(min_length=1)
    adapter_factory: str = Field(min_length=3)
    fault: ToolFaultConfig
    evaluation: Annotated[
        ExactEvaluationConfig | FactsEvaluationConfig,
        Field(discriminator="type"),
    ]

    @field_validator("task")
    @classmethod
    def check_task(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("task must not be blank")
        return value

    @field_validator("adapter_factory")
    @classmethod
    def check_factory_reference(cls, value: str) -> str:
        module, separator, name = value.partition(":")
        if (
            not separator
            or ":" in name
            or not name.isidentifier()
            or not module
            or not all(part.isidentifier() for part in module.split("."))
        ):
            raise ValueError("adapter_factory must be module.path:callable_name")
        return value


def _reject_non_json_constant(value: str) -> None:
    raise ValueError(f"{value} is not valid JSON")


def load_config(path: Path) -> ExperimentConfig:
    """Read and validate data only; never import the configured adapter."""

    try:
        source = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigurationError(f"cannot read {path}: {exc.strerror or exc}") from exc
    try:
        data = json.loads(source, parse_constant=_reject_non_json_constant)
    except (json.JSONDecodeError, ValueError) as exc:
        raise ConfigurationError(f"invalid JSON in {path}: {exc}") from exc
    try:
        return ExperimentConfig.model_validate(data)
    except ValidationError as exc:
        details = "; ".join(
            f"{'.'.join(map(str, error['loc'])) or 'config'}: {error['msg']}"
            for error in exc.errors(include_url=False)
        )
        raise ConfigurationError(f"invalid configuration in {path}: {details}") from exc
