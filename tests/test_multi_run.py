"""Offline tests for bounded sequential repetitions and aggregate statistics."""

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from agentchaos import AgentRun, ComparisonResult, Experiment, FaultSpec, RunStatus
from agentchaos.experiments import ExperimentOutcome
from agentchaos.multi_run import MultiRunRunner, summarize_repetitions
from agentchaos.tracing import TraceRecorder

TASK = "Where is order 123?"
START = datetime(2026, 1, 1, tzinfo=UTC)


def plan() -> Experiment:
    return Experiment(
        experiment_id="batch",
        baseline_run_id="template-baseline",
        faulted_run_id="template-faulted",
        task=TASK,
        fault=FaultSpec(
            fault_type="tool_error",
            target_tool="get_order_status",
            error_message="Temporary failure",
        ),
    )


def completed_run(run_id: str, status: RunStatus = RunStatus.SUCCEEDED) -> AgentRun:
    return AgentRun(
        run_id=run_id,
        task=TASK,
        adapter="fake",
        status=status,
        started_at=START,
        ended_at=START + timedelta(seconds=1),
    )


def pair(
    index: int,
    *,
    recovered: bool | None,
    triggered: bool = True,
    baseline_success: bool | None = True,
    faulted_success: bool | None = True,
    model_delta: int = 0,
    tool_delta: int = 0,
    latency_delta: int | None = 0,
    baseline_status: RunStatus = RunStatus.SUCCEEDED,
    faulted_status: RunStatus = RunStatus.SUCCEEDED,
) -> ExperimentOutcome:
    experiment = plan().model_copy(
        update={
            "experiment_id": f"experiment-{index}",
            "baseline_run_id": f"baseline-{index}",
            "faulted_run_id": f"faulted-{index}",
        }
    )
    baseline = completed_run(experiment.baseline_run_id, baseline_status)
    faulted = completed_run(experiment.faulted_run_id, faulted_status)
    comparison = ComparisonResult(
        experiment_id=experiment.experiment_id,
        baseline_run_id=baseline.run_id,
        faulted_run_id=faulted.run_id,
        recovered=recovered,
        fault_triggered=triggered,
        baseline_task_success=baseline_success,
        faulted_task_success=faulted_success,
        model_call_delta=model_delta,
        tool_call_delta=tool_delta,
        duration_delta_ms=latency_delta,
    )
    return ExperimentOutcome(experiment, baseline, faulted, comparison)


def test_mixed_outcomes_use_explicit_rate_denominators_and_signed_deltas() -> None:
    outcomes = [
        pair(1, recovered=True, model_delta=1, tool_delta=1, latency_delta=211),
        pair(
            2,
            recovered=False,
            faulted_success=False,
            model_delta=2,
            latency_delta=-100,
        ),
        pair(
            3,
            recovered=None,
            baseline_success=None,
            faulted_success=None,
            model_delta=-1,
            tool_delta=-1,
            latency_delta=None,
        ),
        pair(
            4,
            recovered=None,
            triggered=False,
            baseline_success=False,
            model_delta=0,
            baseline_status=RunStatus.FAILED,
            latency_delta=50,
        ),
    ]

    report = summarize_repetitions(
        outcomes, requested_repetitions=5, incomplete_pairs=1, stop_reason="error"
    )

    assert report.requested_repetitions == 5
    assert report.completed_repetitions == 4
    assert report.incomplete_pairs == 1
    assert report.failed_pairs == 1
    assert report.not_started_repetitions == 0
    assert report.confirmed_recoveries == 1
    assert report.confirmed_nonrecoveries == 1
    assert report.unknown_recoveries == 2
    assert report.triggered_faults == 3
    assert report.fault_trigger_rate == 3 / 4
    assert report.evaluable_triggered_repetitions == 2
    assert report.confirmed_recovery_rate == 1 / 2
    assert report.recovery_rate_requested == 1 / 5
    assert report.baseline_task_successes == 2
    assert report.baseline_evaluated_repetitions == 3
    assert report.baseline_task_success_rate == 2 / 3
    assert report.faulted_task_successes == 2
    assert report.faulted_evaluated_repetitions == 3
    assert report.faulted_task_success_rate == 2 / 3
    assert report.model_call_observations == 4
    assert report.average_extra_model_calls == 0.5
    assert report.median_extra_model_calls == 0.5
    assert report.tool_call_observations == 4
    assert report.average_extra_tool_calls == 0
    assert report.median_extra_tool_calls == 0
    assert report.latency_observations == 3
    assert report.average_latency_delta_ms == pytest.approx(161 / 3)
    assert report.median_latency_delta_ms == 50


def test_zero_observations_and_negative_latency_are_not_fabricated() -> None:
    empty = summarize_repetitions([], requested_repetitions=3, stop_reason="requested")
    negative = summarize_repetitions(
        [
            pair(1, recovered=None, latency_delta=-300),
            pair(2, recovered=None, latency_delta=-100),
        ],
        requested_repetitions=2,
    )

    assert empty.completed_repetitions == 0
    assert empty.not_started_repetitions == 3
    assert empty.fault_trigger_rate is None
    assert empty.confirmed_recovery_rate is None
    assert empty.recovery_rate_requested == 0
    assert empty.baseline_task_success_rate is None
    assert empty.average_extra_model_calls is None
    assert empty.median_extra_tool_calls is None
    assert empty.average_latency_delta_ms is None
    assert empty.latency_observations == 0
    assert negative.average_latency_delta_ms == -200
    assert negative.median_latency_delta_ms == -200


class TrackingAdapter:
    """Small deterministic adapter exposing call order and recorder isolation."""

    def __init__(self, stop_event: asyncio.Event | None = None) -> None:
        self.stop_event = stop_event
        self.calls: list[tuple[TraceRecorder, FaultSpec | None]] = []
        self.active = 0
        self.max_active = 0

    async def run(
        self, task: str, *, recorder: TraceRecorder, fault: FaultSpec | None = None
    ) -> AgentRun:
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        self.calls.append((recorder, fault))
        try:
            recorder.start(task=task, adapter="fake")
            recorder.record_model_request(call_id="m1", name="model")
            await asyncio.sleep(0)
            recorder.record_model_response(call_id="m1")
            recorder.record_tool_call(call_id="t1", name="get_order_status")
            if fault is None:
                recorder.record_tool_result(call_id="t1")
            else:
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
                if self.stop_event is not None:
                    self.stop_event.set()
            return recorder.finish(RunStatus.SUCCEEDED, output="shipped")
        finally:
            self.active -= 1


@pytest.mark.asyncio
async def test_default_three_repetitions_are_sequential_and_isolated() -> None:
    adapter = TrackingAdapter()
    original = plan()
    report = await MultiRunRunner(max_repetitions=3).run(
        adapter=adapter, experiment=original, expected_output="shipped"
    )

    assert report.requested_repetitions == report.completed_repetitions == 3
    assert report.planned_agent_executions == 6
    assert len(adapter.calls) == 6
    assert adapter.max_active == 1
    assert [fault is None for _, fault in adapter.calls] == [
        True,
        False,
        True,
        False,
        True,
        False,
    ]
    assert len({id(recorder) for recorder, _ in adapter.calls}) == 6
    assert len({outcome.experiment.experiment_id for outcome in report.outcomes}) == 3
    assert (
        len(
            {
                run.run_id
                for outcome in report.outcomes
                for run in (outcome.baseline, outcome.faulted)
            }
        )
        == 6
    )
    assert all(outcome.comparison.fault_triggered for outcome in report.outcomes)
    assert report.confirmed_recoveries == 3
    assert all(fault is not original.fault for _, fault in adapter.calls if fault)
    assert original.baseline_run_id == "template-baseline"


@pytest.mark.asyncio
async def test_stop_event_stops_between_pairs_without_incomplete_pair() -> None:
    stop = asyncio.Event()
    adapter = TrackingAdapter(stop)
    report = await MultiRunRunner(max_repetitions=5).run(
        adapter=adapter,
        experiment=plan(),
        repetitions=5,
        expected_output="shipped",
        stop_event=stop,
    )

    assert len(adapter.calls) == 2
    assert report.completed_repetitions == 1
    assert report.incomplete_pairs == 0
    assert report.not_started_repetitions == 4
    assert report.stop_reason == "requested"


@pytest.mark.asyncio
async def test_invalid_counts_fail_before_any_agent_execution() -> None:
    adapter = TrackingAdapter()
    with pytest.raises(ValueError, match="max_repetitions"):
        MultiRunRunner(max_repetitions=0)
    with pytest.raises(ValueError, match="max_repetitions"):
        MultiRunRunner(max_repetitions=21)
    runner = MultiRunRunner(max_repetitions=3)
    for repetitions in (0, 4, True):
        with pytest.raises(ValueError, match="repetitions"):
            await runner.run(
                adapter=adapter, experiment=plan(), repetitions=repetitions
            )
    assert adapter.calls == []


@pytest.mark.asyncio
async def test_unexpected_baseline_error_stops_and_preserves_original() -> None:
    original = RuntimeError("provider unavailable")

    class FailingAdapter(TrackingAdapter):
        async def run(
            self, task: str, *, recorder: TraceRecorder, fault: FaultSpec | None = None
        ) -> AgentRun:
            if len(self.calls) == 2:
                self.calls.append((recorder, fault))
                raise original
            return await super().run(task, recorder=recorder, fault=fault)

    adapter = FailingAdapter()
    with pytest.raises(RuntimeError) as caught:
        await MultiRunRunner(max_repetitions=3).run(
            adapter=adapter, experiment=plan(), expected_output="shipped"
        )

    assert caught.value is original
    assert len(adapter.calls) == 3
    report = original.agentchaos_multi_run_report
    assert report.completed_repetitions == 1
    assert report.incomplete_pairs == 1
    assert report.not_started_repetitions == 1
    assert report.confirmed_recoveries == 1
    assert report.stop_reason == "error"


@pytest.mark.asyncio
async def test_faulted_exception_keeps_available_pair_evidence() -> None:
    original = RuntimeError("faulted run failed")

    class FailingFaulted(TrackingAdapter):
        async def run(
            self, task: str, *, recorder: TraceRecorder, fault: FaultSpec | None = None
        ) -> AgentRun:
            if fault is None:
                return await super().run(task, recorder=recorder)
            self.calls.append((recorder, fault))
            recorder.start(task=task, adapter="fake")
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
            original.agentchaos_run = recorder.finish(RunStatus.FAILED)
            raise original

    with pytest.raises(RuntimeError) as caught:
        await MultiRunRunner(max_repetitions=3).run(
            adapter=FailingFaulted(), experiment=plan(), expected_output="shipped"
        )

    assert caught.value is original
    report = original.agentchaos_multi_run_report
    assert report.completed_repetitions == 1
    assert report.incomplete_pairs == 0
    assert report.failed_pairs == 1
    assert report.triggered_faults == 1
    assert report.confirmed_nonrecoveries == 0
    assert report.unknown_recoveries == 1
    assert report.not_started_repetitions == 2


@pytest.mark.asyncio
async def test_cancellation_preserves_partial_report_and_stops() -> None:
    entered = asyncio.Event()

    class WaitingAdapter(TrackingAdapter):
        async def run(
            self, task: str, *, recorder: TraceRecorder, fault: FaultSpec | None = None
        ) -> AgentRun:
            self.calls.append((recorder, fault))
            entered.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    adapter = WaitingAdapter()
    task = asyncio.create_task(
        MultiRunRunner(max_repetitions=3).run(
            adapter=adapter, experiment=plan(), expected_output="shipped"
        )
    )
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError) as caught:
        await task

    report: Any = caught.value.agentchaos_multi_run_report
    assert len(adapter.calls) == 1
    assert report.completed_repetitions == 0
    assert report.incomplete_pairs == 1
    assert report.not_started_repetitions == 2
    assert report.stop_reason == "cancelled"
