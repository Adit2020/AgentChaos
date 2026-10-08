"""Run the first live AgentChaos trace with a deterministic local order tool."""

import asyncio
import os
from uuid import uuid4

from agents import Agent, function_tool
from rich.console import Console
from rich.table import Table

from agentchaos.adapters.openai_agents import OpenAIAgentsAdapter
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
            "an order lookup, then answer briefly using only the returned status."
        ),
        model=os.environ.get("AGENTCHAOS_MODEL") or None,
        tools=[function_tool(get_order_status)],
    )
    adapter = OpenAIAgentsAdapter(agent)
    recorder = TraceRecorder(run_id=uuid4().hex)
    run = await adapter.run("Where is order 123?", recorder=recorder)

    console = Console()
    console.print(f"Answer: {run.output}")
    table = Table(title="AgentChaos trace")
    table.add_column("#", justify="right")
    table.add_column("Event")
    table.add_column("Name")
    table.add_column("Call ID")
    for event in run.events:
        table.add_row(
            str(event.sequence), event.kind.value, event.name or "", event.call_id or ""
        )
    console.print(table)


if __name__ == "__main__":
    asyncio.run(main())
