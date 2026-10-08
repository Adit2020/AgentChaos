"""Run the first live AgentChaos trace with a deterministic local order tool."""

import asyncio
import os
from uuid import uuid4

from agents import Agent, function_tool
from rich.console import Console
from rich.table import Table

from agentchaos.adapters.openai_agents import OpenAIAgentsAdapter
from agentchaos.models import FaultSpec
from agentchaos.tracing import TraceRecorder

ORDERS = {
    "123": "shipped, arriving Friday",
    "456": "processing",
    "789": "delivered",
}


def get_order_status(order_id: str) -> str:
    """Return the local status of an order by its ID."""

    return ORDERS.get(order_id, "order not found")


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
    recorder = TraceRecorder(run_id=uuid4().hex)
    fault = None
    if os.environ.get("AGENTCHAOS_FAULT") == "tool_error":
        fault = FaultSpec(
            fault_type="tool_error",
            target_tool="get_order_status",
            occurrence=1,
            error_message="Temporary order lookup failure. Please retry once.",
        )
    run = await adapter.run("Where is order 123?", recorder=recorder, fault=fault)

    console = Console()
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
