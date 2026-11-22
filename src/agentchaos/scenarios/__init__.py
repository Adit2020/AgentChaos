"""Explicit server-owned scenarios; HTTP clients cannot register factories."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from agentchaos.adapters.base import FaultCapableAdapter
from agentchaos.benchmarks.itsm.evidence import (
    ASSIGNMENT_TOOL,
    evaluate_assignment,
)
from agentchaos.benchmarks.itsm.evidence import (
    DEFAULT_TASK as ITSM_TASK,
)
from agentchaos.compare import TaskEvaluator
from agentchaos.models import FaultSpec
from agentchaos.scenarios import itsm_assignment, order_support

ToolFaultType = Literal["tool_error", "tool_timeout", "malformed_tool_output"]


class ScenarioMetadata(BaseModel):
    """Public, data-only options for a trusted scenario."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    scenario_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    name: str = Field(min_length=1)
    description: str = Field(min_length=1)
    supported_target_tools: tuple[str, ...] = Field(min_length=1)
    supported_fault_types: tuple[ToolFaultType, ...] = Field(min_length=1)
    default_task: str = Field(min_length=1)
    max_occurrence: int = Field(default=3, ge=1, le=10, strict=True)


@dataclass(frozen=True, slots=True)
class Scenario:
    """Metadata plus trusted, nonblocking synchronous setup and evaluation."""

    metadata: ScenarioMetadata
    adapter_factory: Callable[[], FaultCapableAdapter]
    evaluator: TaskEvaluator


def registered_scenarios() -> tuple[Scenario, ...]:
    """List definitions without constructing an adapter or calling a model."""

    return (
        Scenario(
            metadata=ScenarioMetadata(
                scenario_id="order_support",
                name="Order support",
                description=(
                    "Look up a fixed local order and compare baseline versus "
                    "faulted agent behavior. Uses live model calls."
                ),
                supported_target_tools=("get_order_status_json",),
                supported_fault_types=(
                    "tool_error",
                    "tool_timeout",
                    "malformed_tool_output",
                ),
                default_task=order_support.DEFAULT_TASK,
            ),
            adapter_factory=order_support.create_adapter,
            evaluator=order_support.order_answer_is_correct,
        ),
        Scenario(
            metadata=ScenarioMetadata(
                scenario_id="itsm_assignment",
                name="ITSM ticket assignment",
                description=(
                    "Assign a ticket in a fresh local fixture per run and compare "
                    "the committed state. Uses live model calls."
                ),
                supported_target_tools=(ASSIGNMENT_TOOL,),
                supported_fault_types=(
                    "tool_error",
                    "tool_timeout",
                    "malformed_tool_output",
                ),
                default_task=ITSM_TASK,
            ),
            adapter_factory=itsm_assignment.create_adapter,
            evaluator=evaluate_assignment,
        ),
    )


def configured_fault(
    fault_type: ToolFaultType, target_tool: str, occurrence: int
) -> FaultSpec:
    """Use server-owned error text, simulated timeout, and malformed payload."""

    if target_tool == ASSIGNMENT_TOOL:
        error_message = (
            "Temporary assignment failure. Check the assignment before retrying."
        )
        malformed_payload = '{"ticket_id":"INC-001","assignee_id":'
    else:
        error_message = "Temporary order lookup failure. Please retry once."
        malformed_payload = '{"order_id":"123","status":'

    return FaultSpec(
        fault_type=fault_type,
        target_tool=target_tool,
        occurrence=occurrence,
        error_message=None if fault_type == "malformed_tool_output" else error_message,
        parameters=(
            {"payload": malformed_payload}
            if fault_type == "malformed_tool_output"
            else {"timeout_ms": 10}
            if fault_type == "tool_timeout"
            else {}
        ),
    )
