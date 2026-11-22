"""Observe an OpenAI Agents SDK run through per-run lifecycle hooks."""

import asyncio
import copy
from collections import defaultdict
from collections.abc import Callable
from functools import wraps
from hashlib import sha256
from typing import Any

try:
    from agents import Agent, RunConfig, RunHooks, Runner, function_tool
    from agents.items import ModelResponse, TResponseInputItem
    from agents.run_context import RunContextWrapper
    from agents.tool import FunctionTool, Tool
    from openai.types.responses import ResponseFunctionToolCall
except ModuleNotFoundError as exc:
    if exc.name == "agents" or (exc.name and exc.name.startswith("agents.")):
        raise ImportError(
            "OpenAIAgentsAdapter requires the optional OpenAI integration; "
            "install agentchaos[openai]."
        ) from exc
    raise
from pydantic import BaseModel, JsonValue, TypeAdapter, ValidationError

from agentchaos.faults import FaultMatch, FaultMatcher
from agentchaos.models import AgentRun, FaultSpec, RunStatus
from agentchaos.tracing import TraceRecorder

_JSON_VALUE = TypeAdapter(JsonValue)


def _portable_output(value: Any) -> JsonValue:
    """Keep only output that can be represented in a normalized JSON trace."""

    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    try:
        return _JSON_VALUE.validate_python(value)
    except ValidationError:
        return None


class _InjectedCalls:
    """Share per-run fault observations between invokers and lifecycle hooks."""

    def __init__(self) -> None:
        self._by_call_id: dict[str, FaultMatch] = {}
        self._without_call_id: dict[str, list[FaultMatch]] = defaultdict(list)

    def mark(self, context: Any, match: FaultMatch) -> None:
        call_id = getattr(context, "tool_call_id", None)
        if isinstance(call_id, str) and call_id:
            self._by_call_id[call_id] = match
        else:
            self._without_call_id[match.target_tool].append(match)

    def take(self, call_id: str, tool_name: str) -> FaultMatch | None:
        match = self._by_call_id.pop(call_id, None)
        if match is not None:
            return match
        pending = self._without_call_id.get(tool_name)
        return pending.pop(0) if pending else None


def _observe_invocation(
    original: Callable[..., Any],
    tool_name: str,
    outcomes: dict[str, bool],
    matcher: FaultMatcher | None,
    injected: _InjectedCalls | None,
    *,
    observes_outcome: bool,
) -> Callable[..., Any]:
    @wraps(original, updated=())
    async def invoke(context: Any, arguments: str) -> Any:
        match = matcher.match_tool(tool_name) if matcher is not None else None
        if match is not None and injected is not None:
            if match.fault_type == "tool_timeout":
                await asyncio.sleep(match.timeout_ms / 1000)
            injected.mark(context, match)
            if match.fault_type == "malformed_tool_output":
                return match.payload
            return match.error_message

        call_id = getattr(context, "tool_call_id", None) if observes_outcome else None
        try:
            result = await original(context, arguments)
        except (Exception, asyncio.CancelledError):
            if isinstance(call_id, str) and call_id:
                outcomes[call_id] = False
            raise
        if isinstance(call_id, str) and call_id:
            outcomes[call_id] = True
        return result

    return invoke


def _prepare_agent(
    agent: Agent[Any],
    outcomes: dict[str, bool],
    injected: _InjectedCalls | None,
    fault: FaultSpec | None,
) -> Agent[Any]:
    """Clone the agent and its FunctionTools without changing shared objects."""

    matcher = FaultMatcher(fault) if fault is not None else None
    if fault is not None and not any(
        isinstance(tool, FunctionTool) and tool.name == fault.target_tool
        for tool in agent.tools
    ):
        raise ValueError(f"no local FunctionTool named {fault.target_tool!r}")
    if not any(isinstance(tool, FunctionTool) for tool in agent.tools):
        return agent

    copied_tools: list[Tool] = []
    for tool in agent.tools:
        if not isinstance(tool, FunctionTool):
            copied_tools.append(tool)
            continue
        targeted = fault is not None and tool.name == fault.target_tool
        if targeted and tool.output_json_schema is not None:
            raise ValueError(
                "injected tool faults currently support FunctionTools without a "
                "structured output schema"
            )
        copied_tool = copy.copy(tool)
        invoker = copied_tool.on_invoke_tool
        # SDK 0.23.1 handles exceptions inside the generated invoker. Observe
        # its inner implementation while retaining SDK argument preparation,
        # default/custom failure policy, and timeout handling on the copy.
        inner = getattr(invoker, "_invoke_tool_impl", None)
        if callable(inner) and getattr(invoker, "_function_tool", None) is copied_tool:
            observed_invoker = copy.copy(invoker)
            observed_invoker._invoke_tool_impl = _observe_invocation(
                inner,
                tool.name,
                outcomes,
                matcher if targeted else None,
                injected,
                observes_outcome=True,
            )
            copied_tool.on_invoke_tool = observed_invoker
        elif targeted:
            # Keep fault injection available for opaque invokers. Their return
            # values alone cannot distinguish a success from a handled failure.
            copied_tool.on_invoke_tool = _observe_invocation(
                invoker, tool.name, outcomes, matcher, injected, observes_outcome=False
            )
        copied_tools.append(copied_tool)
    return agent.clone(tools=copied_tools)


class _RecorderHooks(RunHooks[Any]):
    """Translate one SDK run's callbacks into one AgentChaos recorder."""

    def __init__(
        self,
        recorder: TraceRecorder,
        outcomes: dict[str, bool],
        injected: _InjectedCalls | None,
        *,
        record_tool_turns: bool = False,
    ) -> None:
        self._recorder = recorder
        self._outcomes = outcomes
        self._injected = injected
        self._record_tool_turns = record_tool_turns
        self._next_model_call = 0
        self._next_tool_call = 0
        self._active_model_calls: list[str] = []
        self._active_tool_calls: dict[str, str] = {}
        self._fallback_tool_calls: dict[tuple[int, int], list[str]] = defaultdict(list)

    async def on_llm_start(
        self,
        context: RunContextWrapper[Any],
        agent: Agent[Any],
        system_prompt: str | None,
        input_items: list[TResponseInputItem],
    ) -> None:
        self._next_model_call += 1
        call_id = f"model-{self._next_model_call}"
        name = agent.model if isinstance(agent.model, str) else "default"
        attributes: dict[str, JsonValue] = {"agent": agent.name}
        if self._record_tool_turns:
            attributes["input_tool_outputs"] = [
                {
                    "call_id": item["call_id"],
                    "sha256": sha256(item["output"].encode()).hexdigest(),
                }
                for item in input_items
                if isinstance(item, dict)
                and item.get("type") == "function_call_output"
                and isinstance(item.get("call_id"), str)
                and isinstance(item.get("output"), str)
            ]
        self._recorder.record_model_request(
            call_id=call_id, name=name, attributes=attributes
        )
        self._active_model_calls.append(call_id)

    async def on_llm_end(
        self,
        context: RunContextWrapper[Any],
        agent: Agent[Any],
        response: ModelResponse,
    ) -> None:
        call_id = self._active_model_calls.pop()
        attributes: dict[str, JsonValue] = {
            "output_item_count": len(response.output),
            "input_tokens": response.usage.input_tokens,
            "output_tokens": response.usage.output_tokens,
            "total_tokens": response.usage.total_tokens,
        }
        if self._record_tool_turns:
            attributes["output_tool_call_ids"] = [
                item.call_id
                for item in response.output
                if isinstance(item, ResponseFunctionToolCall)
            ]
        self._recorder.record_model_response(
            call_id=call_id,
            attributes=attributes,
        )

    async def on_tool_start(
        self, context: RunContextWrapper[Any], agent: Agent[Any], tool: Tool
    ) -> None:
        sdk_call_id = getattr(context, "tool_call_id", None)
        if isinstance(sdk_call_id, str) and sdk_call_id:
            call_id = sdk_call_id
        else:
            self._next_tool_call += 1
            call_id = f"tool-{self._next_tool_call}"
            self._fallback_tool_calls[id(context), id(tool)].append(call_id)
        name = tool.name
        self._recorder.record_tool_call(
            call_id=call_id, name=name, attributes={"agent": agent.name}
        )
        self._active_tool_calls[call_id] = name

    async def on_tool_end(
        self,
        context: RunContextWrapper[Any],
        agent: Agent[Any],
        tool: Tool,
        result: object,
    ) -> None:
        sdk_call_id = getattr(context, "tool_call_id", None)
        if isinstance(sdk_call_id, str) and sdk_call_id:
            call_id = sdk_call_id
        else:
            call_id = self._fallback_tool_calls[id(context), id(tool)].pop()
        match = (
            self._injected.take(call_id, tool.name)
            if self._injected is not None
            else None
        )
        attributes: dict[str, JsonValue] = {"result_type": type(result).__name__}
        success = self._outcomes.pop(call_id, None) if sdk_call_id else None
        # The SDK can reject or replace a result after the function returns.
        if success is True and getattr(tool, "tool_output_guardrails", None):
            success = None
        attributes["success"] = success
        if self._injected is not None:
            attributes["injected"] = match is not None
        if match is not None:
            attributes.update(
                success=False,
                fault_kind=match.fault_type,
                target_tool=match.target_tool,
                occurrence=match.occurrence,
            )
            if match.fault_type == "tool_timeout":
                attributes.update(timeout_simulated=True, timeout_ms=match.timeout_ms)
            elif match.fault_type == "malformed_tool_output":
                attributes.update(malformed_output_delivered=True)
        self._recorder.record_tool_result(call_id=call_id, attributes=attributes)
        self._active_tool_calls.pop(call_id, None)

    def close_open_calls(self, error_type: str) -> None:
        """Mark boundaries left open when the SDK run aborts."""

        while self._active_model_calls:
            self._recorder.record_model_error(
                call_id=self._active_model_calls.pop(),
                attributes={"error_type": error_type},
            )
        for call_id, tool_name in self._active_tool_calls.items():
            match = (
                self._injected.take(call_id, tool_name)
                if self._injected is not None
                else None
            )
            attributes: dict[str, JsonValue] = {"error_type": error_type}
            if self._injected is not None:
                attributes["injected"] = match is not None
            if match is not None:
                attributes.update(
                    fault_kind=match.fault_type,
                    target_tool=match.target_tool,
                    occurrence=match.occurrence,
                    delivery_confirmed=False,
                )
            self._recorder.record_tool_error(call_id=call_id, attributes=attributes)
        self._active_tool_calls.clear()


class OpenAIAgentsAdapter:
    """Run an SDK agent and emit portable AgentChaos events.

    If the SDK raises, the original exception is re-raised with its completed
    failed or cancelled ``AgentRun`` attached as ``agentchaos_run``.
    """

    def __init__(self, agent: Agent[Any], *, record_tool_turns: bool = False) -> None:
        self._agent = agent
        self._record_tool_turns = record_tool_turns

    @classmethod
    def from_function_tools(
        cls,
        *,
        name: str,
        instructions: str,
        tools: list[Callable[..., Any]],
        model: str | None = None,
    ) -> "OpenAIAgentsAdapter":
        """Construct fresh SDK objects for trusted server-side scenarios."""

        return cls(
            Agent(
                name=name,
                instructions=instructions,
                model=model,
                tools=[function_tool(tool) for tool in tools],
            )
        )

    async def run(
        self, task: str, *, recorder: TraceRecorder, fault: FaultSpec | None = None
    ) -> AgentRun:
        injected = _InjectedCalls() if fault is not None else None
        outcomes: dict[str, bool] = {}
        agent = _prepare_agent(self._agent, outcomes, injected, fault)
        recorder.start(task=task, adapter="openai_agents")
        hooks = _RecorderHooks(
            recorder, outcomes, injected, record_tool_turns=self._record_tool_turns
        )
        try:
            result = await Runner.run(
                agent,
                task,
                hooks=hooks,
                run_config=RunConfig(tracing_disabled=True),
            )
        except asyncio.CancelledError as exc:
            self._finish_exception(recorder, hooks, exc, RunStatus.CANCELLED)
            raise
        except Exception as exc:
            self._finish_exception(recorder, hooks, exc, RunStatus.FAILED)
            raise
        return recorder.finish(
            RunStatus.SUCCEEDED, output=_portable_output(result.final_output)
        )

    @staticmethod
    def _finish_exception(
        recorder: TraceRecorder,
        hooks: _RecorderHooks,
        exc: BaseException,
        status: RunStatus,
    ) -> None:
        error_type = type(exc).__name__
        try:
            hooks.close_open_calls(error_type)
            completed = recorder.finish(status, attributes={"error_type": error_type})
            exc.agentchaos_run = completed
        except Exception as recording_error:
            exc.add_note(
                "AgentChaos could not finalize its trace: "
                f"{type(recording_error).__name__}"
            )
