"""Contract tests for framework-neutral trace records."""

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from agentchaos import (
    AgentRun,
    ComparisonResult,
    EventKind,
    Experiment,
    FaultSpec,
    RunEvent,
    RunStatus,
)

START = datetime(2026, 1, 1, tzinfo=UTC)


def event(sequence: int, *, event_id: str | None = None) -> RunEvent:
    return RunEvent(
        event_id=event_id or f"event-{sequence}",
        sequence=sequence,
        occurred_at=START + timedelta(seconds=sequence),
        kind=EventKind.TOOL_CALL,
        call_id="call-1",
        name="lookup_order",
        attributes={"arguments": {"order_id": "123"}},
    )


def run(**changes: object) -> AgentRun:
    values = {
        "run_id": "baseline-1",
        "task": "Find the order status",
        "adapter": "custom",
        "status": RunStatus.SUCCEEDED,
        "started_at": START,
        "ended_at": START + timedelta(seconds=3),
        "events": [event(0), event(1)],
        "output": {"status": "shipped"},
    }
    values.update(changes)
    return AgentRun.model_validate(values)


def test_trace_round_trips_as_portable_json() -> None:
    original = run()

    restored = AgentRun.model_validate_json(original.model_dump_json())

    assert restored == original
    assert restored.events[0].kind == EventKind.TOOL_CALL
    assert restored.events[0].attributes["arguments"] == {"order_id": "123"}


@pytest.mark.parametrize(
    "changes",
    [
        {"events": [event(1), event(0)]},
        {"events": [event(0), event(0, event_id="other")]},
        {"events": [event(0), event(1, event_id="event-0")]},
        {"ended_at": START - timedelta(seconds=1)},
        {"ended_at": None},
        {"status": RunStatus.RUNNING},
    ],
)
def test_run_rejects_inconsistent_trace(changes: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        run(**changes)


def test_running_run_can_have_no_end_time() -> None:
    active = run(status=RunStatus.RUNNING, ended_at=None)

    assert active.ended_at is None


def test_event_rejects_naive_time_provider_objects_and_unknown_fields() -> None:
    base = event(0).model_dump()

    with pytest.raises(ValidationError):
        RunEvent.model_validate({**base, "occurred_at": datetime(2026, 1, 1)})
    with pytest.raises(ValidationError):
        RunEvent.model_validate({**base, "attributes": {"raw": object()}})
    with pytest.raises(ValidationError):
        RunEvent.model_validate({**base, "provider_response": "opaque"})


def test_fault_and_experiment_describe_a_baseline_target() -> None:
    fault = FaultSpec(
        target_event_id="event-1",
        fault_type="tool_timeout",
        parameters={"delay_ms": 500},
    )
    experiment = Experiment(
        experiment_id="experiment-1",
        baseline_run_id="baseline-1",
        fault=fault,
        faulted_run_id="faulted-1",
    )

    assert Experiment.model_validate_json(experiment.model_dump_json()) == experiment
    with pytest.raises(ValidationError):
        Experiment(
            experiment_id="experiment-1",
            baseline_run_id="baseline-1",
            fault=fault,
            faulted_run_id="baseline-1",
        )


def test_comparison_cost_can_be_positive_or_negative() -> None:
    result = ComparisonResult(
        experiment_id="experiment-1",
        baseline_run_id="baseline-1",
        faulted_run_id="faulted-1",
        recovered=True,
        model_call_delta=2,
        tool_call_delta=-1,
        duration_delta_ms=1500,
    )

    assert ComparisonResult.model_validate_json(result.model_dump_json()) == result
    assert result.tool_call_delta == -1
