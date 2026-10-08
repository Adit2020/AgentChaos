"""Run the first live AgentChaos trace with a deterministic local order tool."""

import asyncio
import os
from uuid import uuid4

from agents import Agent, function_tool
from rich.console import Console
from rich.table import Table

from agentchaos.adapters.openai_agents import OpenAIAgentsAdapter
from agentchaos.experiments import ExperimentOutcome, ExperimentRunner
from agentchaos.models import AgentRun, Experiment, FaultSpec
from agentchaos.tracing import TraceRecorder

ORDERS = {
    "123": "shipped, arriving Friday",
    "456": "processing",
    "789": "delivered",
}


def get_order_status(order_id: str) -> str:
    """Return the local status of an order by its ID."""

    return ORDERS.get(order_id, "order not found")


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


async def main() -> None:
    if not os.environ.get("OPENAI_API_KEY"):
        raise SystemExit("Set OPENAI_API_KEY before running this example.")

    agent = Agent(
        name="Order support",
        instructions=(
            "Help customers check order status. Always use get_order_status for "
            "an order lookup, then answer briefly using only the returned status. "
            "If the tool reports a temporary failure, you may retry it once."
        ),
        model=os.environ.get("AGENTCHAOS_MODEL") or None,
        tools=[function_tool(get_order_status)],
    )
    adapter = OpenAIAgentsAdapter(agent)
    task = "Where is order 123?"
    console = Console()
    if os.environ.get("AGENTCHAOS_COMPARE") == "1":
        experiment = Experiment(
            experiment_id=uuid4().hex,
            baseline_run_id=uuid4().hex,
            task=task,
            fault=FaultSpec(
                fault_type="tool_error",
                target_tool="get_order_status",
                occurrence=1,
                error_message="Temporary order lookup failure. Please retry once.",
            ),
        )
        outcome = await ExperimentRunner().run(
            adapter=adapter, experiment=experiment, evaluator=order_answer_is_correct
        )
        show_comparison(console, outcome)
        return

    recorder = TraceRecorder(run_id=uuid4().hex)
    fault = (
        FaultSpec(
            fault_type="tool_error",
            target_tool="get_order_status",
            occurrence=1,
            error_message="Temporary order lookup failure. Please retry once.",
        )
        if os.environ.get("AGENTCHAOS_FAULT") == "tool_error"
        else None
    )
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
