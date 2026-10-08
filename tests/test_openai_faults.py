"""Offline tests for per-run OpenAI FunctionTool fault interception."""

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from agents import Agent, function_tool
from agents.items import ModelResponse
from agents.models.interface import Model
from agents.tool import FunctionTool
from agents.tool_context import ToolContext
from agents.usage import Usage
from openai.types.responses import ResponseFunctionToolCall, ResponseOutputMessage
from openai.types.responses.response_output_text import ResponseOutputText

from agentchaos import EventKind, FaultSpec, RunStatus
from agentchaos.adapters.openai_agents import OpenAIAgentsAdapter
from agentchaos.tracing import TraceRecorder


def fault() -> FaultSpec:
    return FaultSpec(
        fault_type="tool_error",
        target_tool="get_order_status",
        occurrence=1,
        error_message="Temporary lookup failure. Please retry.",
    )


def make_agent(calls: list[str]) -> Agent[Any]:
    @function_tool
    def get_order_status(order_id: str) -> str:
        """Look up a local order."""

        calls.append(f"order:{order_id}")
        return "shipped"

    @function_tool
    def get_account_status(account_id: str) -> str:
        """Look up a local account."""

        calls.append(f"account:{account_id}")
        return "active"

    return Agent(name="support", tools=[get_order_status, get_account_status])


async def invoke_with_hooks(
    hooks: Any, agent: Agent[Any], tool: FunctionTool, call_id: str, arguments: str
) -> str:
    context = ToolContext(
        context=None,
        tool_name=tool.name,
        tool_call_id=call_id,
        tool_arguments=arguments,
    )
    await hooks.on_tool_start(context, agent, tool)
    result = await tool.on_invoke_tool(context, arguments)
    await hooks.on_tool_end(context, agent, tool, result)
    return result


@pytest.mark.asyncio
async def test_first_target_call_fails_without_mutating_agent_or_tools(
    monkeypatch: Any,
) -> None:
    calls: list[str] = []
    agent = make_agent(calls)
    original_list = agent.tools
    order_tool = agent.tools[0]
    account_tool = agent.tools[1]
    original_invoker = order_tool.on_invoke_tool
    original_schema = order_tool.params_json_schema.copy()
    outputs: list[str] = []
    prepared_agents: list[Agent[Any]] = []

    async def fake_run(starting_agent: Any, task: str, *, hooks: Any, run_config: Any):
        prepared_agents.append(starting_agent)
        order = starting_agent.tools[0]
        account = starting_agent.tools[1]
        outputs.append(
            await invoke_with_hooks(
                hooks, starting_agent, account, "account-1", '{"account_id":"A"}'
            )
        )
        outputs.append(
            await invoke_with_hooks(
                hooks, starting_agent, order, "order-1", '{"order_id":"123"}'
            )
        )
        outputs.append(
            await invoke_with_hooks(
                hooks, starting_agent, order, "order-2", '{"order_id":"123"}'
            )
        )
        return SimpleNamespace(final_output="Agent decided how to answer")

    monkeypatch.setattr("agentchaos.adapters.openai_agents.Runner.run", fake_run)
    run = await OpenAIAgentsAdapter(agent).run(
        "Where is order 123?", recorder=TraceRecorder("faulted"), fault=fault()
    )

    assert outputs == [
        "active",
        "Temporary lookup failure. Please retry.",
        "shipped",
    ]
    assert calls == ["account:A", "order:123"]
    assert prepared_agents[0] is not agent
    assert prepared_agents[0].tools is not original_list
    assert all(
        copied is not original
        for copied, original in zip(
            prepared_agents[0].tools, original_list, strict=True
        )
    )
    assert prepared_agents[0].tools[0].params_json_schema == original_schema
    assert prepared_agents[0].tools[0].description == order_tool.description
    assert agent.tools is original_list
    assert agent.tools == [order_tool, account_tool]
    assert order_tool.on_invoke_tool is original_invoker
    assert order_tool.params_json_schema == original_schema
    assert run.status == RunStatus.SUCCEEDED

    target_events = [
        event for event in run.events if event.call_id in {"order-1", "order-2"}
    ]
    assert [event.kind for event in target_events] == [
        EventKind.TOOL_CALL,
        EventKind.TOOL_RESULT,
        EventKind.TOOL_CALL,
        EventKind.TOOL_RESULT,
    ]
    assert target_events[0].call_id == target_events[1].call_id == "order-1"
    assert target_events[1].attributes == {
        "result_type": "str",
        "injected": True,
        "success": False,
        "fault_kind": "tool_error",
        "target_tool": "get_order_status",
        "occurrence": 1,
    }
    assert target_events[3].attributes["injected"] is False
    assert "Temporary lookup failure" not in run.model_dump_json()


@pytest.mark.asyncio
async def test_normal_run_uses_original_agent_and_tool(monkeypatch: Any) -> None:
    calls: list[str] = []
    agent = make_agent(calls)

    async def fake_run(starting_agent: Any, task: str, *, hooks: Any, run_config: Any):
        assert starting_agent is agent
        result = await invoke_with_hooks(
            hooks,
            starting_agent,
            starting_agent.tools[0],
            "normal-1",
            '{"order_id":"123"}',
        )
        return SimpleNamespace(final_output=result)

    monkeypatch.setattr("agentchaos.adapters.openai_agents.Runner.run", fake_run)
    run = await OpenAIAgentsAdapter(agent).run(
        "Where is order 123?", recorder=TraceRecorder("normal")
    )

    assert calls == ["order:123"]
    assert run.output == "shipped"
    assert run.events[2].attributes == {"result_type": "str"}


@pytest.mark.asyncio
async def test_two_faulted_runs_get_independent_counters_and_copies(
    monkeypatch: Any,
) -> None:
    calls: list[str] = []
    agent = make_agent(calls)
    prepared: list[Agent[Any]] = []
    outputs: list[str] = []

    async def fake_run(starting_agent: Any, task: str, *, hooks: Any, run_config: Any):
        prepared.append(starting_agent)
        await asyncio.sleep(0)
        output = await invoke_with_hooks(
            hooks,
            starting_agent,
            starting_agent.tools[0],
            f"call-{task}",
            '{"order_id":"123"}',
        )
        outputs.append(output)
        return SimpleNamespace(final_output=output)

    monkeypatch.setattr("agentchaos.adapters.openai_agents.Runner.run", fake_run)
    adapter = OpenAIAgentsAdapter(agent)
    spec = fault()
    runs = await asyncio.gather(
        adapter.run("one", recorder=TraceRecorder("run-one"), fault=spec),
        adapter.run("two", recorder=TraceRecorder("run-two"), fault=spec),
    )

    assert outputs == [spec.error_message, spec.error_message]
    assert calls == []
    assert prepared[0] is not prepared[1]
    assert prepared[0].tools is not prepared[1].tools
    assert prepared[0].tools[0] is not prepared[1].tools[0]
    assert all(run.events[2].attributes["injected"] is True for run in runs)
    assert agent.tools[0].on_invoke_tool is not prepared[0].tools[0].on_invoke_tool


@pytest.mark.asyncio
async def test_real_sdk_runner_delivers_injected_result_to_offline_model() -> None:
    class ScriptedModel(Model):
        calls = 0
        second_input: Any = None

        async def get_response(self, *args: Any, **kwargs: Any) -> ModelResponse:
            self.calls += 1
            if self.calls == 1:
                output = [
                    ResponseFunctionToolCall(
                        arguments='{"order_id":"123"}',
                        call_id="sdk-call-1",
                        name="get_order_status",
                        type="function_call",
                    )
                ]
            else:
                self.second_input = kwargs["input"]
                output = [
                    ResponseOutputMessage(
                        id="message-1",
                        content=[
                            ResponseOutputText(
                                annotations=[],
                                text="Please retry later.",
                                type="output_text",
                            )
                        ],
                        role="assistant",
                        status="completed",
                        type="message",
                    )
                ]
            return ModelResponse(output=output, usage=Usage(), response_id=None)

        def stream_response(self, *args: Any, **kwargs: Any) -> Any:
            raise NotImplementedError

    calls: list[str] = []
    model = ScriptedModel()
    agent = make_agent(calls).clone(model=model)
    run = await OpenAIAgentsAdapter(agent).run(
        "Where is order 123?", recorder=TraceRecorder("offline-sdk"), fault=fault()
    )

    assert calls == []
    assert model.calls == 2
    assert any(
        isinstance(item, dict)
        and item.get("type") == "function_call_output"
        and item.get("call_id") == "sdk-call-1"
        and "Temporary lookup failure" in str(item.get("output"))
        for item in model.second_input
    )
    assert run.status == RunStatus.SUCCEEDED
    assert run.events[3].kind == EventKind.TOOL_CALL
    assert run.events[4].kind == EventKind.TOOL_RESULT
    assert run.events[3].call_id == run.events[4].call_id == "sdk-call-1"
    assert run.events[4].attributes["injected"] is True


@pytest.mark.asyncio
async def test_structured_output_tool_is_rejected_before_starting_run() -> None:
    async def invoke(context: Any, arguments: str) -> dict[str, str]:
        return {"status": "shipped"}

    structured_tool = FunctionTool(
        name="get_order_status",
        description="Look up an order",
        params_json_schema={
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        },
        on_invoke_tool=invoke,
        output_json_schema={
            "type": "object",
            "properties": {"status": {"type": "string"}},
            "required": ["status"],
            "additionalProperties": False,
        },
    )
    recorder = TraceRecorder("unsupported")
    with pytest.raises(ValueError, match="structured output schema"):
        await OpenAIAgentsAdapter(Agent(name="support", tools=[structured_tool])).run(
            "Task", recorder=recorder, fault=fault()
        )

    assert recorder.events == ()
