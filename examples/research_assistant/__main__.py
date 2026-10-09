"""Run a live research agent against a fictional local knowledge base."""

import asyncio
import os
from uuid import uuid4

from agents import Agent, function_tool
from pydantic import BaseModel, ConfigDict, Field

from agentchaos.adapters.openai_agents import OpenAIAgentsAdapter
from agentchaos.artifacts import export_experiment
from agentchaos.compare import required_facts
from agentchaos.experiments import ExperimentRunner
from agentchaos.models import AgentRun, Experiment, FaultSpec
from agentchaos.tracing import TraceRecorder
from examples.research_assistant.knowledge_base import (
    expected_facts,
    get_document,
    get_project_details,
    search_documents,
)

TASK = (
    "According to the available sources, when is Project Atlas launching, "
    "who owns it, and what is its current status?"
)
TOOL_NAMES = {"search_documents", "get_document", "get_project_details"}
FAULT_TYPES = {"tool_error", "tool_timeout", "malformed_tool_output"}


class ResearchAnswer(BaseModel):
    """Final answer contract: exact date/person and source-worded status."""

    model_config = ConfigDict(extra="forbid")

    launch_date: str
    owner: str
    status: str = Field(
        description="Current project status in the source's lowercase wording."
    )


def evaluate_answer(run: AgentRun) -> bool | None:
    """Grade each required value against the local project record."""

    return required_facts(
        run,
        expected_facts("Project Atlas"),
        case_insensitive_fields={"status"},
    )


def selected_fault(kind: str, target_tool: str) -> FaultSpec:
    if kind not in FAULT_TYPES:
        raise ValueError(f"unsupported example fault: {kind}")
    if target_tool not in TOOL_NAMES:
        raise ValueError(f"unknown target tool: {target_tool}")
    if kind == "tool_error":
        return FaultSpec(
            fault_type=kind,
            target_tool=target_tool,
            error_message="Temporary source failure. Try another source or retry.",
        )
    if kind == "tool_timeout":
        return FaultSpec(
            fault_type=kind,
            target_tool=target_tool,
            error_message="Source lookup timed out. Try another source or retry.",
            parameters={
                "timeout_ms": int(os.environ.get("AGENTCHAOS_TIMEOUT_MS", "10"))
            },
        )
    return FaultSpec(
        fault_type=kind,
        target_tool=target_tool,
        parameters={
            "payload": os.environ.get("AGENTCHAOS_MALFORMED_PAYLOAD", "{not-json")
        },
    )


def make_agent() -> Agent:
    return Agent(
        name="Research assistant",
        instructions=(
            "Answer using only the local tools. search_documents returns JSON with "
            "matching IDs and summaries; get_document returns JSON with full content; "
            "get_project_details returns JSON with structured project facts. "
            "Choose the tools needed for the question. Treat malformed JSON or a "
            "temporary failure as unusable and retry or consult another source. "
            "Return the ISO launch date, owner's full name, and current status "
            "in the structured answer fields. Use the source's lowercase status "
            "wording. Do not guess missing facts."
        ),
        model=os.environ.get("AGENTCHAOS_MODEL") or None,
        tools=[
            function_tool(search_documents),
            function_tool(get_document),
            function_tool(get_project_details),
        ],
        output_type=ResearchAnswer,
    )


async def main() -> None:
    compare = os.environ.get("AGENTCHAOS_COMPARE") == "1"
    export = os.environ.get("AGENTCHAOS_EXPORT") == "1"
    if export and not compare:
        raise SystemExit("AGENTCHAOS_EXPORT=1 requires AGENTCHAOS_COMPARE=1")
    kind = os.environ.get("AGENTCHAOS_FAULT") or ("tool_error" if compare else None)
    target = os.environ.get("AGENTCHAOS_TARGET_TOOL", "get_document")
    fault = selected_fault(kind, target) if kind else None
    adapter = OpenAIAgentsAdapter(make_agent())
    if compare:
        experiment = Experiment(
            experiment_id=uuid4().hex,
            baseline_run_id=uuid4().hex,
            task=TASK,
            fault=fault,
        )
        outcome = await ExperimentRunner().run(
            adapter=adapter, experiment=experiment, evaluator=evaluate_answer
        )
        print(f"Baseline answer: {outcome.baseline.output}")
        print(f"Faulted answer: {outcome.faulted.output}")
        print(f"Comparison: {outcome.comparison.model_dump(mode='json')}")
        if export:
            print(f"Artifacts exported to: {export_experiment(outcome).resolve()}")
        return
    run = await adapter.run(TASK, recorder=TraceRecorder(uuid4().hex), fault=fault)
    print(f"Answer: {run.output}")
    print(f"Task correct: {evaluate_answer(run)}")
    for event in run.events:
        print(
            event.sequence,
            event.kind.value,
            event.name or "",
            event.call_id or "",
            event.attributes,
        )


if __name__ == "__main__":
    asyncio.run(main())
