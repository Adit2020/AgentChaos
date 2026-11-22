"""Offline tests for per-run OpenAI FunctionTool fault interception."""

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from agents import Agent, UserError, function_tool
from agents.items import ModelResponse
from agents.models.interface import Model
from agents.tool import FunctionTool
from agents.tool_context import ToolContext
from agents.usage import Usage
from openai.types.responses import ResponseFunctionToolCall, ResponseOutputMessage
from openai.types.responses.response_output_text import ResponseOutputText

from agentchaos import EventKind, Experiment, ExperimentRunner, FaultSpec, RunStatus
from agentchaos.adapters.openai_agents import OpenAIAgentsAdapter
from agentchaos.artifacts import export_experiment, load_export
from agentchaos.tracing import TraceRecorder


def tool_call(call_id: str, order_id: str) -> ResponseFunctionToolCall:
    return ResponseFunctionToolCall(
        arguments=f'{{"order_id":"{order_id}"}}',
        call_id=call_id,
        name="get_order_status",
        type="function_call",
    )


class OrderScript(Model):
    """Decouple SDK/tool behavior from any model or network variability."""

    def __init__(self, *turns: list[ResponseFunctionToolCall]) -> None:
        self.turns = iter(turns)
        self.inputs: list[list[Any]] = []

    async def get_response(self, *args: Any, **kwargs: Any) -> ModelResponse:
        self.inputs.append(kwargs["input"])
        output = self._output(next(self.turns))
        return ModelResponse(output=output, usage=Usage(), response_id=None)

    def _output(
        self, calls: list[ResponseFunctionToolCall]
    ) -> list[ResponseFunctionToolCall | ResponseOutputMessage]:
        if calls:
            return list(calls)
        return [
            ResponseOutputMessage(
                id=f"answer-{len(self.inputs)}",
                content=[
                    ResponseOutputText(
                        annotations=[], text="shipped", type="output_text"
                    )
                ],
                role="assistant",
                status="completed",
                type="message",
            )
        ]

    def stream_response(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError

    def tool_outputs(self) -> dict[str, Any]:
        return {
            item["call_id"]: item["output"]
            for items in self.inputs
            for item in items
            if isinstance(item, dict) and item.get("type") == "function_call_output"
        }


@pytest.mark.asyncio
@pytest.mark.parametrize("retry_fails", [True, False])
async def test_real_sdk_recovery_requires_a_successful_retry(
    retry_fails: bool, tmp_path: Path
) -> None:
    invocations: list[str] = []

    @function_tool
    def get_order_status(order_id: str) -> str:
        """Look up a local order."""
        invocations.append(order_id)
        if retry_fails and len(invocations) == 2:
            raise ValueError("private lookup failure")
        return "shipped after earlier error"

    model = OrderScript(
        [tool_call("baseline-call", "123")],
        [],
        [tool_call("injected-call", "123")],
        [tool_call("retry-call", "123")],
        [],
    )
    outcome = await ExperimentRunner().run(
        adapter=OpenAIAgentsAdapter(
            Agent(name="support", model=model, tools=[get_order_status])
        ),
        experiment=Experiment(
            experiment_id="handled-error",
            baseline_run_id="baseline",
            task="Where is order 123?",
            fault=fault(),
        ),
        expected_output="shipped",
    )

    assert invocations == ["123", "123"]
    assert model.tool_outputs()["retry-call"] == (
        "An error occurred while running the tool. Please try again."
        if retry_fails
        else "shipped after earlier error"
    )
    assert outcome.comparison.fault_triggered is True
    assert outcome.comparison.target_tool_retries == 1
    assert outcome.comparison.baseline_task_success is True
    assert outcome.comparison.faulted_task_success is True
    assert outcome.comparison.recovered is (None if retry_fails else True)
    export_id = export_experiment(outcome, root=tmp_path).name
    loaded = load_export(export_id, root=tmp_path)
    retry_events = [
        event for event in loaded.faulted.events if event.call_id == "retry-call"
    ]
    assert [event.kind for event in retry_events] == [
        EventKind.TOOL_CALL,
        EventKind.TOOL_RESULT,
    ]
    assert retry_events[1].attributes["success"] is (not retry_fails)
    assert retry_events[1].attributes["injected"] is False
    assert loaded.comparison.recovered is outcome.comparison.recovered
    assert "private lookup failure" not in loaded.faulted.model_dump_json()


@pytest.mark.asyncio
async def test_real_sdk_custom_handler_correlates_interleaved_calls() -> None:
    entered = asyncio.Event()
    handled = asyncio.Event()
    failures: list[tuple[str, Exception]] = []
    error = ValueError("private lookup failure")

    async def format_failure(context: ToolContext[Any], exc: Exception) -> str:
        failures.append((context.tool_call_id, exc))
        handled.set()
        return "Please check again later."

    @function_tool(failure_error_function=format_failure)
    async def get_order_status(order_id: str) -> str:
        """Look up a local order."""
        if order_id == "ok":
            entered.set()
            await handled.wait()
            return "Earlier lookup failed, shipment now found."
        await entered.wait()
        raise error

    model = OrderScript([tool_call("ok", "ok"), tool_call("bad", "bad")], [])
    agent = Agent(name="support", model=model, tools=[get_order_status])
    invoker = get_order_status.on_invoke_tool
    async with asyncio.timeout(5):
        run = await OpenAIAgentsAdapter(agent).run(
            "Look up orders", recorder=TraceRecorder("parallel")
        )

    assert failures == [("bad", error)]
    assert model.tool_outputs() == {
        "bad": "Please check again later.",
        "ok": "Earlier lookup failed, shipment now found.",
    }
    results = {
        event.call_id: event.attributes["success"]
        for event in run.events
        if event.kind == EventKind.TOOL_RESULT
    }
    assert results == {"bad": False, "ok": True}
    assert get_order_status.on_invoke_tool is invoker


@pytest.mark.asyncio
async def test_real_sdk_propagates_failure_with_no_handler() -> None:
    error = ValueError("private lookup failure")

    @function_tool(failure_error_function=None)
    def get_order_status(order_id: str) -> str:
        """Look up a local order."""
        raise error

    model = OrderScript([tool_call("propagated", "123")])
    agent = Agent(name="support", model=model, tools=[get_order_status])
    with pytest.raises(UserError) as caught:
        await OpenAIAgentsAdapter(agent).run(
            "Look up order", recorder=TraceRecorder("propagated")
        )
    assert caught.value.__cause__ is error
    run = caught.value.agentchaos_run
    assert run.status == RunStatus.FAILED
    assert [event.kind for event in run.events if event.call_id == "propagated"] == [
        EventKind.TOOL_CALL,
        EventKind.TOOL_ERROR,
    ]


@pytest.mark.asyncio
async def test_real_sdk_timeout_result_is_a_failed_invocation() -> None:
    pending = asyncio.Event()

    @function_tool(timeout=0.05, timeout_error_function=lambda *_: "Try later.")
    async def get_order_status(order_id: str) -> str:
        """Look up a local order."""
        await pending.wait()
        return "shipped"

    model = OrderScript([tool_call("deadline", "123")], [])
    agent = Agent(name="support", model=model, tools=[get_order_status])
    run = await OpenAIAgentsAdapter(agent).run(
        "Look up order", recorder=TraceRecorder("deadline")
    )
    assert model.tool_outputs() == {"deadline": "Try later."}
    result = next(event for event in run.events if event.kind == EventKind.TOOL_RESULT)
    assert result.call_id == "deadline"
    assert result.attributes["success"] is False


@pytest.mark.asyncio
async def test_real_sdk_opaque_retry_leaves_recovery_unknown() -> None:
    async def invoke(context: ToolContext[Any], arguments: str) -> str:
        return "shipped"

    tool = FunctionTool(
        name="get_order_status",
        description="Look up a local order.",
        params_json_schema={
            "type": "object",
            "properties": {"order_id": {"type": "string"}},
            "required": ["order_id"],
            "additionalProperties": False,
        },
        on_invoke_tool=invoke,
    )
    model = OrderScript(
        [tool_call("baseline-call", "123")],
        [],
        [tool_call("injected-call", "123")],
        [tool_call("retry-call", "123")],
        [],
    )
    outcome = await ExperimentRunner().run(
        adapter=OpenAIAgentsAdapter(Agent(name="support", model=model, tools=[tool])),
        experiment=Experiment(
            experiment_id="opaque-tool",
            baseline_run_id="baseline",
            task="Where is order 123?",
            fault=fault(),
        ),
        expected_output="shipped",
    )
    assert outcome.comparison.fault_triggered is True
    assert outcome.comparison.target_tool_retries == 1
    assert outcome.comparison.faulted_task_success is True
    assert outcome.comparison.recovered is None
    retry = next(
        event
        for event in outcome.faulted.events
        if event.call_id == "retry-call" and event.kind == EventKind.TOOL_RESULT
    )
    assert "success" in retry.attributes
    assert retry.attributes["success"] is None
    assert tool.on_invoke_tool is invoke


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
async def test_normal_run_observes_tool_without_mutating_original(
    monkeypatch: Any,
) -> None:
    calls: list[str] = []
    agent = make_agent(calls)
    original_tool = agent.tools[0]
    assert isinstance(original_tool, FunctionTool)
    invoker = original_tool.on_invoke_tool

    async def fake_run(starting_agent: Any, task: str, *, hooks: Any, run_config: Any):
        assert starting_agent is not agent
        assert starting_agent.tools[0] is not original_tool
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
    assert run.events[2].attributes == {"result_type": "str", "success": True}
    assert agent.tools[0] is original_tool
    assert original_tool.on_invoke_tool is invoker


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
