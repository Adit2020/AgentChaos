"""Run the first live AgentChaos trace with a deterministic local order tool."""

import asyncio
import os
from uuid import uuid4

from agents import Agent, function_tool
from pydantic import BaseModel, ConfigDict
from rich.console import Console
from rich.table import Table

from agentchaos.adapters.openai_agents import OpenAIAgentsAdapter
from agentchaos.artifacts import export_experiment, export_multi_run
from agentchaos.experiments import ExperimentOutcome, ExperimentRunner
from agentchaos.models import AgentRun, Experiment, FaultSpec, RunStatus
from agentchaos.multi_run import MultiRunRunner, ReliabilityReport
from agentchaos.tracing import TraceRecorder

ORDERS = {
    "123": "shipped, arriving Friday",
    "456": "processing",
    "789": "delivered",
}


class OrderStatusRecord(BaseModel):
    """Expected JSON tool output: order_id, status, and nullable arrival_day."""

    model_config = ConfigDict(extra="forbid")

    order_id: str
    status: str
    arrival_day: str | None


def get_order_status(order_id: str) -> str:
    """Return the local status of an order by its ID."""

    return ORDERS.get(order_id, "order not found")


def get_order_status_json(order_id: str) -> str:
    """Return JSON matching OrderStatusRecord, with no extra fields."""

    status, separator, arrival_day = ORDERS.get(order_id, "order not found").partition(
        ", arriving "
    )
    return OrderStatusRecord(
        order_id=order_id,
        status=status,
        arrival_day=arrival_day if separator else None,
    ).model_dump_json()


def selected_fault(kind: str, target_tool: str) -> FaultSpec:
    if kind == "tool_error":
        return FaultSpec(
            fault_type=kind,
            target_tool=target_tool,
            occurrence=1,
            error_message="Temporary order lookup failure. Please retry once.",
        )
    if kind == "tool_timeout":
        return FaultSpec(
            fault_type=kind,
            target_tool=target_tool,
            occurrence=1,
            error_message="Order lookup timed out. Please retry once.",
            parameters={
                "timeout_ms": int(os.environ.get("AGENTCHAOS_TIMEOUT_MS", "10"))
            },
        )
    if kind == "malformed_tool_output":
        return FaultSpec(
            fault_type=kind,
            target_tool=target_tool,
            occurrence=1,
            parameters={
                "payload": os.environ.get(
                    "AGENTCHAOS_MALFORMED_PAYLOAD",
                    '{"order_id":"123","status":',
                )
            },
        )
    raise ValueError(f"unsupported example fault: {kind}")


def order_answer_is_correct(run: AgentRun) -> bool:
    """Check the expected local status and arrival day without a model judge."""

    answer = run.output
    return (
        isinstance(answer, str)
        and "shipped" in answer.lower()
        and "friday" in answer.lower()
    )


def show_comparison(console: Console, outcome: ExperimentOutcome) -> None:
    result = outcome.comparison

    def verdict(value: bool | None) -> str:
        return "unknown" if value is None else str(value).lower()

    console.print(f"Baseline answer: {outcome.baseline.output}")
    console.print(f"Faulted answer: {outcome.faulted.output}")
    table = Table(title="AgentChaos comparison")
    table.add_column("Metric")
    table.add_column("Baseline")
    table.add_column("Faulted")
    table.add_column("Delta")
    rows = [
        ("Run status", outcome.baseline.status.value, outcome.faulted.status.value, ""),
        (
            "Task success",
            verdict(result.baseline_task_success),
            verdict(result.faulted_task_success),
            "",
        ),
        (
            "Model calls",
            str(result.baseline_model_calls),
            str(result.faulted_model_calls),
            f"{result.model_call_delta:+d}",
        ),
        (
            "Tool calls",
            str(result.baseline_tool_calls),
            str(result.faulted_tool_calls),
            f"{result.tool_call_delta:+d}",
        ),
        (
            "Injected failures",
            str(result.baseline_injected_failures),
            str(result.faulted_injected_failures),
            "",
        ),
        (
            "Duration (ms)",
            str(result.baseline_duration_ms),
            str(result.faulted_duration_ms),
            (
                f"{result.duration_delta_ms:+d}"
                if result.duration_delta_ms is not None
                else "unknown"
            ),
        ),
        ("Fault fired", "", verdict(result.fault_triggered), ""),
        ("Continued after fault", "", verdict(result.continued_after_fault), ""),
        ("Target tool retries", "", str(result.target_tool_retries), ""),
        ("Recovered", "", verdict(result.recovered), ""),
    ]
    for row in rows:
        table.add_row(*row)
    console.print(table)


def show_multi_report(console: Console, report: ReliabilityReport) -> None:
    def fraction(count: int, denominator: int, rate: float | None) -> str:
        return (
            f"{count}/{denominator} ({rate:.0%})"
            if rate is not None
            else f"{count}/{denominator} (unknown)"
        )

    def stat(value: float | None, *, unit: str = "") -> str:
        return "unknown" if value is None else f"{value:+.1f}{unit}"

    summary = Table(title="AgentChaos reliability summary")
    summary.add_column("Metric")
    summary.add_column("Value")
    rows = [
        ("Requested repetitions", str(report.requested_repetitions)),
        ("Completed pairs", str(report.completed_repetitions)),
        ("Incomplete pairs", str(report.incomplete_pairs)),
        ("Failed pairs", str(report.failed_pairs)),
        ("Not started", str(report.not_started_repetitions)),
        ("Stop reason", report.stop_reason or "completed"),
        (
            "Faults triggered",
            fraction(
                report.triggered_faults,
                report.completed_repetitions,
                report.fault_trigger_rate,
            ),
        ),
        ("Confirmed recoveries", str(report.confirmed_recoveries)),
        ("Confirmed non-recoveries", str(report.confirmed_nonrecoveries)),
        ("Unknown recoveries", str(report.unknown_recoveries)),
        (
            "Recovery among evaluable triggers",
            fraction(
                report.confirmed_recoveries,
                report.evaluable_triggered_repetitions,
                report.confirmed_recovery_rate,
            ),
        ),
        (
            "Recovery among requested",
            fraction(
                report.confirmed_recoveries,
                report.requested_repetitions,
                report.recovery_rate_requested,
            ),
        ),
        (
            "Baseline task success",
            fraction(
                report.baseline_task_successes,
                report.baseline_evaluated_repetitions,
                report.baseline_task_success_rate,
            ),
        ),
        (
            "Faulted task success",
            fraction(
                report.faulted_task_successes,
                report.faulted_evaluated_repetitions,
                report.faulted_task_success_rate,
            ),
        ),
        (
            "Extra model calls, mean / median",
            f"{stat(report.average_extra_model_calls)} / "
            f"{stat(report.median_extra_model_calls)} "
            f"(n={report.model_call_observations})",
        ),
        (
            "Extra tool calls, mean / median",
            f"{stat(report.average_extra_tool_calls)} / "
            f"{stat(report.median_extra_tool_calls)} "
            f"(n={report.tool_call_observations})",
        ),
        (
            "Latency delta, mean / median",
            f"{stat(report.average_latency_delta_ms, unit=' ms')} / "
            f"{stat(report.median_latency_delta_ms, unit=' ms')} "
            f"(n={report.latency_observations})",
        ),
    ]
    for row in rows:
        summary.add_row(*row)
    console.print(summary)

    repetitions = Table(title="Repetition outcomes")
    for column in (
        "#",
        "Baseline",
        "Faulted",
        "Fault fired",
        "Recovered",
        "Model Δ",
        "Tool Δ",
        "Latency Δ",
    ):
        repetitions.add_column(column)
    for index, outcome in enumerate(report.outcomes, start=1):
        comparison = outcome.comparison
        recovery = (
            comparison.recovered
            if outcome.baseline.status == RunStatus.SUCCEEDED
            and outcome.faulted.status == RunStatus.SUCCEEDED
            else None
        )
        repetitions.add_row(
            str(index),
            outcome.baseline.status.value,
            outcome.faulted.status.value,
            str(comparison.fault_triggered).lower(),
            "unknown" if recovery is None else str(recovery).lower(),
            f"{comparison.model_call_delta:+d}",
            f"{comparison.tool_call_delta:+d}",
            (
                f"{comparison.duration_delta_ms:+d} ms"
                if comparison.duration_delta_ms is not None
                else "unknown"
            ),
        )
    for index in range(
        report.completed_repetitions + 1, report.requested_repetitions + 1
    ):
        state = (
            "incomplete"
            if index <= report.completed_repetitions + report.incomplete_pairs
            else "not started"
        )
        repetitions.add_row(str(index), state, "", "", "unknown", "", "", "")
    console.print(repetitions)


async def main() -> None:
    if not os.environ.get("OPENAI_API_KEY"):
        raise SystemExit("Set OPENAI_API_KEY before running this example.")

    compare = os.environ.get("AGENTCHAOS_COMPARE") == "1"
    repetitions_text = os.environ.get("AGENTCHAOS_REPETITIONS")
    multi_run = repetitions_text is not None
    export_enabled = os.environ.get("AGENTCHAOS_EXPORT") == "1"
    if compare and multi_run:
        raise SystemExit(
            "Choose AGENTCHAOS_COMPARE or AGENTCHAOS_REPETITIONS, not both."
        )
    if export_enabled and not (compare or multi_run):
        raise SystemExit("AGENTCHAOS_EXPORT=1 requires comparison or multi-run mode.")
    fault_kind = os.environ.get("AGENTCHAOS_FAULT") or (
        "tool_error" if compare or multi_run else None
    )
    use_json_tool = fault_kind == "malformed_tool_output"
    target_tool = "get_order_status_json" if use_json_tool else "get_order_status"
    agent = Agent(
        name="Order support",
        instructions=(
            f"Help customers check order status. Always use {target_tool} for "
            "an order lookup, then answer briefly using only the returned status. "
            + (
                "The tool returns JSON matching OrderStatusRecord: order_id and "
                "status are strings, arrival_day is a string or null. If the "
                "response is not valid JSON matching that shape, you may retry once."
                if use_json_tool
                else "If the tool reports a temporary failure, you may retry it once."
            )
        ),
        model=os.environ.get("AGENTCHAOS_MODEL") or None,
        tools=[
            function_tool(get_order_status_json if use_json_tool else get_order_status)
        ],
    )
    adapter = OpenAIAgentsAdapter(agent)
    task = "Where is order 123?"
    console = Console()
    if multi_run:
        max_text = os.environ.get("AGENTCHAOS_MAX_REPETITIONS")
        if max_text is None:
            raise SystemExit(
                "Set AGENTCHAOS_MAX_REPETITIONS (1–20) before a multi-run experiment."
            )
        try:
            repetitions = int(repetitions_text)
            runner = MultiRunRunner(max_repetitions=int(max_text))
            planned = runner.planned_agent_executions(repetitions)
        except ValueError as exc:
            raise SystemExit(f"Invalid repetition configuration: {exc}") from exc
        console.print(
            f"Starting {repetitions} repetitions: {planned} agent executions "
            f"({repetitions} baseline + {repetitions} faulted)."
        )
        experiment = Experiment(
            experiment_id=uuid4().hex,
            baseline_run_id=uuid4().hex,
            task=task,
            fault=selected_fault(fault_kind, target_tool),
        )
        try:
            report = await runner.run(
                adapter=adapter,
                experiment=experiment,
                repetitions=repetitions,
                evaluator=order_answer_is_correct,
            )
        except (Exception, asyncio.CancelledError) as exc:
            partial = getattr(exc, "agentchaos_multi_run_report", None)
            if isinstance(partial, ReliabilityReport):
                show_multi_report(console, partial)
                if export_enabled:
                    try:
                        path = export_multi_run(partial, experiment=experiment)
                    except Exception as export_error:
                        console.print(f"Artifact export failed: {export_error}")
                        exc.add_note(
                            "AgentChaos could not export the partial report: "
                            f"{type(export_error).__name__}"
                        )
                    else:
                        console.print(f"Artifacts exported to: {path.resolve()}")
            raise
        show_multi_report(console, report)
        if export_enabled:
            try:
                path = export_multi_run(report, experiment=experiment)
            except Exception as export_error:
                console.print(f"Artifact export failed: {export_error}")
                raise
            console.print(f"Artifacts exported to: {path.resolve()}")
        return
    if compare:
        experiment = Experiment(
            experiment_id=uuid4().hex,
            baseline_run_id=uuid4().hex,
            task=task,
            fault=selected_fault(fault_kind, target_tool),
        )
        try:
            outcome = await ExperimentRunner().run(
                adapter=adapter,
                experiment=experiment,
                evaluator=order_answer_is_correct,
            )
        except (Exception, asyncio.CancelledError) as exc:
            partial = getattr(exc, "agentchaos_experiment", None)
            if export_enabled and isinstance(partial, ExperimentOutcome):
                try:
                    path = export_experiment(partial)
                except Exception as export_error:
                    console.print(f"Artifact export failed: {export_error}")
                    exc.add_note(
                        "AgentChaos could not export the partial experiment: "
                        f"{type(export_error).__name__}"
                    )
                else:
                    console.print(f"Artifacts exported to: {path.resolve()}")
            raise
        show_comparison(console, outcome)
        if export_enabled:
            try:
                path = export_experiment(outcome)
            except Exception as export_error:
                console.print(f"Artifact export failed: {export_error}")
                raise
            console.print(f"Artifacts exported to: {path.resolve()}")
        return

    recorder = TraceRecorder(run_id=uuid4().hex)
    fault = selected_fault(fault_kind, target_tool) if fault_kind else None
    run = await adapter.run(task, recorder=recorder, fault=fault)

    console.print(f"Answer: {run.output}")
    table = Table(title="AgentChaos trace")
    table.add_column("#", justify="right")
    table.add_column("Event")
    table.add_column("Name")
    table.add_column("Call ID")
    table.add_column("Details")
    for event in run.events:
        details = ""
        if event.attributes.get("injected"):
            details = (
                f"injected {event.attributes['fault_kind']} "
                f"(occurrence {event.attributes['occurrence']})"
            )
        table.add_row(
            str(event.sequence),
            event.kind.value,
            event.name or "",
            event.call_id or "",
            details,
        )
    console.print(table)


if __name__ == "__main__":
    asyncio.run(main())
