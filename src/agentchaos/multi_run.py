"""Sequential repetitions of an existing single-run AgentChaos experiment."""

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from statistics import mean, median
from uuid import uuid4

from pydantic import JsonValue

from agentchaos.adapters.base import FaultCapableAdapter
from agentchaos.compare import TaskEvaluator
from agentchaos.compare.analyzer import _UNSET
from agentchaos.experiments import ExperimentOutcome, ExperimentRunner
from agentchaos.models import Experiment, RunStatus

_HARD_REPETITION_CAP = 20


def _rate(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def _average(values: Sequence[int]) -> float | None:
    return float(mean(values)) if values else None


def _median(values: Sequence[int]) -> float | None:
    return float(median(values)) if values else None


@dataclass(frozen=True, slots=True)
class ReliabilityReport:
    """Aggregate pair outcomes without merging unknown and unstarted work."""

    requested_repetitions: int
    completed_repetitions: int
    incomplete_pairs: int
    not_started_repetitions: int
    failed_pairs: int
    stop_reason: str | None
    outcomes: tuple[ExperimentOutcome, ...]
    confirmed_recoveries: int
    confirmed_nonrecoveries: int
    unknown_recoveries: int
    triggered_faults: int
    fault_trigger_rate: float | None
    evaluable_triggered_repetitions: int
    confirmed_recovery_rate: float | None
    recovery_rate_requested: float
    baseline_task_successes: int
    baseline_evaluated_repetitions: int
    baseline_task_success_rate: float | None
    faulted_task_successes: int
    faulted_evaluated_repetitions: int
    faulted_task_success_rate: float | None
    model_call_observations: int
    average_extra_model_calls: float | None
    median_extra_model_calls: float | None
    tool_call_observations: int
    average_extra_tool_calls: float | None
    median_extra_tool_calls: float | None
    latency_observations: int
    average_latency_delta_ms: float | None
    median_latency_delta_ms: float | None

    @property
    def planned_agent_executions(self) -> int:
        return 2 * self.requested_repetitions


def summarize_repetitions(
    outcomes: Sequence[ExperimentOutcome],
    *,
    requested_repetitions: int,
    incomplete_pairs: int = 0,
    stop_reason: str | None = None,
) -> ReliabilityReport:
    """Summarize observed pairs without imputing missing runs or evaluations."""

    if requested_repetitions < 1:
        raise ValueError("requested_repetitions must be positive")
    if incomplete_pairs < 0 or len(outcomes) + incomplete_pairs > requested_repetitions:
        raise ValueError("incomplete pair count exceeds requested repetitions")

    completed = len(outcomes)
    comparisons = [outcome.comparison for outcome in outcomes]
    triggered = sum(result.fault_triggered for result in comparisons)
    evaluable_pairs = [
        outcome
        for outcome in outcomes
        if outcome.baseline.status == RunStatus.SUCCEEDED
        and outcome.faulted.status == RunStatus.SUCCEEDED
        and outcome.comparison.fault_triggered
    ]
    recoveries = sum(
        outcome.comparison.recovered is True for outcome in evaluable_pairs
    )
    nonrecoveries = sum(
        outcome.comparison.recovered is False for outcome in evaluable_pairs
    )
    evaluable = recoveries + nonrecoveries
    baseline_evaluated = sum(
        result.baseline_task_success is not None for result in comparisons
    )
    baseline_successes = sum(
        result.baseline_task_success is True for result in comparisons
    )
    faulted_evaluated = sum(
        result.faulted_task_success is not None for result in comparisons
    )
    faulted_successes = sum(
        result.faulted_task_success is True for result in comparisons
    )
    model_deltas = [result.model_call_delta for result in comparisons]
    tool_deltas = [result.tool_call_delta for result in comparisons]
    latency_deltas = [
        result.duration_delta_ms
        for result in comparisons
        if result.duration_delta_ms is not None
    ]
    failed_pairs = sum(
        outcome.baseline.status != RunStatus.SUCCEEDED
        or outcome.faulted.status != RunStatus.SUCCEEDED
        for outcome in outcomes
    )
    return ReliabilityReport(
        requested_repetitions=requested_repetitions,
        completed_repetitions=completed,
        incomplete_pairs=incomplete_pairs,
        not_started_repetitions=requested_repetitions - completed - incomplete_pairs,
        failed_pairs=failed_pairs,
        stop_reason=stop_reason,
        outcomes=tuple(outcomes),
        confirmed_recoveries=recoveries,
        confirmed_nonrecoveries=nonrecoveries,
        unknown_recoveries=completed - evaluable,
        triggered_faults=triggered,
        fault_trigger_rate=_rate(triggered, completed),
        evaluable_triggered_repetitions=evaluable,
        confirmed_recovery_rate=_rate(recoveries, evaluable),
        recovery_rate_requested=recoveries / requested_repetitions,
        baseline_task_successes=baseline_successes,
        baseline_evaluated_repetitions=baseline_evaluated,
        baseline_task_success_rate=_rate(baseline_successes, baseline_evaluated),
        faulted_task_successes=faulted_successes,
        faulted_evaluated_repetitions=faulted_evaluated,
        faulted_task_success_rate=_rate(faulted_successes, faulted_evaluated),
        model_call_observations=completed,
        average_extra_model_calls=_average(model_deltas),
        median_extra_model_calls=_median(model_deltas),
        tool_call_observations=completed,
        average_extra_tool_calls=_average(tool_deltas),
        median_extra_tool_calls=_median(tool_deltas),
        latency_observations=len(latency_deltas),
        average_latency_delta_ms=_average(latency_deltas),
        median_latency_delta_ms=_median(latency_deltas),
    )


class MultiRunRunner:
    """Run bounded, sequential baseline/faulted pairs through ExperimentRunner.

    Unexpected errors stop execution. The original exception is re-raised with
    its partial report attached as agentchaos_multi_run_report. The stop event
    is checked only between repetitions.
    """

    def __init__(self, *, max_repetitions: int) -> None:
        if (
            type(max_repetitions) is not int
            or not 1 <= max_repetitions <= _HARD_REPETITION_CAP
        ):
            raise ValueError(
                f"max_repetitions must be an integer from 1 to {_HARD_REPETITION_CAP}"
            )
        self.max_repetitions = max_repetitions
        self._single = ExperimentRunner()

    def planned_agent_executions(self, repetitions: int = 3) -> int:
        """Validate the count and return two planned executions per pair."""

        if type(repetitions) is not int or not 1 <= repetitions <= self.max_repetitions:
            raise ValueError(
                f"repetitions must be an integer from 1 to {self.max_repetitions}"
            )
        return 2 * repetitions

    async def run(
        self,
        *,
        adapter: FaultCapableAdapter,
        experiment: Experiment,
        repetitions: int = 3,
        task: str | None = None,
        expected_output: JsonValue | object = _UNSET,
        evaluator: TaskEvaluator | None = None,
        stop_event: asyncio.Event | None = None,
    ) -> ReliabilityReport:
        self.planned_agent_executions(repetitions)
        outcomes: list[ExperimentOutcome] = []
        incomplete_pairs = 0
        for _ in range(repetitions):
            if stop_event is not None and stop_event.is_set():
                return summarize_repetitions(
                    outcomes,
                    requested_repetitions=repetitions,
                    stop_reason="requested",
                )
            prepared = experiment.model_copy(
                deep=True,
                update={
                    "experiment_id": f"{experiment.experiment_id}:{uuid4().hex}",
                    "baseline_run_id": uuid4().hex,
                    "faulted_run_id": uuid4().hex,
                },
            )
            try:
                outcome = await self._single.run(
                    adapter=adapter,
                    experiment=prepared,
                    task=task,
                    expected_output=expected_output,
                    evaluator=evaluator,
                )
            except (Exception, asyncio.CancelledError) as exc:
                partial = getattr(exc, "agentchaos_experiment", None)
                if isinstance(partial, ExperimentOutcome):
                    outcomes.append(partial)
                else:
                    incomplete_pairs += 1
                try:
                    exc.agentchaos_multi_run_report = summarize_repetitions(
                        outcomes,
                        requested_repetitions=repetitions,
                        incomplete_pairs=incomplete_pairs,
                        stop_reason=(
                            "cancelled"
                            if isinstance(exc, asyncio.CancelledError)
                            else "error"
                        ),
                    )
                except Exception as reporting_error:
                    exc.add_note(
                        "AgentChaos could not summarize the interrupted batch: "
                        f"{type(reporting_error).__name__}"
                    )
                raise
            outcomes.append(outcome)
        return summarize_repetitions(outcomes, requested_repetitions=repetitions)
