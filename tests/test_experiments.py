"""Offline behavior tests for one baseline/faulted experiment."""

import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from agentchaos import AgentRun, EventKind, Experiment, FaultSpec, RunStatus
from agentchaos.compare import compare_runs
from agentchaos.experiments import ExperimentRunner
from agentchaos.tracing import TraceRecorder

TASK = "Where is order 123?"
ANSWER = "Order 123 is shipped, arriving Friday."
START = datetime(2026, 1, 1, tzinfo=UTC)


def clock() -> Iterator[datetime]:
    index = 0
    while True:
        yield START + timedelta(milliseconds=100 * index)
        index += 1


def spec() -> FaultSpec:
    return FaultSpec(
        fault_type="tool_error",
        target_tool="get_order_status",
        occurrence=1,
        error_message="Temporary failure",
    )


def experiment() -> Experiment:
    return Experiment(
        experiment_id="experiment-1",
        baseline_run_id="baseline-1",
        faulted_run_id="faulted-1",
        task=TASK,
        fault=spec(),
    )


def traced_run(
    run_id: str,
    *,
    injected: bool = False,
    retry: bool = False,
    output: str = ANSWER,
    status: RunStatus = RunStatus.SUCCEEDED,
    post_fault_response: bool = True,
) -> AgentRun:
    ticks = clock()
    recorder = TraceRecorder(run_id, clock=lambda: next(ticks))
    recorder.start(task=TASK, adapter="offline")
    recorder.record_model_request(call_id="m1", name="model")
    recorder.record_model_response(call_id="m1")
    recorder.record_tool_call(call_id="t1", name="get_order_status")
    recorder.record_tool_result(
        call_id="t1",
        attributes=(
            {
                "injected": True,
                "success": False,
                "fault_kind": "tool_error",
                "target_tool": "get_order_status",
                "occurrence": 1,
            }
            if injected
            else {"injected": False}
        ),
    )
    if injected:
        recorder.record_model_request(call_id="m2", name="model")
        if post_fault_response:
            recorder.record_model_response(call_id="m2")
    if retry:
        recorder.record_tool_call(call_id="t2", name="get_order_status")
        recorder.record_tool_result(call_id="t2", attributes={"injected": False})
        recorder.record_model_request(call_id="m3", name="model")
        recorder.record_model_response(call_id="m3")
    return recorder.finish(status, output=output)


def test_retry_counts_calls_once_and_confirms_recovery() -> None:
    baseline = traced_run("baseline-1")
    faulted = traced_run("faulted-1", injected=True, retry=True)

    result = compare_runs(experiment(), baseline, faulted, expected_output=ANSWER)

    assert result.baseline_model_calls == 1
    assert result.faulted_model_calls == 3
    assert result.model_call_delta == 2
    assert result.baseline_tool_calls == 1
    assert result.faulted_tool_calls == 2
    assert result.tool_call_delta == 1
    assert result.baseline_injected_failures == 0
    assert result.faulted_injected_failures == 1
    assert result.fault_triggered is True
    assert result.target_tool_retries == 1
    assert result.continued_after_fault is True
    assert result.baseline_task_success is True
    assert result.faulted_task_success is True
    assert result.recovered is True
    assert result.baseline_duration_ms == 500
    assert result.faulted_duration_ms == 1100
    assert result.duration_delta_ms == 600


def test_preplanned_second_tool_call_is_not_counted_as_a_retry() -> None:
    ticks = clock()
    recorder = TraceRecorder("faulted-1", clock=lambda: next(ticks))
    recorder.start(task=TASK, adapter="offline")
    recorder.record_model_request(call_id="m1", name="model")
    recorder.record_model_response(call_id="m1")
    recorder.record_tool_call(call_id="t1", name="get_order_status")
    recorder.record_tool_result(
        call_id="t1",
        attributes={
            "injected": True,
            "success": False,
            "fault_kind": "tool_error",
            "target_tool": "get_order_status",
            "occurrence": 1,
        },
    )
    recorder.record_tool_call(call_id="t2", name="get_order_status")
    recorder.record_tool_result(call_id="t2")
    recorder.record_model_request(call_id="m2", name="model")
    recorder.record_model_response(call_id="m2")
    faulted = recorder.finish(RunStatus.SUCCEEDED, output=ANSWER)

    result = compare_runs(
        experiment(), traced_run("baseline-1"), faulted, expected_output=ANSWER
    )

    assert result.fault_triggered is True
    assert result.target_tool_retries == 0


def test_completed_incorrect_answer_is_not_task_success() -> None:
    result = compare_runs(
        experiment(),
        traced_run("baseline-1"),
        traced_run("faulted-1", injected=True, retry=True, output="I do not know."),
        evaluator=lambda run: run.output == ANSWER,
    )

    assert result.baseline_task_success is True
    assert result.faulted_task_success is False
    assert result.recovered is False


def test_no_evaluator_and_missing_trigger_remain_unknown() -> None:
    baseline = traced_run("baseline-1")
    faulted = traced_run("faulted-1")
    without_evaluator = compare_runs(experiment(), baseline, faulted)
    with_expectation = compare_runs(
        experiment(), baseline, faulted, expected_output=ANSWER
    )

    assert without_evaluator.baseline_task_success is None
    assert without_evaluator.faulted_task_success is None
    assert without_evaluator.recovered is None
    assert with_expectation.fault_triggered is False
    assert with_expectation.faulted_injected_failures == 0
    assert with_expectation.recovered is None
    fired_without_evaluator = compare_runs(
        experiment(), baseline, traced_run("faulted-1", injected=True, retry=True)
    )
    assert fired_without_evaluator.fault_triggered is True
    assert fired_without_evaluator.baseline_task_success is None
    assert fired_without_evaluator.faulted_task_success is None
    assert fired_without_evaluator.recovered is None


@pytest.mark.parametrize("status", [RunStatus.FAILED, RunStatus.CANCELLED])
def test_failed_or_cancelled_run_cannot_pass_task_evaluation(status: RunStatus) -> None:
    result = compare_runs(
        experiment(),
        traced_run("baseline-1"),
        traced_run("faulted-1", injected=True, status=status),
        expected_output=ANSWER,
    )

    assert result.fault_triggered is True
    assert result.faulted_task_success is False
    assert result.recovered is False


def test_incomplete_events_do_not_claim_fault_or_recovery() -> None:
    baseline = traced_run("baseline-1")
    faulted = traced_run("faulted-1", injected=True, post_fault_response=False)
    incomplete = faulted.model_copy(
        update={
            "events": [
                event
                for event in faulted.events
                if not (event.kind == EventKind.TOOL_CALL and event.call_id == "t1")
            ]
        }
    )
    no_pair = compare_runs(experiment(), baseline, incomplete, expected_output=ANSWER)
    no_continuation = compare_runs(
        experiment(), baseline, faulted, expected_output=ANSWER
    )

    assert no_pair.fault_triggered is False
    assert no_pair.faulted_injected_failures == 0
    assert no_pair.faulted_tool_calls == 0
    assert no_pair.recovered is None
    assert no_continuation.fault_triggered is True
    assert no_continuation.continued_after_fault is False
    assert no_continuation.recovered is None


def test_natural_tool_error_is_not_an_injected_failure() -> None:
    faulted = traced_run("faulted-1")
    events = [
        event.model_copy(
            update={"kind": EventKind.TOOL_ERROR, "attributes": {"injected": False}}
        )
        if event.kind == EventKind.TOOL_RESULT
        else event
        for event in faulted.events
    ]
    faulted = faulted.model_copy(update={"events": events})
    result = compare_runs(
        experiment(), traced_run("baseline-1"), faulted, expected_output=ANSWER
    )

    assert result.faulted_tool_calls == 1
    assert result.faulted_injected_failures == 0
    assert result.fault_triggered is False
    assert result.recovered is None


def test_unexpected_baseline_injection_prevents_recovery_claim() -> None:
    baseline = traced_run("baseline-1", injected=True)
    faulted = traced_run("faulted-1", injected=True, retry=True)

    result = compare_runs(experiment(), baseline, faulted, expected_output=ANSWER)

    assert result.baseline_injected_failures == 1
    assert result.fault_triggered is True
    assert result.recovered is None


def test_missing_end_time_and_open_model_call_keep_duration_and_recovery_unknown() -> (
    None
):
    baseline = traced_run("baseline-1")
    faulted = traced_run("faulted-1", injected=True, post_fault_response=False)
    incomplete = AgentRun(
        run_id=faulted.run_id,
        task=faulted.task,
        adapter=faulted.adapter,
        status=RunStatus.RUNNING,
        started_at=faulted.started_at,
        events=faulted.events[:-1],
        output=faulted.output,
    )

    result = compare_runs(experiment(), baseline, incomplete, expected_output=ANSWER)

    assert result.fault_triggered is True
    assert result.faulted_model_calls == 2
    assert result.faulted_duration_ms is None
    assert result.duration_delta_ms is None
    assert result.faulted_task_success is None
    assert result.recovered is None


def test_missing_baseline_lifecycle_does_not_confirm_recovery() -> None:
    baseline = traced_run("baseline-1")
    incomplete = baseline.model_copy(update={"events": baseline.events[1:]})
    faulted = traced_run("faulted-1", injected=True, retry=True)

    result = compare_runs(experiment(), incomplete, faulted, expected_output=ANSWER)

    assert result.baseline_task_success is True
    assert result.fault_triggered is True
    assert result.recovered is None


class OfflineAdapter:
    """Return a deterministic trace while exposing the runner's per-run inputs."""

    def __init__(self) -> None:
        self.recorders: list[TraceRecorder] = []
        self.faults: list[FaultSpec | None] = []

    async def run(
        self, task: str, *, recorder: TraceRecorder, fault: FaultSpec | None = None
    ) -> AgentRun:
        self.recorders.append(recorder)
        self.faults.append(fault)
        recorder.start(task=task, adapter="offline")
        recorder.record_model_request(call_id="m1", name="model")
        await asyncio.sleep(0)
        recorder.record_model_response(call_id="m1")
        recorder.record_tool_call(call_id="t1", name="get_order_status")
        if fault is not None:
            recorder.record_tool_result(
                call_id="t1",
                attributes={
                    "injected": True,
                    "success": False,
                    "fault_kind": fault.fault_type,
                    "target_tool": fault.target_tool,
                    "occurrence": fault.occurrence,
                },
            )
            recorder.record_model_request(call_id="m2", name="model")
            recorder.record_model_response(call_id="m2")
        else:
            recorder.record_tool_result(call_id="t1")
        return recorder.finish(RunStatus.SUCCEEDED, output=ANSWER)


@pytest.mark.asyncio
async def test_runner_isolates_baseline_faulted_recorders_and_repeated_runs() -> None:
    adapter = OfflineAdapter()
    plan = experiment()
    runner = ExperimentRunner()

    first = await runner.run(adapter=adapter, experiment=plan, expected_output=ANSWER)
    second = await runner.run(adapter=adapter, experiment=plan, expected_output=ANSWER)

    assert adapter.faults[0] is None
    assert adapter.faults[1] is not plan.fault
    assert adapter.faults[2] is None
    assert adapter.faults[3] is not plan.fault
    assert len({id(recorder) for recorder in adapter.recorders}) == 4
    assert [event.sequence for event in first.baseline.events] == list(
        range(len(first.baseline.events))
    )
    assert first.baseline.run_id == "baseline-1"
    assert first.faulted.run_id == "faulted-1"
    assert first.comparison.baseline_injected_failures == 0
    assert first.comparison.faulted_injected_failures == 1
    assert first.comparison.recovered is True
    assert second.comparison.fault_triggered is True
    assert plan.task == TASK


@pytest.mark.asyncio
async def test_runner_keeps_baseline_failure_visible() -> None:
    class FailedBaseline(OfflineAdapter):
        async def run(
            self, task: str, *, recorder: TraceRecorder, fault: FaultSpec | None = None
        ) -> AgentRun:
            if fault is None:
                self.recorders.append(recorder)
                self.faults.append(None)
                recorder.start(task=task, adapter="offline")
                recorder.record_model_request(call_id="m1", name="model")
                recorder.record_model_error(call_id="m1")
                return recorder.finish(RunStatus.FAILED)
            return await super().run(task, recorder=recorder, fault=fault)

    adapter = FailedBaseline()
    outcome = await ExperimentRunner().run(
        adapter=adapter, experiment=experiment(), expected_output=ANSWER
    )

    assert len(adapter.recorders) == 2
    assert outcome.baseline.status == RunStatus.FAILED
    assert outcome.comparison.baseline_task_success is False
    assert outcome.comparison.fault_triggered is True
    assert outcome.comparison.recovered is None


@pytest.mark.asyncio
async def test_runner_propagates_original_baseline_exception() -> None:
    original = RuntimeError("baseline stopped")

    class RaisingBaseline(OfflineAdapter):
        async def run(
            self, task: str, *, recorder: TraceRecorder, fault: FaultSpec | None = None
        ) -> AgentRun:
            assert fault is None
            self.recorders.append(recorder)
            raise original

    adapter = RaisingBaseline()
    with pytest.raises(RuntimeError) as caught:
        await ExperimentRunner().run(
            adapter=adapter, experiment=experiment(), expected_output=ANSWER
        )

    assert caught.value is original
    assert len(adapter.recorders) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error_type,status",
    [(RuntimeError, RunStatus.FAILED), (asyncio.CancelledError, RunStatus.CANCELLED)],
)
async def test_runner_preserves_original_faulted_exception_and_partial_report(
    error_type: type[BaseException], status: RunStatus
) -> None:
    original = error_type("agent stopped")

    class RaisingAdapter(OfflineAdapter):
        async def run(
            self, task: str, *, recorder: TraceRecorder, fault: FaultSpec | None = None
        ) -> AgentRun:
            if fault is None:
                return await super().run(task, recorder=recorder)
            recorder.start(task=task, adapter="offline")
            recorder.record_tool_call(call_id="t1", name="get_order_status")
            recorder.record_tool_result(
                call_id="t1",
                attributes={
                    "injected": True,
                    "success": False,
                    "fault_kind": "tool_error",
                    "target_tool": "get_order_status",
                    "occurrence": 1,
                },
            )
            original.agentchaos_run = recorder.finish(status)
            raise original

    with pytest.raises(error_type) as caught:
        await ExperimentRunner().run(
            adapter=RaisingAdapter(), experiment=experiment(), expected_output=ANSWER
        )

    assert caught.value is original
    partial: Any = original.agentchaos_experiment
    assert partial.faulted.status == status
    assert partial.comparison.fault_triggered is True
    assert partial.comparison.faulted_task_success is False
    assert partial.comparison.recovered is False
