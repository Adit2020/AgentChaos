"""Portable, JSON-serializable records shared by all future agent adapters."""

from enum import StrEnum

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    model_validator,
)


class DomainModel(BaseModel):
    """Reject unknown fields so malformed traces fail at the boundary."""

    model_config = ConfigDict(extra="forbid")


class EventKind(StrEnum):
    """Common lifecycle and model/tool boundary events."""

    RUN_STARTED = "run.started"
    RUN_COMPLETED = "run.completed"
    RUN_FAILED = "run.failed"
    RUN_CANCELLED = "run.cancelled"
    MODEL_REQUEST = "model.request"
    MODEL_RESPONSE = "model.response"
    MODEL_ERROR = "model.error"
    TOOL_CALL = "tool.call"
    TOOL_RESULT = "tool.result"
    TOOL_ERROR = "tool.error"


class RunStatus(StrEnum):
    """Overall outcome of one agent execution."""

    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class RunEvent(DomainModel):
    """One ordered observation in a run, independent of its source framework.

    ``call_id`` pairs a request/call with its response/result/error. ``name`` is
    the model or tool name when applicable. Attributes contain only JSON values,
    so adapters must normalize provider objects before emitting an event.
    """

    event_id: str = Field(min_length=1)
    sequence: int = Field(ge=0)
    occurred_at: AwareDatetime
    kind: EventKind
    call_id: str | None = None
    name: str | None = None
    attributes: dict[str, JsonValue] = Field(default_factory=dict)


class AgentRun(DomainModel):
    """A normalized trace for one execution of a task."""

    run_id: str = Field(min_length=1)
    task: str = Field(min_length=1)
    adapter: str = Field(min_length=1)
    status: RunStatus
    started_at: AwareDatetime
    ended_at: AwareDatetime | None = None
    events: list[RunEvent] = Field(default_factory=list)
    output: JsonValue = None

    @model_validator(mode="after")
    def check_trace(self) -> "AgentRun":
        if self.status == RunStatus.RUNNING and self.ended_at is not None:
            raise ValueError("a running run cannot have ended_at")
        if self.status != RunStatus.RUNNING and self.ended_at is None:
            raise ValueError("a finished run requires ended_at")
        if self.ended_at is not None and self.ended_at < self.started_at:
            raise ValueError("ended_at must not precede started_at")
        sequences = [event.sequence for event in self.events]
        if sequences != sorted(set(sequences)):
            raise ValueError("event sequences must be unique and increasing")
        event_ids = [event.event_id for event in self.events]
        if len(event_ids) != len(set(event_ids)):
            raise ValueError("event IDs must be unique within a run")
        return self


class FaultSpec(DomainModel):
    """A planned fault at a baseline event or a named tool occurrence.

    ``target_event_id`` retains the original trace-targeting contract. The
    deterministic tool faults instead use ``target_tool`` and ``occurrence``.
    ``parameters`` supplies timeout_ms or the malformed payload.
    """

    fault_type: str = Field(min_length=1)
    target_event_id: str | None = Field(default=None, min_length=1)
    target_tool: str | None = Field(default=None, min_length=1)
    occurrence: int = Field(default=1, ge=1)
    error_message: str | None = Field(default=None, min_length=1)
    parameters: dict[str, JsonValue] = Field(default_factory=dict)

    @model_validator(mode="after")
    def check_target(self) -> "FaultSpec":
        if self.fault_type in {
            "tool_error",
            "tool_timeout",
            "malformed_tool_output",
        }:
            if self.target_tool is None:
                if (
                    self.fault_type == "tool_timeout"
                    and self.target_event_id is not None
                ):
                    # Preserve the original trace-targeting model contract.
                    return self
                raise ValueError("tool faults require target_tool")
            if self.fault_type == "tool_error" and self.error_message is None:
                raise ValueError("tool_error requires error_message")
            if self.fault_type == "tool_timeout":
                timeout_ms = self.parameters.get("timeout_ms", 10)
                if type(timeout_ms) is not int or not 0 <= timeout_ms <= 250:
                    raise ValueError("timeout_ms must be an integer from 0 to 250")
            if self.fault_type == "malformed_tool_output":
                payload = self.parameters.get("payload")
                if not isinstance(payload, str) or not payload:
                    raise ValueError("malformed_tool_output requires a string payload")
        elif self.target_event_id is None:
            raise ValueError("non-tool faults require target_event_id")
        return self


class Experiment(DomainModel):
    """Links a baseline run, planned fault, and optional faulted rerun."""

    experiment_id: str = Field(min_length=1)
    baseline_run_id: str = Field(min_length=1)
    fault: FaultSpec
    faulted_run_id: str | None = None
    task: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def check_run_ids(self) -> "Experiment":
        if self.faulted_run_id == self.baseline_run_id:
            raise ValueError("baseline and faulted run IDs must differ")
        return self


class ComparisonResult(DomainModel):
    """Outcome and recovery cost of comparing two completed runs.

    Count deltas and duration delta are faulted minus baseline, so negative
    values represent a faulted run that used fewer calls or less time. A
    missing duration or unknown recovery is represented by None.
    """

    experiment_id: str = Field(min_length=1)
    baseline_run_id: str = Field(min_length=1)
    faulted_run_id: str = Field(min_length=1)
    recovered: bool | None
    model_call_delta: int
    tool_call_delta: int
    duration_delta_ms: int | None
    baseline_model_calls: int = Field(default=0, ge=0)
    faulted_model_calls: int = Field(default=0, ge=0)
    baseline_tool_calls: int = Field(default=0, ge=0)
    faulted_tool_calls: int = Field(default=0, ge=0)
    baseline_injected_failures: int = Field(default=0, ge=0)
    faulted_injected_failures: int = Field(default=0, ge=0)
    fault_triggered: bool = False
    baseline_duration_ms: int | None = Field(default=None, ge=0)
    faulted_duration_ms: int | None = Field(default=None, ge=0)
    baseline_task_success: bool | None = None
    faulted_task_success: bool | None = None
    continued_after_fault: bool = False
    target_tool_retries: int = Field(default=0, ge=0)
    alternative_tool_calls: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def check_run_ids(self) -> "ComparisonResult":
        if self.faulted_run_id == self.baseline_run_id:
            raise ValueError("baseline and faulted run IDs must differ")
        return self
