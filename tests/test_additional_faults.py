"""Offline SDK and adapter tests for timeout and malformed tool outputs."""

import asyncio
from time import monotonic
from types import SimpleNamespace
from typing import Any

import pytest
from agents import Agent
from agents.items import ModelResponse
from agents.models.interface import Model
from agents.tool_context import ToolContext
from agents.usage import Usage
from openai.types.responses import ResponseFunctionToolCall, ResponseOutputMessage
from openai.types.responses.response_output_text import ResponseOutputText

from agentchaos import EventKind, Experiment, FaultSpec, RunStatus
from agentchaos.adapters.openai_agents import OpenAIAgentsAdapter
from agentchaos.experiments import ExperimentRunner
from agentchaos.tracing import TraceRecorder
from tests.test_openai_faults import invoke_with_hooks, make_agent


def new_fault(kind: str, *, occurrence: int = 1) -> FaultSpec:
    if kind == "tool_timeout":
        return FaultSpec(
            fault_type=kind,
            target_tool="get_order_status",
            occurrence=occurrence,
            error_message="Order lookup timed out. Please retry.",
            parameters={"timeout_ms": 15},
        )
    return FaultSpec(
        fault_type=kind,
        target_tool="get_order_status",
        occurrence=occurrence,
        parameters={"payload": "{broken-json"},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["tool_timeout", "malformed_tool_output"])
async def test_selected_tool_result_is_injected_without_calling_original(
    monkeypatch: Any, kind: str
) -> None:
    calls: list[str] = []
    agent = make_agent(calls)
    original_tools = agent.tools
    original_invoker = agent.tools[0].on_invoke_tool
    prepared_agents: list[Agent[Any]] = []
    outputs: list[str] = []

    async def fake_run(starting_agent: Any, task: str, *, hooks: Any, run_config: Any):
        prepared_agents.append(starting_agent)
        account, order = starting_agent.tools[1], starting_agent.tools[0]
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
        return SimpleNamespace(final_output="done")

    monkeypatch.setattr("agentchaos.adapters.openai_agents.Runner.run", fake_run)
    started = monotonic()
    run = await OpenAIAgentsAdapter(agent).run(
        "Task", recorder=TraceRecorder("faulted"), fault=new_fault(kind)
    )
    elapsed = monotonic() - started

    assert outputs == [
        "active",
        "Order lookup timed out. Please retry."
        if kind == "tool_timeout"
        else "{broken-json",
        "shipped",
    ]
    assert calls == ["account:A", "order:123"]
    assert agent.tools is original_tools
    assert agent.tools[0].on_invoke_tool is original_invoker
    assert prepared_agents[0] is not agent
    assert prepared_agents[0].tools[0] is not original_tools[0]
    assert prepared_agents[0].tools[1] is not original_tools[1]
    assert run.status == RunStatus.SUCCEEDED
    call, result = [event for event in run.events if event.call_id == "order-1"]
    assert call.kind == EventKind.TOOL_CALL
    assert result.kind == EventKind.TOOL_RESULT
    assert call.call_id == result.call_id == "order-1"
    assert result.attributes["injected"] is True
    assert result.attributes["fault_kind"] == kind
    assert result.attributes["success"] is False
    assert result.attributes["occurrence"] == 1
    if kind == "tool_timeout":
        assert elapsed >= 0.012
        assert result.attributes["timeout_simulated"] is True
        assert result.attributes["timeout_ms"] == 15
    else:
        assert result.attributes["malformed_output_delivered"] is True
        assert "{broken-json" not in run.model_dump_json()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["tool_timeout", "malformed_tool_output"])
async def test_occurrence_two_and_independent_concurrent_runs(
    monkeypatch: Any, kind: str
) -> None:
    calls: list[str] = []
    agent = make_agent(calls)
    prepared: list[Agent[Any]] = []
    outputs: list[list[str]] = []

    async def fake_run(starting_agent: Any, task: str, *, hooks: Any, run_config: Any):
        prepared.append(starting_agent)
        await asyncio.sleep(0)
        order = starting_agent.tools[0]
        first = await invoke_with_hooks(
            hooks, starting_agent, order, f"{task}-1", '{"order_id":"123"}'
        )
        second = await invoke_with_hooks(
            hooks, starting_agent, order, f"{task}-2", '{"order_id":"123"}'
        )
        outputs.append([first, second])
        return SimpleNamespace(final_output="done")

    monkeypatch.setattr("agentchaos.adapters.openai_agents.Runner.run", fake_run)
    adapter = OpenAIAgentsAdapter(agent)
    spec = new_fault(kind, occurrence=2)
    runs = await asyncio.gather(
        adapter.run("one", recorder=TraceRecorder("one"), fault=spec),
        adapter.run("two", recorder=TraceRecorder("two"), fault=spec),
    )

    injected_output = (
        "Order lookup timed out. Please retry."
        if kind == "tool_timeout"
        else "{broken-json"
    )
    assert outputs == [["shipped", injected_output], ["shipped", injected_output]]
    assert calls == ["order:123", "order:123"]
    assert prepared[0] is not prepared[1]
    assert prepared[0].tools[0] is not prepared[1].tools[0]
    assert all(
        next(
            event
            for event in run.events
            if event.call_id == f"{run.task}-2" and event.kind == EventKind.TOOL_RESULT
        ).attributes["injected"]
        is True
        for run in runs
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["tool_timeout", "malformed_tool_output"])
async def test_normal_run_after_fault_has_no_injected_state(
    monkeypatch: Any, kind: str
) -> None:
    calls: list[str] = []
    agent = make_agent(calls)

    async def fake_run(starting_agent: Any, task: str, *, hooks: Any, run_config: Any):
        output = await invoke_with_hooks(
            hooks,
            starting_agent,
            starting_agent.tools[0],
            f"call-{task}",
            '{"order_id":"123"}',
        )
        return SimpleNamespace(final_output=output)

    monkeypatch.setattr("agentchaos.adapters.openai_agents.Runner.run", fake_run)
    adapter = OpenAIAgentsAdapter(agent)
    faulted = await adapter.run(
        "faulted", recorder=TraceRecorder("faulted"), fault=new_fault(kind)
    )
    normal = await adapter.run("normal", recorder=TraceRecorder("normal"))

    assert faulted.events[2].attributes["injected"] is True
    assert normal.output == "shipped"
    assert normal.events[2].attributes == {"result_type": "str"}
    assert calls == ["order:123"]


@pytest.mark.asyncio
async def test_cancellation_during_timeout_does_not_claim_fault_delivery(
    monkeypatch: Any,
) -> None:
    calls: list[str] = []
    agent = make_agent(calls)
    started = asyncio.Event()

    async def fake_run(starting_agent: Any, task: str, *, hooks: Any, run_config: Any):
        tool = starting_agent.tools[0]
        context = ToolContext(
            context=None,
            tool_name=tool.name,
            tool_call_id="cancelled-call",
            tool_arguments='{"order_id":"123"}',
        )
        await hooks.on_tool_start(context, starting_agent, tool)
        started.set()
        result = await tool.on_invoke_tool(context, '{"order_id":"123"}')
        await hooks.on_tool_end(context, starting_agent, tool, result)
        return SimpleNamespace(final_output=result)

    monkeypatch.setattr("agentchaos.adapters.openai_agents.Runner.run", fake_run)
    fault = FaultSpec(
        fault_type="tool_timeout",
        target_tool="get_order_status",
        parameters={"timeout_ms": 200},
    )
    task = asyncio.create_task(
        OpenAIAgentsAdapter(agent).run(
            "Task", recorder=TraceRecorder("cancelled"), fault=fault
        )
    )
    await started.wait()
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError) as caught:
        await task

    run = caught.value.agentchaos_run
    assert run.status == RunStatus.CANCELLED
    assert calls == []
    assert [event.kind for event in run.events] == [
        EventKind.RUN_STARTED,
        EventKind.TOOL_CALL,
        EventKind.TOOL_ERROR,
        EventKind.RUN_CANCELLED,
    ]
    assert run.events[1].call_id == run.events[2].call_id == "cancelled-call"
    assert run.events[2].attributes["injected"] is False


@pytest.mark.asyncio
async def test_cancellation_before_tool_end_keeps_delivery_unconfirmed(
    monkeypatch: Any,
) -> None:
    calls: list[str] = []
    agent = make_agent(calls)

    async def fake_run(starting_agent: Any, task: str, *, hooks: Any, run_config: Any):
        tool = starting_agent.tools[0]
        context = ToolContext(
            context=None,
            tool_name=tool.name,
            tool_call_id="interrupted-call",
            tool_arguments='{"order_id":"123"}',
        )
        await hooks.on_tool_start(context, starting_agent, tool)
        await tool.on_invoke_tool(context, '{"order_id":"123"}')
        raise asyncio.CancelledError

    monkeypatch.setattr("agentchaos.adapters.openai_agents.Runner.run", fake_run)
    with pytest.raises(asyncio.CancelledError) as caught:
        await OpenAIAgentsAdapter(agent).run(
            "Task",
            recorder=TraceRecorder("interrupted"),
            fault=new_fault("tool_timeout"),
        )

    run = caught.value.agentchaos_run
    assert calls == []
    assert run.events[2].kind == EventKind.TOOL_ERROR
    assert run.events[2].attributes["injected"] is True
    assert run.events[2].attributes["delivery_confirmed"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["tool_timeout", "malformed_tool_output"])
async def test_real_sdk_runner_and_experiment_runner_observe_recovery(
    kind: str,
) -> None:
    class ScriptedModel(Model):
        calls = 0
        observed_inputs: list[Any]

        def __init__(self) -> None:
            self.observed_inputs = []

        async def get_response(self, *args: Any, **kwargs: Any) -> ModelResponse:
            self.calls += 1
            self.observed_inputs.append(kwargs["input"])
            if self.calls in {1, 3, 4}:
                output = [
                    ResponseFunctionToolCall(
                        arguments='{"order_id":"123"}',
                        call_id=f"tool-{self.calls}",
                        name="get_order_status",
                        type="function_call",
                    )
                ]
            else:
                output = [
                    ResponseOutputMessage(
                        id=f"message-{self.calls}",
                        content=[
                            ResponseOutputText(
                                annotations=[],
                                text="Order shipped Friday",
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
    plan = Experiment(
        experiment_id=f"experiment-{kind}",
        baseline_run_id="baseline",
        faulted_run_id="faulted",
        task="Where is order 123?",
        fault=new_fault(kind),
    )

    outcome = await ExperimentRunner().run(
        adapter=OpenAIAgentsAdapter(agent),
        experiment=plan,
        expected_output="Order shipped Friday",
    )

    assert model.calls == 5
    assert calls == ["order:123", "order:123"]
    assert outcome.comparison.baseline_injected_failures == 0
    assert outcome.comparison.faulted_injected_failures == 1
    assert outcome.comparison.fault_triggered is True
    assert outcome.comparison.target_tool_retries == 1
    assert outcome.comparison.baseline_task_success is True
    assert outcome.comparison.faulted_task_success is True
    assert outcome.comparison.recovered is True
    assert outcome.comparison.model_call_delta == 1
    assert outcome.comparison.tool_call_delta == 1
    injected = next(
        event
        for event in outcome.faulted.events
        if event.kind == EventKind.TOOL_RESULT and event.attributes.get("injected")
    )
    assert injected.call_id == "tool-3"
    received = [
        item
        for item in model.observed_inputs[3]
        if isinstance(item, dict) and item.get("type") == "function_call_output"
    ]
    assert any(
        item.get("call_id") == "tool-3"
        and (
            "timed out" in str(item.get("output"))
            if kind == "tool_timeout"
            else "{broken-json" in str(item.get("output"))
        )
        for item in received
    )
