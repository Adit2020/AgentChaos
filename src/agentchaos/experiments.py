"""Run one baseline and one faulted execution of the same task."""

import asyncio
from dataclasses import dataclass
from uuid import uuid4

from pydantic import JsonValue

from agentchaos.adapters.base import FaultCapableAdapter
from agentchaos.compare import TaskEvaluator, compare_runs
from agentchaos.compare.analyzer import _UNSET
from agentchaos.models import AgentRun, ComparisonResult, Experiment
from agentchaos.tracing import TraceRecorder


@dataclass(frozen=True, slots=True)
class ExperimentOutcome:
    """Both executions and their normalized comparison."""

    experiment: Experiment
    baseline: AgentRun
    faulted: AgentRun
    comparison: ComparisonResult


class ExperimentRunner:
    """Run baseline then faulted with separate recorders and adapter fault state."""

    async def run(
        self,
        *,
        adapter: FaultCapableAdapter,
        experiment: Experiment,
        task: str | None = None,
        expected_output: JsonValue | object = _UNSET,
        evaluator: TaskEvaluator | None = None,
    ) -> ExperimentOutcome:
        if not callable(getattr(adapter, "run", None)):
            raise TypeError(
                "adapter must provide an async run(task, *, recorder, fault)"
            )
        resolved_task = task if task is not None else experiment.task
        if not resolved_task:
            raise ValueError("experiment task is required")
        if task is not None and experiment.task is not None and task != experiment.task:
            raise ValueError("task does not match experiment.task")

        faulted_id = experiment.faulted_run_id or uuid4().hex
        while faulted_id == experiment.baseline_run_id:
            faulted_id = uuid4().hex
        prepared = experiment.model_copy(
            update={"task": resolved_task, "faulted_run_id": faulted_id}
        )
        baseline = await adapter.run(
            resolved_task, recorder=TraceRecorder(prepared.baseline_run_id)
        )
        try:
            faulted = await adapter.run(
                resolved_task,
                recorder=TraceRecorder(faulted_id),
                fault=prepared.fault.model_copy(deep=True),
            )
        except (Exception, asyncio.CancelledError) as exc:
            # The OpenAI adapter attaches a completed failed/cancelled trace to
            # its original exception. Preserve that exception and add the
            # comparison when both runs are available.
            partial = getattr(exc, "agentchaos_run", None)
            if isinstance(partial, AgentRun):
                try:
                    comparison = compare_runs(
                        prepared,
                        baseline,
                        partial,
                        expected_output=expected_output,
                        evaluator=evaluator,
                    )
                    exc.agentchaos_experiment = ExperimentOutcome(
                        prepared, baseline, partial, comparison
                    )
                except Exception as analysis_error:
                    exc.add_note(
                        "AgentChaos could not compare the failed run: "
                        f"{type(analysis_error).__name__}"
                    )
            raise
        comparison = compare_runs(
            prepared,
            baseline,
            faulted,
            expected_output=expected_output,
            evaluator=evaluator,
        )
        return ExperimentOutcome(prepared, baseline, faulted, comparison)
