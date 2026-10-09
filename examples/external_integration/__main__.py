"""Connect an existing-style SDK agent to AgentChaos's public API."""

import asyncio
import json
import os
from uuid import uuid4

from agents import Agent, function_tool
from pydantic import BaseModel

from agentchaos import AgentRun, Experiment, ExperimentRunner, FaultSpec
from agentchaos.adapters.openai_agents import OpenAIAgentsAdapter
from agentchaos.artifacts import export_experiment


class StockAnswer(BaseModel):
    product_id: str
    in_stock: bool


def get_inventory(product_id: str) -> str:
    """Return deterministic local inventory data as JSON."""

    return json.dumps(
        {"product_id": product_id, "in_stock": product_id == "SKU-7"},
        sort_keys=True,
    )


def task_succeeded(run: AgentRun) -> bool | None:
    """Grade the structured answer without a model judge."""

    if not isinstance(run.output, dict):
        return None
    return run.output == {"product_id": "SKU-7", "in_stock": True}


async def main() -> None:
    if not os.environ.get("OPENAI_API_KEY"):
        raise SystemExit("Set OPENAI_API_KEY before running this live example.")

    agent = Agent(
        name="Inventory assistant",
        instructions=(
            "Use get_inventory to answer whether a product is in stock. "
            "If the tool reports a temporary failure, retry it once. "
            "Return product_id and in_stock in the structured answer."
        ),
        model=os.environ.get("AGENTCHAOS_MODEL") or None,
        tools=[function_tool(get_inventory)],
        output_type=StockAnswer,
    )
    task = "Is SKU-7 in stock?"
    experiment = Experiment(
        experiment_id=uuid4().hex,
        baseline_run_id=uuid4().hex,
        task=task,
        fault=FaultSpec(
            fault_type="tool_error",
            target_tool="get_inventory",
            error_message="Temporary inventory failure. Please retry.",
        ),
    )
    outcome = await ExperimentRunner().run(
        adapter=OpenAIAgentsAdapter(agent),
        experiment=experiment,
        evaluator=task_succeeded,
    )
    print(f"Baseline: {outcome.baseline.output}")
    print(f"Faulted: {outcome.faulted.output}")
    print(f"Comparison: {outcome.comparison.model_dump(mode='json')}")
    if os.environ.get("AGENTCHAOS_EXPORT") == "1":
        print(f"Artifacts: {export_experiment(outcome).resolve()}")


if __name__ == "__main__":
    asyncio.run(main())
