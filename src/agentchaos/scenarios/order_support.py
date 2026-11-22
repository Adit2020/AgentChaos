"""Trusted order-support scenario with a deterministic, local lookup tool."""

import os

from pydantic import BaseModel, ConfigDict

from agentchaos.adapters.base import FaultCapableAdapter
from agentchaos.models import AgentRun

ORDERS = {
    "123": "shipped, arriving Friday",
    "456": "processing",
    "789": "delivered",
}
DEFAULT_TASK = "Where is order 123?"


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

    status, separator, arrival_day = get_order_status(order_id).partition(", arriving ")
    return OrderStatusRecord(
        order_id=order_id,
        status=status,
        arrival_day=arrival_day if separator else None,
    ).model_dump_json()


def order_answer_is_correct(run: AgentRun) -> bool:
    """Check the expected local status and arrival day without a model judge."""

    answer = run.output
    return (
        isinstance(answer, str)
        and "shipped" in answer.lower()
        and "friday" in answer.lower()
    )


def create_adapter() -> FaultCapableAdapter:
    """Build fresh SDK objects only after an acknowledged job is accepted."""

    if not os.environ.get("OPENAI_API_KEY"):
        raise RuntimeError("OpenAI credentials are not configured")

    from agentchaos.adapters.openai_agents import OpenAIAgentsAdapter

    return OpenAIAgentsAdapter.from_function_tools(
        name="Order support",
        instructions=(
            "Help customers check order status. Always use get_order_status_json "
            "for an order lookup, then answer briefly using only the returned "
            "status. The tool returns JSON with order_id and status strings and "
            "arrival_day as a string or null. If a temporary failure occurs or "
            "the response is not valid JSON matching that shape, retry once."
        ),
        model=os.environ.get("AGENTCHAOS_MODEL") or None,
        tools=[get_order_status_json],
    )
