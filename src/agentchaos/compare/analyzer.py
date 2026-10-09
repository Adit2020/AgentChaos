"""Deterministic task evaluation and trace-based recovery analysis."""

from collections import Counter
from collections.abc import Callable

from pydantic import JsonValue

from agentchaos.models import (
    AgentRun,
    ComparisonResult,
    EventKind,
    Experiment,
    RunEvent,
    RunStatus,
)

TaskEvaluator = Callable[[AgentRun], bool | None]
_UNSET = object()


def _duration_ms(run: AgentRun) -> int | None:
    if run.ended_at is None:
        return None
    return round((run.ended_at - run.started_at).total_seconds() * 1000)


def _completed_lifecycle(run: AgentRun) -> bool:
    return (
        run.status == RunStatus.SUCCEEDED
        and len(run.events) >= 2
        and run.events[0].kind == EventKind.RUN_STARTED
        and run.events[-1].kind == EventKind.RUN_COMPLETED
    )


def _task_success(
    run: AgentRun, expected_output: JsonValue | object, evaluator: TaskEvaluator | None
) -> bool | None:
    if evaluator is None and expected_output is _UNSET:
        return None
    if run.status == RunStatus.RUNNING:
        return None
    if run.status != RunStatus.SUCCEEDED:
        return False
    if evaluator is None:
        return run.output == expected_output
    result = evaluator(run)
    if result is not None and not isinstance(result, bool):
        raise TypeError("task evaluator must return bool or None")
    return result


def _injected_failures(run: AgentRun) -> list[tuple[RunEvent, RunEvent]]:
    """Find injected terminal events with one preceding, correlated tool call."""

    calls: dict[str, RunEvent] = {}
    duplicates: set[str] = set()
    for event in run.events:
        if event.kind != EventKind.TOOL_CALL or not event.call_id:
            continue
        if event.call_id in calls:
            duplicates.add(event.call_id)
        else:
            calls[event.call_id] = event

    seen_terminals: set[str] = set()
    failures: list[tuple[RunEvent, RunEvent]] = []
    for event in run.events:
        if event.kind not in {EventKind.TOOL_RESULT, EventKind.TOOL_ERROR}:
            continue
        call_id = event.call_id
        if not call_id or call_id in duplicates or call_id in seen_terminals:
            continue
        seen_terminals.add(call_id)
        call = calls.get(call_id)
        if call is None or call.sequence >= event.sequence:
            continue
        if event.attributes.get("injected") is not True:
            continue
        if (
            event.kind == EventKind.TOOL_ERROR
            and event.attributes.get("delivery_confirmed") is False
        ):
            continue
        if (
            event.kind == EventKind.TOOL_RESULT
            and event.attributes.get("success") is not False
        ):
            continue
        failures.append((call, event))
    return failures


def _post_fault_model_responses(run: AgentRun, failure: RunEvent) -> list[int]:
    """Find responses paired with model requests made after the fault."""

    requests = {
        event.call_id: event.sequence
        for event in run.events
        if event.kind == EventKind.MODEL_REQUEST
        and event.call_id
        and event.sequence > failure.sequence
    }
    return [
        event.sequence
        for event in run.events
        if (
            event.kind == EventKind.MODEL_RESPONSE
            and event.call_id in requests
            and event.sequence > requests[event.call_id]
        )
    ]


def _completed_post_fault_tools(
    run: AgentRun, response_sequences: list[int]
) -> list[RunEvent]:
    """Find successful tool results paired with calls after model continuation."""

    calls: dict[str, RunEvent] = {}
    duplicates: set[str] = set()
    for event in run.events:
        if event.kind != EventKind.TOOL_CALL or not event.call_id:
            continue
        if event.call_id in calls:
            duplicates.add(event.call_id)
        else:
            calls[event.call_id] = event
    terminals: dict[str, list[RunEvent]] = {}
    for event in run.events:
        if (
            event.kind in {EventKind.TOOL_RESULT, EventKind.TOOL_ERROR}
            and event.call_id
        ):
            terminals.setdefault(event.call_id, []).append(event)
    return [
        call
        for call_id, call in calls.items()
        if call_id not in duplicates
        and any(sequence < call.sequence for sequence in response_sequences)
        and len(terminals.get(call_id, [])) == 1
        and terminals[call_id][0].kind == EventKind.TOOL_RESULT
        and terminals[call_id][0].sequence > call.sequence
        and terminals[call_id][0].attributes.get("injected") is not True
        and terminals[call_id][0].attributes.get("success") is not False
    ]


def compare_runs(
    experiment: Experiment,
    baseline: AgentRun,
    faulted: AgentRun,
    *,
    expected_output: JsonValue | object = _UNSET,
    evaluator: TaskEvaluator | None = None,
) -> ComparisonResult:
    """Compare attempted calls and confirm recovery only with paired fault evidence.

    Call counts use request/call events, so their response/error events never add
    another call. An incomplete call still counts as an attempted call.
    """

    if evaluator is not None and expected_output is not _UNSET:
        raise ValueError("provide either expected_output or evaluator")
    if experiment.fault.fault_type not in {
        "tool_error",
        "tool_timeout",
        "malformed_tool_output",
    }:
        raise ValueError("unsupported fault type for comparison")
    if baseline.run_id != experiment.baseline_run_id:
        raise ValueError("baseline run ID does not match experiment")
    if experiment.faulted_run_id and faulted.run_id != experiment.faulted_run_id:
        raise ValueError("faulted run ID does not match experiment")
    if baseline.run_id == faulted.run_id:
        raise ValueError("baseline and faulted runs must differ")
    if baseline.task != faulted.task or (
        experiment.task is not None and baseline.task != experiment.task
    ):
        raise ValueError("baseline and faulted tasks must match experiment")

    baseline_counts = Counter(event.kind for event in baseline.events)
    faulted_counts = Counter(event.kind for event in faulted.events)
    baseline_injected = _injected_failures(baseline)
    faulted_injected = _injected_failures(faulted)
    baseline_markers = sum(
        event.attributes.get("injected") is True for event in baseline.events
    )
    faulted_markers = sum(
        event.attributes.get("injected") is True for event in faulted.events
    )

    target_ordinals = {
        event.sequence: index
        for index, event in enumerate(
            (
                event
                for event in faulted.events
                if event.kind == EventKind.TOOL_CALL
                and event.name == experiment.fault.target_tool
            ),
            start=1,
        )
    }
    target_failure = next(
        (
            terminal
            for call, terminal in faulted_injected
            if call.name == experiment.fault.target_tool
            and terminal.attributes.get("fault_kind") == experiment.fault.fault_type
            and terminal.attributes.get("target_tool") == call.name
            and terminal.attributes.get("occurrence") == experiment.fault.occurrence
            and target_ordinals.get(call.sequence) == experiment.fault.occurrence
        ),
        None,
    )
    post_fault_responses = (
        _post_fault_model_responses(faulted, target_failure)
        if target_failure is not None
        else []
    )
    continued = (
        target_failure is not None
        and _completed_lifecycle(faulted)
        and faulted.events[-1].sequence > target_failure.sequence
        and bool(post_fault_responses)
    )
    retries = (
        sum(
            event.kind == EventKind.TOOL_CALL
            and event.name == experiment.fault.target_tool
            and event.sequence > target_failure.sequence
            and any(
                response_sequence < event.sequence
                for response_sequence in post_fault_responses
            )
            for event in faulted.events
        )
        if target_failure is not None
        else 0
    )
    completed_tools = _completed_post_fault_tools(faulted, post_fault_responses)
    successful_retries = sum(
        call.name == experiment.fault.target_tool for call in completed_tools
    )
    alternative_tool_calls = sum(
        call.name is not None and call.name != experiment.fault.target_tool
        for call in completed_tools
    )
    baseline_success = _task_success(baseline, expected_output, evaluator)
    faulted_success = _task_success(faulted, expected_output, evaluator)
    recovered: bool | None = None
    if (
        target_failure is not None
        and baseline_markers == 0
        and faulted_markers == len(faulted_injected) == 1
        and _completed_lifecycle(baseline)
        and baseline_success is True
    ):
        if faulted_success is False:
            recovered = False
        elif faulted_success is True and continued and successful_retries > 0:
            recovered = True

    baseline_duration = _duration_ms(baseline)
    faulted_duration = _duration_ms(faulted)
    return ComparisonResult(
        experiment_id=experiment.experiment_id,
        baseline_run_id=baseline.run_id,
        faulted_run_id=faulted.run_id,
        recovered=recovered,
        baseline_model_calls=baseline_counts[EventKind.MODEL_REQUEST],
        faulted_model_calls=faulted_counts[EventKind.MODEL_REQUEST],
        model_call_delta=(
            faulted_counts[EventKind.MODEL_REQUEST]
            - baseline_counts[EventKind.MODEL_REQUEST]
        ),
        baseline_tool_calls=baseline_counts[EventKind.TOOL_CALL],
        faulted_tool_calls=faulted_counts[EventKind.TOOL_CALL],
        tool_call_delta=(
            faulted_counts[EventKind.TOOL_CALL] - baseline_counts[EventKind.TOOL_CALL]
        ),
        baseline_injected_failures=len(baseline_injected),
        faulted_injected_failures=len(faulted_injected),
        fault_triggered=target_failure is not None,
        baseline_duration_ms=baseline_duration,
        faulted_duration_ms=faulted_duration,
        duration_delta_ms=(
            faulted_duration - baseline_duration
            if baseline_duration is not None and faulted_duration is not None
            else None
        ),
        baseline_task_success=baseline_success,
        faulted_task_success=faulted_success,
        continued_after_fault=continued,
        target_tool_retries=retries,
        alternative_tool_calls=alternative_tool_calls,
    )
