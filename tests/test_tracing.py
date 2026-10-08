"""Behavioral tests for recording and the asynchronous adapter contract."""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from agentchaos import AgentRun, EventKind, RunStatus
from agentchaos.adapters import AgentAdapter
from agentchaos.tracing import TraceRecorder
from tests.fake_adapter import FakeAdapter

START = datetime(2026, 1, 1, tzinfo=UTC)


def ticking_clock(start: datetime = START) -> Iterator[datetime]:
    index = 0
    while True:
        yield start + timedelta(seconds=index)
        index += 1


def recorder(run_id: str = "run-1") -> TraceRecorder:
    ticks = ticking_clock()
    return TraceRecorder(run_id, clock=lambda: next(ticks))


def test_recording_preserves_order_sequences_and_call_ids() -> None:
    trace = recorder()
    trace.start(task="Find the order", adapter="fake")
    trace.record_model_request(call_id="m1", name="model")
    trace.record_tool_call(call_id="t1", name="lookup")
    trace.record_model_response(call_id="m1")
    trace.record_tool_result(call_id="t1")
    run = trace.finish(RunStatus.SUCCEEDED, output="done")

    assert [event.sequence for event in run.events] == list(range(6))
    assert [event.kind for event in run.events] == [
        EventKind.RUN_STARTED,
        EventKind.MODEL_REQUEST,
        EventKind.TOOL_CALL,
        EventKind.MODEL_RESPONSE,
        EventKind.TOOL_RESULT,
        EventKind.RUN_COMPLETED,
    ]
    assert [event.call_id for event in run.events] == [
        None,
        "m1",
        "t1",
        "m1",
        "t1",
        None,
    ]
    assert [event.occurred_at for event in run.events] == [
        START + timedelta(seconds=index) for index in range(6)
    ]
    assert run.started_at == START
    assert run.ended_at == START + timedelta(seconds=5)


def test_recorders_keep_separate_sequences_and_events() -> None:
    first = recorder("first")
    second = recorder("second")
    first.start(task="One", adapter="fake")
    first.record_model_request(call_id="m1", name="model")
    second.start(task="Two", adapter="fake")
    second.record_tool_call(call_id="t1", name="lookup")

    assert [event.sequence for event in second.events] == [0, 1]
    assert [event.kind for event in second.events] == [
        EventKind.RUN_STARTED,
        EventKind.TOOL_CALL,
    ]
    assert first.events[1].event_id == "first:1"
    assert second.events[1].event_id == "second:1"


def test_snapshots_and_returned_events_cannot_change_recorder_state() -> None:
    trace = recorder()
    trace.start(task="Find the order", adapter="fake")
    attributes = {"nested": {"value": "original"}}
    returned = trace.record_tool_call(
        call_id="t1", name="lookup", attributes=attributes
    )
    snapshot = trace.events
    attributes["nested"]["value"] = "changed"
    returned.attributes["nested"]["value"] = "changed"
    snapshot[1].attributes["nested"]["value"] = "changed"

    assert isinstance(snapshot, tuple)
    assert trace.events[1].attributes == {"nested": {"value": "original"}}
    completed = trace.finish(RunStatus.SUCCEEDED)
    completed.events[1].attributes["nested"]["value"] = "changed"
    assert trace.events[1].attributes == {"nested": {"value": "original"}}


def test_recorder_enforces_lifecycle_and_records_failure_or_cancellation() -> None:
    trace = recorder()
    with pytest.raises(RuntimeError, match="not started"):
        trace.record_model_response(call_id="m1")
    trace.start(task="Task", adapter="fake")
    with pytest.raises(RuntimeError, match="already started"):
        trace.start(task="Task", adapter="fake")
    trace.record_model_error(call_id="m1", attributes={"message": "timeout"})
    failed = trace.finish(RunStatus.FAILED)
    assert failed.events[-1].kind == EventKind.RUN_FAILED
    with pytest.raises(RuntimeError, match="already finished"):
        trace.record_tool_error(call_id="t1")

    cancelled = recorder("cancelled")
    cancelled.start(task="Task", adapter="fake")
    assert cancelled.finish(RunStatus.CANCELLED).events[-1].kind == (
        EventKind.RUN_CANCELLED
    )


def test_timestamps_are_normalized_to_utc() -> None:
    eastern = timezone(timedelta(hours=-5))
    ticks = ticking_clock(datetime(2026, 1, 1, tzinfo=eastern))
    trace = TraceRecorder("run-1", clock=lambda: next(ticks))
    trace.start(task="Task", adapter="fake")
    run = trace.finish(RunStatus.SUCCEEDED)

    assert run.started_at == datetime(2026, 1, 1, 5, tzinfo=UTC)
    assert all(event.occurred_at.tzinfo == UTC for event in run.events)


def test_invalid_output_does_not_finish_recorder() -> None:
    trace = recorder()
    trace.start(task="Task", adapter="fake")
    with pytest.raises(ValidationError):
        trace.finish(RunStatus.SUCCEEDED, output=object())

    assert len(trace.events) == 1
    assert trace.finish(RunStatus.SUCCEEDED, output="valid").status == (
        RunStatus.SUCCEEDED
    )


@pytest.mark.asyncio
async def test_fake_adapter_produces_valid_normalized_run() -> None:
    adapter: AgentAdapter = FakeAdapter()
    run = await adapter.run("Find order 123", recorder=recorder())

    assert isinstance(run, AgentRun)
    assert run.task == "Find order 123"
    assert run.adapter == "fake"
    assert run.status == RunStatus.SUCCEEDED
    assert [event.kind for event in run.events] == [
        EventKind.RUN_STARTED,
        EventKind.MODEL_REQUEST,
        EventKind.MODEL_RESPONSE,
        EventKind.TOOL_CALL,
        EventKind.TOOL_RESULT,
        EventKind.RUN_COMPLETED,
    ]
    assert AgentRun.model_validate_json(run.model_dump_json()) == run
