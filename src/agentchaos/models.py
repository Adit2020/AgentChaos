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
    """A planned fault at an event observed in the baseline run.

    ``fault_type`` and ``parameters`` remain generic until injectors are defined.
    """

    target_event_id: str = Field(min_length=1)
    fault_type: str = Field(min_length=1)
    parameters: dict[str, JsonValue] = Field(default_factory=dict)


class Experiment(DomainModel):
    """Links a baseline run, planned fault, and optional faulted rerun."""

    experiment_id: str = Field(min_length=1)
    baseline_run_id: str = Field(min_length=1)
    fault: FaultSpec
    faulted_run_id: str | None = None

    @model_validator(mode="after")
    def check_run_ids(self) -> "Experiment":
        if self.faulted_run_id == self.baseline_run_id:
            raise ValueError("baseline and faulted run IDs must differ")
        return self


class ComparisonResult(DomainModel):
    """Outcome and recovery cost of comparing two completed runs.

    Count deltas and duration delta are faulted minus baseline, so negative
    values represent a faulted run that used fewer calls or less time.
    """

    experiment_id: str = Field(min_length=1)
    baseline_run_id: str = Field(min_length=1)
    faulted_run_id: str = Field(min_length=1)
    recovered: bool
    model_call_delta: int
    tool_call_delta: int
    duration_delta_ms: int

    @model_validator(mode="after")
    def check_run_ids(self) -> "ComparisonResult":
        if self.faulted_run_id == self.baseline_run_id:
            raise ValueError("baseline and faulted run IDs must differ")
        return self
