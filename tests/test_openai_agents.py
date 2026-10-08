"""SDK-boundary tests that never contact the OpenAI API."""

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from agents import Agent
from agents.items import ModelResponse
from agents.tool_context import ToolContext
from agents.usage import Usage

from agentchaos import AgentRun, EventKind, RunStatus
from agentchaos.adapters import AgentAdapter
from agentchaos.adapters.openai_agents import OpenAIAgentsAdapter
from agentchaos.tracing import TraceRecorder
from examples.order_support.__main__ import get_order_status


def response() -> ModelResponse:
    return ModelResponse(
        output=[],
        usage=Usage(input_tokens=12, output_tokens=5, total_tokens=17),
        response_id="response-1",
    )


def tool_context(call_id: str) -> ToolContext[Any]:
    return ToolContext(
        context=None,
        tool_name="get_order_status",
        tool_call_id=call_id,
        tool_arguments='{"order_id":"123"}',
    )


@pytest.mark.asyncio
async def test_sdk_hooks_translate_model_and_tool_lifecycle(monkeypatch: Any) -> None:
    agent = Agent(name="support", model="test-model")
    tool = SimpleNamespace(name="get_order_status")

    async def fake_run(starting_agent: Any, task: str, *, hooks: Any, run_config: Any):
        assert starting_agent is agent
        assert task == "Where is order 123?"
        assert run_config.tracing_disabled is True
        await hooks.on_llm_start(None, agent, "secret system prompt", [{"secret": 1}])
        await hooks.on_llm_end(None, agent, response())
        context = tool_context("sdk-tool-call-123")
        await hooks.on_tool_start(context, agent, tool)
        await hooks.on_tool_end(context, agent, tool, {"private": "sdk object"})
        await hooks.on_llm_start(None, agent, None, [])
        await hooks.on_llm_end(None, agent, response())
        return SimpleNamespace(final_output="Order 123 is shipped, arriving Friday.")

    monkeypatch.setattr("agentchaos.adapters.openai_agents.Runner.run", fake_run)
    adapter: AgentAdapter = OpenAIAgentsAdapter(agent)
    run = await adapter.run("Where is order 123?", recorder=TraceRecorder("run-1"))

    assert run.status == RunStatus.SUCCEEDED
    assert run.output == "Order 123 is shipped, arriving Friday."
    assert [event.kind for event in run.events] == [
        EventKind.RUN_STARTED,
        EventKind.MODEL_REQUEST,
        EventKind.MODEL_RESPONSE,
        EventKind.TOOL_CALL,
        EventKind.TOOL_RESULT,
        EventKind.MODEL_REQUEST,
        EventKind.MODEL_RESPONSE,
        EventKind.RUN_COMPLETED,
    ]
    assert [event.sequence for event in run.events] == list(range(8))
    assert [event.call_id for event in run.events[1:3]] == ["model-1"] * 2
    assert [event.call_id for event in run.events[3:5]] == ["sdk-tool-call-123"] * 2
    assert [event.call_id for event in run.events[5:7]] == ["model-2"] * 2
    assert run.events[2].attributes == {
        "output_item_count": 0,
        "input_tokens": 12,
        "output_tokens": 5,
        "total_tokens": 17,
    }
    serialized = run.model_dump_json()
    assert "secret system prompt" not in serialized
    assert "private" not in serialized
    assert AgentRun.model_validate_json(serialized) == run


@pytest.mark.asyncio
async def test_fallback_tool_call_id_pairs_events(monkeypatch: Any) -> None:
    agent = Agent(name="support")
    tool = SimpleNamespace(name="get_order_status")

    async def fake_run(starting_agent: Any, task: str, *, hooks: Any, run_config: Any):
        context = SimpleNamespace()
        await hooks.on_tool_start(context, agent, tool)
        await hooks.on_tool_end(context, agent, tool, "done")
        return SimpleNamespace(final_output="done")

    monkeypatch.setattr("agentchaos.adapters.openai_agents.Runner.run", fake_run)
    run = await OpenAIAgentsAdapter(agent).run("Task", recorder=TraceRecorder("run-2"))

    assert run.events[1].call_id == run.events[2].call_id == "tool-1"


@pytest.mark.asyncio
async def test_sdk_model_failure_finishes_trace_and_reraises(monkeypatch: Any) -> None:
    agent = Agent(name="support")
    original = RuntimeError("sensitive request content")

    async def fake_run(starting_agent: Any, task: str, *, hooks: Any, run_config: Any):
        await hooks.on_llm_start(None, agent, None, [])
        raise original

    monkeypatch.setattr("agentchaos.adapters.openai_agents.Runner.run", fake_run)
    with pytest.raises(RuntimeError) as caught:
        await OpenAIAgentsAdapter(agent).run("Task", recorder=TraceRecorder("run-3"))

    assert caught.value is original
    run = caught.value.agentchaos_run
    assert run.status == RunStatus.FAILED
    assert [event.kind for event in run.events] == [
        EventKind.RUN_STARTED,
        EventKind.MODEL_REQUEST,
        EventKind.MODEL_ERROR,
        EventKind.RUN_FAILED,
    ]
    assert run.events[-1].attributes == {"error_type": "RuntimeError"}
    assert "sensitive request content" not in run.model_dump_json()


@pytest.mark.asyncio
async def test_sdk_tool_failure_marks_open_call(monkeypatch: Any) -> None:
    agent = Agent(name="support")
    tool = SimpleNamespace(name="get_order_status")

    async def fake_run(starting_agent: Any, task: str, *, hooks: Any, run_config: Any):
        await hooks.on_tool_start(tool_context("sdk-id"), agent, tool)
        raise ValueError("tool failed")

    monkeypatch.setattr("agentchaos.adapters.openai_agents.Runner.run", fake_run)
    with pytest.raises(ValueError) as caught:
        await OpenAIAgentsAdapter(agent).run("Task", recorder=TraceRecorder("run-4"))

    run = caught.value.agentchaos_run
    assert [event.kind for event in run.events] == [
        EventKind.RUN_STARTED,
        EventKind.TOOL_CALL,
        EventKind.TOOL_ERROR,
        EventKind.RUN_FAILED,
    ]
    assert run.events[1].call_id == run.events[2].call_id == "sdk-id"


@pytest.mark.asyncio
async def test_sdk_cancellation_produces_cancelled_run(monkeypatch: Any) -> None:
    agent = Agent(name="support")

    async def fake_run(starting_agent: Any, task: str, *, hooks: Any, run_config: Any):
        await asyncio.sleep(0)
        raise asyncio.CancelledError()

    monkeypatch.setattr("agentchaos.adapters.openai_agents.Runner.run", fake_run)
    with pytest.raises(asyncio.CancelledError) as caught:
        await OpenAIAgentsAdapter(agent).run("Task", recorder=TraceRecorder("run-5"))

    assert caught.value.agentchaos_run.status == RunStatus.CANCELLED
    assert caught.value.agentchaos_run.events[-1].kind == EventKind.RUN_CANCELLED


def test_order_status_tool_is_local_and_deterministic() -> None:
    assert get_order_status("123") == "shipped, arriving Friday"
    assert get_order_status("456") == "processing"
    assert get_order_status("789") == "delivered"
    assert get_order_status("missing") == "order not found"
