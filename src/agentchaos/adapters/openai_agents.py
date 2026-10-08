"""Observe an OpenAI Agents SDK run through per-run lifecycle hooks."""

import asyncio
from collections import defaultdict
from typing import Any

from agents import Agent, RunConfig, RunHooks, Runner
from agents.items import ModelResponse, TResponseInputItem
from agents.run_context import RunContextWrapper
from agents.tool import Tool
from pydantic import BaseModel, JsonValue, TypeAdapter, ValidationError

from agentchaos.models import AgentRun, RunStatus
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


class _RecorderHooks(RunHooks[Any]):
    """Translate one SDK run's callbacks into one AgentChaos recorder."""

    def __init__(self, recorder: TraceRecorder) -> None:
        self._recorder = recorder
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
        self._recorder.record_model_request(
            call_id=call_id, name=name, attributes={"agent": agent.name}
        )
        self._active_model_calls.append(call_id)

    async def on_llm_end(
        self,
        context: RunContextWrapper[Any],
        agent: Agent[Any],
        response: ModelResponse,
    ) -> None:
        call_id = self._active_model_calls.pop()
        self._recorder.record_model_response(
            call_id=call_id,
            attributes={
                "output_item_count": len(response.output),
                "input_tokens": response.usage.input_tokens,
                "output_tokens": response.usage.output_tokens,
                "total_tokens": response.usage.total_tokens,
            },
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
        self._recorder.record_tool_result(
            call_id=call_id, attributes={"result_type": type(result).__name__}
        )
        self._active_tool_calls.pop(call_id, None)

    def close_open_calls(self, error_type: str) -> None:
        """Mark boundaries left open when the SDK run aborts."""

        while self._active_model_calls:
            self._recorder.record_model_error(
                call_id=self._active_model_calls.pop(),
                attributes={"error_type": error_type},
            )
        for call_id in self._active_tool_calls:
            self._recorder.record_tool_error(
                call_id=call_id, attributes={"error_type": error_type}
            )
        self._active_tool_calls.clear()


class OpenAIAgentsAdapter:
    """Run an SDK agent and emit portable AgentChaos events.

    If the SDK raises, the original exception is re-raised with its completed
    failed or cancelled ``AgentRun`` attached as ``agentchaos_run``.
    """

    def __init__(self, agent: Agent[Any]) -> None:
        self._agent = agent

    async def run(self, task: str, *, recorder: TraceRecorder) -> AgentRun:
        recorder.start(task=task, adapter="openai_agents")
        hooks = _RecorderHooks(recorder)
        try:
            result = await Runner.run(
                self._agent,
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
