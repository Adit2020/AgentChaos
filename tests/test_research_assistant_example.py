"""Offline checks for the local research agent and structured grading."""

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from agents.tool_context import ToolContext

from agentchaos import AgentRun, EventKind, Experiment, RunStatus
from agentchaos.adapters.openai_agents import OpenAIAgentsAdapter
from agentchaos.artifacts import export_experiment
from agentchaos.compare import compare_runs, required_facts
from agentchaos.experiments import ExperimentOutcome
from agentchaos.tracing import TraceRecorder
from examples.research_assistant.__main__ import (
    TASK,
    ResearchAnswer,
    evaluate_answer,
    make_agent,
    selected_fault,
)
from examples.research_assistant.knowledge_base import (
    DOCUMENTS,
    PROJECTS,
    expected_facts,
    get_document,
    get_project_details,
    search_documents,
)

CORRECT = expected_facts("Project Atlas")
ARGS = {
    "search_documents": '{"query":"Project Atlas"}',
    "get_document": '{"document_id":"atlas-brief"}',
    "get_project_details": '{"project_name":"Project Atlas"}',
}


def test_local_tools_return_deterministic_machine_readable_sources() -> None:
    matches = json.loads(search_documents("Project Atlas"))["matches"]
    assert [item["document_id"] for item in matches] == [
        "atlas-brief",
        "atlas-release",
    ]
    assert (
        json.loads(search_documents("When is Project Atlas launching?"))["matches"]
        == matches
    )
    assert all(
        set(item) == {"document_id", "project_name", "summary"} for item in matches
    )
    assert json.loads(search_documents("unlisted"))["matches"] == []
    assert json.loads(get_document("atlas-brief")) == {
        "found": True,
        "document": DOCUMENTS["atlas-brief"],
    }
    assert json.loads(get_document("missing")) == {"found": False, "document": None}
    assert json.loads(get_project_details("Project Atlas")) == {
        "found": True,
        "project": PROJECTS["Project Atlas"],
    }
    assert json.loads(get_project_details("Missing")) == {
        "found": False,
        "project": None,
    }
    for project in PROJECTS.values():
        related = [
            document
            for document in DOCUMENTS.values()
            if document["project_name"] == project["project_name"]
        ]
        assert related
        assert all(project["launch_date"] in doc["content"] for doc in related)
        assert all(project["owner"] in doc["content"] for doc in related)
        assert all(project["status"] in doc["content"] for doc in related)


def finished_run(run_id: str, output: object) -> AgentRun:
    recorder = TraceRecorder(run_id)
    recorder.start(task=TASK, adapter="fake")
    return recorder.finish(RunStatus.SUCCEEDED, output=output)


@pytest.mark.parametrize(
    ("answer", "verdict"),
    [
        (CORRECT, True),
        ({**CORRECT, "status": "Approved"}, True),
        ({**CORRECT, "status": "APPROVED"}, True),
        ({**CORRECT, "launch_date": "2026-11-19"}, False),
        ({**CORRECT, "launch_date": "not 2026-11-18"}, False),
        ({**CORRECT, "owner": "Noah Patel"}, False),
        ({**CORRECT, "owner": "maya chen"}, False),
        ({**CORRECT, "status": "not approved"}, False),
        ({**CORRECT, "status": "cancelled"}, False),
        ({**CORRECT, "status": "planning"}, False),
        ({"launch_date": "2026-11-18", "owner": "Maya Chen"}, None),
        ({**CORRECT, "owner": None}, None),
        ({**CORRECT, "status": None}, None),
        ("{malformed", None),
        (None, None),
    ],
)
def test_structured_answer_requires_all_exact_facts(
    answer: object, verdict: bool | None
) -> None:
    assert evaluate_answer(finished_run("answer", answer)) is verdict


def test_case_insensitivity_is_explicit_and_limited_to_status() -> None:
    answer = finished_run("capitalized", {**CORRECT, "status": "Approved"})
    assert required_facts(answer, CORRECT) is False
    assert required_facts(answer, CORRECT, case_insensitive_fields={"status"}) is True


def test_output_schema_guides_canonical_status_without_restricting_case() -> None:
    schema = ResearchAnswer.model_json_schema()
    assert schema["properties"]["status"]["type"] == "string"
    assert "lowercase" in schema["properties"]["status"]["description"]
    assert ResearchAnswer.model_validate({**CORRECT, "status": "Approved"}).status == (
        "Approved"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind", ["tool_error", "tool_timeout", "malformed_tool_output"]
)
@pytest.mark.parametrize("target", sorted(ARGS))
async def test_fault_targets_only_selected_tool_and_first_occurrence(
    monkeypatch: Any, kind: str, target: str
) -> None:
    agent = make_agent()
    original_tools = tuple(agent.tools)
    outputs: dict[str, list[str]] = {}

    async def fake_run(starting_agent: Any, task: str, *, hooks: Any, run_config: Any):
        for tool in starting_agent.tools:
            for index in (1, 2) if tool.name == target else (1,):
                context = ToolContext(
                    context=None,
                    tool_name=tool.name,
                    tool_call_id=f"{tool.name}-{index}",
                    tool_arguments=ARGS[tool.name],
                )
                await hooks.on_tool_start(context, starting_agent, tool)
                value = await tool.on_invoke_tool(context, ARGS[tool.name])
                await hooks.on_tool_end(context, starting_agent, tool, value)
                outputs.setdefault(tool.name, []).append(value)
        return SimpleNamespace(final_output=CORRECT)

    monkeypatch.setattr("agentchaos.adapters.openai_agents.Runner.run", fake_run)
    run = await OpenAIAgentsAdapter(agent).run(
        TASK, recorder=TraceRecorder("faulted"), fault=selected_fault(kind, target)
    )
    assert run.status == RunStatus.SUCCEEDED
    assert evaluate_answer(run) is True
    assert tuple(agent.tools) == original_tools
    assert len(outputs[target]) == 2
    expected_output = {
        "search_documents": search_documents("Project Atlas"),
        "get_document": get_document("atlas-brief"),
        "get_project_details": get_project_details("Project Atlas"),
    }[target]
    assert outputs[target][1] == expected_output
    for name in ARGS.keys() - {target}:
        assert len(outputs[name]) == 1
        assert json.loads(outputs[name][0])
    injected = [
        event
        for event in run.events
        if event.kind == EventKind.TOOL_RESULT and event.attributes.get("injected")
    ]
    assert len(injected) == 1
    assert injected[0].call_id == f"{target}-1"
    assert injected[0].attributes["fault_kind"] == kind
    assert injected[0].attributes["target_tool"] == target
    assert injected[0].attributes["occurrence"] == 1
    if kind == "malformed_tool_output":
        assert outputs[target][0] == "{not-json"
    else:
        assert "Try another source" in outputs[target][0]


def traced_research_run(
    run_id: str,
    *,
    faulted: bool,
    retry: bool = False,
    alternative: bool = False,
    answer: object = CORRECT,
    target_tool: str = "get_document",
    fault_kind: str = "tool_error",
) -> AgentRun:
    recorder = TraceRecorder(run_id)
    recorder.start(task=TASK, adapter="fake")
    recorder.record_model_request(call_id="model-1", name="model")
    recorder.record_model_response(call_id="model-1")
    recorder.record_tool_call(call_id="tool-1", name=target_tool)
    recorder.record_tool_result(
        call_id="tool-1",
        attributes=(
            {
                "injected": True,
                "success": False,
                "fault_kind": fault_kind,
                "target_tool": target_tool,
                "occurrence": 1,
                "malformed_output_delivered": fault_kind == "malformed_tool_output",
            }
            if faulted
            else {}
        ),
    )
    if faulted:
        recorder.record_model_request(call_id="model-2", name="model")
        recorder.record_model_response(call_id="model-2")
        if retry or alternative:
            recorder.record_tool_call(
                call_id="tool-2",
                name=(
                    target_tool
                    if retry
                    else (
                        "get_project_details"
                        if target_tool == "get_document"
                        else "get_document"
                    )
                ),
            )
            recorder.record_tool_result(call_id="tool-2")
            recorder.record_model_request(call_id="model-3", name="model")
            recorder.record_model_response(call_id="model-3")
    return recorder.finish(RunStatus.SUCCEEDED, output=answer)


@pytest.mark.parametrize(
    ("retry", "alternative", "answer", "recovered", "alternatives"),
    [
        (False, False, CORRECT, None, 0),
        (True, False, CORRECT, True, 0),
        (False, True, CORRECT, None, 1),
        (False, True, {**CORRECT, "status": "planning"}, False, 1),
    ],
)
def test_recovery_requires_retry_and_tracks_alternative_tool_separately(
    retry: bool,
    alternative: bool,
    answer: object,
    recovered: bool | None,
    alternatives: int,
    tmp_path: Path,
) -> None:
    experiment = Experiment(
        experiment_id="research",
        baseline_run_id="baseline",
        faulted_run_id="faulted",
        task=TASK,
        fault=selected_fault("tool_error", "get_document"),
    )
    baseline = traced_research_run("baseline", faulted=False)
    faulted = traced_research_run(
        "faulted", faulted=True, retry=retry, alternative=alternative, answer=answer
    )
    comparison = compare_runs(experiment, baseline, faulted, evaluator=evaluate_answer)
    assert comparison.fault_triggered is True
    assert comparison.continued_after_fault is True
    assert comparison.target_tool_retries == int(retry)
    assert comparison.alternative_tool_calls == alternatives
    assert comparison.recovered is recovered
    destination = export_experiment(
        ExperimentOutcome(experiment, baseline, faulted, comparison), root=tmp_path
    )
    exported = json.loads((destination / "comparison.json").read_text())
    assert exported["schema_version"] == 1
    assert exported["data"]["alternative_tool_calls"] == alternatives
    assert exported["data"]["recovered"] is recovered


def test_unpaired_retry_cannot_confirm_recovery() -> None:
    experiment = Experiment(
        experiment_id="unpaired-retry",
        baseline_run_id="baseline",
        faulted_run_id="faulted",
        task=TASK,
        fault=selected_fault("tool_error", "get_document"),
    )
    baseline = traced_research_run("baseline", faulted=False)
    faulted = traced_research_run("faulted", faulted=True, retry=True)
    incomplete = faulted.model_copy(
        update={
            "events": [
                event
                for event in faulted.events
                if not (
                    event.call_id == "tool-2" and event.kind == EventKind.TOOL_RESULT
                )
            ]
        }
    )
    comparison = compare_runs(
        experiment, baseline, incomplete, evaluator=evaluate_answer
    )
    assert comparison.faulted_task_success is True
    assert comparison.target_tool_retries == 1
    assert comparison.recovered is None


def test_live_shaped_malformed_fault_with_alternative_keeps_recovery_unknown() -> None:
    experiment = Experiment(
        experiment_id="malformed-alternative",
        baseline_run_id="baseline",
        faulted_run_id="faulted",
        task=TASK,
        fault=selected_fault("malformed_tool_output", "get_project_details"),
    )
    baseline = traced_research_run(
        "baseline", faulted=False, target_tool="get_project_details"
    )
    faulted = traced_research_run(
        "faulted",
        faulted=True,
        target_tool="get_project_details",
        fault_kind="malformed_tool_output",
        alternative=True,
        answer={**CORRECT, "status": "Approved"},
    )
    comparison = compare_runs(experiment, baseline, faulted, evaluator=evaluate_answer)
    assert comparison.fault_triggered is True
    assert comparison.faulted_task_success is True
    assert comparison.target_tool_retries == 0
    assert comparison.alternative_tool_calls == 1
    assert comparison.recovered is None
